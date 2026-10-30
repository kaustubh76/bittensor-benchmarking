"""A validator that scores real miners on a real chain.

The loop is the one the in-process `Validator` already runs, with two steps
swapped for their networked equivalents:

    read the metagraph   (chain, was: a list of Python objects)
    query each miner     (HTTP, was: a method call)
    score each print     -- UNCHANGED: scoring.score_print
    weigh the scores     -- UNCHANGED: scoring.weights_from_scores
    commit the weights   (extrinsic, was: a dict returned to the caller)

The two unchanged lines are the point. If running against a real chain had
required touching `scoring.py`, the mechanism would have been wrong rather than
the plumbing.

WHAT A REAL EPOCH CANNOT DO
---------------------------
On a real chain there is no latent truth to score accuracy against, exactly as
on real tape: `score_print(truth=None)` drops the accuracy component rather than
zeroing it, which is departure (b) in docs/BITTENSOR.md. A validator that
invented a truth here would be scoring miners against its own opinion and
calling it consensus.

So this validator is honest about being partial: it scores resistance and
attainability, and says so. The deterministic in-process harness remains the
only place the accuracy pillar can be measured, because it is the only place a
latent exists.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from typing import Any

from ..protocol import RatePrint, Score
from ..scoring import score_print, weights_from_scores
from .bt_chain import Chain, MinerEndpoint

# A miner that has not answered in this long is not answering. Deliberately
# short: an epoch is a deadline, and a validator that waits indefinitely for one
# slow miner lets that miner set the pace for everybody.
QUERY_TIMEOUT_S = 5.0

# The canary. Every price in the clean window is multiplied by this, and a miner
# that is really estimating must answer differently than it did on the clean
# window. Measured on the real Chutes tape: the four-pillar estimator tracks a
# uniform rescale EXACTLY -- K=1.5 gave ratio 1.500000, K=2.0 gave 2.000000,
# K=0.5 gave 0.500000 -- which stands to reason, since the index is a trimmed
# median of prices and scaling every price scales the median.
CANARY_FACTOR = 1.5

# How far the canary answer must sit from the clean answer to count as a
# response. The test is deliberately "did it move at all", NOT "did it track
# 1.5x": requiring proportionality would punish a legitimate estimator shaped
# differently from ours (a floor, a hedonic term that does not scale), while
# requiring movement punishes only a CONSTANT responder, which is precisely the
# echo attack. Headroom is enormous -- an honest estimator deviates by 0.5 and
# the bar is 0.01, 50x margin -- so this threshold is not delicate.
CANARY_MIN_RESPONSE = 0.01


@dataclass(frozen=True)
class Reply:
    """What one miner said, or why it said nothing.

    `print_` and `error` are mutually exclusive, and both are kept because the
    two silences are different: a miner that refused is not a miner that was
    never asked, and neither is a miner that answered wrongly.
    """

    endpoint: MinerEndpoint
    print_: RatePrint | None = None
    error: str | None = None

    @property
    def answered(self) -> bool:
        return self.print_ is not None


def query(endpoint: MinerEndpoint, *, timeout: float = QUERY_TIMEOUT_S) -> Reply:
    """Ask one miner for its print, over plain HTTP.

    httpx rather than an SDK transport because v11 has none: it carries the
    address and nothing else. Every failure mode collapses to `error` on
    purpose — from the mechanism's point of view a malformed print, a refused
    connection and a timeout are the same event, which is "this miner did not
    produce a print this epoch".
    """
    import httpx  # noqa: PLC0415 -- deferred to keep module import free

    try:
        r = httpx.get(f"{endpoint.url}/print", timeout=timeout)
        r.raise_for_status()
        return Reply(endpoint=endpoint, print_=RatePrint.model_validate(r.json()))
    except Exception as exc:  # noqa: BLE001 -- see docstring: all failures are one event
        return Reply(endpoint=endpoint, error=f"{type(exc).__name__}: {exc}")


def build_canary(window: list[dict], factor: float = CANARY_FACTOR) -> list[dict]:
    """The clean window with every price rescaled, and nothing else changed.

    Only `price` moves. `ts`, `seller`, `buyer` and `size` are left exactly as
    they were so that a miner failing the canary cannot blame a malformed
    window: the graph it sees is the same graph, at a different price level.
    """
    out = []
    for e in window:
        d = dict(e)
        if isinstance(d.get("price"), (int, float)):
            d["price"] = d["price"] * factor
        out.append(d)
    return out


@dataclass(frozen=True)
class Resistance:
    """What a resistance probe learned, including whether it was lied to."""

    bp_per_usd: float | None = None
    measured_cost_per_bp: float | None = None
    echoed: bool = False
    """The miner returned the same value for a window it was not given before.

    Not a measurement of resistance but a finding about the miner: it is not
    estimating the window at all. Scored like `is_copy` -- zeroed, not credited.
    """


def probe_resistance(
    endpoint: MinerEndpoint,
    clean_value: float,
    attacked_window: list[dict],
    fees: float,
    *,
    clean_window: list[dict] | None = None,
    timeout: float = QUERY_TIMEOUT_S,
) -> Resistance:
    """(bp moved per USDC, measured cost per bp) for one miner, over HTTP.

    The networked twin of `validator.measure_miner_resistance`, and the reason
    `/estimate` exists. The in-process validator re-runs the miner's estimator
    because it holds the object; here we can only ask, so we hand over the
    attacked window and compare the answer to that miner's own clean print.

    The arithmetic is deliberately identical to the in-process version, down to
    the two edge cases that version established:

      * a miner that **abstains** under attack (here: HTTP 409) is maximally
        resistant, not unscored. Declining to print on a tape it cannot handle
        is correct behaviour, and an index is not moved by a print that was
        never published.
      * `move_bp == 0` yields a measured cost of `None`, not infinity. An
        unmoved estimate means the attack bought nothing, and dividing by zero
        to express that would put a number where there is no measurement.

    THE CANARY, when `clean_window` is supplied
    -------------------------------------------
    Measuring resistance by asking the miner creates the obvious exploit: answer
    every `/estimate` with the clean print and the attack appears to buy nothing,
    so `move_bp` is 0 and the miner scores *maximally* on the pillar it is
    cheating. So before trusting an unmoved answer, send a second window — the
    clean one with every price rescaled by `CANARY_FACTOR` — and check the miner
    answers it differently.

    What makes this trust-free is that **the only way to pass is to estimate the
    window you were given.** A miner hoping to spot the probe would have to
    recognise a proportional rescale and respond proportionally to it, and a
    miner doing that is estimating, which is the whole thing we wanted. There is
    no cheap tell to key off, unlike `/estimate` versus `/print`.
    """
    import httpx  # noqa: PLC0415

    if clean_value <= 0 or fees <= 0:
        return Resistance()
    try:
        r = httpx.post(
            f"{endpoint.url}/estimate", json=attacked_window, timeout=timeout
        )
        if r.status_code == 409:
            # Abstained under attack -- maximally resistant, and NOT an echo.
            # Declining to print on a tape it cannot handle is correct, and must
            # not be confused with refusing to look at the tape.
            return Resistance(bp_per_usd=0.0)
        r.raise_for_status()
        attacked = RatePrint.model_validate(r.json())
    except Exception:  # noqa: BLE001
        # Unreachable or unparseable under attack is not resistance and is not
        # weakness either: it is an absent measurement, and None says so.
        return Resistance()

    move_bp = 1e4 * abs(attacked.value - clean_value) / clean_value

    # Only an UNMOVED answer needs the canary. A miner whose estimate moved has
    # demonstrably read the window, and spending a second request to confirm
    # what we just saw would double the epoch's traffic for nothing.
    if move_bp == 0 and clean_window is not None:
        try:
            cr = httpx.post(
                f"{endpoint.url}/estimate",
                json=build_canary(clean_window),
                timeout=timeout,
            )
            if cr.status_code == 200:
                canary = RatePrint.model_validate(cr.json())
                ratio = canary.value / clean_value
                if abs(ratio - 1.0) < CANARY_MIN_RESPONSE:
                    # Same answer for a window at a different price level: this
                    # miner is a constant, not an estimator.
                    return Resistance(echoed=True)
        except Exception:  # noqa: BLE001
            # The canary could not be run, so the echo is unproven. Fall through
            # and report the unmoved measurement rather than accusing a miner on
            # the strength of a failed request.
            pass

    return Resistance(
        bp_per_usd=move_bp / fees,
        measured_cost_per_bp=(fees / move_bp) if move_bp > 0 else None,
    )


def score_replies(
    replies: list[Reply],
    *,
    resistance: dict[int, Resistance] | None = None,
    truth: float | None = None,
) -> list[Score]:
    """Score the prints that arrived. Silence is not scored as zero.

    A miner that did not answer is left out of the scoring entirely rather than
    given a zero. Zero is a judgement about an estimate, and there is no
    estimate here to judge. The practical difference shows up in
    `weights_from_scores`, which normalises over what it is given: including
    silent miners as zeros would dilute every honest miner's weight in
    proportion to how many neurons happened to be offline.
    """
    out: list[Score] = []
    per_uid = resistance or {}
    for rep in replies:
        if rep.print_ is None:
            continue
        # Resistance is per-MINER, never per-tape. Measuring it once and giving
        # every miner the same number is what made the component decorative in
        # an earlier version of the in-process validator: a naive VWAP and the
        # four-pillar estimator both scored 0.9175, which cannot be right since
        # moving a VWAP is the easiest thing an attacker can do.
        r = per_uid.get(rep.endpoint.uid) or Resistance()
        out.append(
            score_print(
                rep.print_,
                # `truth` is None on a real chain, where no latent exists --
                # departure (b). But it was previously HARDCODED to None, which
                # threw away a latent even when the epoch had one, and that is
                # not a harmless conservatism. Measured on a synthetic epoch
                # (seed 4242) where the latent is 0.500000: four-pillar errs
                # 2.1% and naive-VWAP 6.1%, yet dropping accuracy inverts the
                # ranking outright -- weights go from {0: 1.0, 1: 0.0} to
                # {0: 0.29, 1: 0.71}, paying the contaminated estimator 71%.
                # So pass the latent wherever one genuinely exists.
                truth=truth,
                measured_cost_per_bp=r.measured_cost_per_bp,
                bp_per_usd=r.bp_per_usd,
                # An echoing miner failed the canary: it returned the same value
                # for a window at a different price level, so its print is not
                # an estimate of the window it was handed. That is exactly what
                # `is_copy` already means and already zeroes, which is why this
                # reuses it instead of adding a parameter -- `scoring.py` stays
                # untouched, and "the engine needed no change to run on a real
                # chain" stays a claim that golden-check and eval-gate back.
                is_copy=r.echoed,
            )
        )
    return out


def _build_attack_family(epoch: Any) -> list[tuple[str, list[Any], float]]:
    """Build the adversary family, importing redteam the way callers must.

    `redteam/` is not a package, and `acr_subnet.validator` deliberately does
    not mutate `sys.path` at import time — a library that does so breaks
    whatever imports it, which is why `scripts/subnet_demo.py` does the
    insertion itself. This adapter is a caller in exactly the same position, so
    it does the same thing here rather than at import time.

    Without this, `build_attack_family` hits `except ImportError: return []`
    and the resistance pillar silently disappears, which presents as a validator
    that scores every miner zero for no visible reason.

    A family rather than one attack, because the networked validator needs it for
    the reason the in-process one does: a single in-band adversary is aimed at the
    trimmed median and at nothing else, so a volume-weighted mean measured against
    it scored as six times more resistant while not being the estimator under
    attack.
    """
    import sys  # noqa: PLC0415
    from pathlib import Path  # noqa: PLC0415

    root = Path(__file__).resolve().parents[4]
    rt = str(root / "redteam")
    if rt not in sys.path:
        sys.path.insert(0, rt)
    try:
        from redteam.optimal_attack import run_optimal_attack  # noqa: PLC0415
    except ImportError:
        return []

    from ..validator import Validator  # noqa: PLC0415

    return Validator(attack_fn=run_optimal_attack).build_attack_family(epoch)


def probe_worst_resistance(
    endpoint: MinerEndpoint,
    clean_value: float,
    family: list[tuple[str, list[dict], float]],
    *,
    clean_window: list[dict] | None = None,
    timeout: float = QUERY_TIMEOUT_S,
) -> Resistance:
    """Worst case across the family, measured over the wire.

    One probe per member and the worst kept, mirroring
    `Validator.measure_worst_resistance`. Worst rather than mean for the same
    reason: averaging would let a good answer to the attack aimed elsewhere pay
    for a bad answer to the attack aimed at this estimator.

    An echo caught by *any* member short-circuits the rest. Echoing is a
    property of the miner, not of the member that caught it, and a miner that
    answered one probe honestly must not launder a detected echo on another.
    """
    worst: Resistance | None = None
    for _name, payload, fees in family:
        r = probe_resistance(
            endpoint,
            clean_value,
            payload,
            fees,
            clean_window=clean_window,
            timeout=timeout,
        )
        if r.echoed:
            return r
        if r.bp_per_usd is None:
            continue
        if worst is None or worst.bp_per_usd is None or r.bp_per_usd > worst.bp_per_usd:
            worst = r
    return worst if worst is not None else Resistance()


def stored_matches(
    stored: dict[int, float], expected: dict[int, float], *, tol: float = 1e-4
) -> bool:
    """Does the vector the chain is USING say the same thing as the one we sent?

    `stored` is one setter's row as `Chain.stored_weights` returns it, i.e.
    `{uid: weight}`. Deliberately not `==`, for two reasons, both measured on
    netuid 602 rather than guessed at.

    **The chain drops zero entries.** A `SetWeights` carrying `{0: 1.0, 1: 0.0}`
    read back as `{0: 1.0}`. uid 1's zero is not lost information -- a uid absent
    from a weight vector is paid nothing, which is exactly what the zero said --
    so an equality check would report a mismatch on a write that was perfect,
    and send the next reader hunting a bug that does not exist.

    **Weights are quantised.** They travel as u16, so a weight that is 1/3 in
    the validator is 21845/65535 when it is read back. `tol` defaults an order
    of magnitude above that granularity (1/65535 ~ 1.5e-5).

    What it is FOR: a receipt saying `success=True` does not mean the chain is
    using your numbers. On a commit-reveal subnet it means a sealed commitment
    landed, and the vector in force may still be a previous epoch's -- on 602
    the stale vector in force was paying the *naive* estimator while the fresh
    commitment naming the better one sat encrypted. This is the check that tells
    those two states apart, and it has to be run against the chain, not the
    receipt.
    """
    for uid in {u for u, w in expected.items() if w > 0} | set(stored):
        if abs(float(stored.get(uid, 0.0)) - float(expected.get(uid, 0.0))) > tol:
            return False
    return True


def run_epoch(
    chain: Chain,
    netuid: int,
    *,
    epoch: Any | None = None,
    wallet: Any | None = None,
    carry_forward: dict[int, float] | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """One validator epoch against a real chain.

    `dry_run` defaults to **True**, and that default is load-bearing: the
    non-dry path signs an extrinsic and spends real TAO. A tool that writes to a
    chain because an argument was forgotten is a tool that will eventually write
    to mainnet because an argument was forgotten.
    """
    carry_forward = {} if carry_forward is None else carry_forward
    head = chain.head()
    # Who are we, and will a write here even be live when it returns? Both are
    # free reads, and both are reported on dry runs too, because the point of a
    # dry run is to learn what the live one would actually do.
    own_uid = (
        chain.uid_of(netuid, wallet.hotkey.ss58_address)
        if wallet is not None
        else None
    )
    commit_reveal = chain.commit_reveal_enabled(netuid)
    reachable, unannounced = chain.endpoints(netuid)
    replies = [query(e) for e in reachable]

    # Resistance: the validator brings its OWN tape, builds the adversary
    # family, and probes every miner with the same injected windows. Its own
    # tape, because a validator that asked the miners for the tape it then
    # attacks them with would be letting them choose the exam. The same family
    # for everyone, because a miner must not be advantaged by drawing a weaker
    # adversary -- which now has to hold member by member.
    resistance: dict[int, Resistance] = {}
    attack_note: str | None = None
    if epoch is not None:
        family = _build_attack_family(epoch)
        if not family:
            # Expected on real single-venue tape: the cleaning-evading adversary
            # needs a funding graph to hide in, and a star has none. Measured on
            # the Chutes tape (subnet 64, single_venue=True): no attack
            # constructible, so resistance is unmeasurable there -- the same
            # root cause that makes `attack_cost_per_bp` None on that tape.
            attack_note = "no attack constructible on this tape (single venue?)"
        else:
            clean_payload = [e.model_dump(mode="json") for e in epoch.events]
            payloads = [
                (
                    name,
                    clean_payload + [e.model_dump(mode="json") for e in window],
                    fees,
                )
                for name, window, fees in family
            ]
            for rep in replies:
                if rep.print_ is None:
                    continue
                resistance[rep.endpoint.uid] = probe_worst_resistance(
                    rep.endpoint,
                    rep.print_.value,
                    payloads,
                    clean_window=clean_payload,
                )

    scorable = epoch is not None and getattr(epoch, "truth", None) is not None
    scores = score_replies(
        replies,
        resistance=resistance,
        # The epoch's own latent if it has one. Real-tape epochs carry None and
        # the accuracy pillar correctly drops; synthetic epochs carry a truth
        # and there is no reason to discard it.
        truth=getattr(epoch, "truth", None) if epoch is not None else None,
    )
    weights = weights_from_scores(scores)

    # Departure (b): a real-tape epoch "does not rank anyone; it PUBLISHES,
    # under weights carried from the last epoch that could score."
    # `validator.py` has always done this, holding `last_weights` and updating
    # the carry only when a scored epoch actually paid somebody. This adapter
    # did not, and scored every epoch fresh -- ranking on epochs it could not
    # score, which is the one thing the rule forbids. Measured cost of that
    # mistake on a synthetic epoch with latent 0.500000: the four-pillar
    # estimator (2.1% error) and the naive VWAP (6.1% error) went from weights
    # {0: 1.0, 1: 0.0} to {0: 0.29, 1: 0.71} -- paying the contaminated
    # estimator 71% -- because without accuracy the ranking falls to a single
    # adversary that happens to be tuned against the better estimator.
    if scorable and weights:
        carry_forward.update(weights)
    elif not scorable:
        carried = {
            r.endpoint.uid: carry_forward[r.endpoint.uid]
            for r in replies
            if r.answered and r.endpoint.uid in carry_forward
        }
        total = sum(carried.values())
        weights = (
            {u: w / total for u, w in carried.items()} if total > 0 else {}
        )

    result: dict[str, Any] = {
        "network": head.network,
        "block": head.block,
        "netuid": netuid,
        "reachable": len(reachable),
        "unannounced": unannounced,
        "answered": sum(1 for r in replies if r.answered),
        "scored": len(scores),
        "resistance_measured": sum(
            1 for v in resistance.values() if v.bp_per_usd is not None
        ),
        "echoes_detected": sum(1 for v in resistance.values() if v.echoed),
        "attack_note": attack_note,
        "own_uid": own_uid,
        "commit_reveal": commit_reveal,
        "scorable": scorable,
        "weights_carried_forward": not scorable,
        "weights": weights,
        "committed": None,
        # All three set only on a live run, and only then claims about anything.
        "weights_pending_reveal": None,
        "weights_in_force": None,
        "weights_in_force_match": None,
    }

    if dry_run:
        return result
    if wallet is None:
        raise ValueError("a live run needs a wallet; pass --wallet/--hotkey")
    if chain.is_mainnet():
        # Not a safety blanket -- a deliberate stop. Nothing in this project has
        # been exercised on mainnet, and the first time it is should be a
        # decision somebody makes on purpose, not a flag that happened to be set.
        raise ValueError(
            "refusing to set weights on mainnet from this adapter: it has never "
            "been exercised there. Run on 'test' first and make the mainnet "
            "call deliberately."
        )
    result["committed"] = str(chain.set_weights(wallet, netuid, weights))
    # On a commit-reveal subnet the receipt attests a sealed commitment, not a
    # weight vector. Saying so in the result is the difference between "the
    # mechanism set these weights" and "the mechanism committed to them", and
    # the first of those was claimed here once on the strength of a receipt.
    result["weights_pending_reveal"] = commit_reveal
    # So read back what consensus is ACTUALLY using. On a commit-reveal subnet
    # this is expected NOT to match straight after a commit -- the vector in
    # force is still the previous one -- and reporting the mismatch is the point:
    # it is the difference between a write that landed and a write that counts.
    if own_uid is not None:
        in_force = chain.stored_weights(netuid).get(own_uid, {})
        result["weights_in_force"] = in_force
        result["weights_in_force_match"] = stored_matches(in_force, weights)
    return result


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--network", default="test", help="test (public testnet) or finney")
    p.add_argument("--netuid", type=int, required=True)
    p.add_argument("--wallet", help="wallet name, for a live run")
    p.add_argument("--hotkey", default="default")
    p.add_argument(
        "--commit",
        action="store_true",
        help="actually sign and submit SetWeights (costs TAO). Off by default.",
    )
    a = p.parse_args(argv)

    chain = Chain(network=a.network)
    # Built whenever a wallet is NAMED, not only when committing: loading a
    # keypair spends nothing, and a dry run that knows which uid it is can
    # report it. The write stays gated on --commit.
    wallet = chain.wallet(a.wallet, a.hotkey) if a.wallet else None
    out = run_epoch(
        chain, a.netuid, wallet=wallet, dry_run=not a.commit
    )
    json.dump(out, sys.stdout, indent=2, sort_keys=True)
    print()
    if out["weights_in_force_match"] is False:
        # Not a failure, and usually not even a surprise: on a commit-reveal
        # subnet the fresh commitment is still encrypted and the chain is paying
        # on the previous vector. Said out loud because the alternative is a
        # reader taking `committed` for "the chain is using our numbers".
        print(
            f"note: the vector in force on netuid {a.netuid} is "
            f"{out['weights_in_force']}, not the {out['weights']} just "
            f"committed"
            + (
                " -- commit-reveal is enabled here, so this commitment is "
                "sealed until its drand round publishes, and will only be "
                "acted on at an epoch step after that."
                if out["commit_reveal"]
                else " -- and commit-reveal is OFF, so this needs explaining."
            ),
            file=sys.stderr,
        )
    if out["reachable"] == 0:
        # Worth saying out loud rather than printing an empty weight map and
        # letting it read as "nobody deserved anything". Measured on testnet
        # netuid 1 on 2026-10-30: 0 of 256 neurons announced an axon at all.
        print(
            f"note: no miner on netuid {a.netuid} announced a reachable axon "
            f"({out['unannounced']} unannounced). There was nothing to query, "
            f"which is not the same as nothing being worth paying.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
