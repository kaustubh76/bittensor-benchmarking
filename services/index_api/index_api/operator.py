"""The spend operator's decision — one obligation, start to finish.

RFB 4 asks for "at least one complete business workflow, run by the agent from
start to finish", and for the agent to "know which decisions are not its to
make". This module is that decision, and it is deliberately a PURE FUNCTION:
``decide()`` takes an obligation, what the meter counted, what the market is
charging and what the budget has left, and returns a record. It moves no money
and reads no chain, so every branch below is reachable from a test rather than
from a funded wallet — the same reason ``apps/agent/src/reroute.ts`` keeps
``applyReroute`` pure.

THE ORDER OF REFUSALS IS THE DESIGN. ``scripts/hedger.py`` already learned this:
checks run cheapest-and-most-damning first, each one names itself, and a check
that cannot be evaluated is a refusal rather than a pass. The order here is

    1  duplicate           we already paid this
    2  payable at all      a payee, and an amount
    3  the meter           did we consume what they billed for
    4  the counterparty    may we pay this address at all
    4b the agreement       does this match what we agreed to pay
    5  benchmarked         is there a market price to judge against
    6  the price           at par, over par, cheaper elsewhere
    7  the timing          due now, or worth holding
    8  the budget          per-transaction limit, then the period cap
    9  the cash            can the wallet cover what is already dated

and it is not interchangeable. Pricing an invoice we never owed is wasted work;
rerouting one that is a duplicate pays a stranger twice. The meter comes before
the price because a bill for work nobody did is not a pricing question — that is
Prior Art #06's whole point, and the reason this agent is named after the
official who kept the standard measure rather than after a bargain hunter.

There are TEN. The list showed eight for a long time because 4b arrived with
the commitment register and was never added; 9 arrived with the balance read.
Checks 8 and 9 look alike and are not: 8 is PERMISSION, `cap - spent` from the
contract's own counters, and reads healthy on a wallet holding nothing; 9 is
MONEY. Policy answers first, so a bill that breaches its own limit names that
limit rather than blaming the balance.

And an eleventh outcome does not live in `decide()` at all — `run_obligation`
escalates before reaching it when the business has no budget contract on chain,
which is why that row carries none of the pricing or screening evidence the
others do.

REFUSE, NEVER CLAMP, and never silently. Every outcome carries a ``rule``: one
line naming the check that fired. ``PolicyWallet`` stores the hash of this whole
record before the money moves, so the reasoning is committed rather than
reconstructed afterwards — ``euthyna``, Prior Art #01.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field

from .counterparty import UNKNOWN, CounterpartyVerdict
from .liquidity import HORIZON_S as LIQUIDITY_HORIZON_S
from .liquidity import Liquidity
from .liquidity import short_by as liquidity_short_by
from .par import MATERIAL_BP, Par, assess

log = logging.getLogger("index_api.operator")

#: How far the vendor's reported quantity may exceed our own count before the
#: bill stops being a rounding difference and becomes a question for a human.
#: Deliberately small: the meter is the product's whole claim, and a generous
#: tolerance is a way of not looking.
METER_TOLERANCE = float(os.environ.get("ACR_OPERATOR_METER_TOLERANCE", "0.02"))

#: The most the operator will pay for something it could not price. An
#: unbenchmarked invoice is not refused outright — a brand-new vendor has no
#: market yet and somebody still has to be paid — but it is capped, so the
#: unpriceable case cannot quietly become the expensive one.
UNBENCHMARKED_MAX_USDC = float(os.environ.get("ACR_OPERATOR_UNBENCHMARKED_MAX", "1.0"))

#: Pay this many seconds ahead of the due date at the earliest, unless an
#: early-pay discount beats holding the cash. Net-30 exists because payments
#: used to be expensive; at a cent a transaction the calendar stops being the
#: reason to wait, so the default is short rather than zero.
PAY_WINDOW_S = float(os.environ.get("ACR_OPERATOR_PAY_WINDOW_S", str(3 * 86_400)))

#: Where decisions are written. Like ``hedger.py``'s log this is a DIAGNOSTIC:
#: the checkable record is the signed receipt and the on-chain decision hash.
LOG_PATH = os.environ.get("ACR_OPERATOR_LOG_PATH", "data/operator_decisions.jsonl")

#: The fields `as_record` leaves out of the hash: the payment's own results,
#: which do not exist yet when the commitment is made.
#:
#: Named ONCE because three places need to agree — the writer, a reviewer
#: re-deriving the hash from a log row, and the approve path paying an
#: obligation somebody escalated hours ago. If any of them strips a different
#: set, the record stops matching its own commitment and the only reading left
#: is "this was edited", which would be false.
UNHASHED = ("tx", "paid_usdc", "warns_signature")

#: Intents. ``reroute`` and ``escalate`` are not failures — they are the two
#: ways this agent is useful without being reckless.
PAY = "pay"
HOLD = "hold"
REROUTE = "reroute"
ESCALATE = "escalate"
REFUSE = "refuse"


@dataclass(frozen=True)
class Obligation:
    """Something the business owes, however it arrived."""

    obligation_id: str
    vendor: str
    billed_usdc: float
    #: The business whose money this is, by registry slug. Part of the decision
    #: and therefore part of the hash: a record that does not say whose treasury
    #: paid is not a record anybody can audit, and with one wallet per business
    #: it is the only field that distinguishes two otherwise identical invoices.
    business: str = ""
    category: str = "general"
    #: ``x402`` a machine service we called · ``invoice`` a vendor bill ·
    #: ``milestone`` contractor work · ``subscription`` a renewal.
    kind: str = "invoice"
    resource: str = ""
    #: What the vendor says we consumed, in the service's own unit.
    vendor_quantity: float | None = None
    #: The service's unit ("$/1k tokens", "$/GPU-sec", …). THE UNIT IS THE
    #: MARKET: measured on this repo's own tape, no resource has more than one
    #: seller, so a resource-keyed benchmark can never fire. Grouping by unit
    #: gives `$/1k tokens` four sellers spanning 1.3x. Empty falls back to
    #: comparing whole-bill prices for the same resource.
    unit: str = ""
    #: THE WINDOW THIS BILL COVERS, in unix seconds, and the reason the id can
    #: be trusted twice.
    #:
    #: An obligation had no period, so `(seller, resource)` named a bill for all
    #: of history. Once paid, that pair sat in `settled_refs` forever and the
    #: next settlement from the same seller for the same service summed into an
    #: id that was already there — refused as a duplicate, permanently. A human
    #: running this once never sees it; an unattended run pays on its first tick
    #: and then reports `refuse · already paid` for the rest of its life, while
    #: the decision count keeps climbing. A smaller number, not a broken page.
    #:
    #: `period_end` comes from the DATA — the latest `settled_at` in the bill —
    #: and never from the clock. With `now` it would mint a fresh id on every
    #: tick, the duplicate check would never match, and the same consumption
    #: would be paid again and again.
    period_start: float = 0.0
    period_end: float = 0.0
    due_at: float | None = None
    #: The vendor's own reference, for duplicate detection.
    invoice_ref: str = ""
    #: An early-payment discount, as a fraction (0.02 == 2% off).
    early_pay_discount: float = 0.0


@dataclass
class ObligationDecision:
    """The record. ``billed · metered · par · paid · rule · tx``, plus why."""

    at: float
    obligation_id: str
    vendor: str
    category: str
    billed_usdc: float
    intent: str
    #: One line naming the check that fired. The thing a reviewer reads first.
    rule: str
    #: The business whose money this is, by registry slug.
    business: str = ""
    resource: str = ""
    metered_quantity: float | None = None
    vendor_quantity: float | None = None
    #: Positive == the vendor billed for more than we counted.
    discrepancy: float | None = None
    #: THE SAME GAP, IN MONEY. Prior Art #06's officials kept the standard
    #: measures in the marketplace because a seller who supplies both the goods
    #: and the measuring cup will eventually supply a smaller cup. We have kept
    #: our own count since the beginning — and reported it in the service's own
    #: units, which is the vendor's vocabulary, not a budget's.
    #:
    #: Priced at the bill's OWN effective rate (`billed_usdc / vendor_quantity`)
    #: rather than at par or at the cheapest offer. Those two would answer a
    #: different question — "is this dear?" — and this one is "how much of this
    #: bill is for something that did not happen?". The vendor's own arithmetic
    #: is the only fair basis for that.
    discrepancy_usdc: float | None = None
    #: WHICH CHANNEL the money went out through: ``circle`` for a Circle
    #: developer-controlled wallet, ``local`` for a raw key, ``""`` when no
    #: policy client was offered. The same argument `screen_backend` won one
    #: field below: a verdict without its source is a claim without a basis, and
    #: a payment without its channel is too.
    #:
    #: Set BEFORE the record is hashed, so the commitment on chain says which
    #: channel was authorised rather than which one a later reader assumes. Five
    #: payments on Arc testnet went out from a raw EOA while the config now
    #: resolves the agent role to Circle, and nothing anywhere could tell —
    #: identical calldata either way.
    paid_via: str = ""
    #: ``clear`` · ``flagged`` · ``unknown`` · ``""`` when no screen was offered.
    #: In the hashed record, so the commitment says what was known about the
    #: counterparty at the moment the money moved — not what a later re-screen
    #: concluded.
    screen_risk: str = ""
    screen_matched: list[str] = field(default_factory=list)
    #: WHICH screen answered: ``yente`` · ``denylist`` · ``off``. Dropped at this
    #: boundary until now, which is how a verdict of ``clear`` produced by
    #: comparing against a list of ZERO addresses was indistinguishable from one
    #: produced by a real sanctions dataset. A risk verdict without its source is
    #: a claim without a basis.
    screen_backend: str = ""
    par_usdc: float | None = None
    best_usdc: float | None = None
    over_par_bp: float | None = None
    #: How far above the going rate this bill is, in USDC. `over_par_bp` is the
    #: same fact as a ratio; this is it in the unit a reader budgets in. It was
    #: computed by `assess` and thrown away at the boundary.
    over_par_usdc: float | None = None
    #: Recoverable, because somebody is offering it at that price.
    #:
    #: HYPOTHETICAL, AND THE LABEL MUST SAY SO. A reroute buys nothing — it
    #: declines this vendor and names a cheaper offer. `ledger_export` refuses
    #: to book it as income for exactly this reason: "writing the avoided
    #: overpay as `Income:Savings` would be inventing a credit".
    saving_usdc: float | None = None
    reroute_to: str = ""
    escalated: bool = False
    paid_usdc: float = 0.0
    tx: str | None = None
    notes: list[str] = field(default_factory=list)
    #: Who wrote this record: ``agent`` for a decision the operator reached on
    #: its own authority, ``owner`` for one a person settled out of the queue.
    #: Without it an owner-approved payment is written as a plain ``pay`` and is
    #: indistinguishable from an autonomous one — and "obligations settled
    #: without a human touching them" is precisely the difference.
    actor: str = "agent"
    #: What the agent WOULD have done with authority it did not have. Set only
    #: where it escalated, and deliberately left empty where it has no opinion
    #: to offer: "no budget on chain" is an absence of authority, not a
    #: recommendation, and counting it would invent an agreement to measure.
    recommended_intent: str = ""
    #: The window this bill covered, carried so the NEXT run knows where to
    #: start. Hashed with everything else, because which settlements a payment
    #: was for is part of what the decision rested on.
    period_start: float = 0.0
    period_end: float = 0.0
    # --- the Warns integration ---------------------------------------------
    #: THE ID GOES IN THE HASH; THE SIGNATURE CANNOT.
    #:
    #: Warns reaches its own decision about an invoice, signs it, and hands back
    #: an id. Putting that id inside the hashed record is what makes the
    #: on-chain commitment name WHICH decision authorised the payment:
    #: `PolicyWallet` binds `decisionHash` into the EIP-712 digest that
    #: `spendApproved` recovers, so the owner's signature then covers a
    #: Warns-signed decision rather than just an amount and a payee.
    #:
    #: Empty by default, and that is a real state rather than a placeholder — a
    #: decision this operator reached on its own authority has no Warns id, and
    #: writing one in would claim an authorisation nobody gave.
    warns_decision_id: str = ""
    #: Their signature OVER that hash, which is why it is in ``UNHASHED``: a
    #: signature inside the record it signs is circular and cannot be produced.
    #: Evidence carried beside the commitment, not part of it.
    warns_signature: str = ""
    # --- what the decision was JUDGED ON, so it can be replayed -------------
    #
    # `PolicyWallet.sol` says the hash is "what makes the off-chain ledger
    # REPLAYABLE rather than merely stored". It was not: the record pinned every
    # OUTPUT of the pricing step — par, best, bp, saving — and none of its
    # inputs or thresholds. A reviewer could prove a row had not been edited and
    # could re-check the arithmetic between those four numbers. They could not
    # recompute them, could not tell which market produced them, and could not
    # tell what limits the comparison was judged against.
    #
    # Worst of them was the denomination: given a bill of 1.5 against a par of
    # 0.002, nothing said whether the bill had been divided by a quantity or
    # compared whole. A thousandfold fork with no field to settle it.
    #
    # All of these are inside `as_record()`, so from here the chain commits to
    # the parameters as well as the verdict.
    #: Which agreement this bill was judged against, and how it fared. The id
    #: and the hash together are the reviewer's half of the symbolon: the hash
    #: pins the terms as they stood, and it is inside `as_record()`, so the
    #: chain commits to "this payment was made against that agreement".
    commitment_id: str = ""
    commitment_hash: str = ""
    #: ``within`` · ``over_total`` · ``over_unit_price`` · ``over_quantity`` ·
    #: ``outside_window``, and ``""`` where no agreement covered the bill.
    #:
    #: NOT ``no_commitment``, which `commitments.NONE_FOUND` defines and the
    #: ladder cannot reach: check 4b only runs `assess` when a commitment was
    #: found, so the one branch that would emit it is never taken. The empty
    #: string is the real "no agreement" value, and `statement.py` already
    #: treats both as the same thing.
    commitment_verdict: str = ""
    #: What the bill exceeded the agreement by, in USDC. The figure a mismatch
    #: holds back, and the only one here that is money rather than a comparison.
    over_commitment_usdc: float | None = None
    #: ``x402`` · ``invoice`` · ``milestone`` · ``subscription``.
    kind: str = ""
    #: The service's own unit, which decides WHICH market was consulted.
    unit: str = ""
    early_pay_discount: float = 0.0
    #: ``unit`` or ``whole`` — whether the bill was divided by a quantity.
    par_denomination: str = ""
    #: How many independent sellers the benchmark rested on.
    par_sellers: int | None = None
    #: ``NO_QUOTES`` · ``ONE_SELLER`` · ``NO_INDEPENDENT_SELLER`` · ``NO_QUANTITY``
    par_reason: str = ""
    #: Why the screen said what it said — the field that tells a denylist of
    #: zero addresses from a real dataset answering.
    screen_reason: str = ""
    #: Whether a screen ran at all, as against having nothing to say.
    screen_screened: bool | None = None
    #: Whether an unknown verdict was configured to block — an environment
    #: variable that CHANGES THE BRANCH and used to leave no trace at all.
    screen_required: bool | None = None
    #: The authority the decision cleared against, read from the contract.
    remaining_usdc: float | None = None
    per_tx_limit_usdc: float | None = None
    #: What the cash check saw, so the record carries the picture it was judged
    #: against rather than only the verdict. `None` throughout when no balance
    #: was read — which the rule's own note also says, because a reader
    #: scanning fields should not have to infer it from three nulls.
    held_usdc: float | None = None
    due_usdc: float | None = None
    liquidity_horizon_s: float | None = None
    #: The four thresholds. Three come from the environment at import, so
    #: without them a reviewer cannot recover them from the repo either.
    meter_tolerance: float | None = None
    unbenchmarked_max_usdc: float | None = None
    pay_window_s: float | None = None
    material_bp: float | None = None
    #: Carried from the obligation, so the record can answer "was this settled
    #: on time". It was being dropped at this boundary, which is the whole
    #: reason nothing could.
    due_at: float | None = None
    #: The vendor's own reference, carried so the duplicate check can run BOTH
    #: of its halves from the log. `is_duplicate` matches our id or this one;
    #: with only the id on the record, a re-sent invoice under a fresh id is
    #: invisible — "checking one catches half", and the log only had one.
    invoice_ref: str = ""

    def as_record(self) -> dict:
        """The dict that gets hashed into ``PolicyWallet``.

        The payment's own results are excluded: the commitment is made BEFORE the
        money moves, so a record including its own transaction hash could never
        be hashed in time. Everything the decision rested on is in here, which is
        what a reviewer replays.
        """
        return {k: v for k, v in asdict(self).items() if k not in UNHASHED}


def hashable_record(row: dict) -> dict:
    """The decision as it was HASHED, recovered from a log row.

    `log_decision` writes the whole decision including the payment's results;
    `as_record` hashed it without them. Anything re-deriving that hash later —
    verifying a receipt, or paying an obligation a human has just approved — has
    to strip the same fields, so it strips them through here.
    """
    return {k: v for k, v in (row or {}).items() if k not in UNHASHED}


#: Fields that differ between two ticks for reasons that are not the decision.
#:
#: `at` is the obvious one. The budget headroom is the subtle one: it moves every
#: time anything is paid, and once it was recorded, two otherwise identical
#: re-decisions of the same open bill stopped matching — which would have undone
#: the restatement rule and put an unpaid rerouted bill back to twenty-four
#: identical rows a day.
#:
#: Safe to ignore here precisely because it is NOT noise: if the headroom changed
#: enough to change the answer, `intent` and `rule` change with it, and those are
#: compared.
_DRIFTS = ("at", "remaining_usdc")


def _restates_the_last(d: ObligationDecision, target: str) -> bool:
    """Would this row say exactly what the last row about this bill already said?

    THE LOG RECORDS DECISIONS, NOT TICKS. An unpaid obligation is re-decided on
    every run — deliberately, because the screen is re-asked each time and a
    verdict that can change its mind is the whole point — and a rerouted bill is
    never settled, so it comes back for ever. Run by a person that is a handful
    of rows. On an hourly schedule it is twenty-four identical rows a day per
    open bill, and `work.decisions` on the traction page would climb steadily
    while nothing whatsoever had happened. A number inflated by repetition is
    the kind of flattery this whole module is built to refuse.

    Compared on the HASHED record, so `at` is not the only difference that
    counts and a changed screen verdict, price, meter or intent all still get
    written. Payments can never be suppressed by this: check 1 refuses a second
    payment of the same reference, so a `pay` is never a restatement of a `pay`.

    Reads only the tail, and never raises — `log_decision` must not acquire a
    new way to fail.
    """
    try:
        if not os.path.exists(target):
            return False
        with open(target, encoding="utf-8") as fh:
            rows = fh.readlines()[-200:]
        want = {k: v for k, v in asdict(d).items() if k not in UNHASHED and k not in _DRIFTS}
        for line in reversed(rows):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if str(row.get("obligation_id") or "") != d.obligation_id:
                continue
            have = {k: v for k, v in row.items() if k not in UNHASHED and k not in _DRIFTS}
            return have == want
    except Exception:  # pragma: no cover - a read failure must not block a write
        return False
    return False


def replay(row: dict) -> ObligationDecision:
    """Re-run the decision from its own record.

    `PolicyWallet.sol` says the hash is "what makes the off-chain ledger
    REPLAYABLE rather than merely stored". Until the record carried the
    decision's INPUTS as well as its outputs, that was tamper-evidence wearing
    the word replay: a reviewer could prove a row had not been edited, and could
    re-check the arithmetic between the four pricing numbers, but could not
    recompute them, could not tell which market produced them, and could not
    tell what thresholds they were judged against.

    Now they can. This rebuilds the obligation and the three judgments from the
    row and asks `decide()` the same question again. A verdict that differs means
    one of three things, all worth knowing: the record is incomplete, the code's
    behaviour changed, or the row was written by something other than this
    ladder — which is how the `inv-0044` fixture was caught claiming a per-tx
    escalation that check 5 would have pre-empted.

    THE CLOCK IS TAKEN FROM THE RECORD, not from now. `at` is the only
    non-deterministic input `decide()` has that the record already pinned, and
    replaying against the present would make every held bill come due.
    """
    from .counterparty import CounterpartyVerdict

    def num(key):
        v = row.get(key)
        return float(v) if isinstance(v, (int, float)) else None

    ob = Obligation(
        obligation_id=str(row.get("obligation_id") or ""),
        vendor=str(row.get("vendor") or ""),
        billed_usdc=float(row.get("billed_usdc") or 0.0),
        business=str(row.get("business") or ""),
        category=str(row.get("category") or "general"),
        kind=str(row.get("kind") or ""),
        resource=str(row.get("resource") or ""),
        vendor_quantity=num("vendor_quantity"),
        unit=str(row.get("unit") or ""),
        period_start=float(row.get("period_start") or 0.0),
        period_end=float(row.get("period_end") or 0.0),
        due_at=num("due_at"),
        invoice_ref=str(row.get("invoice_ref") or ""),
        early_pay_discount=float(row.get("early_pay_discount") or 0.0),
    )

    # THE BENCHMARK, REBUILT AS A REAL `Par`, so `assess` re-derives the
    # arithmetic instead of being handed back the answer that was recorded.
    # That is the stronger replay: the recorded `over_par_bp` and `saving_usdc`
    # have to fall out of `par_usdc`, `best_usdc`, the bill and the
    # denomination again, or the row does not reproduce.
    #
    # `quotes` is deliberately not recorded and so cannot be rebuilt — it is
    # other sellers' prices at a moment that has passed. `assess` does not read
    # it, which is why the median is reproducible from the record and the
    # quotes behind it are not.
    par_usdc = num("par_usdc")
    par = None
    if par_usdc is not None or row.get("par_reason"):
        par = Par(
            resource=ob.resource,
            available=par_usdc is not None,
            denomination=str(row.get("par_denomination") or "call"),
            reason=str(row.get("par_reason") or ""),
            par_usdc=par_usdc,
            best_usdc=num("best_usdc"),
            best_seller=str(row.get("reroute_to") or ""),
            sellers=int(row.get("par_sellers") or 0),
        )

    screen = None
    if row.get("screen_risk"):
        screen = CounterpartyVerdict(
            address=ob.vendor,
            risk=str(row["screen_risk"]),
            backend=str(row.get("screen_backend") or ""),
            matched=tuple(row.get("screen_matched") or ()),
            reason=str(row.get("screen_reason") or ""),
            screened=bool(row.get("screen_screened", True)),
        )

    kw = {}
    for threshold in (
        "meter_tolerance", "unbenchmarked_max_usdc", "pay_window_s", "material_bp",
    ):
        v = num(threshold)
        if v is not None:
            kw[threshold] = v

    # The cash picture, rebuilt from the record rather than re-read. A replay
    # that went back to the chain would get TODAY's balance and reach a verdict
    # the original could not have — which is the mistake `par.quotes` is left
    # out for, in reverse. `held_usdc` absent means no balance was read then,
    # and `Liquidity(None, …)` is exactly how the rung sees that.
    #
    # Deliberately not the obligation set: the recorded `due_usdc` IS what the
    # other bills summed to at the time, and that sum is unrecoverable from one
    # row. Rebuilding the picture from its two numbers is the most a single
    # record can honestly support.
    liq = None
    if num("held_usdc") is not None or num("due_usdc") is not None:
        liq = Liquidity(
            held_usdc=num("held_usdc"),
            due_usdc=num("due_usdc") or 0.0,
            due_count=0,
            soonest_at=None,
            undated=0,
            horizon_s=num("liquidity_horizon_s") or LIQUIDITY_HORIZON_S,
        )

    return decide(
        ob,
        par=par,
        screen=screen,
        metered_quantity=num("metered_quantity"),
        liquidity=liq,
        settled_refs=set(),
        remaining_usdc=num("remaining_usdc"),
        per_tx_limit_usdc=num("per_tx_limit_usdc"),
        now=float(row.get("at") or 0.0),
        **kw,
    )


def log_decision(d: ObligationDecision, path: str | None = None) -> None:
    """Append one decision. Never raises: a full disk must not stop a payment
    that already cleared policy, and the authoritative record is on chain.

    Skips a row that only restates the last decision about the same bill — see
    `_restates_the_last` for why a schedule makes that necessary.
    """
    target = path or LOG_PATH
    if _restates_the_last(d, target):
        return
    try:
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(asdict(d), default=str) + "\n")
    except Exception as exc:  # pragma: no cover - disk dependent
        log.warning("operator: could not write the decision log (%s)", exc)


# --- the meter (Prior Art #06) ---------------------------------------------


def meter_quantity(
    receipts: list[dict], vendor: str, resource: str, since: float = 0.0
) -> float | None:
    """Our OWN count of what we consumed from this vendor for this resource.

    Independent because it is not the vendor's number: these are the operator's
    own settlement receipts, each one a payment it made and can prove. "The
    agent's own cup", in the prior-art's terms.

    ``None`` when we have no record at all, which is NOT zero. Zero would assert
    we consumed nothing and make every bill look fraudulent; ``None`` says we
    cannot check, and the caller escalates instead of accusing.
    """
    v = (vendor or "").lower()
    seen = False
    total = 0.0
    for r in receipts or []:
        if (r.get("seller") or "").lower() != v:
            continue
        if resource and r.get("resource") != resource:
            continue
        if float(r.get("settled_at") or 0.0) < since:
            continue
        seen = True
        q = r.get("quantity")
        if isinstance(q, (int, float)) and q > 0:
            total += float(q)
    return total if seen else None


def _payable_to(vendor: str) -> bool:
    """Is there somebody to pay?

    The zero address is the shape this fails in practice: an unparsed payee
    field, or a vendor record with the address missing, renders as
    0x000…0 and a payment to it is burnt rather than refused. A malformed
    non-hex string is also not a payee — it is caught here rather than by
    ``int(..., 16)`` raising three checks later.
    """
    v = (vendor or "").strip()
    if not v:
        return False
    if v.startswith("0x"):
        try:
            return int(v, 16) != 0
        except ValueError:
            return False
    return True


def resource_path(resource: str) -> str:
    """A resource's path, whatever form it arrived in.

    The archive records `/compute/acr-seller-inf-frontier`; a catalog records
    `https://host/compute/acr-seller-inf-frontier`. Matching them as raw strings
    finds nothing, which is exactly what the first version of the runner did —
    it reported zero obligations against 75 real settlements and looked like a
    quiet success.
    """
    r = resource or ""
    if "://" in r:
        rest = r.split("://", 1)[1]
        r = "/" + rest.split("/", 1)[1] if "/" in rest else "/"
    return r


def obligation_key(
    business_slug: str, seller: str, resource: str, period_end: float = 0.0
) -> str:
    """The id a period's bill is known by.

    HERE, AND NOT IN THE RUNNER, because two things now have to agree about it:
    the loop that mints these ids and the audit that looks for a settlement with
    no decision. If they build the key differently the audit reports an omission
    for every receipt, which is the loudest possible way to be wrong.

    THE WHOLE PATH, not its last segment. `rsplit("/", 1)[-1]` gave
    `/curve/ACR-INF` and `/vol/ACR-INF` the same id, and two rows in the
    committed archive still share it.
    """
    path = resource_path(resource)
    leaf = path.strip("/").replace("/", "-") or "root"
    base = f"{business_slug}:{seller[:10]}:{leaf}"
    # NO PERIOD MEANS THE OLD ID, BYTE FOR BYTE. The 19 rows in the committed
    # archive were written before bills had windows, and `settled_refs_from`
    # reads their ids back to decide what has already been paid. A new format
    # that did not reproduce the old one would make every one of them
    # unrecognisable and the next run would pay them all a second time — the
    # error of original entry this check exists to prevent.
    return base if not period_end else f"{base}@{int(period_end)}"


def obligations_for(
    business,
    receipts: list[dict],
    catalog: dict,
    paid_through: dict[tuple[str, str], float] | None = None,
) -> list[Obligation]:
    """One per (seller, resource) this business really bought, for the period.

    THE PERIOD IS NOW A PERIOD. This filtered on `payer` and nothing else, so
    "for the period" meant the whole archive, start to end of file — and since
    `obligation_key` had no time component either, a paid `(seller, resource)`
    pair stayed in `settled_refs` forever and every later settlement from that
    seller was refused as a duplicate. Harmless while a human ran this once.
    Fatal on a schedule: the first tick pays, every tick after it refuses
    everything, and the only symptom is a number that stops going up.

    `paid_through` is `operator.settled_through(decisions)` — where the last
    paid bill for each pair ended. Receipts at or before that boundary are
    already settled; what is left is this period.

    AN OBLIGATION HERE IS A PERIOD'S BILL, not a single call, and that is a unit
    decision rather than a presentational one. The benchmark is a UNIT price
    ($/1k tokens), so the thing compared against it has to be a unit price too.
    Pairing one call's price with the meter's cumulative count divides a
    per-call figure by a period's quantity and reports nine thousand basis
    points of discount — which is precisely what the first version of this
    script did, on real data, before anybody looked.

    So: `billed_usdc` is everything that seller charged for that service, and
    `vendor_quantity` is everything we took. Their ratio is the effective unit
    price, which is the number the market can actually be compared with.
    """
    treasury = business.treasury.lower()
    through = paid_through or {}
    billed: dict[tuple[str, str], float] = defaultdict(float)
    consumed: dict[tuple[str, str], float] = defaultdict(float)
    units: dict[tuple[str, str], str] = {}
    calls: dict[tuple[str, str], int] = defaultdict(int)
    first: dict[tuple[str, str], float] = {}
    last: dict[tuple[str, str], float] = {}

    for r in receipts:
        if (r.get("payer") or "").lower() != treasury:
            continue
        seller, resource = r.get("seller") or "", resource_path(r.get("resource") or "")
        amount, qty = r.get("amount_usdc"), r.get("quantity")
        if not seller or not resource or not isinstance(amount, (int, float)):
            continue
        key = (seller, resource)
        at = float(r.get("settled_at") or 0.0)
        # STRICTLY AFTER the last period we paid for. A receipt exactly on the
        # boundary was in that bill, and counting it again is how a period
        # overlaps its predecessor and the same consumption gets billed twice.
        #
        # A ZERO BOUND MEANS NO BOUND, not a bound at the epoch. Written as
        # `at <= bound` it also dropped every receipt with no `settled_at` —
        # `at` is 0.0 for those — so nothing was ever billed for a pair we had
        # never paid. Two tests in `test_operator_duplicates.py` caught it
        # immediately, which is the only reason it is not in this commit.
        # An unstamped settlement still gets billed, once, in the first period;
        # it cannot drag `period_start` down because `first`/`last` only record
        # a truthy `at`, and the boundary that payment sets excludes it
        # afterwards.
        bound = through.get((seller.lower(), resource), 0.0)
        if bound and at <= bound:
            continue
        billed[key] += float(amount)
        consumed[key] += float(qty or 0.0)
        calls[key] += 1
        if at and (key not in first or at < first[key]):
            first[key] = at
        if at > last.get(key, 0.0):
            last[key] = at
        if r.get("unit"):
            units[key] = str(r["unit"])

    out: list[Obligation] = []
    for key in sorted(billed):
        seller, resource = key
        amount = billed[key]
        if amount <= 0:
            continue
        out.append(
            Obligation(
                obligation_id=obligation_key(
                    business.slug, seller, resource, last.get(key, 0.0)
                ),
                vendor=seller,
                # ROUNDED HERE, AND NOWHERE LATER. These are sums of x402
                # nanopayments — 0.004409607843137255 is a real receipt amount —
                # so a period's total is routinely finer than the six decimals
                # USDC actually has. `usdc_units` refuses such a number on the
                # way to the chain, and it is right to: "round it before paying,
                # so the rounding is a decision someone made."
                #
                # The decision is made HERE rather than at payment because
                # `billed_usdc` is what the ledger prints, what the chain
                # commits, and what `ledger_audit` compares `paid_usdc` against.
                # Rounding later would make every payment disagree with its own
                # bill by a fraction of a cent, and the audit would report an
                # error of original entry on every row — correctly.
                billed_usdc=round(amount, 6),
                business=business.slug,
                category=(business.categories or ("general",))[0],
                kind="x402",
                resource=resource,
                vendor_quantity=consumed[key] or None,
                # The window this bill covers, taken from the settlements in it
                # rather than from the clock. `period_start` is the FIRST
                # settlement included, so the meter — which keeps
                # `settled_at >= since` — measures exactly the set that was
                # billed. A boundary invented from `now` would both disagree
                # with the meter and mint a new id every tick.
                period_start=first.get(key, 0.0),
                period_end=last.get(key, 0.0),
                # The unit is what makes two sellers comparable at all.
                unit=units.get(key, ""),
            )
        )
    return out

def obligations_from_entitlements(
    business,
    entitlements: list[dict],
    paid_through: dict[tuple[str, str], float] | None = None,
) -> list[tuple[Obligation, float | None]]:
    """Bills that did not come from the settlement tape — Prior Art #06.

    `obligations_for` reads x402 receipts, which is the only meter this project
    happened to build first. A recurring vendor does not settle through our
    paywall: it sends an invoice for an entitlement — fifty seats, a tier, a
    retainer — and whether that entitlement was *used* lives in somebody else's
    usage export. Both halves are still a bill and a count; only their source
    changes.

    Returns the obligation AND the independent count beside it, because the two
    arrive together and `run_obligation`'s `metered_quantity` seam is what
    carries the second one into check 3. Keeping them as a pair makes it
    impossible to pass a bill without the count that judges it — which is the
    failure the tri-state exists to prevent: omit the count and the meter check
    silently vanishes, invent one and every bill escalates "unmetered".

    Each row needs `payee`, `resource`, `billed_usdc` and the period it covers.
    `billed_quantity` is what the vendor charged for and `used_quantity` is what
    the independent source says happened; `used_quantity` absent is ``None``,
    which means "we could not check" and is never zero.

    The window comes from the BILL, never from the clock, for the reason commit
    `a20d64a` records: a period taken from `now` mints a new id on every tick and
    the same charge gets paid again and again.
    """
    through = paid_through or {}
    out: list[tuple[Obligation, float | None]] = []

    for row in entitlements or ():
        payee = str(row.get("payee") or "").strip()
        resource = resource_path(str(row.get("resource") or ""))
        amount = row.get("billed_usdc")
        if not payee or not resource or not isinstance(amount, (int, float)):
            log.warning("entitlements: skipping a row with no payee, resource or amount")
            continue
        if float(amount) <= 0:
            continue

        period_end = float(row.get("period_end") or 0.0)
        period_start = float(row.get("period_start") or 0.0)
        # Already settled: the boundary is the end of the last period we paid
        # for this pair, exactly as the tape-fed feeder treats it.
        if period_end and period_end <= through.get((payee.lower(), resource), 0.0):
            continue

        billed_q = row.get("billed_quantity")
        used_q = row.get("used_quantity")
        out.append((
            Obligation(
                obligation_id=obligation_key(business.slug, payee, resource, period_end),
                vendor=payee,
                # Rounded here and nowhere later, for `usdc_units`' sake and so
                # the ledger, the chain and the audit all see one figure.
                billed_usdc=round(float(amount), 6),
                business=business.slug,
                category=str(row.get("category") or "")
                or (business.categories or ("general",))[0],
                kind=str(row.get("kind") or "subscription"),
                resource=resource,
                vendor_quantity=float(billed_q) if isinstance(billed_q, (int, float)) else None,
                unit=str(row.get("unit") or ""),
                period_start=period_start,
                period_end=period_end,
                # The three fields an invoice actually has and the tape never
                # did. `due_at` finally gives check 7 something to say.
                due_at=float(row["due_at"]) if isinstance(row.get("due_at"), (int, float)) else None,
                invoice_ref=str(row.get("invoice_ref") or ""),
                early_pay_discount=float(row.get("early_pay_discount") or 0.0),
            ),
            float(used_q) if isinstance(used_q, (int, float)) else None,
        ))

    return out


def settled_through(decisions) -> dict[tuple[str, str], float]:
    """For each ``(vendor, resource)`` this business has really paid, the moment
    its last paid period ended.

    This is where the next bill starts, and it is DERIVED rather than
    configured: the record of what we paid is the only authority on what is
    still owed, exactly as the budget is read from the contract rather than
    tallied beside it.

    KEYED ON THE PAIR, NOT THE ID, and for the reason `ledger_audit._omission`
    gives for the same choice: the id is our naming convention and it has now
    changed twice. The pair is the fact — this seller, this service.

    THE BOOTSTRAP IS THE DANGEROUS PART. The 19 rows in the committed archive
    predate windows, so they carry no `period_end`; falling back to the
    decision's own `at` is the honest reading of them. Those bills summed every
    receipt on the tape at the time they were paid, and a receipt cannot settle
    after the payment that covered it — so "settled through the moment we paid"
    is true of them, and anything that settled later is genuinely new. Without
    the fallback the next run would re-pay all nineteen.

    Only real payments count, for the same reason `settled_refs_from` ignores
    dry runs: a cleared-but-unsent decision has settled nothing, and treating it
    as a period boundary would skip the window it was supposed to pay for.
    """
    out: dict[tuple[str, str], float] = {}
    for d in decisions or ():
        if str(d.get("intent") or "") != PAY:
            continue
        try:
            if float(d.get("paid_usdc") or 0.0) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        key = (
            str(d.get("vendor") or "").lower(),
            resource_path(str(d.get("resource") or "")),
        )
        try:
            end = float(d.get("period_end") or 0.0) or float(d.get("at") or 0.0)
        except (TypeError, ValueError):
            continue
        if end > out.get(key, 0.0):
            out[key] = end
    return out


def settled_refs_from(decisions) -> set[str]:
    """Every reference money has already moved against.

    THE DUPLICATE CHECK HAD NOTHING TO CHECK AGAINST. `decide` has taken a
    `settled_refs` argument since it was written, and the only real caller —
    `scripts/operator_run.py` — never passed one, so check 1 of 8 ran against an
    empty set on every production run. *Agents and Ledgers* names this exact
    failure as an error of original entry: "the wrong amount on both sides, or
    paid twice after a retry". A retry would have been paid twice.

    Derived from the log rather than kept as a counter, for the same reason the
    budget is read from the contract: a tally beside the record is a second
    opinion about the only authoritative thing, and the two drift.

    A DRY RUN IS NOT A SETTLEMENT. Keying on `intent == "pay"` alone would mark
    every cleared-but-unsent decision as paid, and the next real run would
    refuse the very bill it exists to settle — the duplicate check inverted into
    a denial of service. Money has to have actually moved.
    """
    out: set[str] = set()
    for d in decisions or ():
        if str(d.get("intent") or "") != PAY:
            continue
        try:
            if float(d.get("paid_usdc") or 0.0) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        # Both halves, because `is_duplicate` matches either: our own id for a
        # retried call, the vendor's ref for a re-sent invoice.
        for key in ("obligation_id", "invoice_ref"):
            v = str(d.get(key) or "").strip()
            if v:
                out.add(v)
    return out


def is_duplicate(ob: Obligation, settled_refs: set[str]) -> bool:
    """Have we already paid this?

    Matched on the obligation id AND the vendor's own reference, because the two
    duplicate shapes differ: a re-sent invoice keeps the vendor's ref and gets a
    new id from us, while a retried call keeps our id. Checking one catches half.
    """
    if ob.obligation_id and ob.obligation_id in settled_refs:
        return True
    return bool(ob.invoice_ref) and ob.invoice_ref in settled_refs


# --- the decision ----------------------------------------------------------


def decide(
    ob: Obligation,
    *,
    par: Par | None = None,
    screen: CounterpartyVerdict | None = None,
    metered_quantity: float | None = None,
    #: The agreement this bill was made under, if there is one. Injected like
    #: `par` and `screen` rather than loaded, so `decide` stays pure and a test
    #: can put a known agreement in front of it.
    commitment=None,
    #: What the wallet holds against what is dated, if anybody measured it.
    #: Injected like `par`, `screen` and `commitment` — the balance is a chain
    #: read and the obligations are the caller's set, and `decide` is pure.
    liquidity=None,
    settled_refs: set[str] | None = None,
    remaining_usdc: float | None = None,
    per_tx_limit_usdc: float | None = None,
    now: float | None = None,
    meter_tolerance: float = METER_TOLERANCE,
    unbenchmarked_max_usdc: float = UNBENCHMARKED_MAX_USDC,
    pay_window_s: float = PAY_WINDOW_S,
    material_bp: float = MATERIAL_BP,
) -> ObligationDecision:
    """One obligation in, one record out. Pure."""
    t = now if now is not None else time.time()
    refs = settled_refs or set()

    d = ObligationDecision(
        at=t,
        obligation_id=ob.obligation_id,
        vendor=ob.vendor,
        category=ob.category,
        billed_usdc=ob.billed_usdc,
        business=ob.business,
        resource=ob.resource,
        intent=REFUSE,
        rule="",
        metered_quantity=metered_quantity,
        vendor_quantity=ob.vendor_quantity,
        period_start=ob.period_start,
        period_end=ob.period_end,
        due_at=ob.due_at,
        invoice_ref=ob.invoice_ref,
        kind=ob.kind,
        unit=ob.unit,
        early_pay_discount=ob.early_pay_discount,
        remaining_usdc=remaining_usdc,
        per_tx_limit_usdc=per_tx_limit_usdc,
        held_usdc=getattr(liquidity, "held_usdc", None),
        due_usdc=getattr(liquidity, "due_usdc", None),
        liquidity_horizon_s=getattr(liquidity, "horizon_s", None),
        meter_tolerance=meter_tolerance,
        unbenchmarked_max_usdc=unbenchmarked_max_usdc,
        pay_window_s=pay_window_s,
        material_bp=material_bp,
    )

    # 1 — already paid. First, because every later check would be work done on
    # behalf of a payment that must not happen.
    if is_duplicate(ob, refs):
        d.intent, d.rule = REFUSE, "duplicate: this reference has already settled"
        return d

    # 2 — payable at all.
    if not _payable_to(ob.vendor):
        d.intent, d.rule = REFUSE, "no payee: nothing to pay, and no one to pay it to"
        return d
    if ob.billed_usdc <= 0:
        d.intent, d.rule = REFUSE, "no amount: a bill for nothing is not a bill"
        return d

    # 3 — the meter, before the price. A bill for work nobody did is not a
    # pricing question.
    if ob.vendor_quantity is not None:
        if metered_quantity is None:
            d.intent, d.escalated = ESCALATE, True
            d.recommended_intent = REFUSE
            d.rule = "unmetered: we hold no record of consuming this, so we cannot check the bill"
            return d
        d.discrepancy = ob.vendor_quantity - metered_quantity
        if ob.vendor_quantity:
            d.discrepancy_usdc = round(
                d.discrepancy * (ob.billed_usdc / ob.vendor_quantity), 6
            )
        allowed = abs(metered_quantity) * meter_tolerance
        if d.discrepancy > allowed:
            d.intent, d.escalated = ESCALATE, True
            d.recommended_intent = REFUSE
            # THE MONEY IN THE SENTENCE, because the sentence is what a person
            # reads. "They billed 50, we counted 12" is the finding; "76 USDC of
            # this bill is for something that did not happen" is the reason
            # anybody acts on it.
            d.rule = (
                f"metered below billed: they billed {ob.vendor_quantity:g}, "
                f"we counted {metered_quantity:g}"
                + (f" — {d.discrepancy_usdc:g} USDC of this bill is for something "
                   "that did not happen" if d.discrepancy_usdc else "")
            )
            return d
        if d.discrepancy < -allowed:
            d.notes.append(
                f"they billed {ob.vendor_quantity:g} under our count of {metered_quantity:g}"
            )

    # 4 — the counterparty. Before the price, because a vendor we may not pay is
    # not a pricing question, and "over budget" is the wrong headline for a
    # sanctioned address. RFB 2 wants screening "built into the path"; this is
    # the path.
    if screen is not None:
        d.screen_risk = screen.risk
        d.screen_matched = list(screen.matched)
        d.screen_backend = getattr(screen, "backend", "")
        d.screen_reason = getattr(screen, "reason", "") or ""
        d.screen_screened = bool(getattr(screen, "screened", True))
        # The environment variable that decides whether `unknown` blocks. It
        # changes this branch and left no trace in the record at all, so a
        # replay could reach the opposite verdict and look correct.
        from .counterparty import REQUIRED as _screen_required

        d.screen_required = bool(_screen_required)
        if not screen.payable:
            d.intent, d.escalated = ESCALATE, True
            d.recommended_intent = REFUSE
            where = ", ".join(screen.matched) if screen.matched else screen.backend
            d.rule = (
                f"counterparty {screen.risk}: {where} — not a payment the agent makes"
            )
            return d
        if screen.risk == UNKNOWN:
            # Recorded on the decision, not swallowed. An unscreened payment is
            # a payment somebody should be able to find later.
            d.notes.append(f"not screened: {screen.reason}")
    else:
        d.notes.append("no counterparty screen was offered for this decision")

    # 4b — THE AGREEMENT, when there is one. Prior Art #03, the symbolon.
    #
    # Before the market, because an agreement is more specific than a market: if
    # we wrote down what this vendor would charge for this service, that is the
    # thing to hold them to, and the going rate elsewhere is somebody else's
    # business. A reroute would be the wrong answer here — we are not shopping,
    # we are honouring a commitment we made.
    #
    # AND BECAUSE IT UNBLOCKS THE LADDER. Check 5 escalates any bill it cannot
    # price above `unbenchmarked_max_usdc`, and a benchmark needs two
    # independent sellers of the same unit — which a contractor's hourly rate
    # and a SaaS seat price can never have. A market needs competitors; an
    # agreement needs none, because it is what we agreed. So the price question
    # now has three answers rather than two, and the ceiling is the last resort
    # instead of the only one.
    priced_quantity = (
        metered_quantity
        if metered_quantity is not None and metered_quantity > 0
        else ob.vendor_quantity
    )
    agreed = None
    if commitment is not None:
        from .commitments import assess as assess_commitment

        agreed = assess_commitment(commitment, ob.billed_usdc, priced_quantity, t)
        d.commitment_id = str(agreed.get("commitment_id") or "")
        d.commitment_hash = str(agreed.get("commitment_hash") or "")
        d.commitment_verdict = str(agreed.get("verdict") or "")
        d.over_commitment_usdc = agreed.get("over_usdc")
        if not agreed["matched"]:
            d.intent, d.escalated = ESCALATE, True
            d.recommended_intent = REFUSE
            over = agreed.get("over_usdc")
            d.rule = _commitment_rule(agreed, over)
            return d
        d.notes.append(
            f"within the agreement {d.commitment_id}"
            + (f" (at most {agreed['agreed_total_usdc']:g} USDC)"
               if agreed.get("agreed_total_usdc") is not None else "")
        )

    # 5 and 6 — the price, against observed quotes for the same service.
    # OUR OWN count is the denominator when we have one: "we paid this much for
    # what we actually took". The meter above has already escalated any material
    # overstatement, so by here the two agree; where they differ within
    # tolerance, our count is the one we can defend.
    verdict = (
        assess(ob.billed_usdc, par, material_bp, quantity=priced_quantity)
        if par is not None
        else None
    )
    if verdict is None or not verdict["benchmarked"]:
        reason = (verdict or {}).get("reason") or "NO_PAR"
        # AN AGREEMENT IS A BASIS, SO THE CEILING DOES NOT APPLY.
        #
        # This is the point of check 4b. The ceiling exists because a price
        # nobody can check should not be paid at size on the agent's own
        # authority — but a bill inside an agreement we wrote down HAS been
        # checked, against the better of the two standards. Without this branch
        # a 140 USDC milestone escalates for being unpriceable while sitting
        # exactly inside the contract that priced it, which is the ladder
        # refusing to read its own evidence.
        if agreed is not None and agreed.get("matched"):
            d.notes.append(
                f"no market for this ({reason}), priced against the agreement instead"
            )
        elif ob.billed_usdc > unbenchmarked_max_usdc:
            d.intent, d.escalated = ESCALATE, True
            # No opinion on the price, so the recommendation is to wait for one
            # rather than to refuse a bill that may be perfectly fair.
            d.recommended_intent = HOLD
            d.rule = (
                f"unbenchmarked ({reason}) and above the "
                f"{unbenchmarked_max_usdc:g} USDC ceiling for a price we cannot check"
            )
            return d
        d.notes.append(f"unbenchmarked ({reason}), under the ceiling")
    else:
        d.par_usdc = verdict["par_usdc"]
        d.best_usdc = verdict["best_usdc"]
        d.over_par_bp = verdict["over_par_bp"]
        d.saving_usdc = verdict["saving_usdc"]
        # Computed by `assess` and dropped here until now. It is the gap in
        # DOLLARS rather than basis points — the same fact the bp figure
        # carries, in the unit a reader actually budgets in.
        d.over_par_usdc = verdict.get("over_par_usdc")
        d.par_denomination = str(verdict.get("denomination") or "")
        d.par_sellers = verdict.get("sellers")
        d.par_reason = str(verdict.get("reason") or "")

        # AN AGREEMENT IS NOT SHOPPED. If this bill sits inside a commitment we
        # made, the market comparison above is still RECORDED — the figures are
        # on the row, and what renegotiating would be worth is exactly what an
        # owner needs to know — but it does not reroute. Breaking a written
        # agreement to chase a price is a business decision with consequences
        # the agent cannot see, so it reports the opportunity and honours the
        # commitment. My own test caught this: with a rival 6000 bp cheaper the
        # ladder rerouted away from a vendor we had committed to.
        if agreed is not None and agreed.get("matched"):
            if verdict["verdict"] == "over_par" and verdict.get("saving_usdc"):
                d.notes.append(
                    f"a cheaper offer exists at {verdict['best_usdc']:g} — worth "
                    f"{verdict['saving_usdc']:g} USDC if {d.commitment_id} is renegotiated"
                )
        elif verdict["verdict"] == "over_par":
            cheaper = verdict["best_seller"]
            # A MARKET REFERENCE IS NOT A PAYEE. The anchor basket names models
            # — `openai/gpt-4o-mini` — because that is what a reader can check,
            # and nothing here can send USDC to one. Writing it into
            # `reroute_to` would record a routing the wallet cannot execute, and
            # `applyReroute` downstream would be handed a string to pay.
            #
            # Tested on the SOURCE, not the shape of the address: `_payable_to`
            # answers "is there a counterparty at all" and deliberately accepts
            # a named vendor, so it says yes to `openai/gpt-4o-mini`. The
            # narrower question — can USDC reach it — is answered by where the
            # price came from.
            #
            # The FINDING is still real and still the agent's to report: the
            # bill is materially above the published market rate for its own
            # unit, among sellers of its own model class. What is NOT the
            # agent's is what to do about it with no alternative it can act on —
            # RFB 4's "know which decisions are not its to make", arriving for
            # once on a priced bill rather than merely a large one.
            if cheaper and verdict.get("best_source") == "market":
                # AND THE QUESTION CHANGES WITH THE COMPARISON. Everywhere else
                # the verdict keys off `saving_bp`, measured against the
                # CHEAPEST offer, because that is money genuinely available
                # somewhere we can send it. Against a market basket nothing is
                # available anywhere — so the only meaningful question is
                # whether the bill is above the going RATE, which is
                # `over_par_bp` against the median.
                #
                # Measured, which is why this is here: a bill priced at exactly
                # the market median still escalated, because gpt-4o-mini is
                # cheaper than gpt-4.1-mini. Both are mid-class and the basket
                # medians them on purpose; demanding the cheapest row is
                # demanding a different model, not a better price.
                over_bp = verdict.get("over_par_bp") or 0.0
                if over_bp >= material_bp:
                    d.intent, d.escalated = ESCALATE, True
                    d.recommended_intent = REFUSE
                    d.rule = (
                        f"{over_bp:.0f} bp above the going market rate of "
                        f"{verdict['par_usdc']:g} across {verdict['sellers']} published "
                        f"prices, and the cheaper ones are references rather than "
                        f"addresses we can pay"
                    )
                    return d
                # At or under the going rate. The cheaper row is noted and not
                # acted on, because acting on it is not available.
                d.notes.append(
                    f"at or under the going market rate; {verdict['best_usdc']:g} is "
                    f"published by {cheaper}, which is not an address we can pay"
                )
            # `elif`, NOT a second `if`. Falling out of the market branch used to
            # drop straight into the reroute below, so a bill priced exactly at
            # the going rate was rerouted to `openai/gpt-4o-mini` — the one
            # outcome the branch above exists to make impossible. The market
            # case is handled in full or not at all.
            #
            # A cheaper seller who is the vendor itself is not a reroute; `par`
            # already excludes the biller, so reaching here means a real third
            # party is offering it for less.
            elif cheaper and cheaper.lower() != ob.vendor.lower():
                d.intent, d.reroute_to = REROUTE, cheaper
                # Measured against the CHEAPEST offer, not the median, because
                # that is the comparison the decision was made on. Saying "over
                # par by N bp" here printed "over par by -246 bp" on real data:
                # a price can sit below the median and still have money
                # available below it, and a rule that contradicts itself is
                # worse than one that is merely terse.
                d.rule = (
                    f"{verdict['saving_bp']:.0f} bp above the cheapest offer: "
                    f"{verdict['best_usdc']:g} is available, saving "
                    f"{verdict['saving_usdc']:g} USDC"
                )
                return d
            else:
                # EXPLICIT, because this used to be reached by falling out of
                # the two branches above and that is how the market case ended
                # up here: priced at exactly the going rate, nothing to reroute
                # to, and escalated with a sentence about the biller that was
                # not true of it. An `else` cannot be fallen into by accident.
                d.intent, d.escalated = ESCALATE, True
                d.recommended_intent = REFUSE
                d.rule = (
                    f"{verdict['saving_bp']:.0f} bp above the cheapest offer, "
                    "and that seller is the one billing us"
                )
                return d

    # 7 — the timing. Paying early costs the cash; paying late costs the
    # discount. Only one of those is recoverable, so the discount wins when it
    # exists and the calendar wins when it does not.
    if ob.due_at is not None and ob.early_pay_discount <= 0:
        if ob.due_at - t > pay_window_s:
            d.intent = HOLD
            d.rule = (
                f"not due for {int((ob.due_at - t) / 86_400)} days and no early-pay "
                "discount, so the cash is worth more here"
            )
            return d
    elif ob.early_pay_discount > 0:
        d.notes.append(f"paying early for a {ob.early_pay_discount * 100:g}% discount")

    # 8 — the budget. Last, because it is the only check whose answer the
    # business can change by deciding to, and because PolicyWallet enforces it
    # again on chain regardless of what this function concluded.
    if per_tx_limit_usdc is not None and ob.billed_usdc >= per_tx_limit_usdc:
        d.intent, d.escalated = ESCALATE, True
        # Everything else cleared. This is the one escalation where the agent's
        # recommendation is to PAY: the bill is sound, the authority is not.
        d.recommended_intent = PAY
        d.rule = (
            f"{ob.billed_usdc:g} USDC is at or above the "
            f"{per_tx_limit_usdc:g} per-payment limit, so the owner signs this one"
        )
        return d
    if remaining_usdc is not None and ob.billed_usdc > remaining_usdc:
        d.intent, d.escalated = ESCALATE, True
        # Not refuse: the bill may be fine and the period rolls. Waiting is the
        # honest recommendation when the only thing missing is money.
        d.recommended_intent = HOLD
        d.rule = (
            f"over budget: {ob.billed_usdc:g} USDC against {remaining_usdc:g} left "
            f"in {ob.category} this period"
        )
        return d

    # 9 — the cash. AFTER the budget, because a bill that breaches its own
    # limit should say so rather than blame the balance; and because the two are
    # genuinely different questions, with different answers on a Monday morning.
    # A budget is permission (`cap - spent`, from the contract's counters) and
    # reads healthy on an empty wallet. This is money.
    #
    # Not a duplicate of check 7. That rung is the CALENDAR — "not due for N
    # days, so the cash is worth more here" — and it holds a bill we can afford
    # and need not pay yet. This one holds a bill we cannot afford to pay
    # without stranding something dated sooner.
    if liquidity is not None and liquidity.measured:
        short = liquidity_short_by(liquidity, ob.billed_usdc)
        if short > 0:
            d.intent, d.escalated = ESCALATE, True
            # The same shape as the period cap above, and the same reason: the
            # bill may be perfectly sound and the owner may fund the wallet.
            # Waiting is the honest recommendation when the only thing missing
            # is money.
            d.recommended_intent = HOLD
            # TWO SITUATIONS, and one sentence for each. A single phrasing
            # blamed the dated queue in both, and in the sharper of the two
            # that queue can be EMPTY: a wallet holding 0.5 against a 10 USDC
            # bill reports "0 dated in the next 30 days, so paying 10 would
            # leave 9.5 short", which is arithmetically true and names the
            # wrong cause. The bill is simply larger than the balance and the
            # transfer would revert; nothing is waiting behind it.
            if liquidity.held_usdc < ob.billed_usdc:
                d.rule = (
                    f"the wallet holds {liquidity.held_usdc:g} USDC and this bill is "
                    f"{ob.billed_usdc:g}, so the payment would revert on chain"
                )
            else:
                d.rule = (
                    f"not enough to cover what is due: {liquidity.held_usdc:g} USDC held "
                    f"against {liquidity.due_usdc:g} dated in the next "
                    f"{int(liquidity.horizon_s / 86_400)} days, so paying "
                    f"{ob.billed_usdc:g} would leave {short:g} short"
                )
            return d
    else:
        # Said rather than skipped. Check 4 leaves "no counterparty screen was
        # offered" for exactly this reason, and check 3's silent guard is the
        # asymmetry this file's own docstring now admits to: an absent check
        # that leaves no trace is indistinguishable from a check that passed.
        #
        # `else`, NOT `elif liquidity is None`, which is what this said first
        # and which left no note on the one path it was written for. The keeper
        # and the CLI always pass a `Liquidity`; a wallet that would not answer
        # makes it `Liquidity(held_usdc=None, …)`, which is not `None`. So in
        # production — the only place a chain read can actually fail — the
        # branch that explains the silence was unreachable, and a `pay` written
        # with no balance read looked exactly like one written with a healthy
        # balance. `None` happens only when `decide` is called without the
        # argument at all, which is tests and the sandbox generator.
        d.notes.append("no balance was read, so nothing checked this against cash")

    d.intent = PAY
    d.rule = _pay_rule(d, verdict, material_bp)
    return d


def _commitment_rule(agreed: dict, over: float | None) -> str:
    """Why this bill did not fit the agreement, in one line.

    Each failure gets its own sentence because each calls for a different
    conversation: a price above what was agreed is a renegotiation, a quantity
    above it is an over-delivery, and a bill outside the window is an agreement
    that has run out. "Does not match" would collapse three different problems
    into one shrug.
    """
    from .commitments import OUTSIDE_WINDOW, OVER_QUANTITY, OVER_TOTAL, OVER_UNIT_PRICE

    cid = agreed.get("commitment_id") or "the agreement"
    by = f" by {over:g} USDC" if isinstance(over, (int, float)) else ""
    verdict = agreed.get("verdict")
    if verdict == OUTSIDE_WINDOW:
        return f"outside the window {cid} covers, so there is no agreement to pay under"
    if verdict == OVER_TOTAL:
        return (
            f"over the {agreed['agreed_total_usdc']:g} USDC we agreed in {cid}{by}"
        )
    if verdict == OVER_QUANTITY:
        return f"more than the quantity {cid} agreed to{by}"
    if verdict == OVER_UNIT_PRICE:
        return (
            f"dearer per unit than the {agreed['agreed_unit_price_usdc']:g} "
            f"we agreed in {cid}{by}"
        )
    return f"does not match {cid}"


def _pay_rule(
    d: ObligationDecision, verdict: dict | None, material_bp: float = MATERIAL_BP
) -> str:
    """Why this payment was made, in one line a reviewer can act on.

    TAKES THE THRESHOLD IT IS JUDGED ON. This read the module global while
    `decide()` compared against its own `material_bp` argument, so a caller
    passing a different threshold got the verdict computed on their value and
    the sentence computed on 25.0 — the number and the prose disagreeing about
    the same decision, which is the one thing a record must never do.

    AND IT DOES NOT CLAIM A BUDGET IT NEVER READ. Both sentences used to end
    ", inside budget" unconditionally. But check 8 only runs when a per-payment
    limit or a period remainder was supplied, and `operator_keeper._pass`
    passes `policy=None` in `dry` mode — so every cleared bill on the scheduled
    path, and every business with no wallet, was recorded asserting a budget
    check that had not happened. `d` was an unused parameter at the time, which
    is the tell: the one object carrying the answer was in scope and ignored.
    """
    # `None` on both is exactly the condition check 8 skips on, so it is the
    # honest test for "a budget was consulted".
    budget = (
        ", inside budget"
        if d.per_tx_limit_usdc is not None or d.remaining_usdc is not None
        else ", and no budget was consulted"
    )
    if verdict and verdict.get("benchmarked"):
        bp = verdict["over_par_bp"]
        where = "at par" if abs(bp) < material_bp else f"{bp:+.0f} bp against par"
        return f"{where} on {verdict['sellers']} observed sellers{budget}"
    return f"unbenchmarked but under the ceiling{budget}"


# --- the driver ------------------------------------------------------------
# Impure, and kept thin on purpose: everything worth arguing with lives in
# `decide` above, where a test can reach it. `hedger.py` is the precedent for
# holding the pure helpers and the loop in one file.


def unpayable_vendor(vendor: str, *, agent: str | None, chain_id: int) -> str | None:
    """Why a PAY to ``vendor`` must not be sent, or None.

    Two payees turn a payment into something else, and nothing upstream refused
    either (found wiring the mainnet PolicyWallet, 2026-10-16):

    - THE AGENT ITSELF, on any chain. A payment from the wallet to its own spender
      moves the budget into the spender's pocket; on mainnet `/prints` bills are
      paid to ACR_X402_PAY_TO, which is the press EOA that IS the agent.
    - A DEMO SELLER, on mainnet. Their keys are sha256("acr-attest::" + label),
      so anyone can derive them and sweep what is sent. On testnet that fleet is
      the market being exercised, so it stays payable there.
    """
    v = (vendor or "").lower()
    if agent and v == agent.lower():
        return "the payee is the agent itself, so paying it would move the budget into the spender's own wallet"
    from acr_core.mainnet_guard import MAINNET_CHAIN_ID

    if int(chain_id) == MAINNET_CHAIN_ID:
        from acr_oracle_client.demo_sellers import DEMO_SELLERS

        if v in {d.address.lower() for d in DEMO_SELLERS}:
            return ("the payee is a demo seller whose key is derivable from its public label, "
                    "so mainnet USDC sent there could be swept by anyone")
    return None


def run_obligation(
    ob: Obligation,
    *,
    receipts: list[dict],
    catalog: dict,
    policy=None,
    screen=None,
    #: The agreement this bill was made under. ``None`` means "look it up on
    #: the register"; pass ``False`` to say there is deliberately none, which is
    #: what a test wants when it is asking about the market instead.
    commitment=None,
    #: An independent count, when the meter cannot produce one from the
    #: settlement tape. A timesheet or a seat export is a meter; the tape is
    #: only the meter we happened to build first. Keeps the tri-state: ``None``
    #: means "we could not check", ``0.0`` means "we checked and it was zero".
    metered_quantity: float | None = None,
    #: The cash picture for this pass. Supplied rather than read here, because
    #: `due_usdc` is a sum over the OTHER obligations and this function sees
    #: one — so only the caller holding the set can answer it.
    liquidity=None,
    settled_refs: set[str] | None = None,
    since: float = 0.0,
    now: float | None = None,
    dry_run: bool = True,
    log_path: str | None = None,
) -> ObligationDecision:
    """One obligation, all the way through: meter, price, decide, maybe pay.

    ``dry_run`` defaults to TRUE, matching ``ops_actions``: the operator's
    default behaviour is to say what it would do. A caller that wants money to
    move has to say so, and the audit row exists either way.

    ``policy`` is a ``PolicyClient`` or anything with ``budget(category)`` and
    ``spend(category, to, amount, record)``. Injected rather than constructed so
    the whole path is testable without a chain — and so a business's own wallet
    can be passed in, which is the point of one wallet per business.

    The budget is read from the CONTRACT, not from a local tally. A local tally
    is a second opinion about the only thing that is authoritative, and the two
    drift the moment anything else spends from the same wallet.
    """
    from .par import (
        is_first_party,
        market_basket,
        par_from_quotes,
        quotes_by_unit,
        quotes_from_catalog,
        quotes_from_receipts,
    )
    from .payments import owner_inbox_business

    owner_inbox = owner_inbox_business(ob.business)
    if owner_inbox:
        from .statement import pending_escalations, read_decisions

        previous = read_decisions(path=log_path, business=ob.business, limit=0)
        pending = next((r for r in pending_escalations(previous)
                        if r.get("obligation_id") == ob.obligation_id), None)
        if pending:
            # Do not replace or resolve an obligation while its owner reviews it.
            return ObligationDecision(**{k: v for k, v in pending.items()
                                         if k in ObligationDecision.__dataclass_fields__})

    t = now if now is not None else time.time()

    # AN INJECTED COUNT WINS, because the tape is only the meter we built
    # first. A contractor's timesheet and a seat export are meters too, and
    # without this seam check 3 could never run on either: omit the quantity and
    # the meter silently vanishes, supply it and every bill escalates
    # "unmetered".
    metered = (
        metered_quantity
        if metered_quantity is not None
        else meter_quantity(receipts, ob.vendor, ob.resource, since)
    )

    # The agreement, looked up unless the caller has already answered. `False`
    # is "there is deliberately none" and `None` is "go and look" — the
    # distinction a test needs and a production caller never passes.
    agreement = commitment
    if agreement is None:
        from .commitments import covering

        try:
            agreement = covering(ob.business, ob.vendor, ob.resource, t)
        except Exception as exc:  # noqa: BLE001 - a register must not stop a decision
            log.warning("operator: the commitment register was unreadable (%s)", exc)
            agreement = None
    elif agreement is False:
        agreement = None

    # Screened on every run, not once at onboarding. Prior Art #07's whole point
    # is that a static list "was out of date the moment it was carved"; a verdict
    # fetched per decision is the version that can change its mind.
    if screen is None:
        from .counterparty import get_screen

        screen = get_screen()
    verdict = screen.check(ob.vendor) if screen is not None else None

    if ob.unit:
        # The unit is the market. Per-unit prices make two sellers of the same
        # kind of service comparable at last, which per-call prices never were:
        # a 500-token call and a 2,000-token one are not the same purchase.
        #
        # WHICH MARKET, THOUGH. Two price populations exist for the same unit
        # and they are two orders of magnitude apart, so medianing them together
        # would produce exactly the incomparable number this module's docstring
        # refuses:
        #
        #   the fleet      sellers this deployment operates, priced from
        #                  `IndexSpec.reference_level` — which `anchors/GAP.md`
        #                  measures 10x to 1250x above real market prices.
        #   the market     real published list prices, fetched from their own
        #                  sources with a URL per row, in `anchors/`.
        #
        # The BILL decides. A fleet seller's invoice is like-for-like against
        # fleet quotes, both pinned to the same scale, and that comparison is
        # sound inside the loop. Any other vendor is a real invoice, and the
        # market basket is the only reference that can price it — which is the
        # whole of RFB 3's "what the market is actually paying".
        basket = market_basket(ob.unit, now=t)
        if is_first_party(ob.vendor) or not basket.usable:
            par = par_from_quotes(
                ob.unit,
                quotes_by_unit(receipts, ob.unit),
                exclude_seller=ob.vendor,
                denomination="unit",
            )
        else:
            par = par_from_quotes(
                ob.unit,
                list(basket.quotes),
                exclude_seller=ob.vendor,
                denomination="unit",
            )
    else:
        quotes = quotes_from_catalog(catalog, ob.resource) + quotes_from_receipts(
            receipts, ob.resource
        )
        par = par_from_quotes(ob.resource, quotes, exclude_seller=ob.vendor)

    remaining = per_tx = None
    paid_via = ""
    if policy is not None:
        try:
            paid_via = str(policy.signer_kinds().get("agent") or "")
        except Exception:  # pragma: no cover - a reporting field must not block a payment
            paid_via = ""
        budget = policy.budget(ob.category)
        if budget is None:
            # No budget on chain means no authority, and that is not the same as
            # a budget of zero: zero would read as "this category is exhausted
            # this period", which a human would wait out rather than fix.
            d = ObligationDecision(
                at=now if now is not None else time.time(),
                obligation_id=ob.obligation_id,
                vendor=ob.vendor,
                category=ob.category,
                billed_usdc=ob.billed_usdc,
                business=ob.business,
                resource=ob.resource,
                intent=ESCALATE,
                rule=f"no budget on chain for {ob.category}, so the agent has no authority here",
                escalated=True,
                metered_quantity=metered,
                vendor_quantity=ob.vendor_quantity,
                due_at=ob.due_at,
                invoice_ref=ob.invoice_ref,
                # No `recommended_intent` ON PURPOSE. Without a budget the agent
                # has no authority to form an opinion, and inventing one here
                # would put an agreement into the numerator that nobody made.
            )
            log_decision(d, log_path)
            return d
        remaining = budget["remaining_usdc"]
        per_tx = budget["per_tx_limit_usdc"]

    # Carried onto the decision before `as_record()` hashes it.
    d = decide(
        ob,
        par=par,
        screen=verdict,
        metered_quantity=metered,
        commitment=agreement,
        liquidity=liquidity,
        settled_refs=settled_refs,
        remaining_usdc=remaining,
        per_tx_limit_usdc=per_tx,
        # The clock resolved ONCE above and shared, so the agreement the
        # register was asked about is the one the decision is judged against —
        # an agreement cannot expire between the lookup and the verdict.
        now=t,
    )

    d.paid_via = paid_via
    if owner_inbox and d.intent == PAY:
        d.recommended_intent = PAY
        d.intent = ESCALATE
        d.escalated = True
        d.rule = "Owner-wallet inbox: explicit owner authorization required"
    if d.intent == PAY:
        from acr_core import get_settings

        try:  # a Circle signer resolves its address over the network; the check must not break a decision
            agent = getattr(getattr(policy, "agent_signer", None), "address", None)
        except Exception:
            agent = None
        why = unpayable_vendor(ob.vendor, agent=agent, chain_id=int(get_settings().arc_chain_id))
        if why:
            d.recommended_intent = PAY
            d.intent = ESCALATE
            d.escalated = True
            d.rule = why
    if d.intent == PAY and policy is not None and not dry_run and not getattr(policy, "configured", lambda: True)():
        # The budget is a public read now, so a host without the agent key can
        # reach a PAY. Saying so beats a spend() that cannot be signed.
        d.recommended_intent = PAY
        d.intent = ESCALATE
        d.escalated = True
        d.rule = "no agent key on this host, so it cannot sign the payment"
    if d.intent == PAY and policy is not None:
        if dry_run:
            d.notes.append("dry run: cleared policy, no payment sent")
        else:
            # The record is hashed into the payment, so the commitment to the
            # reasoning is made in the same transaction that moves the money.
            d.tx = policy.spend(ob.category, ob.vendor, ob.billed_usdc, d.as_record())
            d.paid_usdc = ob.billed_usdc

    log_decision(d, log_path)
    return d
