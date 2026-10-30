"""Determinism + the hedonic numpy fallback — the guarantees the docstrings
promise but weren't tested.
"""

from __future__ import annotations

import builtins

import numpy as np
from acr_core import get_settings
from acr_estimator import estimate_index, manipulation_bound
from acr_estimator.hedonic import fit_hedonic
from acr_sim import AttackConfig, SimConfig, simulate


def _events(seed: int, attack: bool = False):
    atk = AttackConfig(budget_usdc=6000.0) if attack else None
    return simulate(SimConfig(seed=seed, horizon=3600.0, events_per_service=2000, attack=atk))


def test_simulate_is_byte_identical():
    a = _events(3, attack=True)
    b = _events(3, attack=True)
    assert len(a.events) == len(b.events)
    assert [(e.event_id, e.price, e.size, e.seller, e.buyer) for e in a.events] == [
        (e.event_id, e.price, e.size, e.seller, e.buyer) for e in b.events
    ]


def test_estimate_index_is_deterministic():
    res = _events(3)
    s = get_settings()
    p1, _ = estimate_index("ACR-INF", res.events, res.attestations, ts=3600.0, settings=s)
    p2, _ = estimate_index("ACR-INF", res.events, res.attestations, ts=3600.0, settings=s)
    assert p1.value == p2.value
    assert (p1.ci_lo, p1.ci_hi) == (p2.ci_lo, p2.ci_hi)
    assert p1.attack_cost_per_bp == p2.attack_cost_per_bp


def test_manipulation_bound_is_deterministic():
    rng = np.random.default_rng(0)
    prices = 0.5 * np.exp(rng.normal(0, 0.03, 400))
    weights = np.full(400, 40.0)
    a = manipulation_bound(prices, weights, 0.1, 1.0, 0.0001, cluster_cap=0.05, raw_total=16000.0)
    b = manipulation_bound(prices, weights, 0.1, 1.0, 0.0001, cluster_cap=0.05, raw_total=16000.0)
    assert a == b


def test_hedonic_numpy_fallback_matches_statsmodels(monkeypatch):
    # Force the numpy fallback by making `import statsmodels.api` fail, and check
    # it recovers coefficients close to the statsmodels WLS fit with a real R².
    res = _events(7)
    events = res.events_for("ACR-INF")
    ref = fit_hedonic(events, res.attestations)  # statsmodels path

    real_import = builtins.__import__

    def _no_statsmodels(name, *args, **kwargs):
        if name.startswith("statsmodels"):
            raise ImportError("blocked for test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_statsmodels)
    fb = fit_hedonic(events, res.attestations)  # numpy fallback path

    assert fb.fitted and fb.r2 > 0.0  # real R², not the old hardcoded 0.0
    assert abs(fb.latency_coef - ref.latency_coef) < 1e-6
    for c, coef in ref.class_coef.items():
        assert abs(fb.class_coef[c] - coef) < 1e-6
