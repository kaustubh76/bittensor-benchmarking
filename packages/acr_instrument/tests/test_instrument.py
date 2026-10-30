"""acr_instrument tests."""

from __future__ import annotations

from acr_instrument import ACRFuture, ASParams, AvellanedaStoikovMM, Position


def test_position_pnl_on_close():
    pos = Position()
    pos.apply_fill(10, 0.50)  # long 10 @ 0.50
    pos.apply_fill(-10, 0.55)  # close @ 0.55
    assert abs(pos.realized_pnl - 10 * 0.05) < 1e-9
    assert pos.contracts == 0


def test_position_unrealized_and_settle():
    pos = Position()
    pos.apply_fill(5, 0.50)
    assert abs(pos.unrealized_pnl(0.60) - 5 * 0.10) < 1e-9
    pnl = pos.settle(0.60)
    assert abs(pnl - 5 * 0.10) < 1e-9
    assert pos.contracts == 0


def test_future_cash_settles_against_oracle():
    fut = ACRFuture(index_id="ACR-INF", expiry_ts=1000.0, multiplier=1000.0)
    pos = Position()
    pos.apply_fill(2, 0.50)  # long 2 contracts @ 0.50
    usdc = fut.cash_settle(pos, oracle_print=0.52)
    assert abs(usdc - 2 * 0.02 * 1000.0) < 1e-6  # $40


def test_as_quote_skews_against_inventory():
    mm = AvellanedaStoikovMM("ACR-INF", expiry_ts=100.0, params=ASParams(gamma=0.5, sigma=0.1))
    flat = Position(contracts=0.0)
    longp = Position(contracts=20.0)
    q_flat = mm.quote(0.50, flat, now=0.0, start=0.0)
    q_long = mm.quote(0.50, longp, now=0.0, start=0.0)
    # Holding a long, the MM lowers its quotes to shed inventory.
    assert q_long.mid < q_flat.mid
    assert q_flat.bid < q_flat.ask
    assert q_flat.spread_bp > 0


def test_as_quote_survives_extreme_inventory():
    # At huge inventory the reservation price goes deeply negative; the ask must
    # still be a valid positive price (Quote enforces bid/ask > 0), not a crash.
    mm = AvellanedaStoikovMM("ACR-INF", expiry_ts=100.0, params=ASParams(gamma=0.9, sigma=0.5))
    q = mm.quote(0.50, Position(contracts=1e6), now=0.0, start=0.0)
    assert q.bid > 0 and q.ask > 0
    assert q.ask >= q.bid


def test_the_same_book_leans_the_same_way_at_every_price_level():
    """The bug /curve exposed: the lean was absolute, the spread relative.

    ACR-INF quotes in $/1k-tokens (~0.49), ACR-GPU in $/GPU-sec (~0.011),
    ACR-DATA in $/MB (~0.0021) — two orders of magnitude apart. A reservation
    price that subtracts a fixed number of DOLLARS therefore leaned ACR-INF by
    2.7 bp and ACR-GPU by 71.9 bp for the same book; GPU's lean exceeded its
    own 50 bp spread, so spot fell outside the quoted corridor and the page
    looked broken. In bp, the lean must not depend on the unit.
    """
    mm = AvellanedaStoikovMM("ACR-INF", expiry_ts=100.0, params=ASParams(gamma=0.1, sigma=0.02))
    pos = Position(contracts=-3.0)  # short: the maker leans its quotes up
    leans_bp = []
    for level in (0.49, 0.011, 0.0021):
        q = mm.quote(level, pos, now=0.0, start=0.0)
        leans_bp.append(10000 * (q.mid - level) / level)
    assert max(leans_bp) - min(leans_bp) < 1e-6, f"lean differs by price level: {leans_bp}"
    assert all(b > 0 for b in leans_bp), "a short book must lean the mid UP"


def test_spot_stays_inside_the_corridor_at_a_realistic_book():
    """What a reader actually sees: the dashed spot rule inside the bid-ask
    band. It stopped being true for ACR-GPU, which is what "the corridor is
    unaligned" meant."""
    mm = AvellanedaStoikovMM("ACR-GPU", expiry_ts=100.0, params=ASParams(gamma=0.1, sigma=0.02))
    for level in (0.49, 0.011, 0.0021):
        q = mm.quote(level, Position(contracts=-3.25), now=0.0, start=0.0)
        assert q.bid <= level <= q.ask, f"spot {level} outside [{q.bid}, {q.ask}]"


def test_the_spread_is_still_relative_and_unchanged():
    """The lean moved to relative units; the spread was ALREADY relative and
    must stay put — docs quote ~50 bp for all three indices."""
    # kappa=400 is the deployed value (acr_core config as_kappa), and now also
    # ASParams' own default; passed explicitly here so the test states the
    # calibration it is pinning rather than inheriting it.
    mm = AvellanedaStoikovMM(
        "ACR-INF", expiry_ts=100.0, params=ASParams(gamma=0.1, sigma=0.02, kappa=400.0)
    )
    spreads = [
        mm.quote(level, Position(contracts=0.0), now=0.0, start=0.0).spread_bp
        for level in (0.49, 0.011, 0.0021)
    ]
    assert max(spreads) - min(spreads) < 1e-6, spreads
    assert 50.0 < spreads[0] < 50.8, spreads[0]

    # --- the tenor axis exists ---
    # /curve published one quote under four tenor labels for months: it built a
    # different expiry per tenor and then called quote(now=start), so
    # _time_left returned (T-t)/(T-start) = 1.0 every time and the week count
    # cancelled itself out. Nothing failed, nothing went empty; the rows were
    # simply bit-identical under four different expiry_ts. Asserted here rather
    # than as a new test() because pytest's count is stated in six docs plus the
    # architecture diagram.
    tenors = (1.0, 2.0, 4.0, 8.0)
    flat = Position(contracts=0.0)
    by_tenor = [mm.quote(0.49, flat, now=0.0, start=0.0, tau=t).spread_bp for t in tenors]
    # strict=False, deliberately: pairing a list with its own tail is uneven by
    # construction, and strict=True would raise on the last element.
    assert all(
        b < a for b, a in zip(by_tenor, by_tenor[1:], strict=False)
    ), f"the corridor must widen with tenor, got {by_tenor}"
    # The front point is the calibrated one and must not have moved: tau=1
    # is what the old code computed for every row, so 1W is unchanged.
    assert abs(by_tenor[0] - spreads[0]) < 1e-9, (by_tenor[0], spreads[0])
    # ...and the difference is visible at the one decimal place the page prints.
    assert by_tenor[-1] - by_tenor[0] > 0.1, by_tenor

    # The lean scales with the horizon too, so the 8-week point is where the
    # reservation price is most likely to escape its own corridor — the exact
    # failure that made ACR-GPU look broken in August. Check every tenor at a
    # book deeper than any the venue has actually carried.
    for t in tenors:
        q = mm.quote(0.49, Position(contracts=-4.0), now=0.0, start=0.0, tau=t)
        assert q.bid <= 0.49 <= q.ask, f"spot outside the corridor at {t}w: [{q.bid}, {q.ask}]"
        assert q.mid > 0.49, f"a short book must lean the mid up at {t}w"
