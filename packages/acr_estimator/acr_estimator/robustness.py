"""Per-print robustness diagnostics — the manipulation-resistance claims, measured.

`docs/methodology.md` §4 claims the trimmed weighted median has a breakdown point
of 1/2 and a per-actor influence bounded by the cluster caps. That first number is
a constant; the load-bearing ones are *per print* and computed here:

  * ``single_cluster_flip_fraction`` — ``gross_error_sensitivity(α, cap)``: the
    share of the flip-threshold one capped cluster can reach. < 1.0 means a single
    capped cluster provably cannot move the print.
  * ``max_cluster_share`` — the largest surviving community's share of cleaned
    weight (concentration actually present this window).
  * ``max_cluster_influence_bp`` — the empirical leave-one-community-out shift of
    the trimmed weighted median, in basis points, maxed over communities: what the
    most influential single actor could remove.
  * ``sybil_clusters_required`` / ``min_identities`` — from the Pillar-3 bound: the
    number of distinct capped clusters (and funded identities) an attacker must
    stand up to move the print by 1bp.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from acr_core import gross_error_sensitivity, trimmed_weighted_median


@dataclass
class RobustnessDiagnostics:
    single_cluster_flip_fraction: float
    max_cluster_share: float
    max_cluster_influence_bp: float
    sybil_clusters_required: int = 0
    min_identities: int = 0


def compute_robustness(
    prices: np.ndarray,
    weights: np.ndarray,
    community_ids: list[int] | np.ndarray,
    alpha: float,
    cluster_cap: float,
    sybil_clusters_required: int = 0,
    min_identities: int = 0,
) -> RobustnessDiagnostics:
    """Compute per-print robustness diagnostics on the cleaned, adjusted flow."""
    prices = np.asarray(prices, dtype=float)
    weights = np.asarray(weights, dtype=float)
    ges = gross_error_sensitivity(alpha, cluster_cap)
    total = float(weights.sum())
    if prices.size == 0 or total <= 0:
        return RobustnessDiagnostics(ges, 0.0, 0.0, sybil_clusters_required, min_identities)

    m0 = trimmed_weighted_median(prices, weights, alpha)
    comm = np.asarray(community_ids)
    max_share = 0.0
    max_infl_bp = 0.0
    for c in np.unique(comm):
        mask = comm == c
        max_share = max(max_share, float(weights[mask].sum() / total))
        keep = ~mask
        if weights[keep].sum() <= 0:
            continue
        m_wo = trimmed_weighted_median(prices[keep], weights[keep], alpha)
        if m0 > 0:
            max_infl_bp = max(max_infl_bp, abs(m_wo - m0) / m0 * 1e4)
    return RobustnessDiagnostics(
        single_cluster_flip_fraction=ges,
        max_cluster_share=max_share,
        max_cluster_influence_bp=max_infl_bp,
        sybil_clusters_required=sybil_clusters_required,
        min_identities=min_identities,
    )
