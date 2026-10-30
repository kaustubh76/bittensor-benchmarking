"""The spend operator, running without being asked.

RFB 4 asks for "obligations settled on time **without a human touching them**",
and until now the honest answer was that the design permits it: the spend path
had exactly one entry point, `scripts/operator_run.py`, reachable only from
`make operator-run`. Nothing in the press, no route and no workflow ever called
it. An agent somebody has to trigger is a tool.

WHY HERE AND NOT IN GITHUB ACTIONS. This repo already decided that, in writing.
`futures-heartbeat.yml` and `futures-lifecycle.yml` both carry
`── RETIRED FROM THE SCHEDULE 2026-08-03 ──`: the job moved into the press
because a scheduled Actions run would need `ACR_CIRCLE_ENTITY_SECRET`, "a
credential that controls every developer-controlled wallet, INCLUDING the one
that signs oracle prints — a strictly larger blast radius than the scoped raw
key it would replace". A cron'd `operator-run --live` needs the policy agent key
in CI: the same trade, refused for the same reason. And independently, GitHub
cron fires only from the default branch, so a workflow on a feature branch is
not a scheduled job at all — `docs/TESTNET_RUNBOOK.md` records that costing days
of a dead book.

THE THREE RULES THIS INHERITS from `keeper.py`, which is the venue's version of
the same problem — a recurring job that moves money on a host that holds the
credentials:

1. it can never take the press down;
2. it does nothing without custody;
3. a cooldown bounds every write.

AND ONE OF ITS OWN: **off by default.** `ACR_OPERATOR_AUTORUN` is `off`, `dry`
or `live`, and `off` is what every checkout gets. A money loop that is armed
unless you remember to disarm it is not a guardrail. `dry` exists because the
first question about any new loop is "does it tick at all on that host?", and
that question should be answerable before the first payment rather than after.

WHAT IT DELIBERATELY WILL NOT DO. It never calls `spend_as_owner`. Clearing an
escalation is the one thing a human is for, it stays behind the token-gated
`POST /ops/actions`, and `traction.py` keys "settled without a human touching
them" on the `actor` field — so a loop that approved its own escalations would
not be more autonomous, it would make the autonomy figure mean nothing.
"""

from __future__ import annotations

import logging
import os
import threading
import time

log = logging.getLogger("acr.operator.keeper")

#: How often the loop considers working. Hourly, matching the press's own
#: refresh: the settlement tape it reads is fed by x402 calls, and a bill is a
#: period's worth of them rather than a single call.
EVERY_S = float(os.environ.get("ACR_OPERATOR_EVERY_S", "3600"))

#: A floor under EVERY_S that no configuration can lower. The loop spends money;
#: a typo in an env var should not be able to turn it into a tight loop against
#: a `PolicyWallet`. The on-chain cap would still hold, but it would hold by
#: being exhausted, which is not the same as being respected.
MIN_EVERY_S = 300.0

#: At most this many obligations per business per tick. The wallet's per-payment
#: limit and period cap are the real guardrails and they are on chain; this is
#: the cheap one that stops a single tick from walking a long tape.
MAX_PER_TICK = int(os.environ.get("ACR_OPERATOR_MAX_PER_TICK", "5"))

#: Only ever one pass at a time. Non-blocking, like `keeper.mirror_once`: if a
#: pass is already running, the next tick has nothing to add — it would read the
#: same tape and reach the same decisions, and two passes racing on one wallet
#: is how a nonce collides.
_lock = threading.Lock()

#: Last outcome, for `/health`. Shape: {"at": epoch, "verdict": str|None}.
_last: dict[str, object] = {}


def mode() -> str:
    """``off`` · ``dry`` · ``live``.

    Anything unrecognised is ``off``. A loop that spends money on a typo is the
    wrong way round: the fail-safe direction here is to do nothing, and
    `/ops` says plainly which mode it is in so "off" is never a silent state.
    """
    raw = (os.environ.get("ACR_OPERATOR_AUTORUN", "") or "").strip().lower()
    if raw in ("live", "dry"):
        return raw
    if raw in ("1", "true", "yes", "on"):
        # Generous about the common spelling, but it means DRY, not live. The
        # one thing a truthy flag must not do is start paying.
        return "dry"
    return "off"


def enabled() -> bool:
    return mode() != "off"


def sleep_s() -> float:
    """How long the clock should wait before considering work again.

    The floor is the point: `EVERY_S` comes from an env var and this loop
    spends money, so a typo must not be able to turn it into a tight loop
    against a `PolicyWallet`. The on-chain cap would still hold — but it would
    hold by being exhausted, which is not the same as being respected.

    Shorter while disarmed, so arming it in-process (a test, an operator
    action) is picked up promptly rather than after a full period of doing
    nothing. Either way the tick itself is a no-op while `mode()` is ``off``.
    """
    every = max(EVERY_S, MIN_EVERY_S)
    return every if enabled() else min(every, 60.0)


def record(verdict: str | None) -> None:
    """Note that the loop ran, whatever it decided.

    The tick and not just the verdict, for `keeper.record`'s reason: a loop that
    correctly finds nothing to do is the healthy majority case, and if only
    verdicts were kept it would be indistinguishable from one that died.
    """
    _last["at"] = time.time()
    _last["verdict"] = verdict


def status() -> dict:
    """What `/health` and `/ops` report. Never raises."""
    at = _last.get("at")
    fired = float(_last.get("fired_at") or 0.0)
    every = max(EVERY_S, MIN_EVERY_S)
    return {
        "mode": mode(),
        "every_s": every,
        "max_per_tick": MAX_PER_TICK,
        "checked_at": at,
        "checked_age_s": round(time.time() - float(at), 1) if at else None,
        "verdict": _last.get("verdict"),
        #: When it last did WORK, as against when it was last checked. They
        #: differ by design — the healthy majority of ticks is a cooldown — and
        #: a reader is owed both, exactly as `keeper._chore_status` gives both.
        "fired_at": fired or None,
        "fired_age_s": round(time.time() - fired, 1) if fired else None,
        "next_due_s": max(0.0, round(every - (time.time() - fired), 1)) if fired else 0.0,
    }


def _businesses():
    """The real businesses this press may spend for, on ITS chain.

    `chain` is a label and labels are what went wrong when the testnet→mainnet
    switch left a mislabelled bundle behind, so this is belt and braces: the
    registry's own `chain` field filters here, and `PolicyClient.wallet_status()`
    tests `eth_getCode` at the moment of payment. A wallet that is not on this
    chain refuses there even if it slipped through here.

    Sandboxes are excluded. Their decisions are hand-written fixtures and an
    autonomous loop writing more of them would merge a demonstration into the
    record of what the agent did for real businesses.
    """
    from acr_core import get_settings

    from . import businesses as reg

    want = "mainnet" if int(getattr(get_settings(), "arc_chain_id", 0) or 0) == 5042 else "testnet"
    return [b for b in reg.real() if b.policy_wallet and b.chain == want]


def tick_once(force: bool = False) -> str | None:
    """One pass. Returns a one-line verdict, or None while on cooldown.

    Reads the press's LIVE settlement tape rather than the archive committed
    beside it. That distinction is the whole of whether this works: the runner
    reads the committed JSONL, which is frozen at the last deploy, so an
    unattended loop reading it would wake every hour and correctly conclude that
    nothing new had been bought — forever. The press already holds the live ring
    the sellers write to.
    """
    global _last
    if not enabled():
        return None
    now = time.time()
    every = max(EVERY_S, MIN_EVERY_S)
    last_fire = float(_last.get("fired_at") or 0.0)
    if not force and last_fire and now - last_fire < every:
        return None
    if not _lock.acquire(blocking=False):
        return None
    try:
        # THE CLOCK IS STAMPED HERE, BEFORE THE WORK, and by `tick_once` rather
        # than by `_pass`. Two reasons, both found by testing it: a `_pass` that
        # raises would otherwise never record its turn, so a failing chain would
        # be retried on every tick instead of once a period; and a cooldown that
        # depends on the work remembering to mark itself is a cooldown that is
        # one refactor from not existing.
        _last["fired_at"] = now
        verdict = _pass(now)
    except Exception as exc:  # noqa: BLE001 — a verdict beats a traceback
        log.warning("operator: the pass failed", exc_info=True)
        verdict = f"failed: {exc}"
    finally:
        _lock.release()
    # RECORDED HERE, not by the caller. A chore whose caller is responsible for
    # saying it ran is a chore that reads as dead the first time a caller
    # forgets — which is not hypothetical: the live press currently reports
    # `checked_at: null` for all three keeper chores, and "nobody recorded it"
    # is one of the candidate explanations.
    record(verdict)
    return verdict


def _policy_for(business):
    """A ``PolicyClient`` for one business, or None.

    The same shape as `app.py:_policy_for` and deliberately not an import of it:
    that one lives beside the HTTP routes and logs against the statement, and a
    loop that spends money should not fail because a web module moved. Returns
    None rather than raising, so one unconfigured business cannot end the pass.
    """
    if not getattr(business, "policy_wallet", ""):
        return None
    try:
        from acr_oracle_client.policy import PolicyClient

        return PolicyClient(wallet_address=business.policy_wallet)
    except Exception as exc:  # noqa: BLE001 - env dependent
        log.warning("operator: no policy client for %s (%s)", business.slug, exc)
        return None


def _pass(now: float) -> str:
    from . import entitlements, liquidity
    from .app import get_facilitator
    from .marketplace import build_receipts
    from .operator import (
        PAY,
        obligations_for,
        obligations_from_entitlements,
        run_obligation,
        settled_refs_from,
        settled_through,
    )
    from .statement import read_decisions

    live = mode() == "live"
    businesses = _businesses()
    if not businesses:
        return "no business on this chain has a wallet to spend from"

    tape = list(build_receipts(get_facilitator()).get("receipts") or ())
    if not tape:
        # AN EMPTY TAPE IS NOT AN EMPTY BILL. The facilitator's ring rehydrates
        # from `ACR_RECEIPT_ARCHIVE_PATH` (set in `render.yaml`); unset — as in
        # a fresh local process — it starts empty, and the first version of this
        # reported "nothing has settled since the last bill paid", which is a
        # claim about the books made without reading them. Billing from a tape
        # we cannot see is the one thing an operator must not do.
        return "the settlement tape is empty, so nothing can be billed from it"
    counts: dict[str, int] = {}
    paid_total = 0.0
    for b in businesses:
        decisions = read_decisions(business=b.slug)
        paid_through = settled_through(decisions)
        # BOTH FEEDERS, which is new. The tape half has always run here; the
        # invoice half existed with sixteen tests and no caller but a CLI flag,
        # so on a schedule the agent could only ever see bills that came through
        # our own paywall. An entitlement is where `due_at`, `invoice_ref` and
        # `early_pay_discount` come from — the three inputs checks 7 and 1 need
        # and never had — so until this, three rungs of a ten-rung ladder could
        # not fire unattended whatever the register held.
        #
        # Paired, never two lists: `metered_quantity` is the seam that carries
        # an independent count into check 3, and check 3 is skipped by a `None`
        # guard that leaves no trace. Keeping the pair together makes it
        # impossible to run a bill past the meter by accident. The tape-fed ones
        # pass `None` and let `run_obligation` meter them from the receipts.
        pairs: list[tuple[object, float | None]] = [
            (ob, None) for ob in obligations_for(b, tape, {}, paid_through)
        ]
        pairs += obligations_from_entitlements(
            b, entitlements.for_business(b.slug), paid_through
        )
        pairs = pairs[:MAX_PER_TICK]
        refs = settled_refs_from(decisions)
        # WITHOUT THIS THE LOOP COULD NOT PAY, AND SAID IT HAD.
        #
        # The first version of this pass called `run_obligation` with no
        # `policy`, so the payment block — `if d.intent == PAY and policy is not
        # None` — was never true. In `live` mode it recorded `intent: "pay"`
        # with `paid_usdc: 0.0` and no `tx`: a decision labelled live that moved
        # nothing. Worse than refusing, because `settled_refs_from` ignores a
        # row with no money on it, so the same bill came back every tick for
        # ever while the traction page counted a payment that never happened.
        # Check 8 could not run either, so the budget was never consulted.
        #
        # Built per business and per pass, never cached, for the reason
        # `app.py:_policy_for` gives: the wallet address comes from the registry,
        # and a cached client keeps spending for a business whose wallet was
        # rotated.
        # READS IN BOTH MODES, SPEND IN ONE. The client is built whatever the
        # mode, because reading what the wallet holds is a read and a `dry` pass
        # that cannot see the cash is a rehearsal of a different performance —
        # the same complaint that made `_pay_rule` stop claiming ", inside
        # budget" after a pass that never read a budget.
        #
        # But only `live` hands it to `run_obligation`. The spend is gated on
        # `dry_run` there, so passing it would also be safe; not passing it
        # means a dry pass has no route to a wallet at all, which is the
        # cheaper thing to be sure of.
        reader = _policy_for(b)
        liq = liquidity.assess(
            reader.balance_usdc() if reader is not None else None,
            [ob for ob, _ in pairs],
            now,
        )
        policy = reader if live else None
        if live and policy is None:
            # Refuse rather than quietly dry-run under the name "live".
            counts["no_wallet"] = counts.get("no_wallet", 0) + 1
            continue
        for ob, metered in pairs:
            try:
                d = run_obligation(
                    ob,
                    receipts=tape,
                    catalog={},
                    policy=policy,
                    metered_quantity=metered,
                    liquidity=liq,
                    settled_refs=refs,
                    since=ob.period_start,
                    dry_run=not live,
                )
            except Exception as exc:  # noqa: BLE001 — one bill must not end the pass
                log.warning("operator: %s failed (%s)", ob.obligation_id, exc, exc_info=True)
                counts["failed"] = counts.get("failed", 0) + 1
                continue
            counts[d.intent] = counts.get(d.intent, 0) + 1
            if d.intent == PAY and d.paid_usdc > 0:
                paid_total += d.paid_usdc
                # Within one pass too: two obligations resolving to one
                # reference is the retry shape, and the second must not be paid
                # because the first just was.
                refs.add(ob.obligation_id)
                if ob.invoice_ref:
                    refs.add(ob.invoice_ref)

    if not counts:
        return f"{len(businesses)} business(es), nothing has settled since the last bill paid"
    tally = " · ".join(f"{k} {v}" for k, v in sorted(counts.items()))
    return f"{mode()}: {tally}" + (f" · {paid_total:.6f} USDC" if paid_total else "")
