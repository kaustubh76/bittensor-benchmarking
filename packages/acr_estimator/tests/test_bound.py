"""Manipulation-bound tests — determinism, monotonicity, cap-awareness, and the
empirical attainability check that keeps the published number honest.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from acr_core import get_settings
from acr_estimator import estimate_index
from acr_estimator.bound import manipulation_bound
from acr_estimator.cleaning import clean
from acr_estimator.hedonic import adjust_prices
from acr_sim import SimConfig, simulate

# redteam/ is not a package — add it to the path for the attack harness.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "redteam"))
from optimal_attack import run_optimal_attack  # noqa: E402

HOUR = 3600.0


def _cleaned(index_id: str, seed: int = 11):
    s = get_settings()
    res = simulate(SimConfig(seed=seed, horizon=HOUR, events_per_service=2500))
    win = res.events_for(index_id)
    adj, _ = adjust_prices(win, res.attestations)
    cr = clean(win, cluster_cap=s.cluster_volume_cap, seed=s.estimator_seed)
    kept = np.where(cr.weights > 0)[0]
    raw_total = float(sum(e.notional for e in win))
    return res, adj[kept], cr.weights[kept], raw_total, s


def test_bound_is_deterministic():
    _, prices, weights, raw_total, s = _cleaned("ACR-INF")
    a = manipulation_bound(prices, weights, s.trim_alpha, s.usdc_fee_bps, s.usdc_fee_flat,
                           cluster_cap=s.cluster_volume_cap, raw_total=raw_total)
    b = manipulation_bound(prices, weights, s.trim_alpha, s.usdc_fee_bps, s.usdc_fee_flat,
                           cluster_cap=s.cluster_volume_cap, raw_total=raw_total)
    assert a == b


def test_bound_monotone_bp_le_1pct():
    _, prices, weights, raw_total, s = _cleaned("ACR-INF")
    b = manipulation_bound(prices, weights, s.trim_alpha, s.usdc_fee_bps, s.usdc_fee_flat,
                           cluster_cap=s.cluster_volume_cap, raw_total=raw_total)
    # Moving 1% costs at least as much notional (and USDC) as moving 1bp.
    assert b.notional_to_move_1pct >= b.notional_per_bp
    assert b.cost_to_move_1pct >= b.cost_per_bp
    assert b.cost_per_bp > 0 and np.isfinite(b.cost_per_bp)


def test_cap_aware_bound_exceeds_legacy():
    # The cap-aware bound prices the funding leg + the fresh identities each
    # cluster needs; it must exceed the legacy cap-unaware marginal cost.
    _, prices, weights, raw_total, s = _cleaned("ACR-INF")
    legacy = manipulation_bound(prices, weights, s.trim_alpha, s.usdc_fee_bps, s.usdc_fee_flat)
    capped = manipulation_bound(prices, weights, s.trim_alpha, s.usdc_fee_bps, s.usdc_fee_flat,
                                cluster_cap=s.cluster_volume_cap, raw_total=raw_total)
    assert capped.cost_per_bp > legacy.cost_per_bp
    assert capped.min_identities >= s.cluster_volume_cap and capped.sybil_clusters_required >= 1


def test_priced_attack_evades_cleaning_and_attains_move():
    # The attack the bound prices must (a) get past the cleaning stack and
    # (b) actually move the median by at least the target — else the number is
    # fiction. Discreteness of the weighted median means the realized move can
    # overshoot; we require it is at least attained.
    res, _, _, _, s = _cleaned("ACR-INF")
    p, d = estimate_index("ACR-INF", res.events, res.attestations, ts=HOUR, settings=s)
    b = d.bound
    out = run_optimal_attack(
        "ACR-INF", res.events, res.attestations,
        b.notional_per_bp, b.sybil_clusters_required, direction=b.cheapest_direction,
    )
    assert out["surviving_injected"] / out["injected_raw"] >= 0.5  # evaded cleaning
    assert out["rel_move_realized"] * 1e4 >= 1.0  # moved >= 1bp


def test_empty_bound_raises():
    with pytest.raises(ValueError):
        manipulation_bound(np.array([]), np.array([]), 0.1, 1.0, 0.0001)
