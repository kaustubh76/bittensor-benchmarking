"""Pillar 3 — Manipulation Cost Bound: the number that prints next to the rate.

"What does it cost to move you?" The trimmed weighted median only shifts if an
adversary injects enough *surviving* weight near the median to drag the 50%
crossing point. We compute that minimal injected mass ``N*`` numerically on the
actual cleaned distribution, then price it against a **cleaning-evading** sybil
adversary — the only threat model that can actually get mass past the stack:

  * The adversary uses fresh identities disjoint from the honest graph, so the
    honest Louvain communities are unchanged and the injection forms its own
    communities. Each such cluster must stay larger than ``SYBIL_MAX_SIZE`` to
    dodge the small-cluster test, and no reciprocal / self-deal edges (those are
    excluded outright). It is unattested, so the hedonic adjustment is identity.
  * α enters through *placement*: the mass sits inside the trim band so it is not
    trimmed away.
  * The **cluster cap** enters through *count*: each cluster may contribute at
    most ``cap`` of window volume, so injecting ``N*`` needs
    ``m = ceil(N* / (cap·(raw_total+N*)))`` distinct clusters, i.e.
    ``m·(SYBIL_MAX_SIZE+1)`` funded identities. Splitting is free in fees but
    costs identities; that identity count is the cap's real bite.

The fee is then Arc's deterministic schedule, including the reverse funding leg
each fresh identity needs:

    cost_per_bp = 2·N*·(fee_bps·1e-4) + (N*/trade_notional + identities)·fee_flat

On Arc the fee is a fixed constant, so cost_per_bp is a *number*; on a
volatile-gas chain it is a distribution — "only computable on Arc".
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from acr_core import trimmed_weighted_median

from .cleaning import SYBIL_MAX_SIZE


@dataclass
class ManipulationBound:
    #: USDC to move the print by 1bp (min over up/down attacks). Marginal lower
    #: bound — cheapest near a dense median. NOTE: this price is dominated by a
    #: fixed admission fee (one evasion-sized sybil cluster of funded
    #: identities), so ``cost_to_move_1pct`` may be LESS than 100x this figure —
    #: the floor is amortized at size, not escaped. Superlinearity lives in the
    #: NOTIONAL required, and only near a dense median (measured: 1,626x mass
    #: for a 100x move on a dense book, ~102x on a sparse one).
    cost_per_bp: float
    #: Minimal wash notional to move 1bp.
    notional_per_bp: float
    #: Direction the adversary would choose ("up" or "down").
    cheapest_direction: str
    #: Number of wash trades implied at ``trade_notional`` granularity.
    n_trades: int
    #: USDC to move the print by a meaningful 1% (100bp) — the quotable figure.
    cost_to_move_1pct: float = 0.0
    #: Wash notional to move 1%.
    notional_to_move_1pct: float = 0.0
    #: Distinct capped sybil clusters an attacker must stand up (1bp move).
    sybil_clusters_required: int = 0
    #: Fresh funded identities implied by those clusters (1bp move).
    min_identities: int = 0
    #: Share of total window volume the 1bp injection represents.
    surviving_share: float = 0.0


def _min_notional_to_move(
    prices: np.ndarray,
    weights: np.ndarray,
    alpha: float,
    m0: float,
    direction: str,
    rel_move: float = 1e-4,
    max_mult: float = 500.0,
) -> float:
    """Least added weight (at the optimal wash price) to move the median 1bp.

    Places all wash mass just past the target so it is not trimmed, then binary
    searches the injected weight. Monotone: more wash -> median moves further.
    """
    V = float(weights.sum())
    if direction == "up":
        target = m0 * (1.0 + rel_move)
        p_adv = m0 * (1.0 + 5.0 * rel_move)  # 5bp above: central, survives trim

        def reached(w: float) -> bool:
            pr = np.append(prices, p_adv)
            wt = np.append(weights, w)
            return trimmed_weighted_median(pr, wt, alpha) >= target
    else:
        target = m0 * (1.0 - rel_move)
        p_adv = m0 * (1.0 - 5.0 * rel_move)

        def reached(w: float) -> bool:
            pr = np.append(prices, p_adv)
            wt = np.append(weights, w)
            return trimmed_weighted_median(pr, wt, alpha) <= target

    lo, hi = 0.0, max_mult * V
    if not reached(hi):  # pragma: no cover - degenerate distribution
        return float("inf")
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        if reached(mid):
            hi = mid
        else:
            lo = mid
    return hi


def _clusters_needed(
    notional: float, cluster_cap: float | None, raw_total: float | None
) -> int:
    """How many capped sybil clusters must inject ``notional`` of surviving mass.

    Each cluster contributes at most ``cap`` of the (post-injection) window
    volume, so ``m = ceil(N* / (cap·(raw_total + N*)))``. Returns 0 when no cap
    context is supplied (the legacy, cap-unaware cost).
    """
    if cluster_cap is None or raw_total is None or not (0.0 < cluster_cap < 1.0):
        return 0
    if not np.isfinite(notional) or notional <= 0:
        return 0
    denom = cluster_cap * (raw_total + notional)
    if denom <= 0:  # pragma: no cover - degenerate
        return 0
    return int(math.ceil(notional / denom))


def manipulation_bound(
    prices: np.ndarray,
    weights: np.ndarray,
    alpha: float,
    fee_bps: float,
    fee_flat: float,
    trade_notional: float = 50.0,
    cluster_cap: float | None = None,
    raw_total: float | None = None,
    sybil_min_evade_size: int = SYBIL_MAX_SIZE + 1,
) -> ManipulationBound:
    """Least USDC to move the trimmed weighted median, priced against a
    cleaning-evading sybil adversary. Pass ``cluster_cap`` + ``raw_total`` for the
    cap-aware bound (the funding-leg + identity accounting); omit them for the
    legacy marginal cost."""
    prices = np.asarray(prices, dtype=float)
    weights = np.asarray(weights, dtype=float)
    m0 = trimmed_weighted_median(prices, weights, alpha)

    def cost_of(notional: float) -> float:
        if not np.isfinite(notional):
            return float("inf")
        n_trades = max(1.0, notional / max(trade_notional, 1e-9))
        m = _clusters_needed(notional, cluster_cap, raw_total)
        if m > 0:
            identities = m * sybil_min_evade_size
            # Wash + reverse funding leg each pay the proportional fee (2×), plus
            # one funding transfer per fresh identity.
            return 2.0 * notional * (fee_bps * 1e-4) + (n_trades + identities) * fee_flat
        return notional * (fee_bps * 1e-4) + n_trades * fee_flat

    def cheapest_move(rel_move: float) -> tuple[float, float, str]:
        n_up = _min_notional_to_move(prices, weights, alpha, m0, "up", rel_move=rel_move)
        n_down = _min_notional_to_move(prices, weights, alpha, m0, "down", rel_move=rel_move)
        c_up, c_down = cost_of(n_up), cost_of(n_down)
        if c_up <= c_down:
            return n_up, c_up, "up"
        return n_down, c_down, "down"

    notional, cost, direction = cheapest_move(1e-4)  # 1bp
    n1pct, c1pct, _ = cheapest_move(1e-2)  # 1%

    n_trades = int(np.ceil(notional / max(trade_notional, 1e-9))) if np.isfinite(notional) else 0
    m_clusters = _clusters_needed(notional, cluster_cap, raw_total)
    surviving_share = (
        float(notional / (raw_total + notional))
        if (raw_total and np.isfinite(notional))
        else 0.0
    )
    return ManipulationBound(
        cost_per_bp=cost,
        notional_per_bp=notional,
        cheapest_direction=direction,
        n_trades=n_trades,
        cost_to_move_1pct=c1pct,
        notional_to_move_1pct=n1pct,
        sybil_clusters_required=m_clusters,
        min_identities=m_clusters * sybil_min_evade_size,
        surviving_share=surviving_share,
    )
