"""Miner strategies — what competes for emission on this subnet.

Four of them, and only the first is meant to win. The other three exist because
a mechanism that has never been attacked is a mechanism nobody has tested: each
one is a specific way to game the scoring, and the demo's job is to show the
validator catching it.

* ``FourPillarMiner`` — the real ACR estimator. The reference implementation,
  published deliberately as a floor for miners to beat rather than an edge kept
  private. A subnet whose operator holds back the baseline is a subnet whose
  operator is competing with its own miners.
* ``VwapMiner`` — ``naive_vwap`` with a fabricated interval. Not a strawman: on
  quiet tape a VWAP is close to the level, so this miner is genuinely
  competitive on accuracy alone. It is supposed to lose on *resistance*, and if
  it does not, the resistance component is not doing its job.
* ``CopycatMiner`` — resubmits the previous epoch's published consensus. The
  only copy a commit-reveal subnet physically permits, since within an epoch
  nothing is visible. It loses to drift, which is the honest reason copying
  fails here — not an assertion that copying is impossible.
* ``OverclaimMiner`` — the four-pillar estimate with its declared bound
  multiplied. Accurate, resistant, and lying about how expensive it is to move.
  Exists to prove the attainability check has teeth.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from acr_core import ACRPrint, SellerAttestation, TapeEvent
from acr_estimator import estimate_index, naive_vwap

from .protocol import RatePrint


class Miner(ABC):
    """A competing rate estimator.

    ``single_venue`` is threaded through because a miner working real
    single-venue tape must estimate with the cleaning stage's self-referential
    checks disarmed, or its window is destroyed before it can estimate anything.
    See ``acr_estimator.cleaning.clean``.
    """

    #: Shown on the scoreboard. Never scored.
    name = "miner"

    def __init__(self, uid: int) -> None:
        self.uid = uid

    @abstractmethod
    def submit(
        self,
        index_id: str,
        epoch: int,
        events: list[TapeEvent],
        attestations: list[SellerAttestation],
        *,
        ts: float | None = None,
        single_venue: bool = False,
        previous_consensus: ACRPrint | None = None,
    ) -> RatePrint | None:
        """Produce this epoch's print, or ``None`` to abstain.

        Abstaining is a legitimate move — a miner with no opinion should not be
        forced to invent one — and carries no weight rather than a zero score.
        """

    # Shared helper so every miner builds a valid RatePrint the same way.
    def _wrap(
        self,
        index_id: str,
        epoch: int,
        value: float,
        lo: float,
        hi: float,
        declared: float | None,
    ) -> RatePrint:
        lo = min(lo, value)
        hi = max(hi, value)
        return RatePrint(
            index_id=index_id,
            epoch=epoch,
            miner_uid=self.uid,
            value=value,
            ci_lo=lo,
            ci_hi=hi,
            declared_cost_per_bp=declared,
            strategy=self.name,
        )


class FourPillarMiner(Miner):
    """The real thing: deconvolution, cleaning, hedonic adjustment, trimmed median."""

    name = "four-pillar"

    def submit(self, index_id, epoch, events, attestations, *, ts=None,
               single_venue=False, previous_consensus=None):
        if not events:
            return None
        try:
            p, _ = estimate_index(
                index_id, events, attestations, ts=ts, single_venue=single_venue
            )
        except ValueError:
            # A window the estimator refuses — degenerate graph, or no events
            # for this service. Abstaining is the correct answer; fabricating a
            # print to stay on the scoreboard is exactly the behaviour the
            # mechanism should not reward.
            return None
        return self._wrap(index_id, epoch, p.value, p.ci_lo, p.ci_hi, p.attack_cost_per_bp)


class VwapMiner(Miner):
    """Contaminated volume-weighted average, with a confidence interval invented.

    The fabricated band is the interesting part. ``naive_vwap`` returns a bare
    float with no uncertainty, so a miner using it has to make one up, and the
    width it chooses is a free parameter it can tune against the scoring rule.
    That is precisely the exploit the calibration gate exists to close: a wide
    band buys coverage, and under a naively-weighted Winkler score it would buy
    the epoch. ``width_bp`` is settable so a test can demonstrate that widening
    stops paying.
    """

    name = "naive-vwap"

    def __init__(self, uid: int, width_bp: float = 500.0,
                 declared_cost_per_bp: float | None = None) -> None:
        super().__init__(uid)
        self.width_bp = width_bp
        self.declared = declared_cost_per_bp

    def submit(self, index_id, epoch, events, attestations, *, ts=None,
               single_venue=False, previous_consensus=None):
        from acr_core import spec_for

        svc = spec_for(index_id).service
        window = [e for e in events if e.service is svc]
        if not window:
            return None
        v = naive_vwap(window)
        if not (v > 0) or v != v:  # NaN or non-positive
            return None
        half = v * self.width_bp / 2e4
        return self._wrap(index_id, epoch, v, v - half, v + half, self.declared)


class CopycatMiner(Miner):
    """Resubmits the last published consensus print, one epoch late.

    Deliberately lagged. In an in-process harness a copier could read every
    other miner's submission and tie for first, which would "prove" that copying
    works by giving the copier information the chain would never give it. Under
    commit-reveal the only thing visible to a miner is what has already been
    published, so that is the only thing this miner is allowed to see.
    """

    name = "copycat"

    def submit(self, index_id, epoch, events, attestations, *, ts=None,
               single_venue=False, previous_consensus=None):
        if previous_consensus is None:
            return None
        p = previous_consensus
        return self._wrap(index_id, epoch, p.value, p.ci_lo, p.ci_hi, p.attack_cost_per_bp)


class OverclaimMiner(FourPillarMiner):
    """Honest estimate, inflated claim about how expensive it is to move."""

    name = "overclaim"

    def __init__(self, uid: int, multiple: float = 50.0) -> None:
        super().__init__(uid)
        self.multiple = multiple

    def submit(self, index_id, epoch, events, attestations, *, ts=None,
               single_venue=False, previous_consensus=None):
        rp = super().submit(index_id, epoch, events, attestations, ts=ts,
                            single_venue=single_venue,
                            previous_consensus=previous_consensus)
        if rp is None:
            return None
        base = rp.declared_cost_per_bp
        # Nothing to inflate if the estimator declined to publish a bound (which
        # is what happens on a degenerate graph). Claiming one anyway would be a
        # different and more interesting lie, but not this miner's.
        if base is None:
            return rp
        return rp.model_copy(update={"declared_cost_per_bp": base * self.multiple,
                                     "strategy": self.name})
