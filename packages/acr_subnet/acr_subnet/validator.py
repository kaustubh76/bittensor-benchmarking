"""The validator: assemble ground truth, score prints, set weights, publish.

Two epoch types, and the difference between them is the whole anti-copy
argument.

**Synthetic epochs** draw a scenario from a seed the validator holds privately.
Because the validator generated the latent process, it knows the true level, and
because the seed is private the miners cannot. There is no answer in the world
to copy — the answer does not exist outside this validator. This is where
accuracy is scored, and it reuses ``gen_evalset.py``'s held-out-seed idiom rather
than inventing one.

**Real-tape epochs** run the miners against live Bittensor flow. There is no
latent here and no honest way to manufacture one, so accuracy is not scored at
all; the epoch measures resistance and attainability, plus the plain fact of
whether a miner's estimator survives contact with real data. A miner that works
only on simulated tape should not be able to hide that.

The resistance measurement is the same adversary the red team already uses
against the index itself — ``redteam/optimal_attack``, the cleaning-evading one,
not the deliberately-catchable simulator adversary. On this subnet that module
stops being a test and becomes validator code.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from acr_core import ACRPrint, SellerAttestation, TapeEvent, get_settings

from .miner import Miner
from .protocol import RatePrint, Score
from .scoring import (
    AttackFn,
    attack_fees_usdc,
    consensus_print,
    covered,
    score_print,
    weights_from_scores,
)


@dataclass
class Epoch:
    """One scoring round.

    ``truth`` is ``None`` for a real-tape epoch — see the module docstring.
    """

    index_id: str
    number: int
    events: list[TapeEvent]
    attestations: list[SellerAttestation]
    truth: float | None = None
    ts: float | None = None
    single_venue: bool = False
    label: str = "synthetic"


@dataclass
class EpochResult:
    epoch: Epoch
    prints: list[RatePrint] = field(default_factory=list)
    scores: list[Score] = field(default_factory=list)
    weights: dict[int, float] = field(default_factory=dict)
    consensus: ACRPrint | None = None
    #: Why no consensus, when there is none — or why the weights are not this
    #: epoch's own.
    note: str = ""
    #: True when at least one component was scorable, i.e. this epoch ranked
    #: miners rather than only publishing a price.
    scored: bool = False


#: The adversary family, as (name, ``rel_move``) pairs. ``build_injection``
#: prices its wash at ``m0 * (1 + 5 * rel_move)``, where ``m0`` is the trimmed
#: weighted median -- so ``rel_move`` is really a dial on WHICH STATISTIC the
#: attack is aimed at, and one value alone aims at one estimator.
#:
#: Measured on a $97,090 window at the then-flat $400 notional (the probe is a
#: fraction of the window now -- see probe_notional), displacement in bp against a
#: common denominator:
#:
#:   price / m0   four-pillar   naive-VWAP
#:         1.00         5.000        2.890   <- in-band only: VWAP looks 2x better
#:         1.05         9.541        4.921
#:         1.50         9.541       23.384   <- VWAP 2.5x worse
#:         3.50         9.541      105.444
#:        11.00         9.541      413.168   <- VWAP 43x worse
#:
#: Two things in that table. The four-pillar estimator **saturates at 9.541 bp**
#: for every out-of-band price across four orders of magnitude, because once the
#: injected price leaves the trim band it carries no weight and extra extremity
#: buys the attacker nothing -- the manipulation bound, visible. A volume-weighted
#: mean trims nothing, so its displacement keeps growing with the price.
#:
#: Hence the single in-band adversary made the VWAP look six times more
#: resistant: it was the only estimator being attacked. The out-of-band member
#: is what a VWAP-targeting attacker would actually do.
#:
#: 1.50x (``rel_move=0.1``) rather than something more extreme, for two reasons.
#: Four-pillar's number is **invariant** to the multiple -- 9.541 bp at 1.05x and
#: at 11x -- so the choice cannot flatter it, and the smallest clearly-out-of-band
#: price therefore UNDERSTATES the VWAP's exposure, which is the safe direction
#: for a gate. And ``BP_PER_USD_REF`` is 800, so a harsher member floors the VWAP
#: at exactly 0.0 and the component stops distinguishing bad from worse.
ATTACK_FAMILY: tuple[tuple[str, float], ...] = (
    ("in-band", 1e-4),
    ("out-of-band", 0.1),
)


class Validator:
    """Scores miners and publishes the consensus print.

    ``attack_fn`` is injected rather than imported: ``redteam/`` has no
    ``__init__.py`` and is reached by ``sys.path`` insertion, which is fine in a
    script and not fine inside a library that other processes import. Left
    ``None``, the resistance component is simply absent and its weight
    redistributes — the validator degrades to scoring accuracy and honesty
    rather than refusing to run.
    """

    def __init__(
        self,
        attack_fn: AttackFn | None = None,
        *,
        attack_notional: float = 400.0,
        attack_clusters: int = 2,
        attack_notional_frac: float = 0.01,
        attack_family: tuple[tuple[str, float], ...] = ATTACK_FAMILY,
        settings=None,
    ) -> None:
        self.attack_fn = attack_fn
        self.attack_notional = attack_notional
        self.attack_clusters = attack_clusters
        self.attack_notional_frac = attack_notional_frac
        self.attack_family = attack_family
        self.settings = settings or get_settings()
        #: miner_uid -> interval-covered-truth history, for the calibration gate.
        self.coverage: dict[int, list[bool]] = {}
        #: The last weights a *scored* epoch produced. Carried forward onto
        #: epochs that cannot score anyone — see ``run_epoch``.
        self.last_weights: dict[int, float] = {}

    # -- resistance ------------------------------------------------------
    def probe_notional(self, epoch: Epoch) -> float:
        """How much wash to inject: a FRACTION of the window, not a fixed sum.

        ``attack_notional`` was a flat $400, and a flat dollar figure cannot mean
        the same thing on two windows of different size. The attainability pillar
        is where that bites: it compares a miner's declared ``attack_cost_per_bp``
        — which the bound derives as the cost of the *cheapest* attack achieving a
        reference move — against the cost per bp the validator actually measures.
        Those are only commensurable if the probe spends roughly what the bound
        says the move costs, and on the windows here $400 was 0.33% of honest
        notional, well short of it.

        Measured across five epochs with the adversary family in place, mean
        attainability by probe size:

            0.33% (the old flat $400)   0.452
            0.50%                       0.460
            1.00%                       0.533   <- best
            2.00%                       0.383
            3.00%                       0.354

        Resistance came out correct 5/5 at *every* one of those, which is the
        point of the family: its discrimination comes from where the attack is
        aimed, not from how much it spends, so this knob is free to serve
        attainability alone. (Before the family, the same sweep put attainability
        at 0.128 here and 0.743 at 1% — most of that gap was the single in-band
        adversary, not the budget.)

        ``attack_notional`` survives as a floor, so a window too thin for 1% to
        buy a measurable move still gets a real probe.
        """
        honest = float(sum(e.notional for e in epoch.events))
        return max(self.attack_notional, self.attack_notional_frac * honest)

    def build_attack(self, epoch: Epoch) -> tuple[list[TapeEvent], float] | None:
        """(the window with wash flow injected, USDC of fees that cost).

        The injection is built once per epoch so every miner faces the *same*
        attack — a miner must not be advantaged by drawing a weaker adversary —
        but it is then applied to each miner separately, because resistance is a
        property of an estimator and not of a tape.

        This is the cleaning-*evading* adversary from ``redteam/optimal_attack``,
        not the deliberately-catchable one in ``acr_sim.adversary``: clusters of
        ``SYBIL_MAX_SIZE + 1`` fresh addresses, one-directional so no reciprocal
        edge forms, priced just inside the trim band. On this subnet that module
        stops being a test and becomes validator code.
        """
        if self.attack_fn is None or not epoch.events:
            return None
        notional = self.probe_notional(epoch)
        try:
            out = self.attack_fn(
                epoch.index_id,
                epoch.events,
                epoch.attestations,
                notional,
                self.attack_clusters,
                settings=self.settings,
            )
        except (ValueError, ZeroDivisionError, KeyError):
            return None

        injected = out.get("injected_events")
        if not injected:
            # `run_optimal_attack` reports the move it measured but does not hand
            # back the events. Rebuild them with the same helper it used, so the
            # attack applied to the miners is the one that was priced.
            try:
                from optimal_attack import build_injection  # type: ignore[import-not-found]
            except ImportError:
                return None
            svc = epoch.events[0].service
            t0 = min(e.ts for e in epoch.events)
            t1 = max(e.ts for e in epoch.events)
            injected = build_injection(
                float(out.get("m0") or 0.0),
                notional,
                "up",
                self.attack_clusters,
                svc,
                t0,
                t1,
            )

        fees = attack_fees_usdc(
            injected_notional=float(out.get("injected_raw") or notional),
            n_injected_events=len(injected),
            n_clusters=self.attack_clusters,
            fee_bps=self.settings.usdc_fee_bps,
            fee_flat=self.settings.usdc_fee_flat,
        )
        if fees <= 0:
            return None
        return list(injected), fees

    def build_attack_family(
        self, epoch: Epoch
    ) -> list[tuple[str, list[TapeEvent], float]]:
        """Every member of the adversary family: (name, injected events, fees).

        One ``attack_fn`` call for the window's trimmed median, then one
        injection per member of ``ATTACK_FAMILY`` priced off it. Built once per
        epoch and applied to every miner, so the existing rule still holds: a
        miner must not be advantaged by drawing a weaker adversary, and that now
        has to be true member by member.

        Returns ``[]`` rather than ``None`` on every failure path, so a caller
        that forgets to check gets "no attack" instead of a crash — the same
        reason ``build_attack`` returns ``None`` and is checked at its one call
        site.
        """
        if self.attack_fn is None or not epoch.events:
            return []
        notional = self.probe_notional(epoch)
        try:
            out = self.attack_fn(
                epoch.index_id,
                epoch.events,
                epoch.attestations,
                notional,
                self.attack_clusters,
                settings=self.settings,
            )
        except (ValueError, ZeroDivisionError, KeyError):
            return []
        try:
            # Bare import, not `redteam.optimal_attack`: see build_attack.
            from optimal_attack import build_injection  # type: ignore[import-not-found]
        except ImportError:
            return []

        m0 = float(out.get("m0") or 0.0)
        if m0 <= 0:
            return []
        svc = epoch.events[0].service
        t0 = min(e.ts for e in epoch.events)
        t1 = max(e.ts for e in epoch.events)

        family: list[tuple[str, list[TapeEvent], float]] = []
        for name, rel_move in self.attack_family:
            injected = build_injection(
                m0,
                notional,
                "up",
                self.attack_clusters,
                svc,
                t0,
                t1,
                rel_move=rel_move,
            )
            if not injected:
                continue
            fees = attack_fees_usdc(
                injected_notional=float(sum(e.notional for e in injected)),
                n_injected_events=len(injected),
                n_clusters=self.attack_clusters,
                fee_bps=self.settings.usdc_fee_bps,
                fee_flat=self.settings.usdc_fee_flat,
            )
            if fees <= 0:
                continue
            family.append((name, list(injected), fees))
        return family

    def measure_worst_resistance(
        self,
        miner: Miner,
        epoch: Epoch,
        clean_value: float,
        family: Sequence[tuple[str, list[TapeEvent], float]],
        previous_consensus: ACRPrint | None,
    ) -> tuple[float | None, float | None, str | None]:
        """This miner's WORST case across the family: (bp/USDC, USDC/bp, member).

        Worst, not mean. Resistance is a claim about what an attacker *can* do,
        and an estimator that is cheap to move by any one route is cheap to
        move; averaging would let a good score against the attack aimed
        elsewhere pay for a bad one against the attack aimed at you. That is
        precisely what a single in-band adversary did for the naive VWAP.

        Abstention keeps its meaning. ``measure_miner_resistance`` reports 0.0
        bp/USDC for a miner that declines to print, i.e. maximally resistant, and
        taking the maximum means declining one attack does not excuse being moved
        by another — while a miner that declines them all still scores as
        resistant, which is the behaviour the in-process harness has always had.
        """
        worst: tuple[float, float | None, str] | None = None
        for name, injected, fees in family:
            bp_per_usd, cost = self.measure_miner_resistance(
                miner,
                epoch,
                clean_value,
                list(epoch.events) + list(injected),
                fees,
                previous_consensus,
            )
            if bp_per_usd is None:
                continue
            if worst is None or bp_per_usd > worst[0]:
                worst = (bp_per_usd, cost, name)
        return worst if worst is not None else (None, None, None)

    def measure_miner_resistance(
        self,
        miner: Miner,
        epoch: Epoch,
        clean_value: float,
        attacked_window: list[TapeEvent],
        fees: float,
        previous_consensus: ACRPrint | None,
    ) -> tuple[float | None, float | None]:
        """(bp moved per USDC, measured cost per bp) for ONE miner's estimator.

        Re-runs the miner on the attacked window and compares its own print to
        its own clean print. That is the quantity the subnet claims to pay for,
        and measuring it once per *tape* instead — as this validator did at
        first — gives every miner the same number and makes the component
        decorative: a naive VWAP and the four-pillar estimator scored an
        identical 0.9175, which cannot be right, since moving a VWAP is the
        easiest thing an attacker can do.

        A miner that abstains under attack is treated as maximally resistant
        rather than unscored: declining to print on a tape it cannot handle is
        the correct behaviour, and the index is not moved by a print that was
        never published.
        """
        if clean_value <= 0 or fees <= 0:
            return None, None
        rp = miner.submit(
            epoch.index_id,
            epoch.number,
            attacked_window,
            epoch.attestations,
            ts=epoch.ts,
            single_venue=epoch.single_venue,
            previous_consensus=previous_consensus,
        )
        if rp is None:
            return 0.0, None
        move_bp = 1e4 * abs(rp.value - clean_value) / clean_value
        bp_per_usd = move_bp / fees
        measured_cost_per_bp = (fees / move_bp) if move_bp > 0 else None
        return bp_per_usd, measured_cost_per_bp

    # -- one epoch -------------------------------------------------------
    def run_epoch(
        self,
        epoch: Epoch,
        miners: Sequence[Miner],
        previous_consensus: ACRPrint | None = None,
    ) -> EpochResult:
        res = EpochResult(epoch=epoch)

        for m in miners:
            rp = m.submit(
                epoch.index_id,
                epoch.number,
                epoch.events,
                epoch.attestations,
                ts=epoch.ts,
                single_venue=epoch.single_venue,
                previous_consensus=previous_consensus,
            )
            if rp is not None:
                res.prints.append(rp)

        if not res.prints:
            res.note = "no miner submitted a print"
            return res

        # The family, built once, applied to every miner's estimator separately.
        # One adversary is not enough: `build_injection` prices its wash off the
        # trimmed median, so a single in-band member aims at the four-pillar
        # estimator and at nothing else, and a naive VWAP measured against it
        # scored as six times more resistant while being merely un-attacked.
        family = self.build_attack_family(epoch)
        by_uid = {m.uid: m for m in miners}

        for rp in res.prints:
            bp_per_usd = measured = None
            if family:
                miner = by_uid.get(rp.miner_uid)
                if miner is not None:
                    bp_per_usd, measured, _worst_member = self.measure_worst_resistance(
                        miner,
                        epoch,
                        rp.value,
                        family,
                        previous_consensus,
                    )

            # A print equal to the last published consensus is a copy, not an
            # estimate. Detected on the value rather than on a commitment hash
            # because a copier is free to re-derive the hash; what it cannot do
            # is submit the previous number while claiming to have computed it.
            is_copy = (
                previous_consensus is not None
                and previous_consensus.value > 0
                and abs(rp.value - previous_consensus.value) / previous_consensus.value < 1e-9
            )

            hist = self.coverage.setdefault(rp.miner_uid, [])
            sc = score_print(
                rp,
                truth=epoch.truth,
                measured_cost_per_bp=measured,
                bp_per_usd=bp_per_usd,
                coverage_history=hist,
                is_copy=is_copy,
            )
            res.scores.append(sc)
            # Record coverage AFTER scoring, so an epoch is never judged against
            # its own outcome.
            c = covered(rp, epoch.truth)
            if c is not None:
                hist.append(c)

        res.weights = weights_from_scores(res.scores)
        res.scored = any(
            s.accuracy is not None or s.resistance is not None or s.attainability is not None
            for s in res.scores
        )

        # Only a scored epoch that actually paid someone updates the carry-
        # forward. Overwriting with an empty dict on an epoch where everyone was
        # gated erases the last good weights, and the next unscored epoch then
        # has nothing to carry — which is how a single bad epoch silenced a
        # twelve-epoch real-tape run.
        if res.scored and res.weights:
            self.last_weights = dict(res.weights)
        elif not res.weights:
            # Nothing was scorable this epoch. On real single-venue tape that is
            # the normal case, not a failure: there is no latent to score
            # accuracy against, and the evading adversary cannot even be run,
            # because measuring resistance would mean measuring a sybil defence
            # that single-venue flow does not have. Scoring it anyway would
            # manufacture a guarantee.
            #
            # So such an epoch does not rank anyone — it publishes. Weights
            # carry forward from the last epoch that could score, which is how
            # Bittensor weights behave anyway (they persist until re-set), and
            # which mirrors how a real reference rate is run: validate the
            # methodology against known truth, then apply it to live data.
            carried = {
                p.miner_uid: self.last_weights[p.miner_uid]
                for p in res.prints
                if p.miner_uid in self.last_weights
            }
            tot = sum(carried.values())
            if tot > 0:
                res.weights = {u: w / tot for u, w in carried.items()}
                res.note = "unscored epoch — weights carried from the last scored epoch"
            else:
                res.note = (
                    "unscored epoch and no prior weights: run scoring epochs first, "
                    "or this epoch can publish nothing it can stand behind"
                )
                return res

        if not res.weights:
            res.note = "every miner scored zero; no weight set and nothing published"
            return res

        try:
            res.consensus = consensus_print(
                res.prints,
                res.weights,
                epoch.index_id,
                ts=epoch.ts if epoch.ts is not None else float(epoch.number),
                trim_alpha=self.settings.trim_alpha,
            )
        except ValueError as exc:
            res.note = str(exc)
        return res
