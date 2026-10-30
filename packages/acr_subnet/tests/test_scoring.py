"""The scoring rule — the part of a subnet that decides who gets paid.

Every test here corresponds to a way the mechanism was observed to pay the wrong
miner while it was being built. They are regression tests for incentive bugs,
which are harder to notice than ordinary bugs because the code runs fine and
simply rewards the wrong behaviour.
"""

from __future__ import annotations

import math

import pytest
from acr_subnet import (
    COVERAGE_MAX,
    COVERAGE_MIN,
    RatePrint,
    Score,
    accuracy_score,
    attack_fees_usdc,
    attainability_score,
    consensus_print,
    covered,
    geometric_mean,
    pinball,
    resistance_score,
    score_print,
    weights_from_scores,
    winkler,
)


def rp(uid=1, value=0.5, lo=0.49, hi=0.51, declared=None, epoch=0, strategy="t") -> RatePrint:
    return RatePrint(
        index_id="ACR-INF", epoch=epoch, miner_uid=uid, value=value,
        ci_lo=lo, ci_hi=hi, declared_cost_per_bp=declared, strategy=strategy,
    )


# --- loss functions -------------------------------------------------------


def test_pinball_at_median_is_half_absolute_error():
    assert pinball(1.0, 0.8) == pytest.approx(0.1)
    assert pinball(0.8, 1.0) == pytest.approx(0.1)


def test_pinball_is_asymmetric_away_from_the_median():
    over = pinball(1.0, 0.9, quantile=0.9)
    under = pinball(0.9, 1.0, quantile=0.9)
    assert over != under


def test_winkler_matches_the_inline_formula_it_was_extracted_from():
    """`scripts/eval.py` publishes `mean_winkler_bp` from three inline lines.

    Extracting them into a function must not change the published number, so the
    formula is pinned against a literal re-derivation rather than against a
    remembered value.
    """
    true, lo, hi, ci_level = 0.50, 0.49, 0.51, 0.95
    alpha = 1.0 - ci_level
    penalty = max(lo - true, 0.0) + max(true - hi, 0.0)
    expected = 1e4 * ((hi - lo) + (2.0 / alpha) * penalty) / true
    assert winkler(true, lo, hi, ci_level) == pytest.approx(expected)


def test_winkler_punishes_a_miss_far_more_than_width():
    """At alpha=0.05 the miss term carries a 40x multiplier.

    This is why calibration is a gate and not a weighted component: on an engine
    whose intervals miss most windows, this term dominates and the score
    degenerates into "minimise the miss", which is bought by widening.
    """
    narrow_miss = winkler(0.50, 0.60, 0.61)
    wide_cover = winkler(0.50, 0.30, 0.70)
    assert narrow_miss > wide_cover


def test_winkler_on_a_nonpositive_truth_is_infinite_not_a_crash():
    assert winkler(0.0, 0.1, 0.2) == float("inf")


# --- the attack denominator ----------------------------------------------


def test_attack_fees_count_one_proportional_leg_and_per_identity_funding():
    """The ring is self-funding, so there is no reverse leg to charge for.

    `bound.cost_of` charges `2 * notional * fee_bps` for "wash plus a reverse
    funding leg", but `optimal_attack.build_injection` emits a one-directional
    ring in which every address is both buyer and seller. Charging the
    proportional term twice would overstate the attacker's cost and therefore
    overstate every miner's resistance.
    """
    fees = attack_fees_usdc(
        injected_notional=400.0, n_injected_events=82, n_clusters=2,
        fee_bps=1.0, fee_flat=1e-4, identities_per_cluster=41,
    )
    expected = 400.0 * 1.0 * 1e-4 + 82 * 1e-4 + 2 * 41 * 1e-4
    assert fees == pytest.approx(expected)


def test_attack_fees_are_dominated_by_the_proportional_term():
    """Which is why dollars are a weak denominator and identities are the real one."""
    fees = attack_fees_usdc(400.0, 82, 2, 1.0, 1e-4)
    assert 0.0 < fees < 1.0  # cents, because wallets are nearly free


# --- components -----------------------------------------------------------


def test_accuracy_is_zero_at_the_reference_error_and_one_at_perfect():
    assert accuracy_score(0.5, 0.5)[0] == pytest.approx(1.0)
    v, err = accuracy_score(0.5, 0.5 * (1 + 300e-4))
    assert err == pytest.approx(300.0, rel=1e-6)
    assert v == pytest.approx(0.0, abs=1e-9)


def test_accuracy_never_goes_negative():
    v, _ = accuracy_score(0.5, 5.0)
    assert v == 0.0


def test_resistance_is_one_for_an_immovable_print():
    assert resistance_score(0.0) == 1.0


def test_resistance_falls_as_the_print_moves_more_per_dollar():
    assert resistance_score(10.0) > resistance_score(500.0)


@pytest.mark.parametrize("factor", [2.0, 10.0, 100.0])
def test_attainability_is_symmetric_in_the_log_ratio(factor):
    """Over- and under-claiming by the same factor score the same.

    A published manipulation cost far BELOW the real one is not modesty: it
    tells every reader the feed is cheaper to attack than it is, which is the
    same false statement as the boast with its sign flipped.
    """
    over, _ = attainability_score(1.0 * factor, 1.0)
    under, _ = attainability_score(1.0, 1.0 * factor)
    assert over == pytest.approx(under)
    assert over == pytest.approx(1.0 / factor)


def test_attainability_is_one_for_an_exact_claim():
    v, ratio = attainability_score(0.02, 0.02)
    assert v == pytest.approx(1.0)
    assert ratio == pytest.approx(1.0)


def test_declining_to_claim_drops_the_component_rather_than_zeroing_it():
    """Silence must never score below a lie, or the rule teaches miners to lie."""
    sc = score_print(rp(declared=None), truth=0.5, measured_cost_per_bp=0.02,
                     bp_per_usd=10.0)
    assert sc.attainability is None
    assert sc.total > 0.0

    liar = score_print(rp(declared=2.0), truth=0.5, measured_cost_per_bp=0.02,
                       bp_per_usd=10.0)
    assert liar.attainability is not None
    assert sc.total > liar.total


# --- the combiner ---------------------------------------------------------


def test_geometric_mean_zeroes_on_any_zero_component():
    """An accurate but trivially manipulable miner must score zero."""
    assert geometric_mean([(0.9, 0.5), (0.0, 0.35)]) == 0.0


def test_geometric_mean_renormalises_absent_components():
    """An absent component is harmless; a zero one is fatal. Not the same thing."""
    only = geometric_mean([(0.64, 0.5)])
    assert only == pytest.approx(0.64)


def test_geometric_mean_matches_the_hand_computation():
    got = geometric_mean([(0.8, 0.5), (0.5, 0.5)])
    assert got == pytest.approx(math.sqrt(0.8 * 0.5))


def test_geometric_mean_of_nothing_is_zero():
    assert geometric_mean([]) == 0.0


def test_geometric_mean_rewards_improvement_in_every_component():
    """Unlike `min`, which is flat in all but the worst — see the module docstring
    in acr_subnet.scoring for why a gate and an incentive differ here."""
    base = geometric_mean([(0.5, 0.5), (0.9, 0.5)])
    better_worst = geometric_mean([(0.6, 0.5), (0.9, 0.5)])
    better_best = geometric_mean([(0.5, 0.5), (0.95, 0.5)])
    assert better_worst > base
    assert better_best > base  # a `min` would leave this unchanged


# --- gates and weights ----------------------------------------------------


def test_coverage_gate_has_no_lower_bound():
    """Deliberate: any floor eliminates the field on a 22%-coverage engine."""
    assert COVERAGE_MIN == 0.0
    assert COVERAGE_MAX < 1.0


def test_always_covering_fails_the_gate_because_width_was_bought():
    sc = score_print(rp(), truth=0.5, measured_cost_per_bp=0.02, bp_per_usd=10.0,
                     coverage_history=[True] * 12)
    assert sc.calibrated is False
    assert sc.total == 0.0


def test_gate_does_not_fire_before_there_is_enough_history():
    sc = score_print(rp(), truth=0.5, measured_cost_per_bp=0.02, bp_per_usd=10.0,
                     coverage_history=[True, True])
    assert sc.calibrated is True


def test_a_copy_is_zeroed_outright():
    """Caught, not merely out-scored — a lagged copy earns high accuracy and
    perfect resistance, and would otherwise beat the only miner doing work."""
    sc = score_print(rp(), truth=0.5, measured_cost_per_bp=0.02, bp_per_usd=0.0,
                     is_copy=True)
    assert sc.total == 0.0
    assert sc.detail["copy_of_consensus"] == 1.0


def test_weights_are_normalised():
    scores = [Score(miner_uid=1, epoch=0, index_id="ACR-INF", total=0.6),
              Score(miner_uid=2, epoch=0, index_id="ACR-INF", total=0.2)]
    w = weights_from_scores(scores)
    assert sum(w.values()) == pytest.approx(1.0)
    assert w[1] > w[2]


def test_an_epoch_nobody_won_pays_nobody():
    """Not a uniform split: an epoch in which nothing worth paying for was
    produced should pay nothing, not pay everyone equally."""
    scores = [Score(miner_uid=1, epoch=0, index_id="ACR-INF", total=0.0)]
    assert weights_from_scores(scores) == {}


def test_covered_is_unknowable_without_truth():
    assert covered(rp(), None) is None
    assert covered(rp(value=0.5, lo=0.4, hi=0.6), 0.5) is True
    assert covered(rp(value=0.5, lo=0.4, hi=0.6), 0.9) is False


# --- consensus ------------------------------------------------------------


def test_consensus_is_weighted_toward_the_heavier_miner():
    prints = [rp(uid=1, value=0.50), rp(uid=2, value=0.60, lo=0.59, hi=0.61)]
    near = consensus_print(prints, {1: 0.99, 2: 0.01}, "ACR-INF", ts=3600.0)
    far = consensus_print(prints, {1: 0.01, 2: 0.99}, "ACR-INF", ts=3600.0)
    assert near.value < far.value


def test_consensus_ignores_zero_weight_miners():
    prints = [rp(uid=1, value=0.50), rp(uid=2, value=9.0, lo=8.9, hi=9.1)]
    c = consensus_print(prints, {1: 1.0, 2: 0.0}, "ACR-INF", ts=3600.0)
    assert c.value == pytest.approx(0.50)


def test_consensus_interval_always_contains_its_own_value():
    """Three independent weighted medians need not nest, and ACRPrint refuses a
    print whose value sits outside its interval."""
    prints = [
        rp(uid=1, value=0.50, lo=0.499, hi=0.501),
        rp(uid=2, value=0.80, lo=0.70, hi=0.90),
        rp(uid=3, value=0.20, lo=0.10, hi=0.30),
    ]
    c = consensus_print(prints, {1: 0.4, 2: 0.3, 3: 0.3}, "ACR-INF", ts=3600.0)
    assert c.ci_lo <= c.value <= c.ci_hi


def test_consensus_bound_is_none_when_nobody_declared_one():
    c = consensus_print([rp(uid=1, declared=None)], {1: 1.0}, "ACR-INF", ts=3600.0)
    assert c.attack_cost_per_bp is None


def test_consensus_bound_ignores_abstainers_rather_than_counting_them_as_zero():
    declared_only = consensus_print(
        [rp(uid=1, declared=0.02), rp(uid=2, declared=None)],
        {1: 0.5, 2: 0.5}, "ACR-INF", ts=3600.0,
    )
    assert declared_only.attack_cost_per_bp == pytest.approx(0.02)


def test_consensus_refuses_an_epoch_with_no_weighted_miner():
    with pytest.raises(ValueError, match="no miner carries weight"):
        consensus_print([rp(uid=1)], {}, "ACR-INF", ts=3600.0)


# --- protocol -------------------------------------------------------------


def test_rate_print_refuses_an_interval_that_excludes_its_value():
    with pytest.raises(ValueError, match="interval must contain"):
        RatePrint(index_id="ACR-INF", epoch=0, miner_uid=1,
                  value=0.5, ci_lo=0.6, ci_hi=0.7)


def test_commitment_binds_every_scored_field():
    a = rp(value=0.50)
    b = rp(value=0.50000001)
    assert a.commitment("salt") != b.commitment("salt")
    assert a.commitment("salt") != a.commitment("other-salt")
    assert a.commitment("salt") == rp(value=0.50).commitment("salt")
