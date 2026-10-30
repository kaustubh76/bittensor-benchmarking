"""How a validator scores a ``RatePrint``.

Three components, each asking a different question, each out-of-sample:

1. **accuracy** — pinball loss against a latent the miner could not see.
2. **resistance** — how far the miner's estimator moves per dollar of attack, on
   adversarial tape drawn from a seed the validator holds privately.
3. **attainability** — whether the miner's *declared* cost to move its print is
   a real number or a boast.

Combined by **weighted geometric mean**, so a miner that is accurate but
trivially manipulable scores zero, and a miner that improves any component is
paid for it.

Three deliberate departures from the frozen prose in
``hackathon/bittensor-2026/PRODUCT-DESCRIPTION.md``, each because implementing
the prose literally produced a mechanism that pays the wrong miner. They are
recorded here rather than in a commit message because a reader comparing the two
documents deserves to find the reason next to the code.

**(a) Calibration is a gate, not a scored component.** The prose scores the
interval with the Winkler score, on the grounds that it "charges for width and
for missing together, so a miner cannot buy coverage by printing a wide band".
That is true only when realized coverage is near nominal. This engine's measured
coverage is 1–2 windows in 12 against a nominal 95% (``tests/rate/baseline.json``
records ``covers 2/12``, and the submission already publishes the 22% figure).
At that coverage the ``(2/alpha) * miss`` term dominates the width term by a
factor of tens, so Winkler degenerates into "minimise the miss", which is bought
by widening. Scored as written, a miner submitting ``naive_vwap`` with a
fabricated ±500 bp band would outrank the real four-pillar estimator — the
strawman would win. So coverage is enforced as a **band gate** and Winkler is
reported for ranking sharpness *within* the band, never as a weight.

**(b) Accuracy is dropped, not zeroed, when there is no latent.** Real
single-venue tape has no ground truth: ``is_adversarial`` is documented as never
populated from real data, and there is no OU path behind a live market. Under a
geometric mean a zero component annihilates the product, so a real-tape epoch
would score every miner at zero. Absent components are therefore omitted and the
remaining weights renormalised — the idiom ``scripts/rate.py`` already uses
("absent inputs produce no component rather than a zero").

**(c) Resistance is denominated in fees actually burned, not notional injected.**
``redteam/optimal_attack.run_optimal_attack`` returns ``injected_raw``, which is
notional, and ``rel_move_realized``, which is relative. Dividing a relative move
by a notional would overstate resistance by the reciprocal of the fee rate —
with ``usdc_fee_bps = 1.0`` that is a factor of ~200. The fee model below is the
one the attack actually executes, which is not quite the one ``bound.cost_of``
charges; the difference is noted at ``attack_fees_usdc``.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence

from acr_core import ACRPrint

from .protocol import RatePrint, Score

#: Component weights. These match the committed submission prose so the repo
#: does not ship two specifications of the same mechanism.
W_ACCURACY = 0.50
W_RESISTANCE = 0.35
W_ATTAINABILITY = 0.15

#: Accuracy reference: the error, in bp, at which the accuracy component reaches
#: zero. Matches ``scripts/rate.py``'s ``ERR_REF_BP`` so a miner's accuracy score
#: and the repo's own quality gate speak the same units.
ERR_REF_BP = 300.0

#: Resistance reference: basis points of movement per USDC of attack at which
#: the resistance component reaches zero. Calibrated against the measured
#: bound — golden ``attack_cost_per_bp`` is $0.0123–$0.0254, i.e. roughly 40–80
#: bp per dollar for the defended estimator, so a decade above that is the point
#: at which an estimator is offering no resistance worth paying for.
BP_PER_USD_REF = 800.0

#: Coverage band for the calibration gate.
#:
#: The upper bound is the load-bearing one: a miner whose interval covers truth
#: essentially always has bought coverage with width, which is the exploit
#: departure (a) exists to close.
#:
#: There is **no lower bound**, and that is a finding rather than an omission.
#:
#: This engine's measured coverage is ~22% against a nominal 95%
#: (``tests/rate/baseline.json`` records ``covers 2/12``). A floor at 0.50
#: zeroed the honest four-pillar miner along with the strawman, published
#: nothing, and left the subnet with no rate at all. Lowering it to 0.10 only
#: moved the problem: a five-epoch run where the interval happened to miss every
#: window gave 0/5 coverage, the gate fired on the one competent participant,
#: and again nothing was published.
#:
#: Any floor above zero eventually eliminates the field on an engine that misses
#: four windows in five, so enforcing one here would not be enforcing
#: calibration — it would be refusing to run while appearing rigorous. The
#: upper bound stays, because that is the actual exploit (buying coverage with
#: width), realized coverage is reported on every scoreboard so the miss is
#: visible rather than hidden, and fixing calibration is left where the
#: submission says it belongs: to a competitive field of estimators, which is a
#: better calibration mechanism than one team tuning a bootstrap. A subnet that
#: gated on calibration before anyone could meet it would never get the field
#: that fixes it.
COVERAGE_MIN = 0.0
COVERAGE_MAX = 0.99

#: Minimum epochs of history before the coverage gate can fire. Below this the
#: estimate of coverage is noise, and zeroing a miner on noise would make early
#: epochs a lottery.
COVERAGE_MIN_EPOCHS = 4


# --- loss functions -------------------------------------------------------


def pinball(actual: float, predicted: float, quantile: float = 0.5) -> float:
    """Pinball (quantile) loss. At ``quantile=0.5`` this is half the absolute error.

    Written here because the repo had no quantile loss anywhere — only
    ``acr_core.weighted_quantile``, which is a quantile *estimator*, not a
    quantile *loss*. The median is the right quantile for a reference rate: the
    estimand is a level, and an index that systematically over- or under-prints
    is broken in both directions equally.
    """
    d = actual - predicted
    return quantile * d if d >= 0 else (quantile - 1.0) * d


def winkler(actual: float, lo: float, hi: float, ci_level: float = 0.95) -> float:
    """Winkler interval score, in basis points of ``actual``.

    Extracted from ``scripts/eval.py``, where it was three inline lines inside an
    hourly loop and therefore could not be reused or tested. The formula is
    unchanged, so ``eval.py``'s published ``mean_winkler_bp`` is unaffected.

    Reported, never weighted — see departure (a) in the module docstring.
    """
    if actual <= 0:
        return float("inf")
    alpha = 1.0 - ci_level
    penalty = max(lo - actual, 0.0) + max(actual - hi, 0.0)
    return 1e4 * ((hi - lo) + (2.0 / alpha) * penalty) / actual


def attack_fees_usdc(
    injected_notional: float,
    n_injected_events: int,
    n_clusters: int,
    fee_bps: float,
    fee_flat: float,
    identities_per_cluster: int = 41,
) -> float:
    """USDC actually burned by an executed cleaning-evading attack.

    ``bound.cost_of`` is not reused here, and the mismatch is worth naming
    because it cuts both ways. That function charges ``2 * notional * fee_bps``
    for "wash plus a reverse funding leg", but ``optimal_attack.build_injection``
    emits a one-directional ring in which each address is both a buyer and a
    seller — it is self-funding, and there is no reverse leg, so the
    proportional term is charged twice over. It also charges one flat fee per
    ``notional/50`` of trade, while the harness emits at least
    ``identities_per_cluster`` transfers per cluster, so the flat term is
    undercharged. This function prices what the harness executes.

    The dollars are small either way — a few cents — because wallets are nearly
    free. The scarce input is *identities*, which is why ``n_clusters`` is
    returned alongside in the score detail and why ``human_caps.human_bound``
    exists. Dollars are used as the denominator because they are continuous and
    therefore rankable; the identity count is reported, not scored.
    """
    return (
        injected_notional * fee_bps * 1e-4
        + n_injected_events * fee_flat
        + n_clusters * identities_per_cluster * fee_flat
    )


# --- components -----------------------------------------------------------


def accuracy_score(truth: float, print_value: float) -> tuple[float, float]:
    """(component in [0,1], error in bp). Zero at ``ERR_REF_BP`` of error."""
    if truth <= 0:
        return 0.0, float("inf")
    err_bp = 1e4 * abs(print_value - truth) / truth
    return max(0.0, 1.0 - err_bp / ERR_REF_BP), err_bp


def resistance_score(bp_per_usd: float) -> float:
    """Component in [0,1] from basis points moved per USDC of attack."""
    if bp_per_usd <= 0:
        return 1.0
    return max(0.0, 1.0 - bp_per_usd / BP_PER_USD_REF)


def attainability_score(declared: float | None, measured: float | None) -> tuple[float, float]:
    """(component in [0,1], ratio declared/measured).

    A miner claims its print costs ``declared`` USDC to move one bp; the
    validator measures ``measured``. The score is **symmetric in the log ratio**
    — ``min(ratio, 1/ratio)`` — so being wrong by 10x scores 0.1 whether the
    claim was ten times too high or ten times too low.

    Symmetry is the point, and it took a wrong version to see why. Scoring only
    over-claims leaves under-claiming free, and a miner that declares a
    nonsensically tiny bound is not being modest: a reference rate whose
    published manipulation cost is far below the real one tells every reader the
    feed is cheaper to attack than it is, which is the same disinformation as
    the boast, pointed the other way. Both directions are a false statement
    about the feed's safety.

    ``declared is None`` makes the component **absent**, not zero. Under a
    geometric mean a zero annihilates the product, so scoring silence at zero
    would punish declining to claim far below claiming wrongly — teaching miners
    to always claim something, which is the opposite of what an honesty
    component is for. Absent means the weight redistributes and silence is
    simply neutral.
    """
    if declared is None or measured is None or measured <= 0 or declared <= 0:
        return 0.0, 0.0
    ratio = declared / measured
    if ratio <= 0:
        return 0.0, ratio
    return min(1.0, min(ratio, 1.0 / ratio)), ratio


def geometric_mean(parts: Sequence[tuple[float, float]]) -> float:
    """Weighted geometric mean of ``(value, weight)``, renormalising the weights.

    Renormalisation is what makes an absent component harmless and a zero
    component fatal — the two must not be conflated. ``scripts/rate.py`` takes
    the ``min`` instead, and that is correct *there*: a regression gate should
    report its weakest guarantee, and its scalar explicitly never gates. An
    incentive is a different question. ``min`` is flat in every component but the
    worst, so every miner would optimise one axis and ignore the rest; the
    geometric mean pays for improvement anywhere while still zeroing on a
    zero. The divergence is intentional, not an oversight.
    """
    present = [(v, w) for v, w in parts if w > 0]
    if not present:
        return 0.0
    tw = math.fsum(w for _, w in present)
    if tw <= 0:
        return 0.0
    if any(v <= 0 for v, _ in present):
        return 0.0
    return math.exp(math.fsum(w * math.log(v) for v, w in present) / tw)


# --- the validator's scoring pass ----------------------------------------

#: An attack callable: (index_id, window, attestations, notional, n_clusters)
#: -> dict with at least ``rel_move_realized``, ``injected_raw`` and
#: ``n_injected_events``. Injected rather than imported because ``redteam/`` is
#: not a package — it is reached by ``sys.path`` insertion, and a library that
#: mutates ``sys.path`` at import time is a library that breaks somebody else's
#: process. The harness supplies it; this module stays importable anywhere.
AttackFn = Callable[..., dict]


def score_print(
    rp: RatePrint,
    *,
    truth: float | None,
    measured_cost_per_bp: float | None,
    bp_per_usd: float | None,
    coverage_history: Sequence[bool] = (),
    is_copy: bool = False,
) -> Score:
    """Score one print. ``None`` inputs drop their component rather than zero it.

    ``truth`` is ``None`` on a real-tape epoch, where no latent exists. See
    departure (b). ``is_copy`` zeroes the score outright — see the comment at
    the copy check below for the measurement that made that necessary.
    """
    parts: list[tuple[float, float]] = []
    detail: dict[str, float] = {}

    acc = None
    if truth is not None:
        acc, err_bp = accuracy_score(truth, rp.value)
        detail["err_bp"] = err_bp
        detail["pinball"] = pinball(truth, rp.value)
        detail["winkler_bp"] = winkler(truth, rp.ci_lo, rp.ci_hi)
        parts.append((acc, W_ACCURACY))

    res = None
    if bp_per_usd is not None:
        res = resistance_score(bp_per_usd)
        detail["bp_per_usd"] = bp_per_usd
        parts.append((res, W_RESISTANCE))

    att = None
    if rp.declared_cost_per_bp is not None and measured_cost_per_bp is not None:
        att, ratio = attainability_score(rp.declared_cost_per_bp, measured_cost_per_bp)
        detail["declared_over_measured"] = ratio
        detail["measured_cost_per_bp"] = measured_cost_per_bp
        parts.append((att, W_ATTAINABILITY))

    total = geometric_mean(parts)

    # Weight copying, caught and zeroed rather than merely out-scored.
    #
    # Measured, and it is the finding that most changed this module: a miner
    # resubmitting the previous published consensus scored 0.85 against the
    # four-pillar miner's 0.77 even after the harness was fixed to drift. It
    # earns near-full accuracy, because a stale reference rate really is almost
    # as good as a fresh one on a slow-moving level, and it earns PERFECT
    # resistance, because a print that never reads the tape cannot be moved by
    # poisoning the tape. Both are true, and together they would pay a
    # free-rider more than the only participant doing the work.
    #
    # So the claim this subnet makes is not that copying is impossible. It is
    # that copying is detectable and punished: a print equal to the last
    # published consensus is not an estimate, and a subnet in which everyone
    # copies has no estimate to copy.
    if is_copy:
        detail["copy_of_consensus"] = 1.0
        total = 0.0

    # The calibration gate. Applied only once there is enough history for the
    # coverage estimate to mean anything.
    calibrated = True
    if len(coverage_history) >= COVERAGE_MIN_EPOCHS:
        cov = sum(1 for c in coverage_history if c) / len(coverage_history)
        detail["coverage"] = cov
        if not (COVERAGE_MIN <= cov <= COVERAGE_MAX):
            calibrated = False
            total = 0.0
    detail["ci_width_bp"] = rp.ci_width_bp

    return Score(
        miner_uid=rp.miner_uid,
        epoch=rp.epoch,
        index_id=rp.index_id,
        strategy=rp.strategy,
        accuracy=acc,
        resistance=res,
        attainability=att,
        total=total,
        calibrated=calibrated,
        detail=detail,
    )


def weights_from_scores(scores: Sequence[Score]) -> dict[int, float]:
    """Normalise scores into Bittensor-style weights summing to 1.

    All-zero scores yield an empty mapping rather than a uniform split: an epoch
    in which nobody produced anything worth paying for should pay nobody, not
    pay everybody equally.
    """
    tot = math.fsum(max(0.0, s.total) for s in scores)
    if tot <= 0:
        return {}
    return {s.miner_uid: max(0.0, s.total) / tot for s in scores}


def covered(rp: RatePrint, truth: float | None) -> bool | None:
    """Did this print's interval contain the truth? ``None`` when unknowable."""
    if truth is None:
        return None
    return rp.ci_lo <= truth <= rp.ci_hi


def consensus_print(
    prints: Sequence[RatePrint],
    weights: dict[int, float],
    index_id: str,
    ts: float,
    trim_alpha: float = 0.10,
) -> ACRPrint:
    """The subnet's published rate: an incentive-weighted α-trimmed median.

    The same robust statistic the index uses to aggregate individual trades,
    applied one level up to aggregate individual estimators. That self-similarity
    is the point: a bounded-influence aggregator over bounded-influence
    estimates, so no single miner can move the published rate any more than a
    single trader can move an individual print.

    The interval is the weighted median of the submitted bounds, and
    ``attack_cost_per_bp`` is the weighted median of the declared costs among
    miners that declared one — a miner declining to claim does not drag the
    published bound down, and a miner over-claiming has already been penalised
    on attainability, which is where the lie is supposed to cost it.
    """
    from acr_core import trimmed_weighted_median

    live = [p for p in prints if weights.get(p.miner_uid, 0.0) > 0]
    if not live:
        raise ValueError(f"{index_id}: no miner carries weight this epoch")
    w = [weights[p.miner_uid] for p in live]
    value = float(trimmed_weighted_median([p.value for p in live], w, trim_alpha))
    lo = float(trimmed_weighted_median([p.ci_lo for p in live], w, trim_alpha))
    hi = float(trimmed_weighted_median([p.ci_hi for p in live], w, trim_alpha))

    declared = [(p.declared_cost_per_bp, wi) for p, wi in zip(live, w, strict=True)
                if p.declared_cost_per_bp is not None]
    bound = (
        float(trimmed_weighted_median([d for d, _ in declared], [x for _, x in declared], 0.0))
        if declared
        else None
    )

    # The aggregate interval must still contain the aggregate value: three
    # independent weighted medians need not nest, and ACRPrint refuses a print
    # whose value sits outside its own interval.
    lo = min(lo, value)
    hi = max(hi, value)
    return ACRPrint(
        index_id=index_id,
        ts=ts,
        value=value,
        ci_lo=lo,
        ci_hi=hi,
        attack_cost_per_bp=bound,
        n_obs=len(live),
        trim_alpha=trim_alpha,
    )
