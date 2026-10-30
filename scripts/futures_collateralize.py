#!/usr/bin/env python
"""Deepen the book by topping up the maker's collateral on the live series.

The mirror of ``futures_withdraw.py``, and the lever ``verify_live.py`` points at
when it warns that headroom is thin. It exists because "the maker is
collateralized" and "the book can absorb a trade" are different facts, and the
gap between them is where this venue has already spent eleven hours frozen: the
maker had drifted short against its margin cap, so ``feasible_qty`` returned
``max_buy=0`` and every buy reverted while every dashboard stayed green.

**Sized in trades, not in USDC.** Nobody can look at "5.00 collateral" and say
whether the book is deep enough. So the target is a number of *reader-sized*
trades — what a faucet-funded reader on the Public Desk can actually put on,
``MARGIN_SAFETY · FAUCET_USDC / (mark · multiplier · MARGIN_BPS)`` — and this
computes the USDC that buys it, using the desk's OWN arithmetic
(``index_api.desk.feasible_qty``) rather than a reimplementation. If this script
and the desk ever disagree about what the book can absorb, one of them is lying
to a reader about a trade they are about to authorize with their PIN.

The thin side is the one that matters. A maker short 2.82 has plenty of room to
be sold to and almost none to be bought from, and only the buy side is what a
judge hits when they press BUY. So the target is applied to ``min(max_buy,
max_sell)``, and the report always names which side binds.

    COLLATERALIZE_DRY_RUN=1 uv run python scripts/futures_collateralize.py
    COLLATERALIZE_TARGET_QTY=1.5 …    # contracts of headroom a side (default MAX_QTY)
    COLLATERALIZE_MAX_USDC=1.5 …      # never post more than this
    COLLATERALIZE_ROLE=maker …        # whose collateral (maker | taker)

Every top-up is reported with four independent witnesses captured before and
after: the contract's own record of the balance, the venue's USDC, the wallet's
USDC, and the headroom the desk quotes. A transaction that mined is not proof
the book got deeper; agreeing witnesses are.

Exit codes: 0 = the book is at or above target (already, or after a successful
top-up, or after a dry run that priced one); 1 = a read failed, a transaction
reverted, the witnesses disagreed, or the wallet cannot afford the top-up
without falling through the floor that funds the next roll.
"""

from __future__ import annotations

import os
import sys

from acr_core import get_settings
from acr_oracle_client import FuturesClient, OracleClient, build_role_signer
from acr_oracle_client.futures import _rpc_retry
from index_api.desk import FAUCET_USDC, MARGIN_SAFETY, MAX_QTY, feasible_qty

INDEX = os.environ.get("COLLATERALIZE_INDEX", "ACR-INF")
ROLE = os.environ.get("COLLATERALIZE_ROLE", "maker")
DRY_RUN = os.environ.get("COLLATERALIZE_DRY_RUN", "") not in ("", "0", "false")
#: Contracts of headroom to aim for on the THIN side, defaulting to the largest
#: trade the desk will ever quote. `feasible_qty` clamps its answer at MAX_QTY,
#: so this is not merely a sensible target — it is the ONLY one that saturates,
#: and any number above it is unreachable at any collateral. The property it
#: buys is the one that matters on a public desk: whatever a reader is offered,
#: the book can actually fill.
TARGET_CONTRACTS = float(os.environ.get("COLLATERALIZE_TARGET_QTY", str(MAX_QTY)))
#: How many FULL reader-sized trades the book should still absorb. The headroom
#: target above CANNOT express this: feasible_qty clamps at MAX_QTY, so a book
#: two trades from frozen and one eleven trades from frozen both report a
#: saturated 2.00 and this script says "already at target". That is exactly how
#: ACR-GPU — the index the desk defaults to — sat 1.9 trades from frozen while
#: every check was green. Mirrors VERIFY_BOOK_DEPTH in scripts/verify_live.py.
TARGET_TRADES = float(os.environ.get("COLLATERALIZE_TARGET_TRADES", "4"))
#: A ceiling on a single run, because this moves real money and the operator
#: should decide the size, not an arithmetic that has already been wrong once.
MAX_TOPUP_USDC = float(os.environ.get("COLLATERALIZE_MAX_USDC", "2.0"))
#: What must stay in the wallet after the top-up: 1.5 to collateralize the next
#: series when the keeper rolls, plus 1.0 of gas. Mirrors
#: VERIFY_VENUE_FLOOR_USDC in scripts/verify_live.py — a top-up that strands the
#: next roll has bought depth today by breaking the venue tomorrow.
WALLET_FLOOR_USDC = float(os.environ.get("COLLATERALIZE_WALLET_FLOOR", "2.5"))

_MARGIN_ABI = [{"type": "function", "name": "MARGIN_BPS", "stateMutability": "view",
                "inputs": [], "outputs": [{"name": "", "type": "uint256"}]}]


def reader_trade_size(mark: float, multiplier: int, margin_bps: int) -> float:
    """The largest trade a faucet-funded reader can actually put on.

    CLAMPED at MAX_QTY, because that is what the desk enforces. Unclamped, a
    cheap index made the unit absurd: on ACR-GPU a 0.50 drip covers ~20
    contracts of margin, so a fully saturated book reported "0.0 reader-sized
    trades a side" — a healthy venue described as a dead one, by a script whose
    whole job is to say whether the book is deep enough.
    """
    per_contract = mark * multiplier * (margin_bps / 10_000)
    if per_contract <= 0:
        return 0.0
    return min(MARGIN_SAFETY * FAUCET_USDC / per_contract, MAX_QTY)


def headroom(mark: float, multiplier: int, margin_bps: int,
             collateral: float, maker_inv: float) -> tuple[float, float]:
    """``(max_buy, max_sell)`` the book would quote at ``collateral``.

    Models the counterparty as a reader with the same stake, which is what
    ``verify_live`` does — the point is to isolate the MAKER's contribution, so
    both sides carry the same taker cap and any asymmetry that survives is the
    maker's inventory talking.
    """
    return feasible_qty(mark, multiplier, margin_bps, collateral, 0.0, collateral, maker_inv)


def collateral_for_depth(
    per_contract: float, maker_inv: float, unit: float, trades: float
) -> float:
    """Collateral so the book still absorbs ``trades`` full reader-sized trades.

    Depth is the maker's UNCLAMPED cap less what it is already carrying, over
    what one reader can put on: ``(m_cap - |inv|) / unit``. Inverting that is
    the only way to size for "how many judges can trade", which is the question
    during a demo — "can the next trade fill" is a different and much easier one.
    """
    if per_contract <= 0 or unit <= 0:
        return 0.0
    need_cap = trades * unit + abs(maker_inv)
    return round(need_cap * per_contract / MARGIN_SAFETY + 0.005, 2)


def required_collateral(mark: float, multiplier: int, margin_bps: int,
                        maker_inv: float, target: float) -> float:
    """The smallest collateral whose thin side clears ``target`` contracts.

    Solved by search rather than algebra on purpose: ``feasible_qty`` clamps at
    ``MAX_QTY`` and rounds to two places, and an inverse derived from the
    formula would quietly disagree with the function that actually decides
    whether a trade reverts. Ask the real thing.
    """
    lo, hi = 0.0, 1.0
    for _ in range(40):  # grow until the target is reachable at all
        if min(headroom(mark, multiplier, margin_bps, hi, maker_inv)) >= target:
            break
        lo, hi = hi, hi * 2
    else:
        return float("inf")
    for _ in range(60):  # bisect to the cent
        mid = (lo + hi) / 2
        if min(headroom(mark, multiplier, margin_bps, mid, maker_inv)) >= target:
            hi = mid
        else:
            lo = mid
    return round(hi + 0.005, 2)


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)
    s = get_settings()
    key = os.environ.get("COLLATERALIZE_PRIVATE_KEY", "")
    signer = build_role_signer(ROLE, s, private_key=key or None)
    if not (s.futures_address and signer):
        print(f"set ACR_FUTURES_ADDRESS, and either ACR_CIRCLE_{ROLE.upper()}_WALLET_ID "
              "(+ ACR_CIRCLE_API_KEY) or COLLATERALIZE_PRIVATE_KEY")
        sys.exit(1)

    from web3 import Web3

    w3 = Web3(Web3.HTTPProvider(s.arc_rpc_url, request_kwargs={"timeout": 25}))
    me = signer.address
    venue = Web3.to_checksum_address(s.futures_address)
    fc = FuturesClient(rpc_url=s.arc_rpc_url, futures_address=s.futures_address, signer=signer)
    oracle = OracleClient(rpc_url=s.arc_rpc_url, oracle_address=s.oracle_address or None)

    # USDC is Arc's native gas token, so a plain balance read IS the USDC balance.
    def usdc(addr: str) -> float:
        return _rpc_retry(w3.eth.get_balance, Web3.to_checksum_address(addr)) / 1e18

    try:
        c = w3.eth.contract(address=venue, abi=_MARGIN_ABI)
        margin_bps = int(_rpc_retry(c.functions.MARGIN_BPS().call))
    except Exception:
        margin_bps = 2000

    desk = fc.read_desk(INDEX)
    if not desk or desk.get("settled"):
        print(f"no live {INDEX} series on {venue}")
        sys.exit(1)
    sid, mult = desk["series_id"], desk["multiplier"]

    mark = float((oracle.read_latest(INDEX) or {}).get("value") or 0.0)
    if mark <= 0:
        print(f"  ✗ no live {INDEX} mark — the margin requirement is priced off it, "
              "and sizing against zero would post a number that means nothing")
        sys.exit(1)

    # A read that failed is not a balance of zero. Posting collateral against a
    # throttled read is how this project has twice paid to learn the difference.
    units = fc.collateral_units_of(sid, me)
    maker_inv = desk.get("maker_inventory")
    if units is None or maker_inv is None:
        print("  ✗ the chain would not say what is already posted — refusing to guess")
        sys.exit(1)
    posted = units / 1e6

    unit = reader_trade_size(mark, mult, margin_bps)
    buy, sell = headroom(mark, mult, margin_bps, posted, float(maker_inv))
    thin, side = (buy, "buy") if buy <= sell else (sell, "sell")

    print(f"venue   {venue}\nwallet  {me} ({ROLE})")
    print(f"series  {sid} ({INDEX}), mark {mark:.5f}, multiplier {mult}, margin {margin_bps}bps")
    print(f"maker inventory {float(maker_inv):+.2f} contracts")
    print(f"\ncollateral posted   {posted:.2f} USDC")
    print(f"headroom            max_buy {buy:.2f} · max_sell {sell:.2f}"
          f"  → thin side is {side.upper()}")
    print(f"a reader can trade  {unit:.2f} contracts on a {FAUCET_USDC:.2f} USDC drip")
    print(f"so the book absorbs {thin / unit:.1f} reader-sized {side}s "
          f"(target {TARGET_CONTRACTS:.2f} contracts = {TARGET_CONTRACTS / unit:.1f})")

    # Depth first — it is the binding constraint on a cheap book, and the
    # headroom target below saturates before it ever fires.
    m_cap = MARGIN_SAFETY * posted / (mark * mult * margin_bps / 10_000)
    depth = (m_cap - abs(float(maker_inv))) / unit if unit else 0.0
    print(f"depth               {depth:.1f} full reader trades before it freezes "
          f"(target {TARGET_TRADES:.0f})")
    want_depth = collateral_for_depth(
        mark * mult * (margin_bps / 10_000), float(maker_inv), unit, TARGET_TRADES
    )

    target_contracts = TARGET_CONTRACTS
    if thin >= target_contracts and depth >= TARGET_TRADES:
        print(f"\n✓ the book is already at target — {thin:.2f} contracts of "
              f"{side} headroom against {target_contracts:.2f} wanted")
        sys.exit(0)

    want = max(
        required_collateral(mark, mult, margin_bps, float(maker_inv), target_contracts),
        want_depth,
    )
    if want == float("inf"):
        print(f"\n  ✗ {target_contracts:.2f} contracts is not reachable at any "
              f"collateral — feasible_qty clamps at MAX_QTY ({MAX_QTY}). "
              "Lower COLLATERALIZE_TARGET_QTY.")
        sys.exit(1)
    need = round(want - posted, 2)
    bal = usdc(me)
    affordable = round(max(0.0, bal - WALLET_FLOOR_USDC), 2)
    capped = min(need, MAX_TOPUP_USDC, affordable)

    print(f"\nto reach {target_contracts:.2f} contracts a side: {want:.2f} collateral "
          f"→ post {need:.2f} more")
    print(f"wallet holds {bal:.3f} USDC, floor {WALLET_FLOOR_USDC:.2f} "
          f"(1.5 to collateralize the next roll + 1.0 gas) → {affordable:.2f} spare")
    if capped < need:
        reason = ("the per-run ceiling" if MAX_TOPUP_USDC <= affordable
                  else "what the wallet can spare without stranding the next roll")
        print(f"  ! capped at {capped:.2f} by {reason} — this is a partial top-up, "
              "and the book will still be under target afterwards")
    if capped < 0.01:
        print("\n  ✗ nothing can be posted without falling through the floor that "
              "funds the next roll. Move USDC into this wallet first.")
        sys.exit(1)

    got_buy, got_sell = headroom(mark, mult, margin_bps, posted + capped, float(maker_inv))
    got = min(got_buy, got_sell)
    print(f"\nposting {capped:.2f} would take headroom {thin:.2f} → {got:.2f} "
          f"({thin / unit:.1f} → {got / unit:.1f} reader-sized trades)")

    if DRY_RUN:
        print("\ndry run — priced but not posted")
        sys.exit(0)

    before = (fc.collateral_units_of(sid, me), usdc(venue), bal)
    try:
        tx = fc.post_collateral(sid, capped)
    except Exception as exc:
        print(f"  ✗ reverted: {str(exc)[:160]}")
        sys.exit(1)
    if not tx:
        print("  ✗ no transaction sent — is the wallet funded and writable?")
        sys.exit(1)
    after = (fc.collateral_units_of(sid, me), usdc(venue), usdc(me))
    if after[0] is None:
        print(f"  tx {tx}\n  ✗ could not re-read the contract's balance — "
              "the top-up may have landed; check before posting again")
        sys.exit(1)

    fresh = fc.read_desk(INDEX) or {}
    inv_now = fresh.get("maker_inventory", maker_inv)
    end_buy, end_sell = headroom(mark, mult, margin_bps, after[0] / 1e6, float(inv_now))

    print(f"\n  tx {tx}")
    print(f"  contract balance  {before[0]} → {after[0]} units")
    print(f"  venue USDC        {before[1]:.6f} → {after[1]:.6f}")
    print(f"  wallet USDC       {before[2]:.6f} → {after[2]:.6f}  (gas is paid from this)")
    print(f"  headroom          {thin:.2f} → {min(end_buy, end_sell):.2f} contracts "
          f"(max_buy {end_buy:.2f} · max_sell {end_sell:.2f})")

    # The witnesses must AGREE. A mined transaction is not a deeper book: the
    # contract's own record rising by exactly what was posted, the venue's USDC
    # rising with it, and the desk quoting more room are three different things.
    expected = int(round(capped * 1e6))
    failures = []
    if after[0] != before[0] + expected:
        failures.append(f"contract balance did not rise by {expected} units")
    if after[1] <= before[1]:
        failures.append("the venue's own USDC did not rise — nothing reached the contract")
    # Headroom OR depth. Asserting headroom alone fails a top-up that worked:
    # feasible_qty clamps at MAX_QTY, so on a cheap book headroom is already
    # saturated and cannot rise however much collateral is posted — the money
    # buys DEPTH, which is the thing that was short. This check called a
    # correct 0.11 USDC top-up a failure while the contract balance, the venue
    # balance and the chain all said it landed.
    _per = mark * mult * (margin_bps / 10_000)
    end_m_cap = MARGIN_SAFETY * (after[0] / 1e6) / _per if _per > 0 else 0.0
    end_depth = (end_m_cap - abs(float(inv_now))) / unit if unit else 0.0
    if min(end_buy, end_sell) <= thin and end_depth <= depth:
        failures.append("neither the quoted room nor the book's depth improved")
    for f in failures:
        print(f"  ✗ {f}")
    if not failures:
        print("  ✓ all witnesses agree")
        print(f"\n✓ the book now absorbs {min(end_buy, end_sell) / unit:.1f} "
              f"reader-sized trades a side")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
