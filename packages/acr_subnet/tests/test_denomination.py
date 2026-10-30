"""ACR-TAO: the rate in TAO, and what a subnet's alpha buys of its own work.

The test that matters most here is `test_the_tao_rate_moves_when_tao_moves`. It
does not guard a behaviour — it *demonstrates a limitation*, which is the honest
way to carry a caveat that a sentence in a docstring would let a reader skip.

Runs off the recorded Taostats fixture where one is present; the arithmetic tests
need nothing at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from acr_core import ACRPrint
from acr_subnet import (
    TAO_USD_DIVERGENCE_WARN,
    TaoDenominated,
    cross_check_tao_usd,
    denominate,
)

FIXTURE = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "bt" / "taostats.json"


def p(value: float = 0.000345, index_id: str = "ACR-INF") -> ACRPrint:
    return ACRPrint(
        index_id=index_id, ts=3600.0, value=value,
        ci_lo=value * 0.9, ci_hi=value * 1.1, n_obs=20, trim_alpha=0.10,
    )


class _Pool:
    """Stands in for acr_tape.taostats_client.SubnetPool."""

    def __init__(self, netuid: int, alpha_price_tao: float | None):
        self.netuid = netuid
        self.alpha_price_tao = alpha_price_tao


# --- the conversion -------------------------------------------------------


def test_the_rate_converts_to_tao():
    d = denominate(p(0.000345), tao_usd=248.57)
    assert d is not None
    assert d.tao_per_unit == pytest.approx(0.000345 / 248.57)
    assert d.usd_per_unit == 0.000345, "the dollar rate must pass through untouched"


def test_units_per_tao_is_the_reciprocal():
    d = denominate(p(0.000345), tao_usd=248.57)
    assert d.units_per_tao == pytest.approx(1.0 / d.tao_per_unit)


def test_the_source_of_the_fx_rate_is_recorded():
    """Recorded, not averaged — see `cross_check_tao_usd`."""
    d = denominate(p(), tao_usd=248.57, tao_usd_source="taostats")
    assert d.tao_usd_source == "taostats"


def test_a_missing_fx_rate_is_none_rather_than_an_infinity():
    """Dividing by zero would produce an infinity that reads like a number, and
    a missing rate means the conversion cannot be done — not that work is free."""
    assert denominate(p(), tao_usd=0.0) is None
    assert denominate(p(), tao_usd=-1.0) is None


# --- the limitation, demonstrated -----------------------------------------


def test_the_tao_rate_moves_when_tao_moves_and_compute_does_not():
    """The caveat, asserted rather than described.

    Hold the dollar rate of compute exactly fixed, halve TAO, and the
    TAO-denominated rate doubles. Nothing happened in the compute market. This is
    why ACR's estimand is a dollar rate, why ACR-TAO is the right number for a
    TAO-native payer and the wrong one for reading the compute market, and why
    ACR-TAO is a numéraire change rather than a fourth index.
    """
    compute_unchanged = p(0.000345)
    before = denominate(compute_unchanged, tao_usd=250.0)
    after = denominate(compute_unchanged, tao_usd=125.0)
    assert before.usd_per_unit == after.usd_per_unit
    assert after.tao_per_unit == pytest.approx(2.0 * before.tao_per_unit)


def test_the_dollar_rate_is_never_altered_by_the_conversion():
    for fx in (1.0, 248.57, 10_000.0):
        assert denominate(p(0.000345), tao_usd=fx).usd_per_unit == 0.000345


# --- alpha: the work-denominated yardstick --------------------------------


def test_alpha_per_unit_is_tao_per_unit_over_the_alpha_price():
    d = denominate(p(0.000345), tao_usd=248.57, pool=_Pool(64, 0.0673))
    assert d.alpha_price_tao == 0.0673
    assert d.alpha_per_unit == pytest.approx(d.tao_per_unit / 0.0673)
    assert d.netuid == 64


def test_alpha_is_absent_without_a_pool():
    d = denominate(p(), tao_usd=248.57)
    assert d.alpha_price_tao is None
    assert d.alpha_per_unit is None
    assert d.netuid is None


def test_a_pool_with_no_alpha_price_yields_no_alpha_figure():
    """An empty alpha side has no price, and 0.0 would read as 'free'."""
    d = denominate(p(), tao_usd=248.57, pool=_Pool(64, None))
    assert d.alpha_per_unit is None


def test_a_cheaper_alpha_buys_less_work_per_token():
    """Direction check: halve the alpha price and one token buys half the work,
    so it takes twice as many tokens per unit."""
    rich = denominate(p(), tao_usd=250.0, pool=_Pool(64, 0.10))
    cheap = denominate(p(), tao_usd=250.0, pool=_Pool(64, 0.05))
    assert cheap.alpha_per_unit == pytest.approx(2.0 * rich.alpha_per_unit)


def test_the_pool_price_is_reused_not_recomputed():
    """One definition of the pool price in the codebase, not two.

    Proven behaviourally, by what `_Pool` deliberately lacks: it carries
    `alpha_price_tao` and **no reserves at all**. If this module re-derived the
    ratio from `tao_in_pool`/`alpha_in_pool` it could not produce a figure from
    this object, so an `alpha_per_unit` here is proof that
    `SubnetPool.alpha_price_tao` — already tested in `acr_tape` — is the single
    definition being consumed.
    """
    pool = _Pool(64, 0.0673)
    assert not hasattr(pool, "tao_in_pool")
    assert not hasattr(pool, "alpha_in_pool")
    d = denominate(p(), tao_usd=250.0, pool=pool)
    assert d.alpha_per_unit is not None


# --- the cross-check ------------------------------------------------------


def test_two_close_feeds_agree():
    """Measured 2026-10-29: Taostats 248.57 vs Chutes 248.96, 0.16% apart."""
    div, ok = cross_check_tao_usd(248.566574973272, 248.9581)
    assert ok is True
    assert div < TAO_USD_DIVERGENCE_WARN


def test_a_diverging_feed_is_flagged():
    div, ok = cross_check_tao_usd(248.57, 300.0)
    assert ok is False
    assert div > TAO_USD_DIVERGENCE_WARN


def test_a_dead_feed_is_infinite_divergence_not_zero():
    div, ok = cross_check_tao_usd(248.57, 0.0)
    assert ok is False
    assert div == float("inf")


def test_the_feeds_are_not_averaged():
    """The cross-check returns a DIVERGENCE, never a blended price.

    An average of a good feed and a stale one has no provenance, looks healthier
    than either input, and hides the one thing worth noticing. Asserted on the
    return value rather than on the source text: two feeds near $248 would
    average to ~248, so a result below 1 is positive evidence that what comes
    back is a ratio and not a price.
    """
    a, b = 248.566574973272, 248.9581
    div, _ = cross_check_tao_usd(a, b)
    assert div < 1.0
    assert div != pytest.approx((a + b) / 2.0)
    # And the denominated rate uses the feed it was GIVEN, not a blend of the two.
    d = denominate(p(0.000345), tao_usd=a, tao_usd_source="taostats")
    assert d.tao_usd == a
    assert d.tao_per_unit == pytest.approx(0.000345 / a)


# --- against the recorded fixture -----------------------------------------


@pytest.mark.skipif(not FIXTURE.exists(), reason="no taostats fixture")
def test_it_works_on_the_recorded_chain_state():
    from acr_tape import load_fixture, pool_from_fixture

    fx = load_fixture()
    tao_usd = float(fx["tao_price_usd"])
    pool = pool_from_fixture(64)
    # The realized Chutes median, measured: ~0.0003 $/1k tokens.
    d = denominate(p(0.000345), tao_usd=tao_usd, pool=pool)
    assert d is not None
    assert d.tao_per_unit > 0
    assert d.alpha_per_unit is not None and d.alpha_per_unit > 0
    assert isinstance(d, TaoDenominated)
    # A unit of inference costs a tiny fraction of a TAO, which is the sanity
    # check that catches an inverted conversion.
    assert d.tao_per_unit < 1.0
    assert "ACR-INF" in d.summary() and "alpha per unit" in d.summary()
