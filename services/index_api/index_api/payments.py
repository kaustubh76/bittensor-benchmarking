"""Owner-wallet payment preparation and receipt reconciliation. Never signs.

One deployment/credential binds one business. The durable snapshot freezes the
original escalation before returning calldata. A local filesystem lock serializes
preparation/reconciliation across workers; all workers must share this volume.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from acr_oracle_client.policy import POLICY_ABI, category_id, decision_hash, usdc_units
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from web3 import Web3
from web3.exceptions import TransactionNotFound

from . import operator, statement

router = APIRouter(prefix="/operator/payments", tags=["owner payments"])

SPENT_ABI = {
    "type": "event",
    "name": "Spent",
    "anonymous": False,
    "inputs": [
        {"name": n, "type": t, "indexed": i}
        for n, t, i in [
            ("category", "bytes32", True),
            ("to", "address", True),
            ("actor", "address", True),
            ("amount", "uint256", False),
            ("decisionHash", "bytes32", False),
            ("ownerApproved", "bool", False),
            ("spentInPeriod", "uint256", False),
            ("cap", "uint256", False),
            ("periodStart", "uint64", False),
        ]
    ],
}
TOKEN_ABI = [
    {
        "type": "function",
        "name": "balanceOf",
        "stateMutability": "view",
        "inputs": [{"name": "account", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "decimals",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint8"}],
    },
]


def refuse(message: str, status: int = 409):
    raise HTTPException(status, message)


def address(value: str) -> str:
    if not isinstance(value, str) or not Web3.is_address(value) or int(value, 16) == 0:
        refuse("A nonzero deployed address is required", 503)
    return Web3.to_checksum_address(value)


@dataclass(frozen=True)
class Deployment:
    business: str
    chain_id: int
    policy_wallet: str
    usdc: str
    api_url: str

    @classmethod
    def load(cls):
        try:
            raw = json.loads(Path(os.environ["ACR_TREASURY_CONFIG"]).read_text())
            if raw.get("enabled") is not True:
                refuse("Treasury payments are not configured", 503)
            business = raw["business"]
            chain = raw["chain_id"]
            if not isinstance(business, str) or not re.fullmatch(
                r"[a-z0-9][a-z0-9-]{0,63}", business
            ):
                raise ValueError("business")
            if type(chain) is not int or not 0 < chain < 2**53:
                raise ValueError("chain")
            return cls(
                business, chain, address(raw["policy_wallet"]), address(raw["usdc"]), raw["api_url"]
            )
        except (KeyError, ValueError, OSError, TypeError):
            refuse("Treasury deployment configuration is missing or invalid", 503)


def owner_inbox_business(business: str) -> bool:
    """Configured inboxes must not also use legacy server-signing paths.

    Invalid explicitly supplied configuration fails closed, including for the
    operator; silently falling back could enable unattended payments.
    """
    config = os.environ.get("ACR_TREASURY_CONFIG")
    if not config:
        return False
    raw = json.loads(Path(config).read_text())
    if raw.get("enabled") is not True:
        return False
    return Deployment.load().business == business


class Payments:
    def __init__(self, deployment: Deployment, web3: Web3, state_dir: Path):
        self.deployment = deployment
        self.w3 = web3
        self.state_dir = state_dir
        self.wallet = web3.eth.contract(
            address=deployment.policy_wallet, abi=[*POLICY_ABI, SPENT_ABI]
        )
        self.token = web3.eth.contract(address=deployment.usdc, abi=TOKEN_ABI)

    def rows(self):
        # No 1000-row truncation: old settlements must not become payable again.
        return statement.read_decisions(business=self.deployment.business, limit=0)

    def chain(self):
        if self.w3.eth.chain_id != self.deployment.chain_id:
            refuse("RPC network does not match the configured deployment", 503)
        for contract in (self.deployment.policy_wallet, self.deployment.usdc):
            if not self.w3.eth.get_code(contract):
                refuse("Configured contract is not deployed", 503)
        if self.wallet.functions.usdc().call().lower() != self.deployment.usdc.lower():
            refuse("PolicyWallet token differs from the configured USDC", 503)
        if self.token.functions.decimals().call() != 6:
            refuse("The configured token must use six decimals", 503)
        # Old deployments without replay protection fail closed on this read.
        self.wallet.functions.consumedDecisionHashes(bytes(32)).call()

    def pending(self):
        rows = self.rows()
        items = []
        for row in statement.pending_escalations(rows):
            item = {
                "obligation_id": row["obligation_id"],
                "evidence": row,
                "evidence_age_seconds": max(0, time.time() - float(row.get("at") or 0)),
                "evidence_notice": "Recorded evidence, not a fresh quote. Missing values are unknown.",
                "remaining_units": None,
            }
            try:
                self.chain()
                item["remaining_units"] = str(
                    self.wallet.functions.remaining(category_id(row["category"])).call()
                )
            except Exception:
                item["budget_notice"] = (
                    "Live budget unavailable; preparation will refuse until verified."
                )
            items.append(item)
        return {"business": self.deployment.business, "payments": items}

    @contextmanager
    def locked(self, obligation_id):
        identity = f"{self.deployment.chain_id}:{self.deployment.policy_wallet}:{self.deployment.business}:{obligation_id}"
        key = hashlib.sha256(identity.encode()).hexdigest()
        self.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.state_dir / f"{key}.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield self.state_dir / f"{key}.json"

    def save(self, path, snapshot):
        temp = path.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8") as stream:
            json.dump(snapshot, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def canonical(self, obligation_id):
        rows = [r for r in self.rows() if r.get("obligation_id") == obligation_id]
        escalations = [r for r in rows if r.get("intent") == "escalate"]
        if not escalations:
            refuse("No recorded escalation for this obligation", 404)
        # Fix the first decision forever. Repeated operator ticks may change
        # timestamps/recommendations, but must not change the payment identity.
        first = escalations[0]
        fields = ("vendor", "category", "billed_usdc", "invoice_ref")
        if any(any(r.get(k) != first.get(k) for k in fields) for r in escalations):
            refuse("Conflicting records for this obligation; resolve outside the payment inbox")
        return first, rows

    def prepare(self, obligation_id, execution_account, chain_id):
        if chain_id != self.deployment.chain_id:
            refuse("Connected wallet is on the wrong network")
        account = address(execution_account)
        with self.locked(obligation_id) as path:
            self.chain()
            owner = self.wallet.functions.owner().call()
            if account.lower() != owner.lower():
                refuse("Connect the PolicyWallet owner execution account", 403)
            row, rows = self.canonical(obligation_id)
            if any(r.get("intent") != "escalate" for r in rows):
                refuse("This obligation is already resolved")
            snapshot = json.loads(path.read_text()) if path.exists() else None
            record = operator.hashable_record(row)
            digest = decision_hash(record)
            if snapshot and snapshot["decision_hash"] != Web3.to_hex(digest):
                refuse("The prepared decision changed; manual reconciliation is required")
            if self.wallet.functions.consumedDecisionHashes(digest).call():
                refuse("This decision is already paid; verify its receipt instead of resubmitting")
            if self.wallet.functions.paused().call():
                refuse("PolicyWallet is paused")
            try:
                amount = usdc_units(row["billed_usdc"])
            except (ValueError, ArithmeticError, OverflowError):
                refuse("Bill amount must be exact, finite six-decimal USDC")
            if not 0 < amount < 2**256:
                refuse("Bill amount must be positive uint256 USDC units")
            recipient = address(row["vendor"])
            category = category_id(row["category"])
            exists, cap, spent, _, _, _ = self.wallet.functions.budgetOf(category).call()
            if not exists:
                refuse("No budget exists for this category")
            if amount > cap - spent:
                refuse("Payment exceeds the remaining category budget")
            if amount > self.token.functions.balanceOf(self.deployment.policy_wallet).call():
                refuse("Treasury has insufficient USDC")
            data = self.wallet.encode_abi(
                "spendAsOwner", args=[category, recipient, amount, digest]
            )
            snapshot = {
                "record": record,
                "usdc": self.deployment.usdc,
                "owner": owner,
                "decision_hash": Web3.to_hex(digest),
                "category": Web3.to_hex(category),
                "recipient": recipient,
                "amount_units": str(amount),
                "transaction": {
                    "chain_id": self.deployment.chain_id,
                    "to": self.deployment.policy_wallet,
                    "data": data,
                    "value": "0",
                },
            }
            self.save(path, snapshot)
            return {
                **snapshot,
                "business": self.deployment.business,
                "obligation_id": obligation_id,
                "remaining_after_units": str(cap - spent - amount),
                "status": "prepared",
                "requires_owner_authorization": True,
                "notice": "Unsigned. Review, simulate and authorize this exact payment; never automatically retry submission.",
            }

    def verify(self, obligation_id, transaction_hash):
        if not re.fullmatch(r"0x[0-9a-fA-F]{64}", transaction_hash):
            refuse("A 32-byte transaction hash is required", 422)
        with self.locked(obligation_id) as path:
            if not path.exists():
                refuse("No prepared decision; manual receipt reconciliation is required")
            snapshot = json.loads(path.read_text())
            self.chain()
            try:
                receipt = self.w3.eth.get_transaction_receipt(transaction_hash)
            except TransactionNotFound:
                return {"status": "pending", "notice": "Receipt unavailable. Do not resubmit."}
            if receipt["status"] != 1:
                refuse("Transaction reverted; no payment recorded")
            block = self.w3.eth.get_block(receipt["blockNumber"])
            if block["hash"] != receipt["blockHash"]:
                refuse("Receipt is not on the canonical chain")
            finalized = self.w3.eth.get_block("finalized")
            if finalized["number"] < receipt["blockNumber"]:
                return {
                    "status": "pending_finality",
                    "notice": "Submitted; wait for finality. Do not resubmit.",
                }
            matches = []
            for log in receipt["logs"]:
                if log["address"].lower() != self.deployment.policy_wallet.lower():
                    continue
                try:
                    args = self.wallet.events.Spent().process_log(log)["args"]
                except Exception:
                    continue
                if (
                    Web3.to_hex(args["category"]) == snapshot["category"]
                    and args["to"].lower() == snapshot["recipient"].lower()
                    and args["actor"].lower() == snapshot["owner"].lower()
                    and str(args["amount"]) == snapshot["amount_units"]
                    and Web3.to_hex(args["decisionHash"]) == snapshot["decision_hash"]
                    and args["ownerApproved"] is True
                ):
                    matches.append(args)
            if len(matches) != 1:
                refuse("Receipt does not contain the exact owner-authorized payment")
            if not self.wallet.functions.consumedDecisionHashes(
                bytes.fromhex(snapshot["decision_hash"][2:])
            ).call():
                refuse("Payment decision is not consumed on-chain")
            tx = transaction_hash.lower()
            # The ledger is the idempotency boundary, including recovery after a
            # crash between appending the resolution and saving snapshot status.
            rows = self.rows()
            resolutions = [
                r
                for r in rows
                if r.get("obligation_id") == obligation_id and r.get("intent") != "escalate"
            ]
            if resolutions:
                if not any(str(r.get("tx", "")).lower() == tx for r in resolutions):
                    refuse("Obligation has a different resolution; manual reconciliation required")
            else:
                resolution = {
                    **snapshot["record"],
                    "at": time.time(),
                    "intent": "pay",
                    "escalated": False,
                    "actor": "owner",
                    "tx": tx,
                    "paid_usdc": int(snapshot["amount_units"]) / 1_000_000,
                    "paid_units": snapshot["amount_units"],
                    "decision_hash": snapshot["decision_hash"],
                    "rule": "Owner authorized; receipt and Spent event verified",
                }
                # Unlike the diagnostic writer, durable reconciliation must fail
                # visibly on disk errors, so the same receipt can safely retry.
                target = Path(operator.LOG_PATH)
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(resolution, ensure_ascii=False, allow_nan=False) + "\n")
                    stream.flush()
                    os.fsync(stream.fileno())
            snapshot["transaction_hash"] = tx
            self.save(path, snapshot)
            return {
                "status": "paid",
                "obligation_id": obligation_id,
                "transaction_hash": tx,
                "amount_units": snapshot["amount_units"],
                "decision_hash": snapshot["decision_hash"],
            }


def authorize(request: Request):
    expected = os.environ.get("ACR_TREASURY_API_TOKEN", "")
    supplied = request.headers.get("authorization", "")
    if not expected or not hmac.compare_digest(supplied.encode(), f"Bearer {expected}".encode()):
        refuse("A business-scoped treasury credential is required", 401)


def require_business_credential(request: Request, business: str):
    if owner_inbox_business(business):
        authorize(request)


def service(request: Request):
    authorize(request)
    deployment = Deployment.load()
    rpc = os.environ.get("ACR_TREASURY_RPC_URL", "")
    state = os.environ.get("ACR_TREASURY_STATE_DIR", "")
    if not rpc or not state:
        refuse("Treasury RPC and persistent state directory must be configured", 503)
    return Payments(
        deployment, Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 15})), Path(state)
    )


class PrepareBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    obligation_id: str = Field(min_length=1, max_length=256)
    execution_account: str
    chain_id: int


class VerifyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    obligation_id: str = Field(min_length=1, max_length=256)
    transaction_hash: str


def invoke(action):
    try:
        return action()
    except HTTPException:
        raise
    except Exception:
        # No RPC URLs, credentials, response bodies or private records in errors.
        refuse(
            "Treasury state could not be verified. Retry the read; do not resubmit a payment.", 503
        )


@router.get("")
def pending(payments: Payments = Depends(service)):
    return invoke(payments.pending)


@router.post("/prepare")
def prepare(body: PrepareBody, payments: Payments = Depends(service)):
    return invoke(
        lambda: payments.prepare(body.obligation_id, body.execution_account, body.chain_id)
    )


@router.post("/verify")
def verify(body: VerifyBody, payments: Payments = Depends(service)):
    return invoke(lambda: payments.verify(body.obligation_id, body.transaction_hash))
