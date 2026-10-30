#!/usr/bin/env python
"""Deploy the ACR contracts via Circle's Smart Contract Platform.

Deploys ``ACROracle`` + ``AttestationRegistry`` from the Foundry-compiled
artifacts using a Circle developer-controlled wallet + Gas Station (or imports
already-deployed contracts by address). Then authorizes the poster wallet as an
oracle signer. Foundry/anvil (`scripts/onchain_demo.py`) remains the local path.

    uv sync --extra circle
    # fill .env (ACR_CIRCLE_*, ACR_ARC_CHAIN_ID) then:
    uv run python scripts/deploy_circle.py            # deploy fresh
    uv run python scripts/deploy_circle.py --dry-run  # print request bodies only
    uv run python scripts/deploy_circle.py --import-by-address \
        --oracle-address 0x… --registry-address 0x…

Exact Circle SDK method/field names are the doc-confirmed FLAG — isolated in
``_RealCircleDeployer``. The request-building helpers below are pure and tested.
"""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path
from typing import Protocol, runtime_checkable

from acr_core import get_settings

ROOT = Path(__file__).resolve().parent.parent

SETSIGNER_ABI = {
    "type": "function",
    "name": "setSigner",
    "inputs": [{"name": "signer", "type": "address"}, {"name": "allowed", "type": "bool"}],
    "outputs": [],
    "stateMutability": "nonpayable",
}


def _artifact(name: str) -> tuple[list, str]:
    art = json.loads((ROOT / f"contracts/out/{name}.sol/{name}.json").read_text())
    return art["abi"], art["bytecode"]["object"]


def deploy_request(name: str, abi: list, bytecode: str, blockchain: str, wallet_id: str) -> dict:
    """Circle Smart Contract Platform deploy body (constructor takes no args here)."""
    return {
        "name": name,
        "walletId": wallet_id,
        "blockchain": blockchain,
        "abiJson": json.dumps(abi),
        "bytecode": bytecode,
        "constructorParameters": [],
        "feeLevel": "MEDIUM",
        "idempotencyKey": str(uuid.uuid4()),
    }


def import_request(name: str, address: str, blockchain: str) -> dict:
    return {"name": name, "address": address, "blockchain": blockchain}


def set_signer_calldata(signer_address: str, allowed: bool = True) -> str:
    """ABI-encode ``setSigner(address,bool)`` (pure — no RPC)."""
    from web3 import Web3

    c = Web3().eth.contract(abi=[SETSIGNER_ABI])
    return c.encode_abi("setSigner", args=[Web3.to_checksum_address(signer_address), allowed])


@runtime_checkable
class CircleDeployer(Protocol):
    def deploy(self, request: dict) -> str: ...  # returns deployed contract address
    def import_contract(self, request: dict) -> str: ...  # returns contract id
    def contract_execution(self, wallet_id: str, contract_address: str, call_data: str) -> str: ...


def run_deploy(
    deployer: CircleDeployer | None,
    *,
    blockchain: str,
    wallet_id: str,
    poster_address: str | None = None,
    import_addrs: tuple[str, str] | None = None,
    dry_run: bool = False,
) -> dict:
    """Plan (and, unless dry-run, execute) the deploy. Returns the request bodies
    and resulting addresses — pure and inspectable for tests."""
    oracle_abi, oracle_bc = _artifact("ACROracle")
    reg_abi, reg_bc = _artifact("AttestationRegistry")

    plan: dict = {"requests": [], "oracle_address": None, "registry_address": None}

    if import_addrs is not None:
        oreq = import_request("ACROracle", import_addrs[0], blockchain)
        rreq = import_request("AttestationRegistry", import_addrs[1], blockchain)
        plan["requests"] += [oreq, rreq]
        plan["oracle_address"], plan["registry_address"] = import_addrs
        if not dry_run:
            deployer.import_contract(oreq)
            deployer.import_contract(rreq)
    else:
        oreq = deploy_request("ACROracle", oracle_abi, oracle_bc, blockchain, wallet_id)
        rreq = deploy_request("AttestationRegistry", reg_abi, reg_bc, blockchain, wallet_id)
        plan["requests"] += [oreq, rreq]
        if not dry_run:
            plan["oracle_address"] = deployer.deploy(oreq)
            plan["registry_address"] = deployer.deploy(rreq)

    # Authorize the poster wallet as an oracle signer (the deployer wallet is
    # already a signer via the constructor; only needed if they differ).
    if poster_address and plan["oracle_address"] and poster_address != wallet_id:
        calldata = set_signer_calldata(poster_address, True)
        plan["set_signer_calldata"] = calldata
        if not dry_run:
            deployer.contract_execution(wallet_id, plan["oracle_address"], calldata)

    return plan


class _RealCircleDeployer:  # pragma: no cover - requires the Circle SDK + live creds
    """Adapter onto circle-developer-controlled-wallets / smart-contract-platform.
    Exact method/field names are the doc-confirmed FLAG."""

    def __init__(self, api_key: str, entity_secret: str) -> None:
        from circle.web3 import utils  # type: ignore

        self._sdk = utils.init_smart_contract_platform_client(
            api_key=api_key, entity_secret=entity_secret
        )

    def deploy(self, request: dict) -> str:
        resp = self._sdk.deploy_contract(**request)  # FLAG: confirm name/fields
        return resp.data.contract_address

    def import_contract(self, request: dict) -> str:
        return self._sdk.import_contract(**request).data.contract.id  # FLAG

    def contract_execution(self, wallet_id: str, contract_address: str, call_data: str) -> str:
        resp = self._sdk.create_contract_execution_transaction(
            wallet_id=wallet_id, contract_address=contract_address, call_data=call_data,
            fee={"type": "level", "config": {"feeLevel": "MEDIUM"}},
            idempotency_key=str(uuid.uuid4()),
        )
        return resp.data.id


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="print request bodies, send nothing")
    ap.add_argument("--import-by-address", action="store_true")
    ap.add_argument("--oracle-address", default="")
    ap.add_argument("--registry-address", default="")
    args = ap.parse_args()

    s = get_settings()
    blockchain = s.caip2()
    wallet_id = s.circle_wallet_id
    import_addrs = None
    if args.import_by_address:
        import_addrs = (args.oracle_address, args.registry_address)

    if not (ROOT / "contracts/out/ACROracle.sol/ACROracle.json").exists():
        print("\n  contracts not built — run `make build-contracts` first.\n")
        raise SystemExit(0)

    deployer = None
    if not args.dry_run:
        if not (s.circle_api_key and wallet_id):
            print("\n  Circle creds missing — set ACR_CIRCLE_API_KEY + ACR_CIRCLE_WALLET_ID "
                  "(or use --dry-run).\n")
            raise SystemExit(0)
        deployer = _RealCircleDeployer(s.circle_api_key, s.circle_entity_secret)

    plan = run_deploy(
        deployer, blockchain=blockchain, wallet_id=wallet_id or "dry-wallet",
        poster_address=None, import_addrs=import_addrs, dry_run=args.dry_run,
    )

    print(f"\n  blockchain: {blockchain}")
    for req in plan["requests"]:
        keys = {k: (v[:24] + "…" if isinstance(v, str) and len(v) > 24 else v)
                for k, v in req.items() if k in ("name", "blockchain", "address", "idempotencyKey")}
        print(f"  request: {keys}")
    if plan["oracle_address"]:
        print(f"\n  export ACR_ORACLE_ADDRESS={plan['oracle_address']}")
        print(f"  export ACR_REGISTRY_ADDRESS={plan['registry_address']}")
    print()


if __name__ == "__main__":
    main()
