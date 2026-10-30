#!/usr/bin/env python
"""Public Desk evidence — prove the user-controlled run actually hit the chain.

The E2E driver (``scripts/desk_e2e.mjs``) asserts on what the UI says. This
asserts on what Arc says, from two independent directions:

  * **Circle's ledger** — every transaction the user's own wallet submitted,
    with its state, tx hash and network fee. The fee is the honest answer to
    "is Gas Station sponsoring this SCA?", which the UI merely claims.
  * **The chain itself** — ``CollateralPosted`` / ``Traded`` logs filtered to
    that wallet, plus a live ``positionOf`` read. A position that reads back
    non-zero is the thing no amount of UI polish can fake.

Reads ``data/desk_e2e_last.json`` for the wallet the run created (or takes
``--address``), and needs ``--user`` to pull the Circle side (a fresh user token
is minted server-side; no PIN involved — these are reads).

    uv run python scripts/desk_evidence.py --user acr-desk-xxxxxxxx
    uv run python scripts/desk_evidence.py --address 0x… --no-circle
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from acr_core import get_settings
from acr_oracle_client.futures import _rpc_retry

EXPLORER = "https://explorer.arc.io"  # Arc mainnet
#: keccak("UserOperationEvent(bytes32,address,address,uint256,bool,uint256,uint256)")
#: — the ERC-4337 EntryPoint's receipt. Its `paymaster` topic is the only
#: trustworthy answer to "was this gas sponsored?".
USER_OP_EVENT_TOPIC = "0x49628fd1471006c1482da88028e9ce4dbb080b815c9b0344d39e5a8e6ec1419f"
#: Bounded numeric spans — Arc's RPC answers 413 to an unbounded getLogs, and
#: "latest" as a range endpoint is what bit the futures tape (see futuresOnchain).
LOG_SPANS = (10_000, 2_500, 1_000, 300)


def _logs(w3, contract, event_name, start, end, **filters):
    ev = getattr(contract.events, event_name)
    return ev().get_logs(from_block=start, to_block=end, argument_filters=filters or None)


def scan_logs(w3, contract, event_name, address, **filters):
    """Walk shrinking windows until the node answers — never an open range."""
    latest = int(_rpc_retry(lambda: w3.eth.block_number))
    for span in LOG_SPANS:
        start = max(0, latest - span)
        try:
            return list(_rpc_retry(lambda s=start: _logs(w3, contract, event_name, s, latest, **filters))), span
        except Exception:
            continue
    return [], 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--address", default="", help="the SCA (default: from the E2E run file)")
    ap.add_argument("--user", default="", help="Circle userId, to list its transactions")
    ap.add_argument("--run", default="data/desk_e2e_last.json")
    ap.add_argument("--no-circle", action="store_true")
    args = ap.parse_args()

    address = args.address
    if not address:
        p = Path(args.run)
        if p.exists():
            address = (json.loads(p.read_text()) or {}).get("address") or ""
    if not address:
        print("no address — pass --address or run scripts/desk_e2e.mjs first")
        sys.exit(1)

    s = get_settings()
    from web3 import Web3

    w3 = Web3(Web3.HTTPProvider(s.arc_rpc_url, request_kwargs={"timeout": 20}))
    addr = Web3.to_checksum_address(address)
    print(f"Public Desk evidence — {addr}")
    print(f"  {EXPLORER}/address/{addr}")

    ok = True

    # --- Circle's side: what the user's wallet submitted, and what it paid.
    if args.user and not args.no_circle:
        import httpx

        H = {
            "Authorization": f"Bearer {s.circle_api_key}",
            "Content-Type": "application/json",
            "User-Agent": "acr-desk-evidence/1.0",
        }
        tok = httpx.post(
            f"{s.circle_base_url}/v1/w3s/users/token", headers=H, json={"userId": args.user},
            timeout=20,
        ).json()["data"]["userToken"]
        txs = httpx.get(
            f"{s.circle_base_url}/v1/w3s/transactions?pageSize=30",
            headers={**H, "X-User-Token": tok}, timeout=20,
        ).json()["data"].get("transactions", [])
        print(f"\n  Circle ledger — {len(txs)} transaction(s) from the user's own wallet:")
        for t in txs:
            print(f"    {t.get('state'):<10} fee={(t.get('networkFee') or '0'):<12} {t.get('txHash')}")
        print("    (networkFee is the operation's gas COST — not necessarily a debit")
        print("     to the user; see the sponsorship check below for who paid it.)")

    # --- The chain's side: the venue's own events, filtered to this wallet.
    from acr_oracle_client import FuturesClient
    from acr_oracle_client.futures import FUTURES_ABI

    fc = FuturesClient(rpc_url=s.arc_rpc_url, futures_address=s.futures_address)
    # The shared ABI now carries the collateral round trip and Settled, so no
    # local fragment is needed — the round trip IS the story worth proving.
    contract = w3.eth.contract(address=Web3.to_checksum_address(s.futures_address), abi=FUTURES_ABI)

    found = 0
    for event, kw, unit in (
        ("CollateralPosted", "trader", "in "),
        ("CollateralWithdrawn", "trader", "OUT "),
    ):
        logs, span = scan_logs(w3, contract, event, addr, **{kw: addr})
        found += len(logs)
        print(f"\n  {event} for this wallet (last {span} blocks): {len(logs)}")
        for ev in logs:
            print(
                f"    series {int(ev['args']['seriesId'])} {unit}"
                f"{int(ev['args']['amount']) / 1e6:.2f} USDC block {ev['blockNumber']}"
            )
            print(f"      {EXPLORER}/tx/{w3.to_hex(ev['transactionHash'])}")

    trades, span = scan_logs(w3, contract, "Traded", addr, taker=addr)
    found += len(trades)
    print(f"\n  Traded logs for this wallet (last {span} blocks): {len(trades)}")
    for ev in trades:
        a = ev["args"]
        qty = int(a["qty"]) / 10**18
        print(
            f"    series {int(a['seriesId'])} qty {qty:+.2f} @ {int(a['mark']) / 10**18:.5f} "
            f"block {ev['blockNumber']}"
        )
        print(f"      {EXPLORER}/tx/{w3.to_hex(ev['transactionHash'])}")
    if not found:
        # No desk activity in the scanned window at all. Old runs fall out of a
        # bounded getLogs range, so say that rather than implying nothing happened.
        print("\n  (no desk events in the scanned window — an older run may have "
              "aged out; the Circle ledger above is the durable record)")
        ok = False

    # --- The resulting state. A flat, empty account is NOT a failure: it is
    # exactly what a completed round trip looks like once the stake is back in
    # the wallet, so the verdict keys on evidence found, not on an open position.
    series_ids = sorted({int(ev["args"]["seriesId"]) for ev in trades}) or [0]
    for sid in series_ids:
        pos = fc.position_of(sid, addr)
        coll = fc.collateral_of(sid, addr)
        print(f"\n  positionOf(series {sid}) → {pos}")
        print(f"  collateral(series {sid}) → {coll} USDC")

    # Who actually paid the gas? Not answerable from Circle's networkFee (that
    # is the cost, whoever bore it) — the authority is the ERC-4337
    # UserOperationEvent's `paymaster` field: non-zero means sponsored.
    probe = trades[-1]["transactionHash"] if trades else None
    if probe is None:
        wlogs, _ = scan_logs(w3, contract, "CollateralWithdrawn", addr, trader=addr)
        probe = wlogs[-1]["transactionHash"] if wlogs else None
    if probe is not None:
        receipt = _rpc_retry(w3.eth.get_transaction_receipt, probe)
        paymaster = None
        for lg in receipt["logs"]:
            topics = [t.hex() if hasattr(t, "hex") else t for t in lg["topics"]]
            t0 = topics[0] if str(topics[0]).startswith("0x") else "0x" + str(topics[0])
            if t0.lower() == USER_OP_EVENT_TOPIC and len(topics) > 3:
                paymaster = "0x" + str(topics[3])[-40:]
        if paymaster and int(paymaster, 16) != 0:
            print(f"\n  Gas Station sponsorship: YES — paymaster {paymaster} paid this user op")
        elif paymaster:
            print("\n  Gas Station sponsorship: NO — the wallet paid its own gas")

    bal = _rpc_retry(w3.eth.get_balance, addr) / 1e18
    print(f"\n  wallet USDC (native == the 0x3600… ERC-20 view): {bal:.6f}")

    print("\nevidence:", "CONFIRMED ON-CHAIN" if ok else "NO ON-CHAIN EVIDENCE IN WINDOW")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
