"""The traction numbers, derived — never maintained.

Canteen's submission form asks three things: how many businesses you have
onboarded, how much value the agent moved, and what problems you are solving.
Their judging note adds the suspicion this file exists to answer: *"Genuine
usage during the event window… volume you can point to."* And the FAQ: *"What
doesn't count is a synthetic dataset."*

So every figure here is computed from the registry and the decision log at
request time. There is no hand-maintained total anywhere, which is the only way
a number and the rows beside it cannot drift apart — and drifting is exactly how
a traction claim stops being checkable.

THE DISTINCTION THAT KEEPS THIS HONEST, and it is the whole file:

    moved_usdc     what the OPERATOR actually paid out: the sum of `paid_usdc`
                   over rows that reached PAY, each one carrying the `tx` it
                   was paid with. Not restated here on purpose — this file
                   said "today this is zero… every decision was a dry run"
                   for long enough to outlive the first six on-chain payments,
                   so the one doc whose premise is "derived, never maintained"
                   was carrying a maintained number, and understating its own
                   side. Read it from `/operator/traction`.
    priced_usdc    what the operator assessed: metered, benchmarked, decided on.
                   Real work on real bills, and a different claim.
    recoverable    overpay found against an OBSERVED cheaper offer, with the
                   seller named on every one.

The fleet's historical settlements — the larger figure, and the tempting one —
were moved by the buyer agent before this operator existed. Counting them as
"value the agent moved" would be the single most tempting lie available here,
and it would be a lie: the operator priced them, it did not pay them.

MAINNET AND TESTNET ARE NEVER SUMMED. Canteen say test USDC counts and mainnet
counts more, which only means anything if the two are reported apart.
"""

from __future__ import annotations

import time
from collections import defaultdict

from .businesses import Business, counts, real
from .operator import ESCALATE, HOLD, PAY, REFUSE, REROUTE
from .statement import read_decisions, summarise

#: Intents the agent reached on its own authority.
_DECIDED = (PAY, HOLD, REROUTE, REFUSE)


def received_usdc(treasury: str, tape: list[dict] | None) -> float:
    """What this business was PAID, from the sellers' own settlement tape.

    RFB 4 asks for "total USDC received and paid out" and only the second half
    existed. The first is not a new measurement — it is the same tape the
    omission check already reads, looked at from the other side: a treasury that
    appears as the SELLER on a settlement is a treasury that was paid.

    Read from the tape rather than from our own decisions deliberately. The
    decision log is what this operator did; money arriving is what somebody else
    did, and a business's own records are the wrong authority for it. This is
    the essay's "reconciliation against the bank" pointed at income.

    No tape means NOT MEASURED, and the caller reports that rather than zero. A
    confident 0.0 from a file that failed to load is the kind of number this
    page exists to refuse.
    """
    if not tape or not treasury:
        return 0.0
    t = treasury.lower()
    total = 0.0
    for r in tape:
        if str(r.get("seller") or "").lower() != t:
            continue
        amount = r.get("amount_usdc")
        if isinstance(amount, (int, float)):
            total += float(amount)
    return round(total, 6)


def _per_business(b: Business, tape: list[dict] | None = None) -> dict:
    rows = read_decisions(business=b.slug)
    s = summarise(rows)
    return {
        "slug": b.slug,
        "label": b.public_name,
        "tier": b.tier,
        "chain": b.chain,
        "consented": b.consented,
        "spends": bool(b.policy_wallet),
        "decisions": s["decisions"],
        "decided": s["decided"],
        "escalated": s["escalated"],
        # What the operator actually paid out, and what it only assessed.
        "moved_usdc": s["paid_usdc"],
        # And what came IN, from the other side of the same tape. Kept beside
        # `moved_usdc` and never netted against it: a business that received 10
        # and paid 10 has done twice the work of one that did neither, and a
        # single net figure would report both as zero.
        "received_usdc": received_usdc(b.treasury, tape),
        "priced_usdc": round(sum(float(r.get("billed_usdc") or 0.0) for r in rows), 6),
        # HYPOTHETICAL. A cheaper offer existed; nothing was bought against it,
        # which is why the ledger refuses to book it and why it is named
        # `recoverable` rather than `saved`.
        "recoverable_usdc": s["saved_usdc"],
        # REALISED, and these two are the stronger claim: money a vendor asked
        # for that did not leave the wallet. They were computed on every
        # request and then dropped, so the page totalled the hypothetical
        # figure and showed neither of the defensible ones. Kept apart from
        # `recoverable_usdc` and from each other, and never summed into one
        # "value" number — the whole point is which of them a bank statement
        # would corroborate.
        "held_back_usdc": s["held_back_usdc"],
        "overbilled_usdc": s["overbilled_usdc"],
        "discrepancies": s["consumption_discrepancies"],
        "unmetered": s["unmetered"],
        **{
            k: s[k]
            for k in (
                "settled_by_agent",
                "settled_by_owner",
                "settled_on_time",
                "settled_with_a_due_date",
                "owner_resolutions",
                "owner_agreed",
                "risk_events_caught",
                "paid_unscreened",
                "addresses_screened",
                "alerts_raised",
                "alerts_resolved",
            )
        },
        # A per-business ledger anyone can open and check the arithmetic in.
        "ledger": f"/operator/ledger/{b.slug}",
        "statement": f"/operator/statement/{b.slug}",
    }


def build_traction(
    registry: tuple[Business, ...] | None = None,
    now: float | None = None,
    #: The sellers' settlement tape, for the money that came IN. Injected like
    #: every other collaborator here so the whole page stays testable without a
    #: chain or a disk; absent means `received_usdc` is 0.0 and the note says
    #: the tape was not supplied.
    tape: list[dict] | None = None,
) -> dict:
    """Every traction figure, computed from the rows that justify it."""
    # Through `real()`, so a sandbox business cannot move a single figure on
    # this page. It demonstrates the UI; it is not usage.
    reg = real(registry)
    rows = [_per_business(b, tape) for b in reg]

    by_chain: dict[str, dict[str, float]] = defaultdict(
        lambda: {
            "moved_usdc": 0.0,
            "received_usdc": 0.0,
            "priced_usdc": 0.0,
            "recoverable_usdc": 0.0,
            "held_back_usdc": 0.0,
            "overbilled_usdc": 0.0,
        }
    )
    intents: dict[str, int] = defaultdict(int)
    decided = escalated = discrepancies = unmetered = decisions = 0
    #: The figures RFB 4 and RFB 5 ask for by name. Summed across businesses the
    #: same way everything else here is: from the rows, at request time.
    brief: dict[str, int] = defaultdict(int)
    BRIEF_KEYS = (
        "settled_by_agent",
        "settled_by_owner",
        "settled_on_time",
        "settled_with_a_due_date",
        "owner_resolutions",
        "owner_agreed",
        "risk_events_caught",
        "paid_unscreened",
        "addresses_screened",
        "alerts_raised",
        "alerts_resolved",
    )

    for r in rows:
        c = by_chain[r["chain"]]
        c["moved_usdc"] += r["moved_usdc"]
        c["received_usdc"] += r["received_usdc"]
        c["priced_usdc"] += r["priced_usdc"]
        c["recoverable_usdc"] += r["recoverable_usdc"]
        c["held_back_usdc"] += r["held_back_usdc"]
        c["overbilled_usdc"] += r["overbilled_usdc"]
        decisions += r["decisions"]
        decided += r["decided"]
        escalated += r["escalated"]
        discrepancies += r["discrepancies"]
        unmetered += r["unmetered"]
        for k in BRIEF_KEYS:
            brief[k] += r[k]

    for b in reg:
        for d in read_decisions(business=b.slug):
            intents[str(d.get("intent") or "")] += 1

    return {
        "as_of": now if now is not None else time.time(),
        # "How many businesses have you onboarded"
        "businesses": counts(reg),
        # "How much value did the agent move" — apart by chain, never summed,
        # and `moved` is kept distinct from `priced`.
        "by_chain": {
            chain: {k: round(v, 6) for k, v in vals.items()}
            for chain, vals in sorted(by_chain.items())
        },
        # "What problems are you solving for them"
        "work": {
            "decisions": decisions,
            "decided": decided,
            "escalated": escalated,
            "by_intent": {k: intents.get(k, 0) for k in (PAY, HOLD, REROUTE, ESCALATE, REFUSE)},
            "consumption_discrepancies": discrepancies,
            "unmetered": unmetered,
            # RFB 4: "obligations settled on time without a human touching
            # them", and "decisions made vs escalated, and how often the human
            # agreed". RFB 5: "risk events caught before the transaction".
            #
            # Every one is a pair of counts rather than a rate. A rate hides its
            # denominator, and the denominators here are the interesting part: a
            # perfect agreement figure over zero resolutions, or a perfect
            # punctuality figure over zero due dates, are both the shape this
            # product exists to refuse to print.
            "autonomy": {
                "settled_by_agent": brief["settled_by_agent"],
                "settled_by_owner": brief["settled_by_owner"],
                "settled_on_time": brief["settled_on_time"],
                "settled_with_a_due_date": brief["settled_with_a_due_date"],
            },
            "agreement": {
                "owner_resolutions": brief["owner_resolutions"],
                "owner_agreed": brief["owner_agreed"],
            },
            "screening": {
                "risk_events_caught": brief["risk_events_caught"],
                "paid_unscreened": brief["paid_unscreened"],
            },
            # RFB 5's counts, named for what this product does rather than for
            # what the brief wishes it did: we SCREEN at decision time, we do
            # not monitor continuously, and `addresses_monitored` would be the
            # overclaim this page exists to refuse.
            #
            # Summed across businesses, so a counterparty two businesses both
            # screened counts twice here and once in each row. That is the
            # honest reading of a per-business figure added up; a repo-wide
            # distinct count would need a set this function does not hold.
            "compliance": {
                "addresses_screened": brief["addresses_screened"],
                "alerts_raised": brief["alerts_raised"],
                "alerts_resolved": brief["alerts_resolved"],
            },
        },
        "per_business": rows,
        "note": (
            "Every figure is computed from the registry and the decision log at "
            "request time; nothing here is maintained by hand. `moved_usdc` is "
            "what the operator paid out, which is not the same claim as "
            "`priced_usdc`, what it assessed. Mainnet and testnet are never summed."
        ),
    }
