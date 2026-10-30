"""What a miner submits and what a validator returns — the subnet's wire format.

Shaped like a Bittensor synapse (a flat, serialisable request/response pair) so
that wrapping it in ``bt.Synapse`` later is a subclass and a field copy, not a
redesign. Kept as plain pydantic here because the mechanism does not need a
chain to be correct, and a dependency that can fail to resolve has no business
sitting under the part of the system that has to run.
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, Field, model_validator


class RatePrint(BaseModel):
    """One miner's estimate of one index for one epoch.

    The declared bound is the load-bearing field and the reason this is not just
    a number. A miner does not only say what the rate is; it says **what it would
    cost an adversary to move its own estimate by a basis point**. That is a
    falsifiable claim, the validator tries to falsify it
    (``scoring.bound_attainability``), and a miner that inflates it loses more
    than it gains. Without it the subnet would pay for accuracy alone, and the
    cheapest way to be accurate on quiet tape is to have no defences at all.
    """

    index_id: str
    epoch: int
    miner_uid: int
    value: float = Field(gt=0)
    ci_lo: float = Field(gt=0)
    ci_hi: float = Field(gt=0)
    #: USDC an adversary must burn to move THIS miner's print one basis point.
    #: ``None`` means "I decline to claim one", which scores zero on the
    #: attainability component but is not punished further — declining to claim
    #: must always beat lying, or the mechanism teaches miners to lie.
    declared_cost_per_bp: float | None = None
    #: Free-form label for the strategy, carried for the scoreboard only. Never
    #: scored: a validator that rewarded self-description would be scoring
    #: marketing.
    strategy: str = ""

    @model_validator(mode="after")
    def _check_interval(self) -> RatePrint:
        if not (self.ci_lo <= self.value <= self.ci_hi):
            raise ValueError(
                f"interval must contain the value: {self.ci_lo} <= {self.value} <= {self.ci_hi}"
            )
        return self

    @property
    def ci_width_bp(self) -> float:
        return 1e4 * (self.ci_hi - self.ci_lo) / self.value

    def commitment(self, salt: str) -> str:
        """A commit-reveal hash over the scored fields.

        Weight copying is the standing failure mode of estimation subnets. The
        accuracy component already denies a copier anything useful, because the
        ground truth it is scored against does not exist when the print is made.
        This closes the remaining leak: without it, a miner could watch another
        miner's submission inside the same epoch and resubmit it.
        """
        payload = f"{self.index_id}|{self.epoch}|{self.miner_uid}|{self.value!r}|" \
                  f"{self.ci_lo!r}|{self.ci_hi!r}|{self.declared_cost_per_bp!r}|{salt}"
        return "0x" + hashlib.sha256(payload.encode()).hexdigest()


class Score(BaseModel):
    """A validator's verdict on one ``RatePrint``.

    Components are kept separately rather than pre-combined so the scoreboard can
    show *why* a miner ranked where it did, and so an absent component is
    visibly absent instead of silently zero.
    """

    miner_uid: int
    epoch: int
    index_id: str
    strategy: str = ""
    #: Each in [0, 1]; ``None`` means "not scorable in this epoch".
    accuracy: float | None = None
    resistance: float | None = None
    attainability: float | None = None
    #: The weighted geometric mean of whichever components are present.
    total: float = 0.0
    #: False when the miner's interval failed the coverage gate, which zeroes
    #: ``total`` regardless of the components.
    calibrated: bool = True
    detail: dict[str, float] = Field(default_factory=dict)
