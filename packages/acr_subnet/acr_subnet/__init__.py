"""acr_subnet — ACR as a Bittensor subnet: competing estimators, scored.

The index this repo builds has one administrator, and that administrator is us.
LIBOR's lesson is that the administrator of a reference rate is its attack
surface, and the usual remedy — publish the methodology, submit to oversight —
is paperwork rather than a property of the system. This package is the other
remedy: make the rate the output of competing estimators, each paid in
proportion to how well it predicts data it could not see and how little it moves
under an attack it did not choose. Nobody sets the number; the number is what
survived.

Runs in-process with no chain and no ``bittensor`` dependency, because whether
the incentive is correct is a question about arithmetic, not about networking.
See ``docs/BITTENSOR.md``.
"""

from __future__ import annotations

from .denomination import (
    TAO_USD_DIVERGENCE_WARN,
    TaoDenominated,
    cross_check_tao_usd,
    denominate,
)
from .harness import (
    RunReport,
    Standing,
    real_tape_epochs,
    run,
    synthetic_epochs,
)
from .miner import (
    CopycatMiner,
    FourPillarMiner,
    Miner,
    OverclaimMiner,
    VwapMiner,
)
from .protocol import RatePrint, Score
from .scoring import (
    BP_PER_USD_REF,
    COVERAGE_MAX,
    COVERAGE_MIN,
    ERR_REF_BP,
    W_ACCURACY,
    W_ATTAINABILITY,
    W_RESISTANCE,
    accuracy_score,
    attack_fees_usdc,
    attainability_score,
    consensus_print,
    covered,
    geometric_mean,
    pinball,
    resistance_score,
    score_print,
    weights_from_scores,
    winkler,
)
from .validator import Epoch, EpochResult, Validator

__all__ = [
    # ACR-TAO: a numeraire change, not a fourth index
    "TaoDenominated",
    "denominate",
    "cross_check_tao_usd",
    "TAO_USD_DIVERGENCE_WARN",
    # protocol
    "RatePrint",
    "Score",
    # miners
    "Miner",
    "FourPillarMiner",
    "VwapMiner",
    "CopycatMiner",
    "OverclaimMiner",
    # scoring
    "pinball",
    "winkler",
    "attack_fees_usdc",
    "accuracy_score",
    "resistance_score",
    "attainability_score",
    "geometric_mean",
    "score_print",
    "weights_from_scores",
    "covered",
    "consensus_print",
    "W_ACCURACY",
    "W_RESISTANCE",
    "W_ATTAINABILITY",
    "ERR_REF_BP",
    "BP_PER_USD_REF",
    "COVERAGE_MIN",
    "COVERAGE_MAX",
    # validator
    "Validator",
    "Epoch",
    "EpochResult",
    # harness
    "run",
    "synthetic_epochs",
    "real_tape_epochs",
    "RunReport",
    "Standing",
]

__version__ = "0.1.0"
