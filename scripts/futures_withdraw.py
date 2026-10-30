#!/usr/bin/env python
"""Take a key's collateral back out of the futures venue, across every series.

The Public Desk gives a *reader* an exit. This is the same exit for the keys the
project itself runs — the heartbeat taker and the maker — which the desk cannot
serve because they are not Circle wallets. It exists because collateral is PER
SERIES: a roll leaves a stake sitting on the retired series, where nothing
trades and nothing reclaims it, and the balance is invisible unless something
walks the whole venue looking for it.

    WITHDRAW_PRIVATE_KEY=0x… uv run python scripts/futures_withdraw.py
    WITHDRAW_SERIES=0 …                        # just one series
    WITHDRAW_DRY_RUN=1 …                       # report what is free, move nothing

The withdrawable amount is computed by the SAME function the desk quotes to
readers (``index_api.desk.free_collateral_units``), so a run here is also a
check on the product: if this script and the desk ever disagree about what is
free, one of them is lying to somebody about their money.

Every withdrawal is reported with four independent witnesses, captured before
and after: the contract's own record of the balance, the venue's USDC, the
recipient's USDC, and the ``CollateralWithdrawn`` event. A passing script is not
evidence; agreeing witnesses are.
"""

from __future__ import annotations

import os
import sys

from acr_core import get_settings
from acr_oracle_client import FuturesClient, OracleClient, build_role_signer
from acr_oracle_client.futures import _rpc_retry
from index_api.desk import free_collateral_units

ONLY = os.environ.get("WITHDRAW_SERIES", "")
DRY_RUN = os.environ.get("WITHDRAW_DRY_RUN", "") not in ("", "0", "false")

_MARGIN_ABI = [{"type": "function", "name": "MARGIN_BPS", "stateMutability": "view",
                "inputs": [], "outputs": [{"name": "", "type": "uint256"}]}]


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)
    s = get_settings()
    # WITHDRAW_ROLE picks which custody wallet to reclaim for ("maker" or
    # "taker"); an explicit key still overrides for anvil and offline runs.
    key = os.environ.get("WITHDRAW_PRIVATE_KEY", "") or os.environ.get("TAKER_PRIVATE_KEY", "")
    role = os.environ.get("WITHDRAW_ROLE", "taker")
    signer = build_role_signer(role, s, private_key=key or None)
    if not (s.futures_address and signer):
        print("set ACR_FUTURES_ADDRESS, and either ACR_CIRCLE_{MAKER,TAKER}_WALLET_ID "
              "(+ ACR_CIRCLE_API_KEY, with WITHDRAW_ROLE) or WITHDRAW_PRIVATE_KEY")
        sys.exit(1)

    from web3 import Web3

    w3 = Web3(Web3.HTTPProvider(s.arc_rpc_url, request_kwargs={"timeout": 25}))
    me = signer.address
    venue = Web3.to_checksum_address(s.futures_address)
    fc = FuturesClient(rpc_url=s.arc_rpc_url, futures_address=s.futures_address, signer=signer)
    oracle = OracleClient(rpc_url=s.arc_rpc_url, oracle_address=s.oracle_address or None)

    try:
        c = w3.eth.contract(address=venue, abi=_MARGIN_ABI)
        margin_bps = int(_rpc_retry(c.functions.MARGIN_BPS().call))
    except Exception:
        margin_bps = 2000

    # USDC is Arc's native gas token, so a plain balance read IS the USDC balance.
    def usdc(addr: str) -> float:
        return _rpc_retry(w3.eth.get_balance, Web3.to_checksum_address(addr)) / 1e18

    series = fc.read_all_series()
    if ONLY:
        series = [x for x in series if str(x["series_id"]) == ONLY]
    if not series:
        print("no series on this venue" + (f" matching {ONLY}" if ONLY else ""))
        sys.exit(1)

    print(f"venue {venue}\nwallet {me}\n")

    claims: list[tuple[dict, int, int]] = []  # (series, units_held, units_free)
    for x in series:
        sid = x["series_id"]
        units = fc.collateral_units_of(sid, me)
        if units is None:
            print(f"  series {sid}: could not read collateral — refusing to guess")
            sys.exit(1)
        if units == 0:
            continue
        pos = fc.position_of(sid, me) or {"contracts": 0.0}
        contracts = pos["contracts"]
        settled = bool(x["settled"])
        # A settled or flat account needs no mark: the contract's margin
        # requirement is zero before it ever consults the oracle. An OPEN
        # position does, and it must be the LIVE oracle mark the contract will
        # margin against — a settled series' settlement_price is 0 until it
        # settles, and quoting against zero would report the whole balance as
        # free and then revert "below margin" on a transaction that cost gas.
        mark = 0.0
        if not settled and contracts != 0:
            print_ = oracle.read_latest(x["index_id"])
            mark = float((print_ or {}).get("value") or 0.0)
            if mark <= 0:
                print(f"  series {sid}: holds {units / 1e6:.6f} USDC with an open "
                      f"position and no live {x['index_id']} mark — cannot size a "
                      "safe withdrawal; wait for the next print")
                continue
        free = free_collateral_units(units, contracts, mark, x["multiplier"], margin_bps, settled)
        state = "settled" if settled else f"open, {contracts:+.2f} contracts"
        print(f"  series {sid} ({x['index_id']}, {state}): "
              f"holds {units / 1e6:.6f} USDC, free {free / 1e6:.6f}")
        if free > 0:
            claims.append((x, units, free))

    if not claims:
        print("\nnothing to withdraw — every stake is either empty or pinned by margin")
        sys.exit(0)
    if DRY_RUN:
        print(f"\ndry run — would withdraw {sum(f for _, _, f in claims) / 1e6:.6f} USDC")
        sys.exit(0)

    failures = 0
    for x, _held, free in claims:
        sid = x["series_id"]
        print(f"\n  withdrawing {free / 1e6:.6f} USDC from series {sid}")
        before = (fc.collateral_units_of(sid, me), usdc(venue), usdc(me))
        try:
            tx = fc.withdraw_collateral(sid, free)
        except Exception as exc:
            print(f"    ✗ reverted: {str(exc)[:160]}")
            failures += 1
            continue
        if not tx:
            print("    ✗ no transaction sent — is the key funded and writable?")
            failures += 1
            continue
        after = (fc.collateral_units_of(sid, me), usdc(venue), usdc(me))

        moved = None
        try:
            rcpt = _rpc_retry(w3.eth.get_transaction_receipt, tx)
            ev = fc._contract().events.CollateralWithdrawn().process_receipt(rcpt)
            if ev:
                moved = int(ev[0]["args"]["amount"])
        except Exception:  # the receipt is a witness, not the transaction itself
            pass

        print(f"    tx {tx}")
        print(f"    contract balance  {before[0]} → {after[0]} units")
        print(f"    venue USDC        {before[1]:.6f} → {after[1]:.6f}")
        print(f"    wallet USDC       {before[2]:.6f} → {after[2]:.6f}  (gas is paid from this)")
        print(f"    CollateralWithdrawn event  {'—' if moved is None else f'{moved} units'}")

        # The witnesses must AGREE. A tx that mined is not proof the money moved:
        # the contract's own record falling by exactly what was asked, and the
        # venue's balance falling with it, is.
        if after[0] != before[0] - free:
            print(f"    ✗ contract balance did not fall by {free} units")
            failures += 1
        elif moved is not None and moved != free:
            print(f"    ✗ event says {moved} units, asked for {free}")
            failures += 1
        elif after[1] >= before[1]:
            print("    ✗ the venue's own USDC did not fall — nothing left the contract")
            failures += 1
        else:
            print("    ✓ all witnesses agree")

    print("\nwithdraw:", "FAILED" if failures else "DONE")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
