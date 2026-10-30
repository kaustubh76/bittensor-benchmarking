#!/usr/bin/env python
"""Seed the demo sellers into the real Arc tape — a tiny USDC transfer to each.

The estimator's hedonic stage reads on-chain attestations and matches them to
tape sellers by address. ``attest_once.py`` registers the demo sellers on-chain;
this makes them *appear in the tape* by sending each a tiny USDC amount via the
ERC-20 ``transfer`` on Arc's native USDC contract (a plain value send wouldn't
emit a ``Transfer`` log, which is what ``ArcSource`` decodes). After this,
``ACR_TAPE_SOURCE=arc`` sees each attested address as a real seller
(``seller = to``), so the estimator consumes their real on-chain attestations.

Needs a funded ``ACR_POSTER_PRIVATE_KEY`` (gas + the transferred amount are USDC
on Arc). Offline it prints what it would do and exits 0.

    uv run python scripts/seed_sellers.py            # or: make seed-sellers
"""

from __future__ import annotations

import os
import sys
import time

from acr_core import get_settings
from acr_oracle_client.demo_sellers import DEMO_SELLERS

FALLBACK_EXPLORER = "https://explorer.arc.io"  # Arc mainnet; the chain profile overrides it
USDC_DECIMALS = 6
SEED_USDC = 0.01  # tiny — just enough to create a real Transfer log per seller
PACE_S = 3.0
RETRIES = 4

# Minimal ERC-20 ABI (Arc USDC is a standard ERC-20 at the native system address).
ERC20_ABI = [
    {
        "name": "transfer",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}],
        "outputs": [{"name": "", "type": "bool"}],
    }
]


def _explorer(settings) -> str:
    base = getattr(settings, "explorer_base", "") or os.environ.get("ACR_EXPLORER_BASE", "")
    return (base or FALLBACK_EXPLORER).rstrip("/")


def _with_retry(fn):
    last = None
    for i in range(RETRIES):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - surface after retries
            last = exc
            msg = str(exc).lower()
            if "429" in msg or "too many" in msg or "timeout" in msg or "rate" in msg:
                time.sleep(PACE_S * (i + 1))
                continue
            raise
    raise last


def main() -> None:
    s = get_settings()
    explorer = _explorer(s)
    key = s.poster_private_key
    if not key or key.startswith("#"):
        print("\n  no funded ACR_POSTER_PRIVATE_KEY — nothing sent. Would seed:")
        for d in DEMO_SELLERS:
            print(f"    {d.address}  {d.service.name}/{d.model_class.name}")
        print()
        return

    from eth_account import Account
    from web3 import Web3

    w3 = Web3(Web3.HTTPProvider(s.arc_rpc_url, request_kwargs={"timeout": 10}))
    acct = Account.from_key(key)
    usdc = w3.eth.contract(address=w3.to_checksum_address(s.usdc_address), abi=ERC20_ABI)
    amount = int(SEED_USDC * 10**USDC_DECIMALS)

    print(f"\n  seeding {len(DEMO_SELLERS)} sellers with {SEED_USDC} USDC each (from {acct.address[:10]}…)")
    print(f"  {'SELLER':<44} tx")
    print("  " + "-" * 100)
    sent = 0
    for i, d in enumerate(DEMO_SELLERS):
        if i:
            time.sleep(PACE_S)

        def _send(to=d.address):
            tx = usdc.functions.transfer(w3.to_checksum_address(to), amount).build_transaction(
                {
                    "from": acct.address,
                    "nonce": w3.eth.get_transaction_count(acct.address),
                    "chainId": w3.eth.chain_id,
                }
            )
            signed = acct.sign_transaction(tx)
            raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
            h = w3.to_hex(w3.eth.send_raw_transaction(raw))
            rcpt = w3.eth.wait_for_transaction_receipt(h, timeout=40)
            if rcpt.status != 1:
                raise RuntimeError("transfer reverted")
            return h

        try:
            tx = _with_retry(_send)
        except Exception as exc:
            print(f"  {d.address:<44} ✗ {exc}")
            continue
        sent += 1
        print(f"  {d.address:<44} {explorer}/tx/{tx}")

    if sent == 0:
        print("\n  ✗ no transfers landed — check the RPC + poster USDC balance.\n")
        sys.exit(1)
    print(
        f"\n  ✓ seeded {sent}/{len(DEMO_SELLERS)} sellers into the tape — with ACR_TAPE_SOURCE=arc"
        " the estimator now reads their on-chain attestations.\n"
    )


if __name__ == "__main__":
    main()
