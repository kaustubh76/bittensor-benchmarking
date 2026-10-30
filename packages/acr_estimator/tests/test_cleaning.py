"""Cleaning-stack tests — each of the four defenses is exercised, and the
default red-team attack is fully neutralized.

Regression guard for the audit finding that self-dealing and wash-cycle
detection were dead paths under the old simulator.
"""

from __future__ import annotations

import networkx as nx
import numpy as np
from acr_core import Service
from acr_core.types import TapeEvent
from acr_estimator.cleaning import (
    SYBIL_INTERNAL_RATIO,
    _detect_sybil_communities,
    clean,
)
from acr_sim import AttackConfig, SimConfig, simulate

HOUR = 3600.0


def _ev(eid: str, seller: str, buyer: str, price: float = 1.0, size: float = 100.0) -> TapeEvent:
    return TapeEvent(
        event_id=eid, ts=1.0, service=Service.INFERENCE, seller=seller, buyer=buyer,
        price=price, size=size,
    )


# --- defense 1: self-dealing --------------------------------------------------

def test_self_dealing_is_flagged_and_zeroed():
    events = [_ev(f"h{i}", f"s{i%3}", f"b{i%3}") for i in range(9)]
    events.append(_ev("self", "0xself", "0xself"))  # buyer == seller
    cr = clean(events)
    assert "self" in cr.flagged_self_dealing
    assert cr.weights[-1] == 0.0


# --- defense 2: reciprocal wash cycle ----------------------------------------

def test_reciprocal_pair_is_flagged_wash():
    # A pays B and B pays A → a 2-cycle. Both directions must be flagged.
    events = [_ev(f"f{i}", f"s{i%3}", f"b{i%3}") for i in range(9)]  # background
    events.append(_ev("ab", "0xB", "0xA"))  # buyer A -> seller B
    events.append(_ev("ba", "0xA", "0xB"))  # buyer B -> seller A (reverse)
    cr = clean(events)
    assert "ab" in cr.flagged_wash and "ba" in cr.flagged_wash


# --- defense 3: sybil community ----------------------------------------------

def test_sybil_community_is_flagged():
    # A self-contained cluster that trades only with itself (no self-deal, no
    # 2-cycle) must be caught by community detection. A hub of buyers all funding
    # one seller is a cohesive, fully-internal community (ratio 1.0).
    events = [_ev(f"w{i}", "0xsyS", f"0xsyB{i}") for i in range(6)]
    cr = clean(events)
    assert len(cr.sybil_communities) >= 1
    assert cr.flagged_sybil  # non-empty
    assert not cr.flagged_wash  # no reciprocal edges exist


def test_sybil_ratio_threshold_semantics():
    # Corrected ratio is I/(I+B), each edge counted once.
    node_comm = {"a": 0, "b": 0, "c": 1, "d": 1}
    # comm 0: internal a-b (w=10), boundary a-c (w=10) → ratio 0.5 < 0.6 → NOT sybil.
    g = nx.Graph()
    g.add_edge("a", "b", weight=10.0)
    g.add_edge("a", "c", weight=10.0)
    assert {"a", "b"} not in _detect_sybil_communities(g, [{"a", "b"}, {"c", "d"}], node_comm)

    # Purely internal triangle → ratio 1.0 → sybil.
    g2 = nx.Graph()
    for u, v in [("x", "y"), ("y", "z"), ("x", "z")]:
        g2.add_edge(u, v, weight=5.0)
    nc2 = {"x": 0, "y": 0, "z": 0}
    assert {"x", "y", "z"} in _detect_sybil_communities(g2, [{"x", "y", "z"}], nc2)
    assert SYBIL_INTERNAL_RATIO == 0.6


# --- defense 4: cluster caps --------------------------------------------------

def test_concentrated_actor_is_capped():
    # A single buyer funnels large volume through many sellers. The resulting
    # cluster (>SYBIL_MAX_SIZE so NOT sybil-excluded) must be scaled to ~cap of
    # total window volume — bounding any one actor's pull. The buyer-side
    # attribution is what keeps the whole hub-and-spoke cluster under the cap.
    cap = 0.05
    events = []
    # Diffuse honest background: 200 distinct small pairs.
    for i in range(200):
        events.append(_ev(f"h{i}", f"0xhs{i}", f"0xhb{i}", price=1.0, size=10.0))
    # One buyer, 60 distinct big sellers (cluster size 61 > 40 → not sybil).
    for i in range(60):
        events.append(_ev(f"x{i}", f"0xxs{i}", "0xHUB", price=1.0, size=500.0))
    cr = clean(events, cluster_cap=cap)
    total = sum(e.notional for e in events)
    hub_weight = sum(w for e, w in zip(cr.events, cr.weights, strict=True) if e.buyer == "0xHUB")
    assert hub_weight <= cap * total * 1.05  # capped near the 5% ceiling
    assert cr.cap_scaled_fraction > 0.0


# --- integration: the default red-team attack --------------------------------

def test_default_attack_exercises_all_defenses_and_is_neutralized():
    atk = AttackConfig(budget_usdc=8000.0, target_multiplier=2.5, trade_notional=40.0)
    res = simulate(SimConfig(seed=11, horizon=HOUR, events_per_service=2500, attack=atk))
    cr = clean(res.events_for("ACR-INF"))
    # All three exclusion filters fire on the same attack.
    assert cr.flagged_self_dealing, "self-dealing filter never fired"
    assert cr.flagged_wash, "wash-cycle filter never fired"
    assert cr.flagged_sybil, "sybil-community filter never fired"
    assert cr.cap_scaled_fraction > 0.0, "cluster caps never bit"
    # And the attack is neutralized: ~no adversarial weight survives.
    total = float(cr.weights.sum())
    adv = sum(w for e, w in zip(cr.events, cr.weights, strict=True) if e.is_adversarial)
    assert adv / total < 1e-3


def test_clean_is_deterministic():
    events = [_ev(f"e{i}", f"0xs{i%5}", f"0xb{i%7}", size=float(10 + i)) for i in range(50)]
    a = clean(events)
    b = clean(events)
    assert np.array_equal(a.weights, b.weights)
    assert a.flagged_sybil == b.flagged_sybil
