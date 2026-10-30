"""``MirrorClient`` — put an off-chain Gateway settlement on chain.

Circle Gateway settles x402 nanopayments off-chain and hands back a batch UUID,
not a transaction. There is therefore **no settlement event on Arc for a
subgraph to index**, and the only ledger is a JSONL file the seller writes about
itself. ``ReceiptMirror.sol`` is the bridge; this is its client.

Two phases, because the two facts arrive at different times — the amount as soon
as `/v1/x402/settle` returns, the quantity once the receipt is decoded. The
split is what commits the arrival anchor (``settledAt``) *before* the unit price
is known, so the keeper cannot see what its own benchmark would be and then pick
it. See ``contracts/src/ReceiptMirror.sol`` for the full argument.

Shared by ``scripts/mirror_receipts.py`` (operator) and the in-service keeper
chore, so the two cannot drift into mirroring different things.
"""

from __future__ import annotations

import logging

from acr_core import get_settings

#: The service codes ``ReceiptMirror.unit`` carries — the same encoding
#: ``AttestationRegistry`` uses, so one number means one thing everywhere.
from .registry import SERVICE_TO_CODE
from .signer import Signer, build_role_signer

log = logging.getLogger("acr_oracle_client.mirror")

OPEN_TYPES = {
    "OpenSettlement": [
        {"name": "settlementId", "type": "bytes32"},
        {"name": "payer", "type": "address"},
        {"name": "seller", "type": "address"},
        {"name": "indexId", "type": "bytes32"},
        {"name": "amountUsdc", "type": "uint256"},
        {"name": "settledAt", "type": "uint64"},
        {"name": "gatewayRef", "type": "bytes32"},
        {"name": "synthetic", "type": "bool"},
    ]
}

FINALIZE_TYPES = {
    "FinalizeSettlement": [
        {"name": "settlementId", "type": "bytes32"},
        {"name": "unit", "type": "uint8"},
        {"name": "quantity", "type": "uint256"},
    ]
}

MIRROR_ABI = [
    {
        "name": "openSettlement",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "settlementId", "type": "bytes32"},
            {"name": "payer", "type": "address"},
            {"name": "seller", "type": "address"},
            {"name": "indexId", "type": "bytes32"},
            {"name": "amountUsdc", "type": "uint256"},
            {"name": "settledAt", "type": "uint64"},
            {"name": "gatewayRef", "type": "bytes32"},
            {"name": "synthetic", "type": "bool"},
            {"name": "v", "type": "uint8"},
            {"name": "r", "type": "bytes32"},
            {"name": "s", "type": "bytes32"},
        ],
        "outputs": [],
    },
    {
        "name": "finalizeSettlement",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "settlementId", "type": "bytes32"},
            {"name": "unit", "type": "uint8"},
            {"name": "quantity", "type": "uint256"},
            {"name": "v", "type": "uint8"},
            {"name": "r", "type": "bytes32"},
            {"name": "s", "type": "bytes32"},
        ],
        "outputs": [],
    },
    {
        "name": "openDigest",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "settlementId", "type": "bytes32"},
            {"name": "payer", "type": "address"},
            {"name": "seller", "type": "address"},
            {"name": "indexId", "type": "bytes32"},
            {"name": "amountUsdc", "type": "uint256"},
            {"name": "settledAt", "type": "uint64"},
            {"name": "gatewayRef", "type": "bytes32"},
            {"name": "synthetic", "type": "bool"},
        ],
        "outputs": [{"name": "", "type": "bytes32"}],
    },
    {
        "name": "finalizeDigest",
        "type": "function",
        "stateMutability": "view",
        "inputs": [
            {"name": "settlementId", "type": "bytes32"},
            {"name": "unit", "type": "uint8"},
            {"name": "quantity", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bytes32"}],
    },
    {
        "name": "settlements",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "", "type": "bytes32"}],
        "outputs": [
            {"name": "payer", "type": "address"},
            {"name": "settledAt", "type": "uint64"},
            {"name": "unit", "type": "uint8"},
            {"name": "synthetic", "type": "bool"},
            {"name": "late", "type": "bool"},
            {"name": "finalized", "type": "bool"},
            {"name": "seller", "type": "address"},
            {"name": "openedAt", "type": "uint64"},
            {"name": "indexId", "type": "bytes32"},
            {"name": "amountUsdc", "type": "uint256"},
            {"name": "quantity", "type": "uint256"},
        ],
    },
    {
        "name": "refUsed",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "", "type": "bytes32"}],
        "outputs": [{"name": "", "type": "bool"}],
    },
]

#: USDC is 1e6 on Arc; quantities are WAD 1e18 in the index's own unit.
USDC = 10**6
WAD = 10**18


def to_bytes32(text: str) -> bytes:
    """A stable bytes32 from an arbitrary reference string.

    A Gateway ref is a 16-byte UUID today, but ``refKind()`` in the Terminal
    already anticipates a 32-byte tx hash, so hash rather than pad: one rule
    that keeps working when the ref shape changes under us.
    """
    from web3 import Web3

    return bytes(Web3.keccak(text=text))


def settlement_id(tx_ref: str) -> bytes:
    """The on-chain id for a receipt. Derived, so a re-run is idempotent at the
    contract (a second open reverts "already opened") rather than duplicating a
    settlement into the tape under a fresh id."""
    return to_bytes32(f"acr.settlement::{tx_ref}")


class MirrorClient:
    """Mirrors receipts onto ``ReceiptMirror``. Offline-tolerant like OracleClient."""

    def __init__(
        self,
        rpc_url: str | None = None,
        mirror_address: str | None = None,
        signer: Signer | None = None,
        settings=None,
    ) -> None:
        s = settings or get_settings()
        self.rpc_url = rpc_url or s.arc_rpc_url
        self.mirror_address = mirror_address or (s.receipt_mirror_address or None)
        # The press's own signer — the wallet ACROracle already trusts. One trust
        # anchor for prints and for the settlements benchmarked against them.
        self.signer = signer or build_role_signer("poster", s)
        self._w3 = None

    # --- plumbing -----------------------------------------------------------

    def configured(self) -> bool:
        return bool(self.mirror_address and self.signer)

    def _connect(self):
        if self._w3 is not None:
            return self._w3
        try:
            from web3 import Web3

            w3 = Web3(Web3.HTTPProvider(self.rpc_url, request_kwargs={"timeout": 20}))
            self._w3 = w3 if w3.is_connected() else None
        except Exception as exc:  # pragma: no cover - env dependent
            log.warning("MirrorClient: web3 unavailable (%s)", exc)
            self._w3 = None
        return self._w3

    def _contract(self):
        from web3 import Web3

        return self._connect().eth.contract(
            address=Web3.to_checksum_address(self.mirror_address), abi=MIRROR_ABI
        )

    def _domain(self, chain_id: int) -> dict:
        from web3 import Web3

        return {
            "name": "ACR Receipt Mirror",
            "version": "1",
            "chainId": int(chain_id),
            "verifyingContract": Web3.to_checksum_address(self.mirror_address),
        }

    def _check_recovers(self, domain: dict, types: dict, message: dict, primary: str, v, r, s_) -> bool:
        """Does our own signature recover to our own signer?

        Checked before broadcasting, against ``eth_account`` rather than against
        a reimplementation of the hash. An EIP-712 type mismatch recovers to a
        stranger, and the only symptom on chain would be "bad signer" on a call
        that should have worked — after the gas was spent.
        """
        from eth_account import Account
        from eth_account.messages import encode_typed_data

        recovered = Account.recover_message(
            encode_typed_data(domain_data=domain, message_types=types, message_data=message),
            vrs=(v, int.from_bytes(r, "big"), int.from_bytes(s_, "big")),
        )
        return recovered.lower() == self.signer.address.lower()

    def _send(self, fn, wait: bool = True) -> str:
        w3 = self._connect()
        tx = fn.build_transaction(
            {
                "from": self.signer.address,
                "nonce": w3.eth.get_transaction_count(self.signer.address),
                "chainId": w3.eth.chain_id,
            }
        )
        tx_hash = self.signer.send_transaction(w3, tx)
        if wait:
            rcpt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=45)
            if rcpt.status != 1:
                raise RuntimeError(f"mirror tx reverted ({tx_hash})")
        return str(tx_hash)

    # --- state --------------------------------------------------------------

    def state(self, sid: bytes) -> dict | None:
        """What the chain already holds for this settlement, or None."""
        if not self.configured() or self._connect() is None:
            return None
        try:
            row = self._contract().functions.settlements(sid).call()
        except Exception:  # pragma: no cover - live chain
            return None
        if int(row[7]) == 0:  # openedAt
            return None
        return {"opened": True, "finalized": bool(row[5]), "settled_at": int(row[1])}

    def state_for(self, tx_ref: str) -> dict | None:
        """What the chain holds for a receipt, keyed by its Gateway ref.

        Callers hold receipts, not settlement ids; deriving here keeps the id
        rule in one place so a caller cannot compute a different one and mirror
        the same settlement twice under two ids.
        """
        return self.state(settlement_id(tx_ref))

    # --- the two phases -----------------------------------------------------

    def open_settlement(
        self,
        tx_ref: str,
        payer: str,
        seller: str,
        index_id: str,
        amount_usdc: float,
        settled_at: float,
        synthetic: bool = True,
        dry_run: bool = False,
    ) -> str | None:
        """Phase 1 — commit the arrival anchor. Returns a tx hash, or None."""
        from web3 import Web3

        from .client import index_id_to_bytes32

        sid = settlement_id(tx_ref)
        ref = to_bytes32(f"acr.gateway::{tx_ref}")
        message = {
            "settlementId": sid,
            "payer": Web3.to_checksum_address(payer),
            "seller": Web3.to_checksum_address(seller),
            "indexId": index_id_to_bytes32(index_id),
            "amountUsdc": int(round(amount_usdc * USDC)),
            "settledAt": int(settled_at),
            "gatewayRef": ref,
            "synthetic": bool(synthetic),
        }
        if not self.configured() or self._connect() is None:
            log.info("MirrorClient offline: would open %s", tx_ref)
            return None

        chain_id = self._connect().eth.chain_id
        domain = self._domain(chain_id)
        v, r, s_ = self.signer.sign_typed_data(domain, OPEN_TYPES, message, "OpenSettlement")
        if not self._check_recovers(domain, OPEN_TYPES, message, "OpenSettlement", v, r, s_):
            raise RuntimeError("open signature does not recover to our signer — not broadcasting")
        if dry_run:
            return None
        c = self._contract()
        return self._send(
            c.functions.openSettlement(
                message["settlementId"], message["payer"], message["seller"],
                message["indexId"], message["amountUsdc"], message["settledAt"],
                message["gatewayRef"], message["synthetic"], v, r, s_,
            )
        )

    def finalize_settlement(
        self,
        tx_ref: str,
        service,
        quantity: float,
        dry_run: bool = False,
    ) -> str | None:
        """Phase 2 — add the decoded quantity, and with it the unit price."""
        sid = settlement_id(tx_ref)
        unit = SERVICE_TO_CODE[service]
        message = {
            "settlementId": sid,
            "unit": int(unit),
            "quantity": int(round(quantity * WAD)),
        }
        if not self.configured() or self._connect() is None:
            log.info("MirrorClient offline: would finalize %s", tx_ref)
            return None
        if message["quantity"] <= 0:
            # The contract rejects it, but failing here says why without a revert.
            raise ValueError(f"{tx_ref}: quantity must be positive to have a unit price")

        chain_id = self._connect().eth.chain_id
        domain = self._domain(chain_id)
        v, r, s_ = self.signer.sign_typed_data(
            domain, FINALIZE_TYPES, message, "FinalizeSettlement"
        )
        if not self._check_recovers(
            domain, FINALIZE_TYPES, message, "FinalizeSettlement", v, r, s_
        ):
            raise RuntimeError("finalize signature does not recover to our signer")
        if dry_run:
            return None
        c = self._contract()
        return self._send(
            c.functions.finalizeSettlement(
                message["settlementId"], message["unit"], message["quantity"], v, r, s_
            )
        )
