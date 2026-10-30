"""acr_core — shared types, index registry, config, and math primitives."""

from __future__ import annotations

from .config import ACRSettings, get_settings, reset_settings
from .indices import (
    ALL_INDEX_IDS,
    INDEX_REGISTRY,
    IndexSpec,
    Service,
    index_for_service,
    spec_for,
)
from .mainnet_guard import (
    MAINNET_CHAIN_ID,
    MainnetGuardError,
    assert_mainnet_ready,
    is_mainnet,
    testnet_surfaces_enabled,
)
from .mathutils import (
    alpha_trim_mask,
    breakdown_point,
    gross_error_sensitivity,
    trimmed_weighted_median,
    volume_time_bars,
    weighted_median,
    weighted_quantile,
)
from .types import (
    ACRPrint,
    ModelClass,
    Quote,
    SellerAttestation,
    Side,
    TapeEvent,
)

__all__ = [
    "MAINNET_CHAIN_ID",
    "MainnetGuardError",
    "assert_mainnet_ready",
    "is_mainnet",
    "testnet_surfaces_enabled",
    "ACRSettings",
    "get_settings",
    "reset_settings",
    "Service",
    "IndexSpec",
    "INDEX_REGISTRY",
    "ALL_INDEX_IDS",
    "spec_for",
    "index_for_service",
    "TapeEvent",
    "SellerAttestation",
    "ACRPrint",
    "Quote",
    "Side",
    "ModelClass",
    "weighted_median",
    "weighted_quantile",
    "alpha_trim_mask",
    "trimmed_weighted_median",
    "breakdown_point",
    "gross_error_sensitivity",
    "volume_time_bars",
]

__version__ = "0.1.0"
