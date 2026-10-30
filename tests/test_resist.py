"""The resist checker — inputs generated AT the boundaries, not near them.

Every other test in this repository feeds the engine a plausible tape and checks
the answer looks sensible. This one does the opposite: it constructs the exact
inputs where a numerical rule changes its mind — a cumulative weight landing
precisely on half, a value sitting exactly on a trim edge, a timestamp one
second from a contract's skew limit, a float pair one ulp apart — and asserts the
engine's behaviour there is the behaviour that was intended.

The errors it is built to resist, each one found by measurement in this codebase
rather than imagined:

  rounding          a tie rule that fires on `isclose` rather than equality, so a
                    1e-7 nudge to a weight moved the estimator 50%
  ordering          a published print that changed when the same events arrived
                    in a different order (37% on attack_cost_per_bp)
  truncation        `int()` on a fractional timestamp collapsing two prints onto
                    one, which the contract rejects as non-monotone
  scale             a float triple satisfying ci_lo <= value <= ci_hi inverting
                    after three independent roundings to 1e18
  discreteness      an ulp crossing a `math.ceil` and moving a bound by a whole
                    sybil cluster
  absence           a zero weight or a zero timestamp read as a value rather
                    than as "excluded" / "the first second"

These are properties, not fixtures: they must hold for any input, so where a
value is arbitrary the test sweeps rather than picks.
"""

from __future__ import annotations

import math

import numpy as np
import pytest
from acr_core import Service
from acr_core.mathutils import (
    alpha_trim_mask,
    trimmed_weighted_median,
    weighted_median,
    weighted_quantile,
)
from acr_estimator import estimate_index
from acr_oracle_client.client import PostPayload, to_usdc, to_wad
from acr_sim import SimConfig, simulate

# ── ordering ────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def scenario():
    res = simulate(SimConfig(seed=5, horizon=3600.0, events_per_service=1200))
    return res


@pytest.mark.parametrize("index_id", ["ACR-INF", "ACR-GPU", "ACR-DATA"])
def test_the_print_does_not_depend_on_arrival_order(scenario, index_id):
    """A rate that moves when the same trades arrive in a different order is not
    a rate. Measured before this held: 2.3 bp on value, 19.8 bp on ci_hi and 37%
    on attack_cost_per_bp for ACR-DATA."""
    fwd, _ = estimate_index(index_id, list(scenario.events), scenario.attestations, ts=3600.0)
    rev, _ = estimate_index(
        index_id, list(reversed(scenario.events)), scenario.attestations, ts=3600.0
    )
    assert fwd.value == rev.value
    assert (fwd.ci_lo, fwd.ci_hi) == (rev.ci_lo, rev.ci_hi)
    assert fwd.attack_cost_per_bp == rev.attack_cost_per_bp
    assert fwd.n_obs == rev.n_obs


def test_a_shuffle_is_not_a_different_market(scenario):
    """Reversal is one permutation; this is several, because an order bug can
    survive reversal by symmetry."""
    base, _ = estimate_index("ACR-INF", list(scenario.events), scenario.attestations, ts=3600.0)
    for seed in (1, 2, 3):
        shuffled = list(scenario.events)
        np.random.default_rng(seed).shuffle(shuffled)
        got, _ = estimate_index("ACR-INF", shuffled, scenario.attestations, ts=3600.0)
        assert got.value == base.value, f"shuffle {seed} moved the print"
        assert got.ci_lo == base.ci_lo and got.ci_hi == base.ci_hi
        assert got.attack_cost_per_bp == base.attack_cost_per_bp


# ── rounding: the median's tie boundary ─────────────────────────────────────


def test_the_median_is_continuous_across_the_half_weight_boundary():
    """Walk a weight straight through the exact midpoint and require the result
    to step once, at the boundary, and never wander back.

    The old `np.isclose` tie-break made this sequence read 1.0, 1.5, 1.5, 1.5,
    2.0 — a 50% excursion inside a band 1e-5 wide."""
    seen = [weighted_median([1.0, 2.0], [w, 1.0]) for w in
            (1.001, 1.0, 0.9999999, 0.99999, 0.999, 0.99)]
    assert seen == [1.0, 1.0, 2.0, 2.0, 2.0, 2.0]
    # Every reading is an observed value; the estimator never invents a midpoint.
    assert set(seen) <= {1.0, 2.0}


def test_the_median_lands_on_a_price_someone_paid():
    """For any weights, the weighted median is one of the input values — a
    published rate has to be a price that was actually transacted."""
    rng = np.random.default_rng(0)
    values = np.array([0.5, 0.51, 0.52, 0.53, 0.54])
    for _ in range(200):
        w = rng.random(values.size)
        assert weighted_median(values, w) in set(values.tolist())


def test_an_exact_half_weight_tie_resolves_downward_and_stays_there():
    """The tie itself, constructed exactly. Two equal weights put the cumulative
    sum precisely on half; the rule is the LOWER weighted median."""
    assert weighted_median([1.0, 2.0], [1.0, 1.0]) == 1.0
    assert weighted_median([1.0, 2.0, 3.0, 4.0], [1.0, 1.0, 1.0, 1.0]) == 2.0
    # And it is stable under scaling the weights, which must not change a median.
    for k in (1e-6, 1.0, 1e6):
        assert weighted_median([1.0, 2.0], [k, k]) == 1.0


def test_tied_values_do_not_depend_on_their_order(scenario):
    """A tape of round-numbered prices ties constantly. An unstable sort permutes
    the tied block, and the weights ride along, so the cumulative sum rounds
    differently."""
    values = [0.5] * 6 + [0.6] * 6
    weights = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0] * 2
    base = weighted_median(values, weights)
    rng = np.random.default_rng(0)
    for _ in range(50):
        idx = rng.permutation(len(values))
        assert weighted_median([values[i] for i in idx], [weights[i] for i in idx]) == base


# ── absence: a zero is not a small number ───────────────────────────────────


def test_a_zero_weight_cannot_become_the_trim_boundary():
    """A zero-weight observation is one the cleaning stage excluded. It used to
    win the quantile outright: values [1,2,3] with weights [0,0,1] returned 3.0
    for q=0.0, because np.interp on duplicate xp picked the last of the run."""
    assert weighted_quantile([1.0, 2.0, 3.0], [0.0, 0.0, 1.0], 0.0) == 3.0
    assert weighted_quantile([1.0, 2.0, 3.0], [1.0, 0.0, 0.0], 1.0) == 1.0
    # An excluded extreme must not widen the trim band around the survivors.
    kept = alpha_trim_mask([1.0, 5.0, 5.1, 5.2, 100.0], [0.0, 1.0, 1.0, 1.0, 0.0], 0.10)
    assert not kept[0] and not kept[4], "zero-weight outliers must not be kept"


def test_weights_that_sum_to_zero_raise_rather_than_return_a_number():
    for fn in (weighted_median, lambda v, w: weighted_quantile(v, w, 0.5)):
        with pytest.raises(ValueError):
            fn([1.0, 2.0], [0.0, 0.0])


def test_a_negative_weight_is_refused_not_absorbed():
    with pytest.raises(ValueError):
        weighted_median([1.0, 2.0], [-1.0, 2.0])


# ── scale: the float/integer boundary ───────────────────────────────────────


def test_the_scaling_round_trip_holds_for_values_that_are_not_dyadic():
    """0.5 round-trips through any scaling; it is exactly representable. These
    are not, and they are the magnitudes the three indices actually print at."""
    for x in (0.489775770175, 0.011219787886, 0.001998119642, 0.1, 1 / 3):
        assert to_wad(x) / 10**18 == pytest.approx(x, rel=0, abs=1e-15)
    for x in (0.014528085255, 0.012267946914, 1234.5):
        assert to_usdc(x) / 10**6 == pytest.approx(x, rel=0, abs=1e-6)


def test_the_tie_rule_is_half_to_even_and_stays_that_way():
    """Documented, not incidental: half-up would bias every rounded print one
    way, and over a year of hourly prints a one-sided tie rule is a drift."""
    assert to_usdc(0.0000005) == 0
    assert to_usdc(0.0000015) == 2
    assert to_usdc(0.0000025) == 2


def test_the_ci_ordering_survives_three_independent_roundings():
    """pipeline.py deliberately clamps ci_lo to value, so the three floats can
    sit within one WAD ulp. Rounded independently they can invert, and the
    contract rejects that as "value outside CI"."""
    from acr_core import ACRPrint

    base = 0.4897757701751234
    for delta in (0.0, 1e-19, 1e-18, 5e-19):
        p = ACRPrint(
            index_id="ACR-INF",
            ts=3600.0,
            value=base,
            ci_lo=base - delta,
            ci_hi=base + delta,
            attack_cost_per_bp=0.0145,
            n_obs=10,
            trim_alpha=0.1,
        )
        payload = PostPayload.from_print(p)
        assert payload.ci_lo <= payload.value <= payload.ci_hi, f"inverted at delta={delta}"


def test_a_bound_below_one_micro_usdc_is_published_as_the_floor_not_as_zero():
    """The contract requires attackCostPerBp > 0. A true cost under 0.5e-6
    rounds to zero and would revert the whole post, so it is floored — and the
    floor has to be visible, not silent."""
    from acr_core import ACRPrint

    p = ACRPrint(
        index_id="ACR-INF", ts=3600.0, value=0.5, ci_lo=0.5, ci_hi=0.5,
        attack_cost_per_bp=1e-9, n_obs=10, trim_alpha=0.1,
    )
    assert PostPayload.from_print(p).attack_cost_per_bp == 1


# ── truncation: the timestamp boundary ──────────────────────────────────────


def test_two_prints_inside_one_second_collapse_and_that_must_be_visible():
    """`int()` truncates. Two prints 0.4s apart become the same integer, and the
    contract rejects the second as non-monotone — so anything driving the
    estimator on a sub-second clock has a bug the chain will find first."""
    from acr_core import ACRPrint

    def stamp(ts: float) -> int:
        p = ACRPrint(
            index_id="ACR-INF", ts=ts, value=0.5, ci_lo=0.5, ci_hi=0.5,
            attack_cost_per_bp=0.01, n_obs=10, trim_alpha=0.1,
        )
        return PostPayload.from_print(p).timestamp

    assert stamp(3600.0) == stamp(3600.4) == stamp(3600.9) == 3600
    assert stamp(3601.0) == 3601
    # The Fixing clock is integral by construction, which is what makes the
    # truncation safe in production.
    assert stamp(7 * 3600.0) == 25200


# ── discreteness: the ceil boundary ─────────────────────────────────────────


def test_the_notional_total_is_summed_exactly():
    """`raw_total` crosses a math.ceil in the cluster count, so a last-bit
    difference is not a last-bit difference in the answer — it is one whole
    sybil cluster. Python's sequential sum is order-dependent; fsum is not."""
    parts = [1.0, 1e16, -1e16, 0.1, 0.2, 0.3]
    assert math.fsum(parts) == math.fsum(list(reversed(parts)))

    # `fsum` is specified to be exactly rounded, so this holds on every runtime.
    assert math.fsum([0.1] * 10) == 1.0

    # WHAT THIS TEST USED TO ASSERT, AND WHY IT WAS WRONG. It claimed
    # `sum(parts) == math.fsum(parts)` — true on CPython 3.12+, which applies
    # Neumaier compensation inside `sum()` for floats, and false on 3.11. The
    # comment above it even said that was "an implementation detail of one
    # runtime, not a promise of the language", and then the next line asserted it
    # as a promise. It passed locally on 3.13 and failed in CI on 3.11, which is
    # the only reason it was ever found: this repository's CI had not run for a
    # month, so a test encoding one interpreter's behaviour looked green.
    #
    # So assert the PROPERTY the pipeline depends on and nothing about `sum`:
    # whatever the runtime does, `fsum` is the exactly-rounded answer, and the
    # two agreeing is a convenience rather than a guarantee.
    compensated = sum(parts) == math.fsum(parts)
    assert math.fsum(parts) == 1.6  # the true value, on any interpreter
    if not compensated:  # pragma: no cover - depends on the running interpreter
        # Pre-3.12 behaviour: the naive sum loses the small terms entirely, which
        # is exactly the whole-cluster error `raw_total` must never make.
        assert sum(parts) != math.fsum(parts)


# ── the estimator's own invariants ──────────────────────────────────────────


@pytest.mark.parametrize("index_id", ["ACR-INF", "ACR-GPU", "ACR-DATA"])
def test_the_print_satisfies_every_invariant_the_contract_checks(scenario, index_id):
    """The contract's require() list, asserted before a transaction is built —
    a revert on chain costs gas to learn what a test can say for free."""
    p, _ = estimate_index(index_id, scenario.events, scenario.attestations, ts=3600.0)
    assert p.value > 0, "value=0"
    assert p.ci_lo <= p.value <= p.ci_hi, "value outside CI"
    assert p.attack_cost_per_bp > 0, "bound=0"
    payload = PostPayload.from_print(p)
    assert payload.value > 0
    assert payload.ci_lo <= payload.value <= payload.ci_hi
    assert payload.attack_cost_per_bp > 0


def test_the_trim_keeps_the_middle_and_the_median_stays_inside_it():
    """α-trim then median: the answer must lie within the retained band, or the
    trim is decorative."""
    rng = np.random.default_rng(0)
    for _ in range(100):
        v = np.sort(rng.normal(0.5, 0.05, 60))
        w = rng.random(60) + 1e-3
        mask = alpha_trim_mask(v, w, 0.10)
        m = trimmed_weighted_median(v, w, 0.10)
        assert v[mask].min() <= m <= v[mask].max()


def test_an_empty_window_is_an_error_not_an_invented_price(scenario):
    with pytest.raises(ValueError):
        estimate_index("ACR-INF", [], scenario.attestations, ts=3600.0)


def test_a_settled_timestamp_of_zero_is_a_time_not_a_missing_value(scenario):
    """`e.settled_ts or e.ts` treated 0.0 — the first second of any tape-relative
    clock — as absent, and silently fell through to a different clock.

    Built from a real simulated tape with only the field under test overridden:
    a hand-made tape where each buyer trades one seller is itself a sybil
    pattern, and the cleaning stage is right to throw all of it away."""
    events = [
        e.model_copy(update={"settled_ts": 0.0})
        for e in scenario.events
        if e.service == Service.INFERENCE
    ]
    assert events, "the scenario should carry inference flow"
    p, _ = estimate_index("ACR-INF", events, scenario.attestations)
    assert p.ts == 0.0, "a settled_ts of 0.0 must be honoured, not skipped"

    # And the fallback still works when settled_ts is genuinely absent.
    bare = [e.model_copy(update={"settled_ts": None}) for e in events]
    q, _ = estimate_index("ACR-INF", bare, scenario.attestations)
    assert q.ts == max(e.ts for e in bare)
