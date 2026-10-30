#!/usr/bin/env python
"""Hand ACRFutures ownership from the retiring EOA to a Circle custody wallet.

``openSeries`` is ``onlyOwner``, and the owner is currently a raw EOA whose key
lives in ``.env`` and in GitHub secrets. That one key was also the venue maker,
the keeper and the x402 ``payTo`` address — four jobs, one credential. Every
other venue write already signs through Circle (see docs/WALLETS.md); this moves
the last one.

**Two-step by design, and that is the safety property.** ``transferOwnership``
only nominates: it sets ``pendingOwner`` and changes nothing else. Ownership
moves when — and only when — the nominee calls ``acceptOwnership``. So if the
Circle leg fails for any reason, the current owner keeps full control and the
venue is exactly as it was. There is no window in which nobody owns it.

The nominee's ability to execute is verified BEFORE the handover by reading its
allowance, because a nominee that cannot transact is a venue that can never open
another series.

    VENUE_OWNER_DRY_RUN=1 uv run python scripts/migrate_venue_owner.py   # default
    VENUE_OWNER_DRY_RUN=0 uv run python scripts/migrate_venue_owner.py   # do it

Dry run is the default: this is a one-way-ish transfer of authority over a live
venue, so it should take a deliberate act, not a typo.
"""

from __future__ import annotations

import os
import sys
import time

from acr_core import get_settings
from acr_oracle_client import build_role_signer
from acr_oracle_client.futures import _rpc_retry

DRY_RUN = os.environ.get("VENUE_OWNER_DRY_RUN", "1") not in ("0", "false", "no")

_OWNER_ABI = [
    {"type": "function", "name": "owner", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "address"}]},
    {"type": "function", "name": "pendingOwner", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "address"}]},
    {"type": "function", "name": "transferOwnership", "stateMutability": "nonpayable",
     "inputs": [{"name": "to", "type": "address"}], "outputs": []},
    {"type": "function", "name": "acceptOwnership", "stateMutability": "nonpayable",
     "inputs": [], "outputs": []},
]


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)
    s = get_settings()
    old_key = os.environ.get("VENUE_OWNER_PRIVATE_KEY", "") or (s.poster_private_key or "")
    if not (s.futures_address and old_key):
        print("set ACR_FUTURES_ADDRESS and VENUE_OWNER_PRIVATE_KEY (the CURRENT owner)")
        sys.exit(1)

    from eth_account import Account
    from web3 import Web3

    w3 = Web3(Web3.HTTPProvider(s.arc_rpc_url, request_kwargs={"timeout": 25}))
    venue = Web3.to_checksum_address(s.futures_address)
    c = w3.eth.contract(address=venue, abi=_OWNER_ABI)

    nominee_signer = build_role_signer("maker", s)
    if nominee_signer is None:
        print("no maker signer — set ACR_CIRCLE_MAKER_WALLET_ID (+ ACR_CIRCLE_API_KEY)")
        sys.exit(1)
    if type(nominee_signer).__name__ != "CircleWalletSigner":
        print("  ✗ the maker role resolved to a LOCAL key, so this would hand the venue "
              "to another raw EOA — which is the thing being fixed. Configure "
              "ACR_CIRCLE_MAKER_WALLET_ID and unset MAKER_PRIVATE_KEY.")
        sys.exit(1)

    nominee = Web3.to_checksum_address(nominee_signer.address)
    old = Account.from_key(old_key)
    owner_now = _rpc_retry(c.functions.owner().call)
    pending_now = _rpc_retry(c.functions.pendingOwner().call)

    print(f"venue     {venue}")
    print(f"owner     {owner_now}")
    print(f"pending   {pending_now}")
    print(f"nominee   {nominee}  (Circle developer-controlled)")

    if owner_now.lower() == nominee.lower():
        print("\n  ↩ already owned by the Circle wallet — nothing to do")
        sys.exit(0)
    if owner_now.lower() != old.address.lower():
        print(f"\n  ✗ the key provided is {old.address}, but the venue's owner is "
              f"{owner_now} — refusing to send a transaction that would revert")
        sys.exit(1)

    # A nominee that cannot transact is a venue that can never open another
    # series. Prove it can before handing anything over.
    from acr_oracle_client import FuturesClient

    fc = FuturesClient(rpc_url=s.arc_rpc_url, futures_address=s.futures_address,
                       signer=nominee_signer)
    usdc = os.environ.get("ACR_USDC_ADDRESS", "0x3600000000000000000000000000000000000000")
    allowance = fc.allowance_units(usdc, nominee)
    gas = _rpc_retry(w3.eth.get_balance, nominee) / 1e18
    print(f"\nnominee readiness: allowance={allowance}, {gas:.4f} USDC for gas")
    if allowance is None:
        print("  ✗ could not read the nominee's allowance — refusing to hand over on a guess")
        sys.exit(1)
    if gas <= 0:
        print("  ✗ the nominee holds no USDC; on Arc that is the gas token, so it could "
              "not accept ownership or open a series. Fund it first.")
        sys.exit(1)

    if DRY_RUN:
        print("\ndry run — would transferOwnership then acceptOwnership. "
              "Re-run with VENUE_OWNER_DRY_RUN=0.")
        sys.exit(0)

    # 1) Nominate. Sets pendingOwner ONLY — the current owner still owns the venue.
    print("\n① transferOwnership (the retiring EOA's last act of authority)")
    tx = c.functions.transferOwnership(nominee).build_transaction({
        "from": old.address,
        "nonce": _rpc_retry(w3.eth.get_transaction_count, old.address),
        "chainId": _rpc_retry(lambda: w3.eth.chain_id),
    })
    signed = w3.eth.account.sign_transaction(tx, old.key)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    h = w3.eth.send_raw_transaction(raw)
    r = w3.eth.wait_for_transaction_receipt(h, timeout=180, poll_latency=2.0)
    if r.status != 1:
        print(f"  ✗ reverted ({w3.to_hex(h)}) — ownership unchanged")
        sys.exit(1)
    print(f"  ✓ {w3.to_hex(h)}")
    print(f"  pendingOwner now {_rpc_retry(c.functions.pendingOwner().call)}")
    time.sleep(3)

    # 2) Accept, signed by Circle. Until this lands the old owner still owns it.
    print("\n② acceptOwnership (signed by Circle custody — no key involved)")
    data = c.functions.acceptOwnership().build_transaction({"gas": 120000})["data"]
    h2 = nominee_signer.send_transaction(w3, {"to": venue, "data": data})
    print(f"  ✓ {h2}")
    time.sleep(4)

    owner_after = _rpc_retry(c.functions.owner().call)
    pending_after = _rpc_retry(c.functions.pendingOwner().call)
    print(f"\nowner     {owner_now} → {owner_after}")
    print(f"pending   {pending_after}")
    ok = owner_after.lower() == nominee.lower()
    verdict = (
        "✓ the venue is owned by a Circle wallet; no private key has authority over it"
        if ok
        else "✗ ownership did not move — the old owner still holds it"
    )
    print(f"\n{verdict}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
