"""acr_tape — TapeSource ingestion (Sim + Arc + Receipt + Graph + Chutes)."""

from __future__ import annotations

from .arc_source import ArcSource
from .base import TapeSource
from .chutes_metagraph import (
    CHUTES_NETUID,
    VenueConcentration,
    concentration,
    load_metagraph,
    venue_concentration,
)
from .chutes_source import (
    ChutesSource,
    class_from_gpu_count,
    load_gpu_counts,
    model_class_for,
)
from .graph_client import graph_query, record_chain_check, wrong_chain_reason
from .graph_source import GraphSource
from .receipt_source import ReceiptSource
from .sim_source import SimSource
from .taostats_client import (
    MAX_CALLS_PER_CYCLE,
    RAO,
    ROUTES,
    RegistrationCost,
    SubnetPool,
    TaostatsBudgetExhausted,
    TaostatsClient,
    load_fixture,
    pool_from_fixture,
    registration_cost_from_fixture,
)

__all__ = ["TapeSource", "SimSource", "ArcSource", "ReceiptSource", "GraphSource", "ChutesSource",
           "model_class_for", "class_from_gpu_count", "load_gpu_counts",
           "VenueConcentration", "concentration", "venue_concentration", "load_metagraph",
           "CHUTES_NETUID",
           "TaostatsClient", "TaostatsBudgetExhausted", "SubnetPool",
           "RegistrationCost", "ROUTES", "RAO", "MAX_CALLS_PER_CYCLE",
           "load_fixture", "pool_from_fixture", "registration_cost_from_fixture",
           "graph_query", "record_chain_check", "wrong_chain_reason"]

__version__ = "0.1.0"
