"""Numerical primitives reused across the estimator.

Kept dependency-light (numpy only) and side-effect free so every stage of the
pipeline — and its tests — can import them without pulling in the whole stack.
"""

from __future__ import annotations

import numpy as np
from numpy.typing import ArrayLike, NDArray


def weighted_median(values: ArrayLike, weights: ArrayLike) -> float:
    """Volume-weighted median.

    The 50% weighted quantile: the value ``m`` such that the total weight below
    ``m`` and above ``m`` are each <= half. Robust central estimator that, unlike
    a weighted mean (VWAP), does not chase a fat tail of wash volume.
    """
    v = np.asarray(values, dtype=float)
    w = np.asarray(weights, dtype=float)
    if v.size == 0:
        raise ValueError("weighted_median of empty input")
    if np.any(w < 0):
        raise ValueError("weights must be non-negative")
    # lexsort by (value, then weight), not a stable argsort.
    #
    # The default quicksort permutes tied values arbitrarily, and while the tied
    # VALUES are equal the weights ride along — so the cumulative sum below adds
    # the same numbers in a different order and rounds differently. A stable sort
    # fixes that only relative to the CALLER's order, which makes this function a
    # function of how the caller happened to arrange its input. Sorting on the
    # weight as the second key makes the sorted arrays a function of the multiset
    # alone, so the answer is the same however the pairs arrived.
    order = np.lexsort((w, v))
    v, w = v[order], w[order]
    cw = np.cumsum(w)
    total = cw[-1]
    if total <= 0:
        raise ValueError("weights sum to zero")
    cutoff = 0.5 * total
    idx = int(np.searchsorted(cw, cutoff))
    # The lower weighted median: the first value whose cumulative weight reaches
    # half. No interpolation and no tolerance.
    #
    # There used to be a tie-break here that averaged the two straddling values
    # when `np.isclose(cw[idx - 1], cutoff)`. It was wrong twice over. It could
    # not fire on an exact tie at all — searchsorted(side="left") returns the
    # LEFTMOST index with cw[i] >= cutoff, so cw[idx-1] == cutoff is
    # unreachable — so it only ever fired on INEXACT matches inside its 1e-5
    # relative band. And inside that band it was discontinuous: with values
    # [1, 2] and weights [w, 1], the result was 1.0 at w=1.0, 1.5 at w=0.9999999
    # and 2.0 at w=0.9999. A 1e-7 nudge to one weight moved the estimator 50%,
    # and 1e-7 is exactly the scale the cap-scaling multiply produces.
    #
    # Averaging is also the wrong answer for this estimator on its own terms: a
    # published rate should be a price somebody actually paid, not the midpoint
    # of two prices nobody paid.
    return float(v[min(idx, v.size - 1)])


def weighted_quantile(values: ArrayLike, weights: ArrayLike, q: float) -> float:
    """Weighted quantile ``q`` in [0, 1] via the cumulative-weight rule."""
    if not 0.0 <= q <= 1.0:
        raise ValueError("q must be in [0, 1]")
    v = np.asarray(values, dtype=float)
    w = np.asarray(weights, dtype=float)
    # Same guards as weighted_median — otherwise `cw /= sum(w)` silently yields nan.
    if v.size == 0:
        raise ValueError("weighted_quantile of empty input")
    if np.any(w < 0):
        raise ValueError("weights must be non-negative")
    total = float(np.sum(w))
    if total <= 0:
        raise ValueError("weights sum to zero")
    # Zero-weight entries make `cw` non-strictly-increasing, and np.interp's
    # behaviour on duplicate xp is unspecified — it picked the LAST of a tied run,
    # so weights [0, 0, 1] over values [1, 2, 3] returned 3.0 for the median.
    # A zero-weight observation is one the cleaning stage excluded; it must not
    # be able to become the trim boundary.
    keep = w > 0
    if not np.any(keep):  # pragma: no cover - guarded by the total check above
        raise ValueError("weights sum to zero")
    v, w = v[keep], w[keep]
    order = np.lexsort((w, v))
    v, w = v[order], w[order]
    cw = (np.cumsum(w) - 0.5 * w) / total
    return float(np.interp(q, cw, v))


def alpha_trim_mask(
    values: ArrayLike, weights: ArrayLike, alpha: float
) -> NDArray[np.bool_]:
    """Boolean mask selecting the central (1 - 2α) weighted mass.

    Drops the α weighted fraction in each tail. Returning a mask (rather than the
    trimmed arrays) lets callers apply the same trim consistently to values,
    weights, and any parallel arrays (seller ids, timestamps).
    """
    if not 0.0 <= alpha < 0.5:
        raise ValueError("alpha must be in [0, 0.5)")
    v = np.asarray(values, dtype=float)
    w = np.asarray(weights, dtype=float)
    lo = weighted_quantile(v, w, alpha)
    hi = weighted_quantile(v, w, 1.0 - alpha)
    return (v >= lo) & (v <= hi)


def trimmed_weighted_median(
    values: ArrayLike, weights: ArrayLike, alpha: float
) -> float:
    """The robust central estimator: α-trim then weighted median."""
    v = np.asarray(values, dtype=float)
    w = np.asarray(weights, dtype=float)
    mask = alpha_trim_mask(v, w, alpha)
    if not np.any(mask):  # pragma: no cover - degenerate
        return weighted_median(v, w)
    return weighted_median(v[mask], w[mask])


def breakdown_point(alpha: float) -> float:
    """Asymptotic breakdown point of the α-trimmed weighted median.

    The largest fraction of arbitrarily-corrupted weight the estimator tolerates
    before it can be driven without bound. Because the core statistic is a
    *median* of the trimmed sample, the breakdown point is ``1/2`` for any
    ``alpha`` in [0, 0.5): trimming does not raise it (the median is already
    maximally robust) but it does bound the estimator's gross-error sensitivity
    — how far a within-band outlier can pull the print. Reported per the
    blueprint as a documented, honest number.

    NOTE: this is the *statistical* breakdown against value corruption. The
    estimator is a *weighted* median, so an adversary who also controls weight
    (wash volume) is separately constrained by the per-cluster volume caps in
    the cleaning stack — see :func:`gross_error_sensitivity`.
    """
    if not 0.0 <= alpha < 0.5:
        raise ValueError("alpha must be in [0, 0.5)")
    return 0.5


def gross_error_sensitivity(alpha: float, cluster_cap: float) -> float:
    """Upper bound on the per-cluster weight share that survives trim + caps.

    A single adversarial cluster contributes at most ``cluster_cap`` of window
    weight (enforced upstream). To flip the trimmed weighted median it must
    supply more than half of the *retained* central mass, ``0.5 * (1 - 2*alpha)``.
    This returns the fraction of that flip-threshold a single capped cluster can
    reach — values < 1.0 mean one capped cluster provably cannot move the print,
    which is the lever Pillar 3 turns into a USDC number.
    """
    if not 0.0 <= alpha < 0.5:
        raise ValueError("alpha must be in [0, 0.5)")
    flip_threshold = 0.5 * (1.0 - 2.0 * alpha)
    if flip_threshold <= 0:  # pragma: no cover - degenerate
        return float("inf")
    return float(cluster_cap / flip_threshold)


def volume_time_bars(
    ts: ArrayLike, notional: ArrayLike, bar_volume: float
) -> NDArray[np.int64]:
    """Assign events to volume-time bars of ~``bar_volume`` USDC each.

    Sampling in volume time (equal traded value per bar) rather than clock time
    normalizes the observation cadence: quiet hours and bursts contribute
    comparably, which is what the state-space observation model expects.

    Returns a per-event bar index (0-based). Events must be time-sorted.
    """
    n = np.asarray(notional, dtype=float)
    if bar_volume <= 0:
        raise ValueError("bar_volume must be positive")
    if np.any(~np.isfinite(n)) or np.any(n < 0):
        raise ValueError("notional must be finite and non-negative")
    cum = np.cumsum(n)
    return (cum // bar_volume).astype(np.int64)
