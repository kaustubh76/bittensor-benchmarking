"""acr_estimator pillar + pipeline tests."""

from __future__ import annotations

import numpy as np
from acr_core import get_settings
from acr_estimator import (
    ObservationModel,
    clean,
    estimate,
    estimate_index,
    manipulation_bound,
)
from acr_estimator.pipeline import _bar_series
from acr_sim import AttackConfig, SimConfig, simulate


# --- Pillar 1: observation model deconvolves the (causal) batch operator ---
def _trailing_ma(x: np.ndarray, k: int) -> np.ndarray:
    """Causal trailing box average of width k — the operator H the model uses."""
    return np.array([x[max(0, t - k + 1) : t + 1].mean() for t in range(x.size)])


def test_kalman_deconvolves_box_filter():
    # Model-consistent test: latent is a random walk (local-level), observed as a
    # trailing moving average (the batching operator) plus noise. The smoother
    # knows H and inverts it, so it must beat the raw batched observation.
    rng = np.random.default_rng(0)
    n, k = 300, 8
    proc_std, obs_std = 0.05, 0.02
    latent = np.cumsum(rng.normal(0.0, proc_std, n))
    obs = _trailing_ma(latent, k) + rng.normal(0.0, obs_std, n)

    om = ObservationModel(batch_width=k, process_var=proc_std**2, obs_var=obs_std**2)
    sm = om.smooth(obs)
    err_obs = np.mean((obs - latent) ** 2)
    err_sm = np.mean((sm.latent - latent) ** 2)
    assert err_sm < err_obs, f"smoother {err_sm:.4f} should beat raw obs {err_obs:.4f}"


# --- robust estimator ---
def test_robust_estimate_has_valid_ci():
    rng = np.random.default_rng(1)
    prices = 0.5 * np.exp(rng.normal(0, 0.05, 400))
    weights = rng.gamma(2, 1, 400)
    est = estimate(prices, weights, alpha=0.1, n_bootstrap=200)
    assert est.ci_lo <= est.value <= est.ci_hi
    assert est.breakdown_point >= 0.5


# --- Pillar 3: bound is positive and finite on clean data ---
def test_manipulation_bound_positive_and_deterministic():
    rng = np.random.default_rng(2)
    prices = 0.5 * np.exp(rng.normal(0, 0.03, 500))
    weights = np.full(500, 40.0)
    mb = manipulation_bound(prices, weights, alpha=0.1, fee_bps=1.0, fee_flat=0.0001)
    assert mb.cost_per_bp > 0
    assert np.isfinite(mb.cost_per_bp)
    assert mb.cheapest_direction in ("up", "down")


# --- cleaning flags injected wash ---
def test_cleaning_flags_adversarial_flow():
    atk = AttackConfig(budget_usdc=3000.0, target_multiplier=2.0, trade_notional=50.0)
    res = simulate(SimConfig(seed=9, horizon=3600.0, events_per_service=1500, attack=atk))
    inf = [e for e in res.events if e.service.value == "inference"]
    cr = clean(inf, cluster_cap=0.05)
    flagged = cr.all_flagged
    adv_ids = {e.event_id for e in inf if e.is_adversarial}
    # A majority of injected wash should be caught (self-dealing/wash/sybil).
    caught = len(flagged & adv_ids) / max(1, len(adv_ids))
    assert caught > 0.5


# --- Pillar 2: hedonic recovers the true rate across all indices ---
def test_hedonic_estimate_tracks_true_level_all_indices():

    res = simulate(SimConfig(seed=7, horizon=3600.0, events_per_service=3000))
    for iid in ("ACR-INF", "ACR-GPU", "ACR-DATA"):
        p, d = estimate_index(iid, res.events, res.attestations)
        true = res.true_window_level(iid)
        err = abs(p.value - true) / true
        assert d.hedonic.r2 > 0.8, f"{iid} hedonic R2 too low: {d.hedonic.r2}"
        assert err < 0.05, f"{iid} off true by {err:.2%} (guards seller-id collision)"


# --- THE headline property: ACR resists an attack that fools VWAP ---
def test_acr_resists_attack_that_swings_vwap():
    cfg_clean = SimConfig(seed=11, horizon=3600.0, events_per_service=2500)
    clean_res = simulate(cfg_clean)

    atk = AttackConfig(budget_usdc=8000.0, target_multiplier=2.5, trade_notional=40.0)
    cfg_atk = SimConfig(
        seed=11, horizon=3600.0, events_per_service=2500, attack=atk
    )
    atk_res = simulate(cfg_atk)

    idx = "ACR-INF"
    settings = get_settings()

    p_clean, d_clean = estimate_index(idx, clean_res.events, clean_res.attestations, settings=settings)
    p_atk, d_atk = estimate_index(idx, atk_res.events, atk_res.attestations, settings=settings)

    # Naive VWAP is dragged up hard by the wash flow...
    vwap_swing = abs(d_atk.naive_vwap - d_clean.naive_vwap) / d_clean.naive_vwap
    # ...while ACR barely moves.
    acr_swing = abs(p_atk.value - p_clean.value) / p_clean.value

    assert vwap_swing > 0.10, f"attack should move VWAP >10% (got {vwap_swing:.3f})"
    assert acr_swing < vwap_swing / 2, f"ACR moved {acr_swing:.3f} vs VWAP {vwap_swing:.3f}"
    # Absolute stability, not just relative: ACR itself barely moves.
    assert acr_swing < 0.03, f"ACR should stay within 3% under attack (got {acr_swing:.3f})"
    assert p_atk.attack_cost_per_bp and p_atk.attack_cost_per_bp > 0


# --- bars are built from cleaned flow: zero-weight wash can't move them ---
def test_zero_weight_events_do_not_shift_bars():
    rng = np.random.default_rng(3)
    from acr_core import Service
    from acr_core.types import TapeEvent

    honest = [
        TapeEvent(event_id=f"h{i}", ts=float(i), service=Service.INFERENCE,
                  seller=f"s{i%5}", buyer=f"b{i%7}", price=float(0.5 * np.exp(rng.normal(0, 0.02))),
                  size=100.0)
        for i in range(300)
    ]
    w = np.ones(len(honest))
    adj = np.array([e.price for e in honest])
    bars0, dur0 = _bar_series(honest, w, adj, bar_volume=5000.0, alpha=0.1)

    # Append excluded (zero-weight) wash at a wild price.
    wash = [
        TapeEvent(event_id=f"w{i}", ts=float(i), service=Service.INFERENCE,
                  seller="0xw", buyer="0xw2", price=5.0, size=100.0)
        for i in range(200)
    ]
    events2 = honest + wash
    w2 = np.concatenate([w, np.zeros(len(wash))])
    adj2 = np.concatenate([adj, np.array([e.price for e in wash])])
    bars1, dur1 = _bar_series(events2, w2, adj2, bar_volume=5000.0, alpha=0.1)

    assert np.array_equal(bars0, bars1)
    assert dur0 == dur1


def test_tilt_stays_small_under_attack():
    atk = AttackConfig(budget_usdc=8000.0, target_multiplier=2.5, trade_notional=40.0)
    res = simulate(SimConfig(seed=11, horizon=3600.0, events_per_service=2500, attack=atk))
    _, d = estimate_index("ACR-INF", res.events, res.attestations, ts=3600.0)
    # The deconvolution tilt is a shrunk end-of-window displacement — wash flow
    # (now removed before bar construction) must not perturb it into the clip.
    assert abs(d.tilt) < 0.02, f"tilt should stay small under attack (got {d.tilt})"
    assert d.robustness is not None
    assert d.robustness.single_cluster_flip_fraction < 1.0  # one cluster can't flip
