"""acr_estimator — the four-pillar ACR estimator (THE product)."""

from __future__ import annotations

from .bound import ManipulationBound, manipulation_bound
from .cleaning import CleaningResult, DegenerateGraphError, clean
from .hedonic import HedonicModel, adjust_prices, fit_hedonic
from .indexer import Bar, build_bars
from .observation_model import ObservationModel, SmoothResult
from .pipeline import (
    PrintDiagnostics,
    estimate_all,
    estimate_index,
    naive_vwap,
)
from .robust import RobustEstimate, estimate

__all__ = [
    # indexer
    "Bar",
    "build_bars",
    # pillar 1
    "ObservationModel",
    "SmoothResult",
    # cleaning
    "CleaningResult",
    "DegenerateGraphError",
    "clean",
    # robust
    "RobustEstimate",
    "estimate",
    # pillar 2
    "HedonicModel",
    "fit_hedonic",
    "adjust_prices",
    # pillar 3
    "ManipulationBound",
    "manipulation_bound",
    # pipeline
    "estimate_index",
    "estimate_all",
    "naive_vwap",
    "PrintDiagnostics",
]

__version__ = "0.1.0"
