"""Beancount export — the ledger an agent writes to, connected to money.

Prior Art #01 names this one outright: *"beancount/beancount is a ledger an
agent can write to and a human can read, and firefly-iii/firefly-iii already has
the rules engine. **Neither has ever been connected to money that actually
moves.**"* So this is not a CSV dump with a different extension; it is a real
double-entry file whose every transaction sums to zero.

AND IT CARRIES THE THING THE ESSAY SAYS LEDGERS MISS. *Agents and Ledgers*
opens: "A ledger checks that debits equal credits. It does not check that the
vendor was the right one, that the invoice was real, or that a retry didn't pay
it twice." Every transaction here is annotated with the decision that produced
it — the rule that fired, what we independently metered, the par it was checked
against, what the counterparty screen said, and the hash the chain holds. A
reader with this file can answer all three of the essay's questions, which is
the whole point of the fourth ledger check.

ONLY A PAYMENT IS A TRANSACTION. A reroute moved no money: we declined to pay
this vendor, and there is no cash flow to book. Writing the avoided overpay as
`Income:Savings` would be inventing a credit — exactly the fabrication the essay
warns about, in the format that makes it look official. Reroutes, holds,
refusals and escalations become `note` directives on the treasury account: facts
on the record, with no pretend money.
"""

from __future__ import annotations

import datetime
import re

#: USDC is the operating currency throughout, at its 6 decimals.
CURRENCY = "USDC"
PLACES = 6

#: The tolerance declared on the closing balance assertion: one unit at the
#: currency's own precision. Declared rather than implied, because *Agents and
#: Ledgers* praises beancount for exactly one control — "Balance assertions with
#: declared tolerance; next assertion catches wrong predictions" — and an
#: assertion whose tolerance nobody wrote down is a tolerance nobody agreed to.
BALANCE_TOLERANCE = 10 ** -PLACES

_BAD = re.compile(r"[^A-Za-z0-9-]")


def _component(name: str, fallback: str = "Unsorted") -> str:
    """One beancount account component.

    Components must start with a letter or digit and hold only letters, digits
    and dashes, so a category written by a person ("machine-services", "R&D")
    has to be transformed rather than interpolated. An empty result falls back
    rather than emitting `Expenses::` and a file nothing can parse.
    """
    cleaned = _BAD.sub("-", (name or "").strip()).strip("-")
    if not cleaned:
        return fallback
    return cleaned[0].upper() + cleaned[1:]


def _day(at: float) -> str:
    return datetime.datetime.fromtimestamp(
        float(at or 0.0), datetime.UTC
    ).strftime("%Y-%m-%d")


def _amount(usdc: float) -> str:
    return f"{float(usdc):.{PLACES}f}"


def _q(text: str) -> str:
    """A beancount string. Quotes and newlines would end the token early."""
    return str(text or "").replace('"', "'").replace("\n", " ").strip()


def to_beancount(
    decisions: list[dict], business_slug: str, title: str = "ACR spend operator"
) -> str:
    """A double-entry file for one business's decisions.

    Accounts are opened on the earliest day anything touches them, because
    beancount rejects a posting to an account that was never opened and a file
    that will not load is not a ledger.
    """
    rows = sorted(decisions or [], key=lambda d: float(d.get("at") or 0.0))
    treasury = f"Assets:Treasury:{_component(business_slug, 'Unnamed')}"

    paid = [
        d for d in rows
        if d.get("intent") == "pay" and float(d.get("paid_usdc") or 0.0) > 0
    ]
    expense_accounts = sorted(
        {f"Expenses:{_component(d.get('category') or '')}" for d in paid}
    )

    # With no decisions yet there is no earliest day, and `_day(0)` would open
    # the account on 1970-01-01 — valid beancount and an obviously wrong
    # artifact for a business onboarded this week. Today is the honest answer.
    first_day = (
        _day(rows[0]["at"])
        if rows
        else datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    )
    out: list[str] = [
        f'option "title" "{_q(title)}: {_q(business_slug)}"',
        f'option "operating_currency" "{CURRENCY}"',
        "",
        "; Generated from the operator's own decision log. Every transaction",
        "; carries the rule that produced it and the hash the chain holds, so a",
        "; reader can check the vendor, the invoice and the retry — the three",
        "; things a balanced ledger does not check on its own.",
        "",
        f"{first_day} open {treasury} {CURRENCY}",
    ]
    out += [f"{first_day} open {a} {CURRENCY}" for a in expense_accounts]
    out.append("")

    for d in rows:
        day = _day(d.get("at"))
        intent = str(d.get("intent") or "")
        rule = _q(d.get("rule"))

        if intent == "pay" and float(d.get("paid_usdc") or 0.0) > 0:
            amount = float(d["paid_usdc"])
            expense = f"Expenses:{_component(d.get('category') or '')}"
            out.append(f'{day} * "{_q(d.get("vendor"))}" "{rule}"')
            out.append(f'  acr-obligation: "{_q(d.get("obligation_id"))}"')
            out.append(f'  acr-rule: "{rule}"')
            if d.get("metered_quantity") is not None:
                out.append(f'  acr-metered: "{d["metered_quantity"]}"')
            if d.get("vendor_quantity") is not None:
                out.append(f'  acr-billed-quantity: "{d["vendor_quantity"]}"')
            if d.get("par_usdc") is not None:
                out.append(f'  acr-par: "{d["par_usdc"]}"')
            if d.get("screen_risk"):
                out.append(f'  acr-screen: "{_q(d["screen_risk"])}"')
            if d.get("tx"):
                out.append(f'  acr-tx: "{_q(d["tx"])}"')
            # The two postings sum to zero. That is the whole of double entry,
            # and the test asserts it rather than trusting this comment.
            out.append(f"  {expense}  {_amount(amount)} {CURRENCY}")
            out.append(f"  {treasury}  {_amount(-amount)} {CURRENCY}")
            out.append("")
            continue

        # No money moved, so no transaction. A `note` records the fact without
        # inventing a cash flow.
        #
        # A `pay` that reaches here cleared policy and was never sent — a dry
        # run. Labelling it "pay" in the ledger would be the most misleading
        # line in the file: a reader would count it as spend.
        label = "cleared policy, not sent" if intent == "pay" else intent
        detail = f"{label}: {rule}"
        if intent == "reroute" and d.get("saving_usdc"):
            detail += (
                f" (declined; {_amount(d['saving_usdc'])} {CURRENCY} was available"
                f" from {_q(d.get('reroute_to'))})"
            )
        out.append(f'{day} note {treasury} "{_q(detail)}"')
        out.append("")

    # THE CLOSING ASSERTION. The one beancount control the essay names by
    # itself, and the one this file did not have.
    #
    # Be precise about what it buys. It does not prove the payments were right —
    # nothing inside a ledger can, which is the essay's whole argument and why
    # `ledger_audit.py` exists beside this file. What it does is make the file
    # answerable to itself: `bean-check` now refuses it if a transaction is
    # edited, dropped or appended after the fact, and the NEXT export of the
    # same period has to arrive at the same total or fail out loud. A ledger
    # that merely balances cannot tell you any of that, because every edit that
    # keeps two postings equal still balances.
    #
    # Dated the day AFTER the last entry: beancount evaluates a balance at the
    # start of its date, so asserting on the final day would assert a total that
    # excludes that day's own payments.
    total = sum(float(d.get("paid_usdc") or 0.0) for d in paid)
    last = max((float(d.get("at") or 0.0) for d in rows), default=0.0)
    assert_day = (
        _day(last + 86_400)
        if rows
        else datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    )
    out += [
        "",
        "; Every posting above sums to zero. So would a payment to the wrong",
        "; vendor, in the wrong account, for the wrong amount, or booked",
        "; backwards. Those six are checked outside this file, which is where",
        "; the essay says such controls have to live.",
    ]
    out.append(
        f"{assert_day} balance {treasury}  "
        f"{_amount(-total)} ~ {_amount(BALANCE_TOLERANCE)} {CURRENCY}"
    )

    return "\n".join(out).rstrip() + "\n"


def balance_problems(text: str) -> list[str]:
    """Every transaction's postings must sum to zero. Returns what did not.

    This is the double-entry property itself, checked by reading the emitted
    file rather than by trusting the writer. Beancount is deliberately NOT a
    dependency — the exporter emits text and this verifies it, so the claim
    holds on a host with nothing installed.
    """
    problems: list[str] = []
    header: str | None = None
    postings: list[float] = []

    def close() -> None:
        if header is None:
            return
        total = round(sum(postings), PLACES)
        if not postings:
            problems.append(f"{header}: a transaction with no postings")
        elif total != 0:
            problems.append(f"{header}: postings sum to {total:+.6f}, not zero")

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith(";"):
            continue
        if re.match(r"^\d{4}-\d{2}-\d{2}\s+\*", line):
            close()
            header, postings = line.strip(), []
            continue
        if re.match(r"^\d{4}-\d{2}-\d{2}\s+\S", line) or line.startswith("option"):
            close()
            header, postings = None, []
            continue
        if header is not None and line.startswith("  "):
            m = re.search(r"(-?\d+\.\d+)\s+" + CURRENCY + r"\s*$", line)
            if m:
                postings.append(float(m.group(1)))
    close()
    return problems
