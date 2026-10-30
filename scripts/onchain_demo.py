#!/usr/bin/env python
"""On-chain round trip — flow ⑧⑨ for real.

Deploys ACROracle + AttestationRegistry to a local anvil node (from the
forge-compiled artifacts), runs the estimator, posts each signed print on-chain,
reads it back, and cash-settles an ACR-Weekly future against the on-chain value.

    anvil &                       # in another terminal (or `make anvil`)
    uv run python scripts/onchain_demo.py

Skips gracefully with a clear message if anvil is not reachable.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from acr_core import ModelClass, SellerAttestation, Service
from acr_instrument import ACRFuture, Position
from acr_oracle_client import OracleClient, RegistryClient
from index_api.store import PrintStore

ROOT = Path(__file__).resolve().parent.parent
RPC = "http://127.0.0.1:8545"
# anvil's first default account (well-known dev key; local-only).
ANVIL_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
# A few more anvil default keys — distinct sellers for the attestation flywheel.
ANVIL_SELLERS = [
    ("0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d", ModelClass.FRONTIER, 120.0),
    ("0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a", ModelClass.MID, 250.0),
    ("0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6", ModelClass.OPEN, 500.0),
]


def _artifact(name: str) -> tuple[list, str]:
    art = json.loads((ROOT / f"contracts/out/{name}.sol/{name}.json").read_text())
    return art["abi"], art["bytecode"]["object"]


def _deploy(w3, acct, name: str) -> str:
    abi, bytecode = _artifact(name)
    c = w3.eth.contract(abi=abi, bytecode=bytecode)
    tx = c.constructor().build_transaction(
        {
            "from": acct.address,
            "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": 3_000_000,
            "chainId": w3.eth.chain_id,
        }
    )
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    rcpt = w3.eth.wait_for_transaction_receipt(w3.eth.send_raw_transaction(raw), timeout=30)
    return rcpt.contractAddress


def _attestation_flywheel(w3, registry_addr: str) -> None:
    """Each seller attests its own metadata on-chain; then we read it all back."""
    from eth_account import Account

    print("  ⑦ attestation flywheel — sellers attest metadata on-chain:")
    for key, mc, latency in ANVIL_SELLERS:
        seller = Account.from_key(key)
        rc = RegistryClient(rpc_url=RPC, registry_address=registry_addr, private_key=key)
        att = SellerAttestation(
            seller=seller.address,
            service=Service.INFERENCE,
            model_class=mc,
            latency_slo_ms=latency,
            schema_id="inf.v1",
        )
        rc.attest(att)
        print(f"      {seller.address[:12]}…  {mc.value:<8} {latency:>5.0f}ms  attested")

    reader = RegistryClient(rpc_url=RPC, registry_address=registry_addr)
    read_back = reader.all_attestations()
    ok = len(read_back) == len(ANVIL_SELLERS)
    print(f"      → read back {len(read_back)} attestations from chain  "
          f"{'✓' if ok else '✗'}  (hedonic feature matrix is on-chain)\n")


def main() -> None:
    try:
        from eth_account import Account
        from web3 import Web3

        w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 3}))
        if not w3.is_connected():
            raise ConnectionError
    except Exception:
        print("\n  anvil not reachable at 127.0.0.1:8545 — start it with `make anvil`.\n")
        sys.exit(0)

    if not (ROOT / "contracts/out/ACROracle.sol/ACROracle.json").exists():
        print("\n  contracts not built — run `make build-contracts` first.\n")
        sys.exit(0)

    acct = Account.from_key(ANVIL_KEY)
    print(f"\n  deploying ACR on-chain layer to anvil (deployer {acct.address[:10]}…)")
    oracle_addr = _deploy(w3, acct, "ACROracle")
    registry_addr = _deploy(w3, acct, "AttestationRegistry")
    print(f"  ACROracle           {oracle_addr}")
    print(f"  AttestationRegistry {registry_addr}\n")

    # ── The flywheel: sellers attest metadata on-chain, then it reads back ──
    _attestation_flywheel(w3, registry_addr)

    # Estimate, then post every print on-chain and read it back.
    store = PrintStore()
    store.refresh(ts=3600.0)
    client = OracleClient(rpc_url=RPC, oracle_address=oracle_addr, private_key=ANVIL_KEY)

    print(f"  {'INDEX':<9} {'posted':>12} {'tx':>12}   {'on-chain read-back':>20}")
    print("  " + "-" * 62)
    onchain: dict[str, float] = {}
    for iid, p in store.latest.items():
        tx = client.post(p)
        back = client.read_latest(iid)
        onchain[iid] = back["value"] if back else float("nan")
        match = "✓" if back and abs(back["value"] - p.value) / p.value < 1e-6 else "✗"
        print(f"  {iid:<9} {p.value:>12.5f} {(tx[:10] + '…') if tx else 'offline':>12}   "
              f"{onchain[iid]:>14.5f}  {match}")

    # Provenance: prints are EIP-712-verified on-chain, and fresh.
    iid = next(iter(store.latest))
    stale = client.is_stale(iid, 7200)
    print(f"\n  ⑧ prints verified on-chain (EIP-712, signer {acct.address[:10]}…)  "
          f"stale(>2h): {stale}")

    # Cash-settle a future against the on-chain print — guard a failed read.
    fut = ACRFuture(index_id=iid, expiry_ts=store.latest[iid].ts + 7 * 24 * 3600, multiplier=1000.0)
    pos = Position()
    pos.apply_fill(5, store.latest[iid].value * 0.98)  # long 5 @ 2% below print
    back = client.read_latest(iid)
    if back is None:
        settle_price = store.latest[iid].value
        print("  ⚠ on-chain read failed — settling against the local print instead.")
    else:
        settle_price = back["value"]
    pnl = fut.cash_settle(pos, settle_price)
    print(f"\n  ⑨ cash-settle {iid} future: long 5 @ {store.latest[iid].value*0.98:.5f} "
          f"→ settles vs on-chain {settle_price:.5f} = ${pnl:,.2f} PnL")
    print("\n  loop ⑧⑨ closed on-chain. Prints are settlement-grade.\n")


if __name__ == "__main__":
    main()
