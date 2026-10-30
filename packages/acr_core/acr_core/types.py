"""Core data types shared across the ACR pipeline.

These are the wire/domain contracts every zone of the blueprint speaks:

    market exhaust  ->  TapeEvent, SellerAttestation
    estimator core  ->  ACRPrint (value + CI + attack-cost-per-bp)
    instrument      ->  Quote

They are deliberately transport-agnostic pydantic models so the same object
flows from the simulator, through the estimator, into the oracle client, and
out of the FastAPI layer without translation.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field, model_validator

from .indices import Service


class ModelClass(str, Enum):
    """Coarse quality tier of a seller's offering (a hedonic feature)."""

    FRONTIER = "frontier"
    MID = "mid"
    SMALL = "small"
    OPEN = "open"


class TapeEvent(BaseModel):
    """One observed payment authorization — the atomic unit of market exhaust.

    Shaped after an EIP-3009 signed payload: a buyer authorizes a transfer to a
    seller for a quantity of a service at a price. ``batch_id`` records which
    Gateway settlement batch the event netted into — the hook the observation
    model uses to deconvolve the batching operator.
    """

    event_id: str
    #: Economic timestamp, in the index's own clock: ``k·step_s``, i.e. the
    #: Fixing number × 3600. NOT unix time — it is seeded from the newest
    #: on-chain print and only ever moves forward, so it stays monotone across
    #: restarts and tape wraps. The wall-clock anchor is the oracle's
    #: ``postedAt`` (block time), which is what freshness and arrival
    #: selection are measured against.
    ts: float = Field(
        description="Economic timestamp — the index's Fixing clock (k·step_s), not unix seconds."
    )
    service: Service
    seller: str
    buyer: str
    #: Price in USDC per service unit ($/1k tokens, $/GPU-sec, $/MB).
    price: float = Field(gt=0)
    #: Quantity in service units.
    size: float = Field(gt=0)
    #: Settlement batch this event netted into (None = not yet settled).
    batch_id: int | None = None
    #: Settlement timestamp (>= ts); differs from ``ts`` because of batching.
    settled_ts: float | None = None
    model_class: ModelClass | None = None
    #: Ground-truth flag: True if the simulator injected this as manipulation.
    #: Never populated from real chain data; used only for evaluation.
    is_adversarial: bool = False

    @property
    def notional(self) -> float:
        """USDC notional of the authorization."""
        return self.price * self.size


class SellerAttestation(BaseModel):
    """EIP-712 signed service metadata feeding ``AttestationRegistry.sol``.

    The hedonic regression's feature matrix. Sellers attest voluntarily because
    better metadata -> better index placement -> more buyer flow (the flywheel).
    """

    seller: str
    service: Service
    model_class: ModelClass
    #: Latency SLO in milliseconds (p99).
    latency_slo_ms: float = Field(gt=0)
    #: Free-form schema / capability identifier.
    schema_id: str
    #: Hex EIP-712 signature over the attestation (optional off-chain).
    signature: str | None = None
    ts: float = 0.0


class ACRPrint(BaseModel):
    """A single hourly print of an ACR index — the product's output.

    Every print ships with its confidence interval *and* its attack cost: the
    USDC an adversary must burn to move the print by one basis point. "Try to
    move my number — here's the bill."
    """

    index_id: str
    ts: float
    #: The constant-quality rate, in the index's unit.
    value: float = Field(gt=0)
    ci_lo: float = Field(gt=0)
    ci_hi: float = Field(gt=0)
    #: USDC required to move this print by 1bp (Pillar 3). None if not computed.
    attack_cost_per_bp: float | None = None
    #: Number of clean observations backing the print.
    n_obs: int = 0
    #: α used by the trimmed estimator (for reproducibility).
    trim_alpha: float = 0.0
    #: Digest of the cleaning policy this print was made under. What turns "the
    #: keeper decides what is wash" from an objection into a re-derivable claim.
    #: None on a print made before the policy was committed anywhere.
    policy_hash: str | None = None
    #: USDC to move the print 1bp via VERIFIED HUMANS rather than wallets.
    #: None = not computed. Never zero-as-a-placeholder: identities are the
    #: scarce input, so a human-denominated bound is by construction at least
    #: the wallet one, and a zero would understate the cost of moving the index.
    human_adjusted_bound: float | None = None
    #: Distinct verified humans a 1bp move would need. THIS is the computed
    #: figure; `human_adjusted_bound` scales it by a floored cost and is a lower
    #: bound. Quote the count, not the dollars.
    humans_required: int | None = None
    #: The nominal span this print summarizes, in the SAME clock as ``ts``
    #: (Fixing seconds, not unix), so a verifier recomputes over the same window
    #: instead of guessing it. ``window_end == ts`` by construction.
    window_start: float | None = None
    window_end: float | None = None

    @model_validator(mode="after")
    def _check_ci(self) -> ACRPrint:
        if self.ci_lo > self.ci_hi:
            raise ValueError(f"ci_lo ({self.ci_lo}) > ci_hi ({self.ci_hi})")
        if not (self.ci_lo <= self.value <= self.ci_hi):
            raise ValueError(
                f"value {self.value} outside CI [{self.ci_lo}, {self.ci_hi}]"
            )
        return self

    @property
    def ci_width_bp(self) -> float:
        """CI width in basis points of the print value."""
        return 1e4 * (self.ci_hi - self.ci_lo) / self.value


class Side(str, Enum):
    BID = "bid"
    ASK = "ask"


class Quote(BaseModel):
    """A two-sided quote on an ACR future (Pillar 4)."""

    index_id: str
    expiry_ts: float
    bid: float = Field(gt=0)
    ask: float = Field(gt=0)
    bid_size: float = Field(ge=0)
    ask_size: float = Field(ge=0)
    ts: float = 0.0

    @property
    def mid(self) -> float:
        return 0.5 * (self.bid + self.ask)

    @property
    def spread_bp(self) -> float:
        return 1e4 * (self.ask - self.bid) / self.mid
