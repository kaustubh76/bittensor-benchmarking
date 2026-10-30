"""acr_core math primitive tests."""

from __future__ import annotations

import numpy as np
import pytest
from acr_core import (
    ACRPrint,
    breakdown_point,
    trimmed_weighted_median,
    volume_time_bars,
    weighted_median,
    weighted_quantile,
)


def test_weighted_median_matches_unweighted_when_equal_weights():
    v = [1.0, 2.0, 3.0, 100.0, 101.0]
    w = [1.0] * 5
    assert weighted_median(v, w) == 3.0


def test_weighted_median_follows_weight_mass():
    # Almost all weight on the low value -> median sits there.
    assert weighted_median([1.0, 1000.0], [99.0, 1.0]) == 1.0


def test_trimmed_median_rejects_wash_tail():
    # Clean prices ~0.50; a fat tail of wash prints at 5.0 with heavy weight.
    clean = np.full(90, 0.50)
    wash = np.full(10, 5.0)
    v = np.concatenate([clean, wash])
    w = np.concatenate([np.ones(90), np.full(10, 8.0)])  # wash outweighs per-obs
    trimmed = trimmed_weighted_median(v, w, alpha=0.15)
    assert abs(trimmed - 0.50) < 1e-9


def test_breakdown_point_is_one_half_for_median():
    # The trimmed weighted median is a median at core: breakdown 0.5 for any alpha.
    assert breakdown_point(0.0) == 0.5
    assert breakdown_point(0.2) == 0.5


def test_volume_time_bars_partition_by_notional():
    ts = np.arange(6, dtype=float)
    notional = np.full(6, 10.0)  # 60 total
    bars = volume_time_bars(ts, notional, bar_volume=20.0)
    # cumsum 10,20,30,40,50,60 // 20 -> 0,1,1,2,2,3
    assert bars.tolist() == [0, 1, 1, 2, 2, 3]


def test_weighted_quantile_guards_degenerate_input():
    # Empty / negative / zero-sum weights must raise, not silently return nan.
    with pytest.raises(ValueError):
        weighted_quantile([], [], 0.5)
    with pytest.raises(ValueError):
        weighted_quantile([1.0, 2.0], [-1.0, 1.0], 0.5)
    with pytest.raises(ValueError):
        weighted_quantile([1.0, 2.0], [0.0, 0.0], 0.5)


def test_volume_time_bars_rejects_nan_and_negative():
    with pytest.raises(ValueError):
        volume_time_bars(np.arange(3.0), np.array([1.0, np.nan, 1.0]), bar_volume=1.0)
    with pytest.raises(ValueError):
        volume_time_bars(np.arange(2.0), np.array([1.0, -1.0]), bar_volume=1.0)


def test_acrprint_rejects_value_outside_ci():
    ok = ACRPrint(index_id="ACR-INF", ts=0.0, value=0.5, ci_lo=0.4, ci_hi=0.6)
    assert ok.ci_width_bp > 0
    try:
        ACRPrint(index_id="ACR-INF", ts=0.0, value=0.9, ci_lo=0.4, ci_hi=0.6)
    except ValueError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected value-outside-CI to raise")
