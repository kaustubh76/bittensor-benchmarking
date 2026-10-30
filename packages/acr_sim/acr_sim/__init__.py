"""acr_sim — calibrated payment-exhaust simulator (data source #1)."""

from __future__ import annotations

from .adversary import AttackConfig, generate_wash_flow, make_sybil_sellers
from .batching import assign_batches
from .processes import LatentPath, OUParams, simulate_ou
from .sellers import CLASS_PREMIUM, Seller, make_seller_population
from .simulator import SimConfig, SimResult, default_indices, simulate

__all__ = [
    "OUParams",
    "LatentPath",
    "simulate_ou",
    "Seller",
    "CLASS_PREMIUM",
    "make_seller_population",
    "assign_batches",
    "AttackConfig",
    "generate_wash_flow",
    "make_sybil_sellers",
    "SimConfig",
    "SimResult",
    "simulate",
    "default_indices",
]

__version__ = "0.1.0"
