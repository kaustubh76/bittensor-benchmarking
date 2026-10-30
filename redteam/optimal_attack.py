"""Execute the manipulation bound's *own* attack through the real cleaning stack.

The Pillar-3 bound claims that moving the trimmed weighted median by ``rel_move``
needs a certain injected notional split across ``m`` cleaning-evading sybil
clusters. This module builds exactly that injection — clusters larger than
``SYBIL_MAX_SIZE`` (dodging the small-cluster test), one-directional (no
reciprocal edge), distinct buyer/seller (no self-deal), priced inside the trim
band — merges it into a real window, and runs the full ``clean()`` + ``estimate``
so tests can check the bound is *attainable and not loose*.
"""

from __future__ import annotations

import numpy as np
from acr_core import Service, TapeEvent, get_settings, spec_for, trimmed_weighted_median
from acr_estimator.cleaning import SYBIL_MAX_SIZE, clean
from acr_estimator.hedonic import adjust_prices


def build_injection(
    m0: float,
    notional: float,
    direction: str,
    n_clusters: int,
    service: Service,
    t0: float,
    t1: float,
    rel_move: float = 1e-4,
    trade_notional: float = 50.0,
    cluster_size: int = SYBIL_MAX_SIZE + 1,
) -> list[TapeEvent]:
    """Build the injected wash events the bound prices (see module docstring)."""
    p_adv = m0 * (1.0 + 5.0 * rel_move) if direction == "up" else m0 * (1.0 - 5.0 * rel_move)
    n_clusters = max(1, int(n_clusters))
    per_cluster = notional / n_clusters
    # Spread each cluster's mass over *at least* cluster_size events so all
    # (>SYBIL_MAX_SIZE) identities actually appear on the tape — otherwise the
    # cluster is too small and the sybil filter catches it.
    n_per = max(cluster_size, int(round(per_cluster / trade_notional)))
    ev_notional = per_cluster / n_per
    events: list[TapeEvent] = []
    k = 0
    for c in range(n_clusters):
        addrs = [f"0xatk{c}_{i:03d}" for i in range(cluster_size)]  # > SYBIL_MAX_SIZE
        for j in range(n_per):
            t = t0 + (t1 - t0) * ((k % 97) / 97.0)
            events.append(
                TapeEvent(
                    event_id=f"atk-{c}-{k}",
                    ts=t,
                    service=service,
                    seller=addrs[j % cluster_size],
                    buyer=addrs[(j + 1) % cluster_size],  # distinct, one-directional
                    price=p_adv,
                    size=ev_notional / p_adv,
                    is_adversarial=True,
                )
            )
            k += 1
    return events


def run_optimal_attack(
    index_id: str,
    window: list[TapeEvent],
    attestations: list,
    notional: float,
    n_clusters: int,
    direction: str = "up",
    rel_move: float = 1e-4,
    settings=None,
) -> dict:
    """Inject ``notional`` of wash (split into ``n_clusters``) and measure what the
    real cleaning stack lets through and how far the median actually moves."""
    settings = settings or get_settings()
    svc = spec_for(index_id).service
    win = [e for e in window if e.service == svc]
    ts = [e.ts for e in win]
    t0, t1 = (min(ts), max(ts)) if ts else (0.0, 1.0)

    adj0, _ = adjust_prices(win, attestations)
    # seed=, like the pipeline. Without it this harness partitions under seed 0
    # while the estimator it is validating partitions under the configured one,
    # and policy_hash asserts they agree. Silent while both default to 0.
    cr0 = clean(win, cluster_cap=settings.cluster_volume_cap, seed=settings.estimator_seed)
    kept0 = np.where(cr0.weights > 0)[0]
    m0 = trimmed_weighted_median(adj0[kept0], cr0.weights[kept0], settings.trim_alpha)

    injection = build_injection(m0, notional, direction, n_clusters, svc, t0, t1, rel_move=rel_move)
    merged = win + injection
    adj, _ = adjust_prices(merged, attestations)
    cr = clean(merged, cluster_cap=settings.cluster_volume_cap, seed=settings.estimator_seed)
    kept = np.where(cr.weights > 0)[0]
    m1 = trimmed_weighted_median(adj[kept], cr.weights[kept], settings.trim_alpha)

    inj_ids = {e.event_id for e in injection}
    surviving_inj = float(
        sum(w for e, w in zip(cr.events, cr.weights, strict=True) if e.event_id in inj_ids)
    )
    injected_raw = float(sum(e.notional for e in injection))
    return {
        "m0": m0,
        "m1": m1,
        "rel_move_realized": abs(m1 - m0) / m0 if m0 > 0 else 0.0,
        "surviving_injected": surviving_inj,
        "injected_raw": injected_raw,
        "n_injected_events": len(injection),
    }
