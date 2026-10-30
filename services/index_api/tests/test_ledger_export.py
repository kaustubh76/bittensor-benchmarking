"""Beancount export — a real double-entry file, and nothing invented.

Prior Art #01: beancount is "a ledger an agent can write to", and "neither has
ever been connected to money that actually moves." So the claim under test is
not "we emit something beancount-shaped" — it is that every transaction sums to
zero, that nothing which did not move money becomes a transaction, and that each
entry carries what *Agents and Ledgers* says a balanced ledger cannot check:
which vendor, which invoice, and whether a retry paid twice.

`test_the_balance_checker_can_actually_fail` is here because a validator that
never fails is not a validator.
"""

from __future__ import annotations

import re

from index_api.ledger_export import (
    CURRENCY,
    _component,
    balance_problems,
    to_beancount,
)

V = "0x" + "bb" * 20
W = "0x" + "cc" * 20


def _paid(**kw):
    base = dict(
        at=1_790_900_000, obligation_id="inv-1", vendor=V, category="machine-services",
        billed_usdc=2.5, intent="pay", paid_usdc=2.5, rule="at par, inside budget",
        business="acr-fleet", metered_quantity=5.0, vendor_quantity=5.0,
        par_usdc=0.5, screen_risk="clear", tx="0x" + "ee" * 32,
    )
    base.update(kw)
    return base


def _other(intent, **kw):
    base = dict(
        at=1_790_900_050, obligation_id="inv-2", vendor=V, category="machine-services",
        billed_usdc=9.0, intent=intent, paid_usdc=0.0, rule=f"{intent} happened",
        business="acr-fleet",
    )
    base.update(kw)
    return base


def _txns(text: str) -> list[str]:
    return [ln for ln in text.splitlines() if re.match(r"^\d{4}-\d{2}-\d{2}\s+\*", ln)]


# --- the double-entry property --------------------------------------------

def test_every_transaction_sums_to_zero():
    """The whole of double entry, checked by reading the emitted file rather
    than by trusting the writer."""
    text = to_beancount([_paid(), _paid(obligation_id="inv-9", paid_usdc=0.000001)],
                        "acr-fleet")
    assert balance_problems(text) == []
    assert len(_txns(text)) == 2


def test_the_balance_checker_can_actually_fail():
    """A validator that never fails is not a validator."""
    broken = f'''option "title" "x"
2026-01-01 open Assets:A {CURRENCY}
2026-01-01 * "v" "n"
  Expenses:B  2.500000 {CURRENCY}
  Assets:A  -1.000000 {CURRENCY}
'''
    problems = balance_problems(broken)
    assert problems and "sum to" in problems[0]


def test_a_transaction_with_no_postings_is_reported():
    broken = '2026-01-01 * "v" "n"\n  acr-rule: "only metadata"\n'
    assert balance_problems(broken), "a transaction that moves nothing is a defect"


def test_sub_cent_amounts_still_balance():
    """x402 bills are fractions of a cent, so the places have to be enough."""
    text = to_beancount([_paid(paid_usdc=0.000001)], "acr-fleet")
    assert balance_problems(text) == []
    assert "0.000001" in text


# --- nothing is invented ---------------------------------------------------

def test_a_reroute_is_not_a_transaction():
    """It moved no money. Booking the avoided overpay as income would be
    inventing a credit, in the format that makes it look official."""
    text = to_beancount([_other("reroute", saving_usdc=1.5, reroute_to=W)], "acr-fleet")
    assert _txns(text) == []
    assert "note" in text and "declined" in text
    assert "Income" not in text, "no invented credit anywhere in the file"


def test_a_dry_run_payment_is_not_recorded_as_spend():
    """`pay` with nothing sent cleared policy and never moved. Labelling that
    "pay" in a ledger is the most misleading line the file could carry: a reader
    counts it as spend."""
    text = to_beancount([_other("pay")], "acr-fleet")
    assert _txns(text) == []
    assert "cleared policy, not sent" in text


def test_holds_refusals_and_escalations_are_notes_not_transactions():
    rows = [_other("hold"), _other("refuse"), _other("escalate")]
    text = to_beancount(rows, "acr-fleet")
    assert _txns(text) == []
    assert text.count("note") == 3


# --- what the essay says a ledger misses ----------------------------------

def test_each_transaction_carries_the_decision_that_produced_it():
    """"A ledger does not check that the vendor was the right one, that the
    invoice was real, or that a retry didn't pay it twice." These are the
    annotations that let a reader check all three."""
    text = to_beancount([_paid()], "acr-fleet")
    for key in ("acr-obligation:", "acr-rule:", "acr-metered:",
                "acr-billed-quantity:", "acr-par:", "acr-screen:", "acr-tx:"):
        assert key in text, key


def test_a_decision_without_a_meter_omits_the_key_rather_than_claiming_zero():
    text = to_beancount([_paid(metered_quantity=None, par_usdc=None)], "acr-fleet")
    assert "acr-metered:" not in text
    assert "acr-par:" not in text
    assert balance_problems(text) == []


# --- the file has to load -------------------------------------------------

def test_every_account_is_opened_before_it_is_posted_to():
    """Beancount refuses a posting to an account it never saw opened, and a file
    that will not load is not a ledger."""
    text = to_beancount([_paid(), _paid(category="contractors", obligation_id="i2")],
                        "acr-fleet")
    opened = {m.group(1) for m in re.finditer(r"^\d{4}-\d{2}-\d{2} open (\S+)", text, re.M)}
    posted = {
        m.group(1)
        for m in re.finditer(r"^  ([A-Z][\w:-]+)\s+-?\d", text, re.M)
    }
    assert posted and posted <= opened, f"posted but never opened: {posted - opened}"


def test_a_category_a_person_wrote_becomes_a_valid_component():
    assert _component("machine-services") == "Machine-services"
    assert _component("R&D") == "R-D"
    assert _component("  ") == "Unsorted", "never emits an empty component"
    assert _component("123-infra") == "123-infra"


def test_a_quote_in_a_rule_cannot_end_the_string_early():
    """A rule is prose written by the operator. An unescaped quote would
    truncate the token and corrupt every line after it."""
    text = to_beancount([_paid(rule='he said "too dear" and we agreed')], "acr-fleet")
    assert '"too dear"' not in text
    assert balance_problems(text) == []
    # the narration is still one quoted token
    assert len(_txns(text)) == 1 and _txns(text)[0].count('"') == 4


def test_a_newline_in_a_rule_cannot_break_the_file():
    text = to_beancount([_paid(rule="first line\nsecond line")], "acr-fleet")
    assert balance_problems(text) == []
    assert len(_txns(text)) == 1


def test_an_empty_log_is_a_valid_empty_ledger():
    text = to_beancount([], "acr-fleet")
    assert balance_problems(text) == []
    assert "operating_currency" in text and "Assets:Treasury:Acr-fleet" in text


# --- the shipped archive --------------------------------------------------

def test_the_committed_decision_archive_exports_and_balances():
    from index_api.statement import ARCHIVE_PATH, read_decisions

    rows = read_decisions(str(ARCHIVE_PATH))
    text = to_beancount(rows, "acr-fleet")
    assert balance_problems(text) == []


def test_an_empty_ledger_does_not_open_its_account_in_1970():
    """`_day(0)` is the Unix epoch. Valid beancount, and an obviously wrong
    artifact for a business onboarded this week."""
    import datetime

    text = to_beancount([], "acr-fleet")
    today = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
    assert "1970-01-01" not in text
    assert f"{today} open Assets:Treasury:Acr-fleet" in text


# --- the closing assertion ------------------------------------------------


def _assertion(text: str) -> tuple[str, float, float]:
    """The `balance` directive, parsed back out of the file."""
    m = re.search(
        r"^(\d{4}-\d{2}-\d{2}) balance (\S+)\s+(-?\d+\.\d+) ~ (\d+\.\d+) " + CURRENCY,
        text,
        re.M,
    )
    assert m, f"no balance assertion in:\n{text}"
    return m.group(1), float(m.group(3)), float(m.group(4))


def test_the_ledger_asserts_its_own_closing_balance():
    """*Agents and Ledgers* praises beancount for exactly one control —
    "Balance assertions with declared tolerance" — and this file had none."""
    _day, amount, tol = _assertion(to_beancount([_paid(), _paid(obligation_id="i2")], "acr-fleet"))
    assert amount == -5.0, "two 2.5 payments leave the treasury 5.0 down"
    assert tol == 0.000001, "the tolerance is declared, not implied"


def test_the_assertion_is_dated_after_the_last_entry():
    """beancount evaluates a balance at the START of its date, so asserting on
    the final day would assert a total that excludes that day's own payments."""
    day, amount, _ = _assertion(to_beancount([_paid()], "acr-fleet"))
    assert day > "2026-10-02", day
    assert amount == -2.5


def test_a_dry_run_moves_the_asserted_balance_by_nothing():
    """Every row in the committed archive is `pay` with `paid_usdc: 0.0`. If
    those counted, the file would assert a treasury that had paid out money it
    still holds."""
    _day, amount, _ = _assertion(to_beancount([_paid(paid_usdc=0.0)], "acr-fleet"))
    assert amount == 0.0


def test_the_assertion_matches_the_postings_it_closes():
    """Derived independently here, the way `balance_problems` re-derives the
    postings rather than trusting the writer."""
    rows = [_paid(), _paid(obligation_id="i2", paid_usdc=1.25), _paid(obligation_id="i3", intent="reroute", paid_usdc=0.0)]
    text = to_beancount(rows, "acr-fleet")
    _day, asserted, _tol = _assertion(text)
    treasury_postings = [
        float(m) for m in re.findall(r"^  Assets:Treasury:\S+\s+(-?\d+\.\d+) " + CURRENCY, text, re.M)
    ]
    assert round(sum(treasury_postings), 6) == asserted
