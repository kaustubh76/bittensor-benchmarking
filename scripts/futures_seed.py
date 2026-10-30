#!/usr/bin/env python
"""Seed a LIVE futures series on Arc testnet against the ALREADY-DEPLOYED venue.

Unlike ``futures_demo.py`` (which deploys its own oracle+USDC to anvil), this
targets the real Arc deployment: it takes the deployed ``ACRFutures`` address, the
existing ``ACROracle``, and the native USDC predeploy. It opens one series, has a
maker + taker post USDC collateral (the live ``approve``/``transferFrom`` test —
Arc's USDC is a native system contract), and puts on a couple of real trades so
the public desk shows an on-chain book.

    ACR_FUTURES_ADDRESS=0x… MAKER_PRIVATE_KEY=0x… TAKER_PRIVATE_KEY=0x… \
    uv run python scripts/futures_seed.py

Small multiplier (default 10) keeps notional/collateral within testnet balances.
Reports honestly and exits 0 if collateral transfer reverts (venue stays live,
series just flat).
"""

from __future__ import annotations

import os
import sys
import time

from acr_oracle_client import FuturesClient
from acr_oracle_client.futures import _rpc_retry

RPC = os.environ.get("ACR_ARC_RPC_URL", "https://rpc.mainnet.arc.io")
FUTURES = os.environ.get("ACR_FUTURES_ADDRESS", "")
USDC = os.environ.get("ACR_USDC_ADDRESS", "0x3600000000000000000000000000000000000000")
INDEX = os.environ.get("SEED_INDEX", "ACR-INF")
MULT = int(os.environ.get("SEED_MULT", "10"))
COLLATERAL = float(os.environ.get("SEED_COLLATERAL", "3"))  # USDC each side
QTY = float(os.environ.get("SEED_QTY", "2"))  # contracts the taker longs
EXPIRY_DAYS = float(os.environ.get("SEED_EXPIRY_DAYS", "2"))
#: Open a NEW series even when an unsettled one exists for the index — used to
#: roll the venue onto a longer expiry before the old series lapses
#: (select_series_for_index prefers the newest unsettled, so the roll is clean).
FORCE_NEW = os.environ.get("SEED_FORCE_NEW", "") == "1"

_ERC20 = [
    {"type": "function", "name": "approve", "stateMutability": "nonpayable",
     "inputs": [{"name": "s", "type": "address"}, {"name": "a", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
    {"type": "function", "name": "balanceOf", "stateMutability": "view",
     "inputs": [{"name": "o", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]},
]


def main() -> None:
    maker_key = os.environ.get("MAKER_PRIVATE_KEY", "")
    taker_key = os.environ.get("TAKER_PRIVATE_KEY", "")
    if not (FUTURES and maker_key and taker_key):
        print("set ACR_FUTURES_ADDRESS + MAKER_PRIVATE_KEY + TAKER_PRIVATE_KEY")
        sys.exit(1)

    from eth_account import Account
    from web3 import Web3

    w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 15}))
    if not w3.is_connected():
        print("Arc RPC unreachable")
        sys.exit(1)

    maker = Account.from_key(maker_key)
    taker = Account.from_key(taker_key)
    usdc = w3.eth.contract(address=w3.to_checksum_address(USDC), abi=_ERC20)

    def bal(a: str) -> float:
        return _rpc_retry(usdc.functions.balanceOf(a).call) / 1e6

    print(f"  venue   {FUTURES}")
    print(f"  maker   {maker.address}  {bal(maker.address):.4f} USDC")
    print(f"  taker   {taker.address}  {bal(taker.address):.4f} USDC")

    def approve(acct, key):  # USDC.approve(futures, big) — the transferFrom prereq
        fn = usdc.functions.approve(w3.to_checksum_address(FUTURES), 2**256 - 1)
        tx = _rpc_retry(fn.build_transaction, {
            "from": acct.address,
            "nonce": w3.eth.get_transaction_count(acct.address),
            "chainId": w3.eth.chain_id,
        })
        signed = acct.sign_transaction(tx)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        h = w3.eth.send_raw_transaction(raw)
        r = w3.eth.wait_for_transaction_receipt(h, timeout=90, poll_latency=2.0)
        time.sleep(2)  # pace the throttled Arc RPC between txs
        return r.status == 1

    maker_fc = FuturesClient(rpc_url=RPC, futures_address=FUTURES, private_key=maker_key)
    taker_fc = FuturesClient(rpc_url=RPC, futures_address=FUTURES, private_key=taker_key)

    # 1) Open the series (owner == maker, since the deployer is the maker).
    expiry = int(w3.eth.get_block("latest")["timestamp"] + EXPIRY_DAYS * 86400)
    # Reuse an existing open series for this index if one exists (idempotent-ish),
    # unless SEED_FORCE_NEW=1 rolls the index onto a fresh series.
    existing = None
    if not FORCE_NEW:
        for s in maker_fc.read_all_series():
            if s["index_id"] == INDEX and not s["settled"]:
                existing = s
                break
    if existing:
        sid = existing["series_id"]
        print(f"  ↩ reusing open series {sid} ({INDEX}, mult {existing['multiplier']})")
    else:
        maker_fc.open_series(INDEX, expiry, MULT, maker.address)
        sid = len(maker_fc.read_all_series()) - 1
        print(f"  ① opened series {sid} ({INDEX}, mult {MULT}, expiry +{EXPIRY_DAYS}d)")

    # 2) Collateral — the LIVE approve + transferFrom test on Arc's native USDC.
    try:
        assert approve(maker, maker_key), "maker approve failed"
        assert approve(taker, taker_key), "taker approve failed"
        maker_fc.post_collateral(sid, COLLATERAL)
        taker_fc.post_collateral(sid, COLLATERAL)
        print(f"  ② maker + taker each posted ${COLLATERAL:.2f} collateral "
              f"(USDC transferFrom works on Arc ✓)")
    except Exception as exc:  # noqa: BLE001 — report the FLAG honestly, don't crash the go-live
        print(f"  ✗ collateral step reverted — USDC transferFrom quirk on the Arc predeploy: {exc}")
        print("    venue stays LIVE; the series is flat (desk renders 'venue deployed'). "
              "Investigate Arc USDC allowance semantics before seeding.")
        sys.exit(0)

    # 3) Taker longs; maker takes the exact mirror.
    taker_fc.trade(sid, QTY)
    print(f"  ③ taker LONG {QTY} contracts @ oracle mark — maker short {QTY} (net zero)")

    # 4) Read the live desk back off-chain.
    desk = maker_fc.read_desk(INDEX)
    if desk:
        print(f"\n  live desk: maker inventory {desk['maker_inventory']:+.1f} · "
              f"unrealized ${desk['maker_unrealized_usdc']:+,.4f} · "
              f"OI {desk['open_interest']:.1f} · traders {desk['trader_count']}")
    print("\n  → pillar 4 is trading on Arc testnet. Set ACR_FUTURES_ADDRESS on the seller.\n")


if __name__ == "__main__":
    main()
