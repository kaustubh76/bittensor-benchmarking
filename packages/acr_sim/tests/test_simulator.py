"""acr_sim simulator tests."""

from __future__ import annotations

import numpy as np
from acr_core import Service, spec_for
from acr_sim import AttackConfig, OUParams, SimConfig, simulate, simulate_ou


def test_ou_reverts_to_mean():
    rng = np.random.default_rng(0)
    p = OUParams(mu=0.0, theta=0.5, sigma=0.02)
    path = simulate_ou(p, horizon=10_000.0, dt=1.0, rng=rng)
    # Long-run average log-price should sit near mu.
    assert abs(path.log_price[1000:].mean()) < 0.05


def test_simulate_produces_events_for_each_index():
    res = simulate(SimConfig(seed=1, horizon=3600.0, events_per_service=500))
    assert len(res.events) > 0
    for svc in Service:
        evs = [e for e in res.events if e.service == svc]
        assert len(evs) > 100
    # Batching stamped every event.
    assert all(e.batch_id is not None and e.settled_ts is not None for e in res.events)
    assert all(e.settled_ts >= e.ts for e in res.events)


def test_simulation_is_reproducible():
    a = simulate(SimConfig(seed=42, horizon=3600.0, events_per_service=300))
    b = simulate(SimConfig(seed=42, horizon=3600.0, events_per_service=300))
    assert len(a.events) == len(b.events)
    assert a.events[0].price == b.events[0].price


def test_quality_premia_order_prices():
    # The simulator plants a known premium structure (frontier > mid > open) that
    # Pillar 2 must later strip. Here we only assert the structure is present:
    # frontier-class prints are dearer than open-class prints for the same index.
    from acr_core import ModelClass

    res = simulate(SimConfig(seed=3, horizon=3600.0, events_per_service=2000))
    svc = spec_for("ACR-INF").service
    evs = [e for e in res.events if e.service == svc and not e.is_adversarial]
    frontier = [e.price for e in evs if e.model_class == ModelClass.FRONTIER]
    openp = [e.price for e in evs if e.model_class == ModelClass.OPEN]
    assert frontier and openp
    assert np.median(frontier) > np.median(openp)


def test_reference_quality_prices_at_latent():
    # A MID-class seller at the 250ms reference latency prices ~at the latent
    # rate (premium normalized to 1.0), so the constant-quality index == latent.
    from acr_core import ModelClass, Service
    from acr_sim.sellers import Seller

    s = Seller(
        seller_id="0xref",
        service=Service.INFERENCE,
        model_class=ModelClass.MID,
        latency_slo_ms=250.0,
        schema_id="inf.v1",
        log_bias=0.0,
        weight=1.0,
    )
    assert abs(s.quality_premium() - 1.0) < 1e-9


def test_attack_injects_flagged_wash_within_budget():
    atk = AttackConfig(budget_usdc=1000.0, target_multiplier=2.0, trade_notional=50.0)
    res = simulate(
        SimConfig(seed=5, horizon=3600.0, events_per_service=500, attack=atk)
    )
    adv = [e for e in res.events if e.is_adversarial]
    assert len(adv) > 0
    assert res.n_adversarial == len(adv)
    # Spend never exceeds the per-service budget (× n services).
    assert res.usdc_attacked <= atk.budget_usdc * len(res.config.services) + 1e-6
