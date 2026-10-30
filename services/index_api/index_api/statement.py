"""The Spend Statement — what the owner reads, and what it is allowed to claim.

THE ONE SEPARATION THIS FILE EXISTS TO ENFORCE. Two different benchmarks are in
play and only one of them may be denominated in dollars:

  SAVINGS, in USDC, come from the operator's own decision log, where every
  figure is the gap between a price billed and a price another seller was
  actually offering for the same service. Real money, available elsewhere.

  MARKET CONTEXT, in basis points only, comes from ``payer_tca``, which measures
  against the ACR arrival print. ``anchors/GAP.md`` records that print's
  reference level sitting 10x to 1250x off real market prices, so its bp figures
  are meaningful (they are exactly scale-invariant — GAP.md re-anchored ACR-INF
  667x without the error moving) while its **USDC** figures are not.

So ``payer_tca``'s ``overpaid_usdc`` is deliberately NOT forwarded. It is a real
number about a synthetic scale, and on an owner-facing page it would read as
money. The bp figures from the same call are forwarded, labelled as context.

If that distinction is ever collapsed — one "total saved" adding the two — the
statement starts making a claim the repo's own anchor report contradicts, and
that report is public.
"""

from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from pathlib import Path

from .businesses import Business, resolve
from .counterparty import FLAGGED, UNKNOWN
from .liquidity import assess as liquidity_assess
from .operator import ESCALATE, HOLD, LOG_PATH, PAY, REFUSE, REROUTE

log = logging.getLogger("index_api.statement")

#: Most recent decisions carried on the statement. A statement is a summary; the
#: full ledger is the log and the chain.
RECENT_LIMIT = 50

#: How long the market context may take before the statement goes without it.
#:
#: MEASURED: `payer_tca` costs ~7 SECONDS on a cold cache, because it reaches a
#: live subgraph, and then caches. The proxy in front of this endpoint times out
#: at 5s — so the first visitor after any press restart saw "this statement
#: could not be read" for a statement whose decisions were already in hand after
#: 1.3 ms. On a free tier that restarts, that is the common case, not the edge.
#:
#: The context is explicitly optional: it is basis points beside a figure, it
#: already has an `available: false` path with a reason, and `anchors/GAP.md` is
#: why it can never be the headline. Something optional must not be able to fail
#: the thing it decorates.
TCA_BUDGET_S = float(os.environ.get("ACR_STATEMENT_TCA_BUDGET_S", "2"))

#: The committed decision archive, inside the package.
#:
#: Same two-tier shape the receipts use, and for the same reason: `data/` is
#: untracked AND in `.dockerignore`, so a log written only there is present in
#: development and absent in production — a statement that shows a business's
#: decisions locally and nothing at all on the deployed site. The archive ships
#: in the image; the runtime log accumulates beside it.
ARCHIVE_PATH = Path(__file__).with_name("operator_decisions.jsonl")

#: The SANDBOX's decisions, in their own file.
#:
#: They could live in the archive beside the real ones — every row carries its
#: `business` and `read_decisions` filters on it — but then somebody opening
#: `operator_decisions.jsonl` to audit what the agent really did would be
#: reading a mixture. Two files cost one extra read and remove that question
#: entirely.
SANDBOX_ARCHIVE_PATH = Path(__file__).with_name("operator_decisions.sandbox.jsonl")


def _read_one(target: Path) -> list[dict]:
    """One JSONL file's rows, tolerantly.

    A missing file is an empty list, not an error. A malformed line is skipped
    rather than taken as the end of the file: an interrupted write leaves half a
    line, and treating that as EOF would silently drop every decision behind it.
    """
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except Exception as exc:
        log.warning("statement: %s unreadable (%s)", target.name, exc)
        return []
    rows: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def decision_key(row: dict) -> tuple[str, float, str]:
    """What makes two decision rows THE SAME DECISION, for an archiver.

    Receipts dedupe on ``tx_ref``. A decision has no such key: the log is
    append-only and the same obligation legitimately appears twice — once
    escalated, once resolved — so ``intent`` is part of the identity. Keying on
    ``obligation_id`` alone would drop every resolution, which is precisely the
    half of the record that proves a human was involved. ``at`` carries
    sub-second precision from ``time.time()``, so two decisions about one
    obligation inside the same second stay distinct.

    This lives here, rather than in ``scripts/archive_decisions.py`` where it
    was written, because the systems ledger now answers "how many live
    decisions would a redeploy destroy?" — and that answer is only true if it
    is computed with the SAME rule the archiver will apply. Two copies of this
    tuple in two files is a dashboard that reports a backlog the archiver
    considers already saved, or the reverse, with nothing to make either of
    them notice.

    Distinct from :func:`read_decisions`'s own dedupe, which compares whole
    rows: that one is protecting a READER from showing the archive-plus-live
    overlap twice, and for that job the strictest possible key is the safe one.
    """
    return (
        str(row.get("obligation_id") or ""),
        float(row.get("at") or 0.0),
        str(row.get("intent") or ""),
    )


def read_decisions(
    path: str | Path | None = None,
    business: str = "",
    since: float = 0.0,
    limit: int = 1_000,
) -> list[dict]:
    """The operator's decisions, newest last: the archive, then the live log.

    An explicit ``path`` reads only that file — tests and one-off inspections
    want exactly what they name. Otherwise both tiers are read and deduped, so a
    decision that has been archived does not appear twice once the live log is
    rotated into it.

    Deduped on the ROW'S WHOLE CONTENT, not on a composite of a few fields. A
    guessed key collapses rows it should not: an (obligation_id, at, intent) key
    merged two genuinely different decisions in the first version of this, which
    is a worse failure than showing a duplicate. Identical content is the only
    thing that can safely be treated as one decision, and it is exactly what the
    archive-plus-live overlap produces.
    """
    if path is not None:
        rows = _read_one(Path(path))
    else:
        rows = (
            _read_one(ARCHIVE_PATH)
            + _read_one(SANDBOX_ARCHIVE_PATH)
            + _read_one(Path(LOG_PATH))
        )

    out: list[dict] = []
    seen: set[str] = set()
    for row in rows:
        if business and (row.get("business") or "") != business:
            continue
        if float(row.get("at") or 0.0) < since:
            continue
        key = json.dumps(row, sort_keys=True, separators=(",", ":"), default=str)
        if key in seen:
            continue
        seen.add(key)
        out.append(row)
    out.sort(key=lambda r: float(r.get("at") or 0.0))
    return out[-limit:] if limit else out


def summarise(decisions: list[dict]) -> dict:
    """The counts and the one dollar figure that is defensible.

    ``decisions_vs_escalations`` is the pair Canteen's traction questions ask
    for, kept as two numbers rather than a ratio: a ratio hides how much the
    agent actually did, and the interesting failure is a high ratio over three
    decisions.
    """
    by_intent = {k: 0 for k in (PAY, HOLD, REROUTE, ESCALATE, REFUSE)}
    saved = 0.0
    paid = 0.0
    #: What a bill exceeded the agreement it was made under by, summed over the
    #: bills that did not go out because of it.
    #:
    #: THE DIFFERENCE BETWEEN THIS AND `saved_usdc` IS THE WHOLE POINT. A
    #: reroute's saving is measured against somebody else's OFFER and nothing
    #: was bought, which is why `ledger_export` refuses to book it as income —
    #: "writing the avoided overpay as `Income:Savings` would be inventing a
    #: credit". This is money that was asked for, was outside an agreement we
    #: had written down, and did not leave the wallet. A realised avoidance, not
    #: a counterfactual, and the only value figure here that can be defended
    #: against the bank statement.
    held_back = 0.0
    #: What a vendor billed for and our own meter could not find, priced at the
    #: bill's own rate, over the bills that did not go out because of it.
    #:
    #: Prior Art #06, in the unit a budget is written in. `discrepancies` next
    #: to it has always been a COUNT, and a count of findings is not a reason to
    #: act: "three bills disagreed with our meter" and "seventy-six USDC of
    #: those bills was for something that did not happen" are the same fact, and
    #: only one of them gets answered.
    #:
    #: It cannot double-count with `held_back`: the meter is check 3 and returns
    #: before the agreement at 4b, so no single decision can be both.
    overbilled = 0.0
    discrepancies = 0
    unmetered = 0

    for d in decisions:
        intent = str(d.get("intent") or "")
        if intent in by_intent:
            by_intent[intent] += 1
        # Only a reroute recovers money: it names a seller who was offering the
        # same service for less. An escalation might save more and might save
        # nothing, and which it was is not known until a human acts.
        if intent == REROUTE:
            s = d.get("saving_usdc")
            if isinstance(s, (int, float)) and s > 0:
                saved += float(s)
        if intent == PAY:
            p = d.get("paid_usdc")
            if isinstance(p, (int, float)):
                paid += float(p)
        # Only where the money really did not go out. A bill the owner later
        # approved anyway was not held back by anything.
        if intent in (ESCALATE, REFUSE, HOLD) and str(d.get("commitment_verdict") or "") not in (
            "", "within", "no_commitment",
        ):
            over = d.get("over_commitment_usdc")
            if isinstance(over, (int, float)) and over > 0:
                held_back += float(over)
        disc = d.get("discrepancy")
        if isinstance(disc, (int, float)) and disc > 0:
            discrepancies += 1
            # Only where the money stayed. A bill the owner approved anyway went
            # out, whatever the meter said about it.
            if intent in (ESCALATE, REFUSE, HOLD):
                over = d.get("discrepancy_usdc")
                if isinstance(over, (int, float)) and over > 0:
                    overbilled += float(over)
        if "unmetered" in str(d.get("rule") or ""):
            unmetered += 1

    acted = by_intent[PAY] + by_intent[HOLD] + by_intent[REROUTE] + by_intent[REFUSE]
    return {
        "decisions": len(decisions),
        "by_intent": by_intent,
        # The agent's own authority vs a human's, side by side.
        "decided": acted,
        "escalated": by_intent[ESCALATE],
        "paid_usdc": paid,
        # Recoverable, because another seller was offering it.
        "saved_usdc": saved,
        #: Realised, unlike `saved_usdc`. See the accumulator for why the two
        #: must never be added together.
        "held_back_usdc": round(held_back, 6),
        #: Realised, like `held_back_usdc` and for the same reason: the money
        #: did not leave. Never add the two to `saved_usdc`.
        "overbilled_usdc": round(overbilled, 6),
        "consumption_discrepancies": discrepancies,
        "unmetered": unmetered,
        **autonomy(decisions),
        **agreement(decisions),
        **screening(decisions),
        **compliance(decisions),
    }


def compliance(decisions: list[dict]) -> dict:
    """RFB 5's counts, named for what this product actually does.

    THE BRIEF SAYS "addresses monitored". WE DO NOT MONITOR. The screen runs
    once, inside the decision, at the moment the money would move; nothing
    re-screens on a schedule, and `counterparty.py` records that as an
    aspiration rather than a feature. So the figure is `addresses_screened`, and
    calling it anything else would be the overclaim this whole product is built
    to refuse — the same refusal as `unknown` never reading as `clear`.

    An ALERT is a flagged counterparty. It is RESOLVED when that obligation
    reached a terminal decision rather than sitting in the queue; `escalate` is
    the alert still open. Two counts, never a rate: a resolution figure over one
    alert is not a percentage of anything.

    Counted over DISTINCT addresses, because one vendor billed nine times is one
    counterparty, and counting the bills would make a quiet month look like
    diligence.
    """
    terminal = (PAY, HOLD, REROUTE, REFUSE)
    screened: set[str] = set()
    alerted: set[str] = set()
    resolved: set[str] = set()

    for d in decisions:
        vendor = str(d.get("vendor") or "").lower()
        risk = str(d.get("screen_risk") or "")
        if not vendor or not risk:
            # No verdict on this row means no screen reached it — checks 1 to 3
            # return before the counterparty is consulted. Not screened, and not
            # counted as if it were.
            continue
        screened.add(vendor)
        if risk == FLAGGED:
            alerted.add(vendor)
            if str(d.get("intent") or "") in terminal:
                resolved.add(vendor)

    return {
        "addresses_screened": len(screened),
        "alerts_raised": len(alerted),
        "alerts_resolved": len(resolved),
    }


def autonomy(decisions: list[dict]) -> dict:
    """"Obligations settled on time without a human touching them" — RFB 4.

    Two facts, kept as counts rather than one percentage, because the
    interesting failure is a perfect rate over three decisions.

    UNTIL `actor` EXISTED THIS WAS NOT ANSWERABLE. An owner's approval was
    written as a plain ``pay``, identical in every field to one the agent
    reached alone, and landed in `decided` and `moved_usdc` beside it. The
    autonomy figure a log like that produces is 100%, always, which is the most
    flattering number on the page and means nothing.

    ON TIME IS REPORTED AGAINST ITS OWN DENOMINATOR. An obligation with no due
    date cannot be late, and counting it as punctual would turn "we do not know
    when this was due" into evidence of promptness. The runner does not set due
    dates yet, so `due_known` is honestly zero today and the figure starts
    working the day obligations carry one.
    """
    terminal = (PAY, HOLD, REROUTE, REFUSE)
    by_agent = by_owner = 0
    on_time = due_known = 0

    for d in decisions:
        if str(d.get("intent") or "") not in terminal:
            continue
        if str(d.get("actor") or "agent") == "owner":
            by_owner += 1
        else:
            by_agent += 1
        due = d.get("due_at")
        at = d.get("at")
        if isinstance(due, (int, float)) and isinstance(at, (int, float)):
            due_known += 1
            if float(at) <= float(due):
                on_time += 1

    return {
        "settled_by_agent": by_agent,
        "settled_by_owner": by_owner,
        "settled_on_time": on_time,
        "settled_with_a_due_date": due_known,
    }


def agreement(decisions: list[dict]) -> dict:
    """"Decisions made vs escalated, and HOW OFTEN THE HUMAN AGREED" — RFB 4.

    Agreement needs two opinions. The agent now records what it would have done
    when it escalated (`recommended_intent`), so an owner's resolution can be
    compared against it instead of against nothing.

    THE DENOMINATOR IS RESOLUTIONS, NOT ESCALATIONS. A queue nobody has answered
    is not unanimous agreement; it is an empty sample, and dividing by the
    escalation count would report a confident 0% while the owner is on holiday.

    AND NOT EVERY ESCALATION CARRIES A RECOMMENDATION. "No budget on chain"
    means the agent had no authority to form one, so those are excluded rather
    than counted as a disagreement the agent never voiced.
    """
    agreed = resolved = 0
    for d in decisions:
        if str(d.get("actor") or "") != "owner":
            continue
        want = str(d.get("recommended_intent") or "")
        if not want:
            continue
        resolved += 1
        if str(d.get("intent") or "") == want:
            agreed += 1
    return {"owner_resolutions": resolved, "owner_agreed": agreed}


def screening(decisions: list[dict]) -> dict:
    """"Risk events caught BEFORE the transaction" — RFB 5.

    Sound by construction rather than by assertion: `decide` runs the
    counterparty check at step 4 and only ever assigns ``pay`` after step 8,
    returning early in between, so a flagged screen cannot coexist with a
    payment. "Before" is the ordering, not a hope.

    `unknown` IS NOT COUNTED AS CAUGHT. It means no screen could answer, and a
    product that files "we could not check" under "we caught something" has
    inverted the one claim this check makes. It is reported on its own line, as
    the thing it is: an unscreened payment somebody should be able to find.
    """
    caught = unscreened = 0
    for d in decisions:
        risk = str(d.get("screen_risk") or "")
        intent = str(d.get("intent") or "")
        if risk == FLAGGED and intent != PAY:
            caught += 1
        elif risk == UNKNOWN and intent == PAY:
            unscreened += 1
    return {"risk_events_caught": caught, "paid_unscreened": unscreened}


def _market_card(tca_fn, treasury: str, days: int) -> dict:
    """The market context, or a stated reason there isn't one, within a budget.

    Run on a worker thread because `payer_tca` is synchronous and talks to a
    subgraph. A thread that outlives the budget is abandoned rather than waited
    on: its result would arrive after the response, and the caching inside
    `tca.py` means the NEXT reader gets it for free. Nothing is cancelled
    because nothing needs to be — it is a read.

    An exception is `UNAVAILABLE` rather than a raise: a statement whose
    decisions are all in hand must not 500 because an optional decoration
    failed.
    """
    # NOT a `with` block. `ThreadPoolExecutor.__exit__` calls
    # `shutdown(wait=True)`, which blocks on the very thread the timeout just
    # abandoned — measured: the budget fired at 2s and the function still took
    # 6.3s, so the timeout looked right and fixed nothing.
    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="statement-tca")
    try:
        fut = pool.submit(tca_fn, treasury, days=days)
        return fut.result(timeout=TCA_BUDGET_S) or {}
    except FutureTimeout:
        log.info("statement: market context exceeded %.1fs, going without", TCA_BUDGET_S)
        return {"available": False, "reason": "SLOW"}
    except Exception as exc:  # noqa: BLE001
        log.warning("statement: market context unavailable (%s)", exc)
        return {"available": False, "reason": "UNAVAILABLE"}
    finally:
        # Let the worker finish on its own time: its answer warms `tca.py`'s
        # cache, so the next reader gets the context for free. Nothing is
        # cancelled because nothing needs to be — it is a read.
        pool.shutdown(wait=False)


def pending_escalations(decisions: list[dict]) -> list[dict]:
    """The escalations still waiting on a person, newest first.

    An escalation is NOT resolved by being old; it is resolved by a later
    decision about the same obligation. The log is append-only, so approving one
    appends a payment rather than editing the escalation — and a queue that
    filtered on `intent == "escalate"` alone would keep showing an obligation the
    owner paid an hour ago, which is the one thing an action queue must never do.

    Resolution is per obligation id. A second escalation of the same obligation
    (a retry that stopped again) does not resolve the first: both are the same
    id, and only a non-escalating outcome clears it.
    """
    resolved = {
        d.get("obligation_id")
        for d in decisions
        if d.get("intent") != ESCALATE and d.get("obligation_id")
    }
    seen: set[str] = set()
    out: list[dict] = []
    for d in reversed(decisions):
        if d.get("intent") != ESCALATE:
            continue
        oid = d.get("obligation_id") or ""
        if oid in resolved or oid in seen:
            continue
        # One row per obligation: re-escalating the same bill is the same item
        # of work, not two.
        seen.add(oid)
        out.append(d)
    return out


def _liquidity(business: Business, policy_for, escalations: list[dict], t: float) -> dict:
    """What the wallet holds, against what is dated and waiting on the owner.

    A BUDGET IS NOT CASH, and the statement showed only the budget. `cap -
    spent` comes from the contract's counters and reads healthy on a wallet
    holding nothing — so an owner could read a full remaining allowance beside
    an escalation queue the wallet could not actually settle. RFB 4 asks the
    agent to decide "whether there is enough liquidity to cover what is due";
    this is the same question put to the person.

    WHAT IS SUMMED, exactly: the escalated bills on this statement that carry a
    due date, inside the liquidity horizon. Not every obligation in the world —
    the statement is built from the decision log, so these are the bills the
    agent has already seen and handed over. Escalations without a due date are
    counted apart for the same reason the ladder does it: "we do not know when
    this is due" is not "it is due later".

    Degrades the way `_budgets` below does, and for the reason it gives: a
    statement whose decisions are all in hand must not fail because one
    optional chain read did. `held_usdc` is None when unreadable, never 0.0,
    and `reason` says which.
    """
    picture = liquidity_assess(None, escalations, t)
    out = picture.as_dict()
    out["reason"] = ""
    if policy_for is None or not business.policy_wallet:
        # Not an error: a business with no wallet is being measured, not spent
        # for. The caller's sibling `spends` key already says so, so this does
        # not repeat it — it just declines to invent a balance.
        out["reason"] = "no wallet, so there is no balance to read"
        return out
    client = policy_for(business)
    if client is None or not hasattr(client, "balance_usdc"):
        out["reason"] = "no policy client, so the balance is unknown rather than zero"
        return out
    try:
        held = client.balance_usdc()
    except Exception as exc:  # pragma: no cover - env dependent
        log.warning("statement: balance unreadable (%s)", exc)
        held = None
    if held is None:
        out["reason"] = "the chain could not be reached, so this is unknown rather than empty"
        return out
    return {**liquidity_assess(held, escalations, t).as_dict(), "reason": ""}


def _budgets(business: Business, policy_for) -> list[dict]:
    """Each category's live budget, read from the contract.

    An empty list means one of two different things and the caller is told
    which: no PolicyWallet (the business is being measured, not spent for) or no
    categories configured yet.
    """
    if policy_for is None or not business.policy_wallet:
        return []
    client = policy_for(business)
    if client is None:
        return []

    # WHY it is unconfigured, not just that it is. `budget()` answers None for
    # four different situations and the page rendered all four as one gold chip:
    # no wallet, the node is down, this wallet is on another chain, and the
    # honest "nobody has set a limit for this category yet". Those are four
    # different things to do on a Monday morning.
    status = "ok"
    if hasattr(client, "wallet_status"):
        try:
            status = client.wallet_status()
        except Exception as exc:  # pragma: no cover - env dependent
            log.warning("statement: wallet status unreadable (%s)", exc)
            status = "no_rpc"
    reasons = {
        "no_wallet": "no wallet address on this business",
        "no_rpc": "the chain could not be reached, so this is unknown rather than unset",
        "not_on_this_chain": (
            "no contract at this address on the chain this service reads, so no "
            "budget here can be trusted"
        ),
    }

    out = []
    for category in business.categories:
        # GUARDED, like the market card below. A raising client would 500 the
        # whole statement, and a statement whose decisions are all in hand must
        # not fail because one optional read did.
        try:
            b = client.budget(category) if status == "ok" else None
        except Exception as exc:  # pragma: no cover - env dependent
            log.warning("statement: budget read failed for %s (%s)", category, exc)
            b = None
            status = "no_rpc"
        if b is None:
            row = {"category": category, "configured": False}
            if status != "ok":
                row["reason"] = reasons[status]
            out.append(row)
            continue
        out.append({**b, "configured": True})
    return out


def build_statement(
    ident: str,
    *,
    days: int = 7,
    registry: tuple[Business, ...] | None = None,
    tca_fn=None,
    policy_for=None,
    log_path: str | Path | None = None,
    now: float | None = None,
    #: How many decision rows to return. The page wants a readable page; the
    #: archiver that preserves these rows across a redeploy wants all of them,
    #: and a cap it cannot raise would silently truncate the record it exists to
    #: save — counts running SHORT, never long, which is the hardest shape of
    #: this bug to notice.
    limit: int | None = None
) -> dict | None:
    """The owner-facing statement for one business, or None if unknown.

    Every collaborator is injected so the whole page is testable without a
    chain, a subgraph or a wallet — the same reason ``decide`` is pure.
    """
    t = now if now is not None else time.time()
    b = resolve(ident, registry)
    if b is None:
        return None

    since = t - days * 86_400
    decisions = read_decisions(log_path, business=b.slug, since=since)
    summary = summarise(decisions)

    # Market context, in bp only. See the module docstring for why the USDC
    # figures from this call are not forwarded.
    context: dict = {"available": False, "reason": "not requested"}
    if tca_fn is not None:
        card = _market_card(tca_fn, b.treasury, days)
        if card.get("available"):
            context = {
                "available": True,
                # WHETHER THE TAPE HAS EVER SEEN THIS TREASURY, forwarded rather
                # than dropped. This dict is rebuilt by hand from the card, and
                # leaving `seen` out meant a treasury with no settlements
                # rendered `Purchases 0` and `Against the index —` on /spend as
                # though it had been measured and found clean. The statement's
                # own "No indexed history for this treasury yet." copy sits on
                # the `available: false` branch and so could never fire for it.
                "seen": card.get("seen"),
                "purchases": card.get("purchases"),
                "benchmarked": card.get("benchmarked"),
                "spent_usdc": card.get("spent_usdc"),
                "vw_slippage_bp": card.get("vw_slippage_bp"),
                "basis": "ACR arrival print, bp only",
                # The CARD's own note is deliberately not forwarded: it explains
                # an absence, and this field explains the bp denomination. Two
                # different sentences for two different facts — `seen` is what
                # tells a reader which one applies.
                "note": (
                    "bp against the published index, which is scale-invariant. "
                    "USDC savings on this statement come from observed quotes "
                    "instead; see anchors/GAP.md."
                ),
            }
        else:
            context = {
                "available": False,
                "reason": card.get("reason") or "no tape for this payer",
            }

    cap = RECENT_LIMIT if limit is None else max(1, min(1000, int(limit)))
    escalations = pending_escalations(decisions)[:cap]

    return {
        "business": b.as_public_dict(),
        "period_days": days,
        "as_of": t,
        "spend": summary,
        "budgets": _budgets(b, policy_for),
        # Beside the budgets, because permission and money are different
        # questions and the page showed only the first.
        "liquidity": _liquidity(b, policy_for, escalations, t),
        "spends": bool(b.policy_wallet),
        # What the owner has to act on. First, because it is the only part of
        # the statement that is waiting on them.
        "escalations": escalations,
        "recent": list(reversed(decisions))[:cap],
        "market_context": context,
    }
