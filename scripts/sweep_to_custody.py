#!/usr/bin/env python
"""Move a retiring EOA's USDC into a Circle custody wallet.

The last step of the wallet migration (docs/WALLETS.md). Once the venue's maker
and taker are Circle developer-controlled wallets, the old raw-key EOAs still
hold the capital — and capital sitting behind a key in `.env` is exactly what
the migration set out to end. This sweeps it across.

Leaves a **reserve** behind rather than draining to zero, because on Arc USDC is
the gas token: an address with nothing left cannot send anything, including the
transaction that would fix a mistake. The default reserve keeps the retiring
owner able to run `migrate_venue_owner.py`.

    SWEEP_DRY_RUN=1 uv run python scripts/sweep_to_custody.py    # default
    SWEEP_DRY_RUN=0 SWEEP_RESERVE=1.5 uv run python scripts/sweep_to_custody.py

Reports the balances on both sides, before and after — a transfer that claims to
have happened is worth less than two balances that agree it did.
"""

from __future__ import annotations

import os
import sys
import time

from acr_core import get_settings
from acr_oracle_client import build_role_signer
from acr_oracle_client.futures import _rpc_retry

DRY_RUN = os.environ.get("SWEEP_DRY_RUN", "1") not in ("0", "false", "no")
RESERVE = float(os.environ.get("SWEEP_RESERVE", "1.5"))
TO_ROLE = os.environ.get("SWEEP_TO_ROLE", "maker")

_ERC20 = [
    {"type": "function", "name": "transfer", "stateMutability": "nonpayable",
     "inputs": [{"name": "to", "type": "address"}, {"name": "value", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
]


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)
    s = get_settings()
    key = os.environ.get("SWEEP_FROM_PRIVATE_KEY", "") or (s.poster_private_key or "")
    if not key:
        print("set SWEEP_FROM_PRIVATE_KEY (the retiring EOA)")
        sys.exit(1)

    from eth_account import Account
    from web3 import Web3

    w3 = Web3(Web3.HTTPProvider(s.arc_rpc_url, request_kwargs={"timeout": 25}))
    src = Account.from_key(key)
    dest_signer = build_role_signer(TO_ROLE, s)
    if dest_signer is None or type(dest_signer).__name__ != "CircleWalletSigner":
        print(f"  ✗ role '{TO_ROLE}' does not resolve to a Circle wallet — sweeping to "
              "another raw EOA would defeat the purpose. Set "
              f"ACR_CIRCLE_{TO_ROLE.upper()}_WALLET_ID.")
        sys.exit(1)
    dest = Web3.to_checksum_address(dest_signer.address)
    src_addr = Web3.to_checksum_address(src.address)

    src_before = _rpc_retry(w3.eth.get_balance, src_addr) / 1e18
    dst_before = _rpc_retry(w3.eth.get_balance, dest) / 1e18
    amount = src_before - RESERVE

    print(f"from  {src_addr}  {src_before:.6f} USDC  (retiring EOA)")
    print(f"to    {dest}  {dst_before:.6f} USDC  (Circle custody, role '{TO_ROLE}')")
    print(f"reserve kept behind: {RESERVE:.2f} USDC — on Arc that is gas, and an "
          f"address with none cannot send the transaction that fixes a mistake")

    if amount <= 0:
        print(f"\n  ↩ nothing to sweep: {src_before:.6f} is at or below the "
              f"{RESERVE:.2f} reserve")
        sys.exit(0)
    print(f"\nwould move {amount:.6f} USDC")
    if DRY_RUN:
        print("dry run — re-run with SWEEP_DRY_RUN=0")
        sys.exit(0)

    token = Web3.to_checksum_address(s.usdc_address)
    erc20 = w3.eth.contract(address=token, abi=_ERC20)
    tx = erc20.functions.transfer(dest, int(amount * 1_000_000)).build_transaction({
        "from": src_addr,
        "nonce": _rpc_retry(w3.eth.get_transaction_count, src_addr),
        "chainId": _rpc_retry(lambda: w3.eth.chain_id),
    })
    signed = w3.eth.account.sign_transaction(tx, src.key)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    h = w3.eth.send_raw_transaction(raw)
    r = w3.eth.wait_for_transaction_receipt(h, timeout=180, poll_latency=2.0)
    print(f"  tx {w3.to_hex(h)}  status {r.status}")
    if r.status != 1:
        sys.exit(1)
    time.sleep(3)

    src_after = _rpc_retry(w3.eth.get_balance, src_addr) / 1e18
    dst_after = _rpc_retry(w3.eth.get_balance, dest) / 1e18
    print(f"\nfrom  {src_before:.6f} → {src_after:.6f}")
    print(f"to    {dst_before:.6f} → {dst_after:.6f}")
    moved = dst_after - dst_before
    ok = abs(moved - amount) < 0.01
    print(f"\n{'✓' if ok else '✗'} destination gained {moved:.6f} USDC "
          f"(expected {amount:.6f})")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
