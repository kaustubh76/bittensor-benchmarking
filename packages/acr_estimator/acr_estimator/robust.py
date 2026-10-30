"""Robust estimator — volume-time trimmed weighted median with a per-print CI.

The central estimator. A weighted mean (VWAP) is a broken statistic for this
problem: it chases whichever cluster prints the most volume, which is exactly
the adversary's lever. The α-trimmed weighted median has a documented breakdown
point of 1/2 and — after the cleaning stack's per-cluster caps — a bounded,
computable sensitivity to any single actor. Each print ships with a bootstrap
confidence interval.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from acr_core import breakdown_point, trimmed_weighted_median


@dataclass
class RobustEstimate:
    value: float
    ci_lo: float
    ci_hi: float
    n_obs: int
    alpha: float

    @property
    def breakdown_point(self) -> float:
        return breakdown_point(self.alpha)


def estimate(
    prices: np.ndarray,
    weights: np.ndarray,
    alpha: float,
    n_bootstrap: int = 500,
    ci_level: float = 0.95,
    seed: int = 0,
) -> RobustEstimate:
    """Trimmed weighted median point estimate + weighted-bootstrap CI.

    The CI is a weighted bootstrap: resample observations with probability
    proportional to weight, recompute the trimmed weighted median, and take the
    empirical quantiles. This propagates both sampling noise and weight
    concentration into the interval — a fat-but-legitimate cluster widens the CI
    rather than silently moving the point.
    """
    prices = np.asarray(prices, dtype=float)
    weights = np.asarray(weights, dtype=float)
    n = prices.size
    if n == 0:
        raise ValueError("cannot estimate from zero observations")

    point = trimmed_weighted_median(prices, weights, alpha)
    if n < 3:
        return RobustEstimate(point, point, point, n, alpha)

    rng = np.random.default_rng(seed)
    total = float(weights.sum())
    if total <= 0:
        raise ValueError("weights sum to zero")
    p = weights / total
    boot = np.empty(n_bootstrap)
    for b in range(n_bootstrap):
        idx = rng.choice(n, size=n, replace=True, p=p)
        boot[b] = trimmed_weighted_median(prices[idx], weights[idx], alpha)
    lo_q = (1.0 - ci_level) / 2.0
    hi_q = 1.0 - lo_q
    ci_lo = float(np.quantile(boot, lo_q))
    ci_hi = float(np.quantile(boot, hi_q))
    # Guard: keep the point inside its own interval (bootstrap can be one-sided).
    ci_lo = min(ci_lo, point)
    ci_hi = max(ci_hi, point)
    return RobustEstimate(point, ci_lo, ci_hi, n, alpha)
