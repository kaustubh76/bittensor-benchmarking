#!/usr/bin/env python
"""Move USDC between two Circle custody wallets — treasury → maker, say.

`sweep_to_custody.py` moves money INTO custody and signs with a raw key, so it
cannot help once both ends are Circle wallets. That is now the ordinary case:
the press/treasury, the maker and the heartbeat taker are all developer-
controlled wallets (docs/WALLETS.md), and the maker runs out first because it
is the one that collateralizes every book.

Two things about this path are easy to get wrong, and both have already cost
this project a live failure:

  **The source signer.** `build_role_signer` falls back to `build_signer`, which
  prefers any raw key it can find — and ACR_POSTER_PRIVATE_KEY is always in
  `.env`. Without an explicit check the "treasury" transfer would be signed by
  the retired deploy EOA, which holds nothing. That exact fallback made
  `futures_roll` revert "not owner" after its budget guard had already passed.
  So both ends are asserted to be Circle wallets before anything is sent.

  **Read /1e18, write x1e6.** USDC *is* Arc's native token and the 0x3600…0000
  predeploy is its ERC-20 view of the same balance, so a balance read is
  18-decimal and a `transfer` amount is 6-decimal. `desk.py` reconciles this in
  its faucet; getting it backwards here would move a millionth of the intended
  amount, or a million times it.

The faucet's reserve is IMPORTED, never restated: this wallet also funds every
judge's stake, and a transfer that leaves the drip unable to run has traded a
deeper book for a demo nobody can start.

    FUND_DRY_RUN=1 uv run python scripts/fund_role.py --to maker --amount 0.5
    FUND_DRY_RUN=0 …                                  # actually send
    --from poster --keep-drips 4                      # leave N drips behind

Exit codes: 0 = the transfer landed and both balances agree; 1 = a guard
refused, a read failed, or the witnesses disagreed.
"""

from __future__ import annotations

import argparse
import os
import sys
import time

from acr_core import get_settings

#: Dry by default. This moves real money between wallets nobody can undo.
DRY_RUN = os.environ.get("FUND_DRY_RUN", "1") not in ("0", "false", "no")


def spendable(balance: float, reserve: float, drip: float, keep_drips: int) -> float:
    """How much may leave the source without stranding the faucet.

    The press reserve covers gas runway; the drips are the judges' stakes. Both
    have to survive the transfer, so the answer is what is left after both — and
    it is a REFUSAL, not a clamp: a caller asking for more than this gets told,
    rather than quietly sent less than it asked for.
    """
    return round(max(0.0, balance - reserve - keep_drips * drip), 6)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--to", default="maker", help="destination role (maker | taker | poster)")
    ap.add_argument("--from", dest="src", default="poster", help="source role")
    ap.add_argument("--amount", type=float, required=True, help="USDC to move")
    ap.add_argument("--keep-drips", type=int, default=4,
                    help="faucet stakes the source must still be able to pay")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    from acr_oracle_client import build_role_signer
    from index_api.desk import FAUCET_RESERVE_USDC, FAUCET_USDC
    from web3 import Web3

    s = get_settings()
    usdc = Web3.to_checksum_address(s.usdc_address)

    def circle_signer(role: str):
        sg = build_role_signer(role, s)
        if sg is None or type(sg).__name__ != "CircleWalletSigner":
            kind = "nothing" if sg is None else type(sg).__name__
            print(f"  ✗ role '{role}' resolves to {kind}, not a Circle wallet — "
                  f"set ACR_CIRCLE_{role.upper()}_WALLET_ID. Signing this with the "
                  "ambient poster key would send from the retired deploy EOA.")
            sys.exit(1)
        return sg

    src = circle_signer(args.src)
    dst = circle_signer(args.to)
    if src.address.lower() == dst.address.lower():
        print("  ✗ source and destination are the same wallet")
        sys.exit(1)

    w3 = Web3(Web3.HTTPProvider(s.arc_rpc_url, request_kwargs={"timeout": 25}))

    def bal(addr: str) -> float:
        # Native read: USDC IS the gas token, so this IS the USDC balance.
        return w3.eth.get_balance(Web3.to_checksum_address(addr)) / 1e18

    src_before, dst_before = bal(src.address), bal(dst.address)
    free = spendable(src_before, FAUCET_RESERVE_USDC, FAUCET_USDC, args.keep_drips)

    print(f"from  {args.src:7} {src.address}  {src_before:.4f} USDC")
    print(f"to    {args.to:7} {dst.address}  {dst_before:.4f} USDC")
    print(f"guard  reserve {FAUCET_RESERVE_USDC:.2f} + {args.keep_drips} drips "
          f"({args.keep_drips * FAUCET_USDC:.2f}) → {free:.4f} may leave")

    if args.amount <= 0:
        print("  ✗ amount must be positive")
        sys.exit(1)
    if args.amount > free:
        print(f"  ✗ {args.amount:.2f} would leave the faucet unable to pay "
              f"{args.keep_drips} more stakes — refusing rather than sending less")
        sys.exit(1)

    units = int(round(args.amount * 1_000_000))  # ERC-20 view is 6-decimal
    calldata = (
        "0xa9059cbb"
        + dst.address.lower().replace("0x", "").rjust(64, "0")
        + hex(units)[2:].rjust(64, "0")
    )
    if DRY_RUN:
        print(f"\ndry run — would transfer {args.amount:.2f} USDC ({units} units)")
        sys.exit(0)

    tx = src.send_transaction(None, {"to": usdc, "data": calldata})
    print(f"\n  tx {tx}")
    time.sleep(4)
    src_after, dst_after = bal(src.address), bal(dst.address)
    print(f"  {args.src:7} {src_before:.4f} → {src_after:.4f}  (gas comes out of this too)")
    print(f"  {args.to:7} {dst_before:.4f} → {dst_after:.4f}")

    # Two witnesses. A transaction that returned is not money that moved, and
    # the destination's own balance is the only one that proves it.
    moved = dst_after - dst_before
    if abs(moved - args.amount) > 0.01:
        print(f"  ✗ destination rose by {moved:.4f}, expected {args.amount:.2f}")
        sys.exit(1)
    print("  ✓ both balances agree")
    sys.exit(0)


if __name__ == "__main__":
    main()
