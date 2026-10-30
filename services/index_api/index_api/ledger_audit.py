"""The six errors a trial balance cannot see.

*Agents and Ledgers* — the analysis this hackathon hands every team, three times,
on three slides — makes one argument and names six consequences:

    "A ledger checks that debits equal credits. That equality is the ledger's
    one built-in check and nearly every mistake an LLM can make with money
    passes it."

    omission             a transaction nobody recorded
    commission           the right amount in the wrong account of the same kind
    principle            the right amount in the wrong KIND of account
    original entry       the wrong amount on both sides
    compensating         two mistakes that cancel
    complete reversal    the debit and the credit swapped

    "After each one, debits still equal credits."

And it says where the controls live: **outside the ledger** — reconciliation
against the bank, the match of an invoice to an order and a receipt, change
control on vendor bank details, a cutoff at period end.

`ledger_export.py` already answers that charge in prose: every transaction it
writes carries the rule that produced it, what we independently metered, the par
it was checked against, what the counterparty screen said, and the hash the
chain holds. But prose on a row is an invitation to check, not a check. This
module does the checking, so "we looked" is a result instead of a claim.

WHAT MAKES THESE CHECKS WORTH ANYTHING is that none of them reads the ledger
alone. Omission compares the decision log against the settlement tape — our
bank statement, written by the seller, not by us. Commission asks whether the
payee ever served us. Principle asks the registry, not the exporter. That is the
essay's own prescription: the control has to come from outside the thing it is
checking, or it is the trial balance again under a new name.

EVERY CHECK REPORTS WHAT IT SEARCHED. A check that looked at nothing and found
nothing is indistinguishable from a clean book, and this repo has shipped that
bug before — an audit walker that skipped empty lists, so `notes: []` was never
examined and the ledger of exemptions said it was fine.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass

from .operator import PAY, REROUTE, obligation_key, resource_path

#: The essay's six, in its order. Named here so the output cannot quietly grow a
#: seventh or lose one: the module's whole claim is that it checks all six.
ERRORS = (
    "omission",
    "commission",
    "principle",
    "original entry",
    "compensating",
    "complete reversal",
)

#: NOT ONE OF THE SIX, and kept apart on purpose.
#:
#: The essay's list is a list of errors a *trial balance* cannot see. This one is
#: the error it says nobody catches at all: "SolidInvoice: the most complete
#: write path, and **no way to disprove a phantom payment**", and "fictitious
#: entries, where you booked a payment the bank never made".
#:
#: We could produce one. A `PolicyWallet` address is read with whatever RPC the
#: press holds; on a chain where it has no code a CALL neither reverts nor fails
#: to estimate, so a transaction broadcasts, returns `status: 1`, and gets
#: written down as a payment with a hash as its evidence. `PolicyClient` now
#: refuses that outright — this is the check that looks for one anyway, because
#: a control nobody audits is a control nobody can show you.
PHANTOM = "phantom payment"

#: Two quantities that disagree by less than this are the same quantity. Matches
#: `operator.METER_TOLERANCE`'s intent: a meter is not a promise of exactness.
CANCEL_TOLERANCE_USDC = 1e-6


@dataclass(frozen=True)
class Finding:
    """One thing that balances and is still wrong."""

    error: str
    #: What a reader should go and look at. Empty when the finding is about the
    #: period rather than a single row.
    obligation_id: str
    detail: str


@dataclass(frozen=True)
class Check:
    """One of the six, run. `searched` is what makes `found: 0` mean anything."""

    error: str
    question: str
    searched: int
    found: int


def _f(v) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _paid(d: dict) -> bool:
    """Money actually moved. A dry run that cleared policy did not pay."""
    amt = _f(d.get("paid_usdc"))
    return str(d.get("intent") or "") == PAY and amt is not None and amt > 0


# --- 1 · omission -----------------------------------------------------------


def _omission(decisions: list[dict], receipts: list[dict], treasury: str, slug: str):
    """A settlement the seller recorded and we did not.

    The receipts tape is the closest thing we have to a bank statement: it is
    written by the counterparty, at settlement, and we cannot edit it. The essay
    puts exactly this first — "Bank reveals payments never recorded".

    MATCHED ON (seller, resource), NOT ON THE ID. The id is our naming
    convention and it has already changed once; keying on it made every receipt
    look uncovered the moment the convention improved, which is a check that
    reports on itself rather than on the books. The pair is the fact: this
    seller, this service, bought by this treasury.

    A receipt with no `seller` cannot be attributed to any obligation, so it is
    reported as UNATTRIBUTABLE rather than counted clean. Four of the rows
    payable by the fleet's treasury are that shape, and silently treating them
    as fine would be the omission this check exists to find.
    """
    covered = {
        (str(d.get("vendor") or "").lower(), resource_path(str(d.get("resource") or "")))
        for d in decisions
    }
    seen: dict[tuple[str, str], dict] = {}
    unattributable = 0
    t = (treasury or "").lower()

    for r in receipts:
        if str(r.get("payer") or "").lower() != t:
            continue
        seller = str(r.get("seller") or "")
        path = resource_path(str(r.get("resource") or ""))
        if not seller or not path or path == "/":
            unattributable += 1
            continue
        row = seen.setdefault((seller.lower(), path), {"calls": 0, "usdc": 0.0, "seller": seller})
        row["calls"] += 1
        row["usdc"] += _f(r.get("amount_usdc")) or 0.0

    found = [
        Finding(
            "omission",
            obligation_key(slug, v["seller"], path),
            f"{v['calls']} settlement(s) worth {v['usdc']:.6f} USDC on {path} "
            f"from {v['seller']} are on the tape with no decision covering them",
        )
        for (_seller, path), v in sorted(seen.items())
        if (_seller, path) not in covered
    ]
    check = Check(
        "omission",
        "is every settlement the seller recorded also a decision we recorded?",
        len(seen),
        len(found),
    )
    return found, check, unattributable


# --- 2 · commission ---------------------------------------------------------


def _commission(decisions: list[dict], receipts: list[dict], treasury: str):
    """The right amount, to the wrong party.

    The essay's hardest case: "A payment to the wrong vendor's account
    reconciles perfectly, because the bank confirms that you paid exactly whom
    you told it to pay." The ledger cannot help, so this asks the tape instead:
    did the payee we sent money to ever serve this business?

    And the reroute shape, which is ours rather than the essay's: a decision
    that says it redirected the money has to name somebody else to redirect it
    to. A reroute back to the vendor it was escaping is a no-op wearing the
    label of a saving.
    """
    t = (treasury or "").lower()
    served = {
        str(r.get("seller") or "").lower()
        for r in receipts
        if str(r.get("payer") or "").lower() == t and r.get("seller")
    }
    # A SIGNED AGREEMENT IS BETTER EVIDENCE THAN THE TAPE, and without this the
    # check inverts into a false positive on every obligation that is not an
    # x402 call. The tape only records machine-service settlements, so a
    # contractor or a subscription vendor can never appear on it — and this
    # check would report "paid somebody who never served this treasury" about
    # every single one of them, which is the audit's hardest finding fired at
    # the wrong target.
    #
    # The question the check asks is "was this party meant to be paid?". A
    # commitment we wrote down, whose hash is on the decision and inside the
    # transaction that paid it, answers that better than a receipt does: the
    # receipt says money moved, the agreement says we intended it to.
    #
    # Not firing today only because nothing non-x402 has been paid yet.
    for d in decisions:
        if str(d.get("commitment_verdict") or "") == "within" and d.get("vendor"):
            served.add(str(d["vendor"]).lower())
    found: list[Finding] = []
    searched = 0

    for d in decisions:
        oid = str(d.get("obligation_id") or "")
        if _paid(d):
            searched += 1
            vendor = str(d.get("vendor") or "").lower()
            if served and vendor not in served:
                found.append(
                    Finding(
                        "commission",
                        oid,
                        f"paid {d.get('vendor')}, who never appears as a seller "
                        "to this treasury on the settlement tape",
                    )
                )
        elif str(d.get("intent") or "") == REROUTE:
            searched += 1
            to = str(d.get("reroute_to") or "")
            if not to:
                found.append(
                    Finding("commission", oid, "rerouted, but names nobody to reroute to")
                )
            elif to.lower() == str(d.get("vendor") or "").lower():
                found.append(
                    Finding(
                        "commission",
                        oid,
                        f"rerouted to {to}, which is the vendor it was escaping",
                    )
                )

    return found, Check(
        "commission",
        "did the money go to a party that actually served this business?",
        searched,
        len(found),
    )


# --- 3 · principle ----------------------------------------------------------


def _principle(decisions: list[dict], categories: tuple[str, ...]):
    """The right amount in the wrong KIND of account.

    `ledger_export` books every payment to `Expenses:{category}`, so the account
    is only ever as right as the category. The registry is the outside authority
    here: a category the business never declared is an expense posted to an
    account nobody recognises, and it balances perfectly.

    A business that declared no categories at all is not checkable, and saying
    so is better than passing it.
    """
    declared = {c.strip().lower() for c in (categories or ()) if c.strip()}
    searched = 0
    found: list[Finding] = []
    if not declared:
        return found, Check(
            "principle",
            "is every expense booked to an account the business declared?",
            0,
            0,
        )
    for d in decisions:
        if not _paid(d):
            continue
        searched += 1
        cat = str(d.get("category") or "").strip().lower()
        if cat not in declared:
            found.append(
                Finding(
                    "principle",
                    str(d.get("obligation_id") or ""),
                    f"booked to Expenses:{d.get('category') or '(none)'}, which "
                    f"{'is not among' if cat else 'is missing from'} the "
                    f"{len(declared)} category/categories this business declared",
                )
            )
    return found, Check(
        "principle",
        "is every expense booked to an account the business declared?",
        searched,
        len(found),
    )


# --- 4 · original entry -----------------------------------------------------


def _original_entry(decisions: list[dict]):
    """The wrong amount on both sides — and the retry that paid twice.

    Both halves balance by construction: `ledger_export` writes `paid_usdc` to
    the expense and its negation to the treasury, so any amount at all sums to
    zero. That is the essay's point, in our own file.

    The second half is the control that existed and never ran. `decide`'s check
    1 takes a set of settled references and `operator_run.py` never passed one,
    so a retried bill was paid again and the ledger showed two clean payments.
    """
    searched = 0
    found: list[Finding] = []
    paid_once: dict[str, dict] = {}

    for d in decisions:
        if not _paid(d):
            continue
        searched += 1
        oid = str(d.get("obligation_id") or "")
        paid = _f(d.get("paid_usdc")) or 0.0
        billed = _f(d.get("billed_usdc"))

        if billed is not None and abs(paid - billed) > CANCEL_TOLERANCE_USDC:
            found.append(
                Finding(
                    "original entry",
                    oid,
                    f"paid {paid:.6f} against a bill of {billed:.6f} USDC, and "
                    "both postings carry the paid figure, so it balances",
                )
            )
        if oid and oid in paid_once:
            found.append(
                Finding(
                    "original entry",
                    oid,
                    f"settled twice: {paid_once[oid]['usdc']:.6f} then "
                    f"{paid:.6f} USDC against one reference",
                )
            )
        elif oid:
            paid_once[oid] = {"usdc": paid}

    return found, Check(
        "original entry",
        "does the amount paid match the amount billed, and only once?",
        searched,
        len(found),
    )


# --- 5 · compensating -------------------------------------------------------


def _compensating(decisions: list[dict]):
    """Two mistakes that cancel.

    Ours is the meter. Every decision records `discrepancy` — what the vendor
    claimed minus what we counted. One bill overstated and another understated
    by the same amount leaves a period whose net discrepancy is zero while both
    bills are individually wrong, and a reader who only ever sees the total
    would call that clean.

    Reported as a period-level finding with no `obligation_id`, because no
    single row is the error.
    """
    over = [d for d in decisions if (_f(d.get("discrepancy")) or 0.0) > 0]
    under = [d for d in decisions if (_f(d.get("discrepancy")) or 0.0) < 0]
    searched = len(over) + len(under)
    found: list[Finding] = []
    if over and under:
        net = sum(_f(d.get("discrepancy")) or 0.0 for d in over + under)
        if abs(net) <= CANCEL_TOLERANCE_USDC:
            found.append(
                Finding(
                    "compensating",
                    "",
                    f"{len(over)} bill(s) over our count and {len(under)} under "
                    "cancel to a net of zero; the period looks clean and no "
                    "single bill is",
                )
            )
    return found, Check(
        "compensating",
        "do over- and under-counts cancel, hiding both?",
        searched,
        len(found),
    )


# --- 6 · complete reversal --------------------------------------------------


def _complete_reversal(decisions: list[dict]):
    """The debit and the credit swapped.

    `ledger_export` writes `+amount` to the expense and `-amount` to the
    treasury. Negate the amount and the pair still sums to zero: the file now
    says this vendor paid US and the treasury grew, and it balances exactly as
    well. Nothing downstream would notice, because nothing downstream checks a
    sign.
    """
    searched = 0
    found: list[Finding] = []
    for d in decisions:
        if str(d.get("intent") or "") != PAY:
            continue
        searched += 1
        paid = _f(d.get("paid_usdc"))
        billed = _f(d.get("billed_usdc"))
        if paid is not None and paid < 0:
            found.append(
                Finding(
                    "complete reversal",
                    str(d.get("obligation_id") or ""),
                    f"a payment of {paid:.6f} USDC books the treasury UP and the "
                    "expense down, and still sums to zero",
                )
            )
        if billed is not None and billed < 0:
            found.append(
                Finding(
                    "complete reversal",
                    str(d.get("obligation_id") or ""),
                    f"billed {billed:.6f} USDC: a bill that pays us",
                )
            )
    return found, Check(
        "complete reversal",
        "does any posting run in the wrong direction?",
        searched,
        len(found),
    )


# --- not one of the six: the phantom ---------------------------------------


def _phantom(decisions: list[dict], confirm):
    """A payment the books assert and the chain cannot corroborate.

    Two strengths, and the weaker one always runs:

    * **structural** — a decision claiming money moved must carry a transaction.
      A payment with no `tx` is a claim with no receipt, and it balances.
    * **corroborated** — when a `confirm` collaborator is supplied, each `tx` is
      put to the chain. `confirm(tx)` answers True, False, or None for "could
      not tell". Only False is a finding: an unreachable node is not evidence of
      a phantom, and treating it as one would cry wolf every time a node blips.

    Without `confirm` the corroborated half reports that it searched nothing,
    which is the same rule the six follow. An audit that quietly downgrades to
    the cheap check and still says "clean" is the thing this module exists to
    refuse.
    """
    claimed = [d for d in decisions if _paid(d)]
    found: list[Finding] = []
    for d in claimed:
        oid = str(d.get("obligation_id") or "")
        tx = str(d.get("tx") or "")
        if not tx:
            found.append(
                Finding(
                    PHANTOM,
                    oid,
                    f"{_f(d.get('paid_usdc')) or 0.0:.6f} USDC recorded as paid with no "
                    "transaction to point at",
                )
            )
            continue
        if confirm is None:
            continue
        try:
            verdict = confirm(tx)
        except Exception:  # pragma: no cover - env dependent
            verdict = None
        if verdict is False:
            found.append(
                Finding(
                    PHANTOM,
                    oid,
                    f"the chain does not corroborate {tx[:18]}…: the books say this "
                    "was paid and the record of it is not there",
                )
            )

    # Every claimed payment IS examined either way — the structural half always
    # runs. What changes is how hard the question is, which the question text
    # says rather than the count pretending to.
    return found, Check(
        PHANTOM,
        "does the chain corroborate every payment the books assert?"
        if confirm is not None
        else "does every payment at least carry a transaction? (chain not consulted)",
        len(claimed),
        len(found),
    )


# --- the audit --------------------------------------------------------------


def audit(
    decisions: list[dict],
    receipts: list[dict],
    *,
    treasury: str = "",
    slug: str = "",
    categories: tuple[str, ...] = (),
    now: float | None = None,
    #: ``confirm(tx) -> bool | None`` — the chain's opinion of a transaction we
    #: claim paid. Injected rather than imported so `audit` stays pure and a
    #: test can hand it a known answer; absent means the phantom check reports
    #: that it did not consult the chain, rather than passing as if it had.
    confirm=None,
) -> dict:
    """All six, over one business's period.

    Pure, like `decide`: rows in, verdict out, no I/O. The caller supplies the
    decision log and the settlement tape, which is what lets a test put a known
    error in front of each check rather than hoping production grows one.
    """
    t = now if now is not None else time.time()
    rows = list(decisions or ())
    tape = list(receipts or ())

    om, c_om, unattributable = _omission(rows, tape, treasury, slug)
    cm, c_cm = _commission(rows, tape, treasury)
    pr, c_pr = _principle(rows, categories)
    oe, c_oe = _original_entry(rows)
    cp, c_cp = _compensating(rows)
    cr, c_cr = _complete_reversal(rows)
    ph, c_ph = _phantom(rows, confirm)

    checks = [c_om, c_cm, c_pr, c_oe, c_cp, c_cr, c_ph]
    assert tuple(c.error for c in checks) == ERRORS + (PHANTOM,), (
        "a check was lost or renamed"
    )
    findings = om + cm + pr + oe + cp + cr + ph

    return {
        "as_of": t,
        "decisions": len(rows),
        # Not "clean". A book with nothing in it is not a book that passed, and
        # the essay's first error is the one you cannot see by looking harder at
        # what is there.
        "clean": not findings,
        "unattributable_settlements": unattributable,
        "checks": [asdict(c) for c in checks],
        "findings": [asdict(f) for f in findings],
        "note": (
            "Balancing is the weakest check here: every error below sums to "
            "zero. The last one is not one of the essay's six — it is the "
            "fictitious entry the essay says nobody can disprove. See "
            "thecanteenapp.com/analysis/2026/09/12/agents-and-ledgers."
        ),
    }


# --- the one piece of I/O ---------------------------------------------------
#
# `audit` above is pure and stays that way, so a test can put a known error in
# front of each check rather than hoping production grows one. The loader lives
# here beside it for the same reason `operator.py` keeps `decide` and
# `run_obligation` in one file: the pure rule and the thing that feeds it belong
# together, and splitting them puts a module boundary between a check and its
# own evidence.


def load_tape(path: str = "") -> list[dict]:
    """The settlement archive — our bank statement, written by the sellers.

    Read from the same place the facilitator rehydrates from
    (`ACR_RECEIPT_ARCHIVE_PATH`, set in `render.yaml`), falling back to the copy
    committed beside this module so the check works in a fresh checkout and in
    the image. A missing tape returns nothing and the omission check then
    reports that it searched nothing, which is the honest answer — not that the
    books are clean.
    """
    import json
    import os
    from pathlib import Path

    p = Path(path or os.environ.get("ACR_RECEIPT_ARCHIVE_PATH", "")
             or Path(__file__).with_name("receipts_live.jsonl"))
    if not p.exists():
        return []
    rows: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows
