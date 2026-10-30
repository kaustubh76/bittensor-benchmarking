"""Bills that did not come from our paywall — Prior Art #06, the agoranomoi.

Every Greek market had officials who kept the standard weights, because a seller
who supplies both the goods and the measuring cup will eventually supply a
smaller cup. Software billing has no such official: the vendor counts the seats
and the invoice arrives already totalled.

This repo has kept its own count since the beginning, and could only ever apply
it to x402 settlements — the one meter it happened to build first. A recurring
vendor does not settle through our paywall. It bills for an entitlement, and
whether that entitlement was *used* lives in somebody else's usage export.

Two things were missing and both are small: a feeder that is not the tape, and a
seam that lets an external count reach the meter check. With them, "you are
paying for fifty seats and twelve are open" becomes a decision with a number
attached — and the number is in USDC, because `discrepancies` has always been a
count, and a count of findings is not a reason to act.
"""

from __future__ import annotations

from index_api.operator import (
    ESCALATE,
    PAY,
    obligation_key,
    obligations_from_entitlements,
    run_obligation,
    settled_through,
)

VENDOR = "0x" + "5e" * 20
AT = 1_790_900_000.0
MONTH = 30 * 86_400


class _Biz:
    slug = "acme"
    treasury = "0x" + "11" * 20
    categories = ("software",)


def _row(**kw) -> dict:
    row = {
        "payee": VENDOR,
        "resource": "/saas/seats",
        "unit": "seat-month",
        "billed_usdc": 100.0,
        "billed_quantity": 50.0,
        "used_quantity": 12.0,
        "period_start": AT - MONTH,
        "period_end": AT - 3_600,
        "invoice_ref": "ACME-1001",
    }
    row.update(kw)
    return row


# --- the feeder -------------------------------------------------------------


def test_an_entitlement_becomes_an_obligation_with_its_count_beside_it():
    """The pair is the point. Returning the bill without the count that judges
    it would make it possible to run a bill past the meter — and the meter's
    absence leaves no trace in the record, so nobody would see it."""
    (ob, used), = obligations_from_entitlements(_Biz(), [_row()])
    assert ob.vendor == VENDOR
    assert ob.billed_usdc == 100.0
    assert ob.vendor_quantity == 50.0, "what the vendor charged for"
    assert used == 12.0, "what the independent source says happened"
    assert ob.kind == "subscription"
    assert ob.unit == "seat-month"


def test_the_three_fields_an_invoice_has_and_the_tape_never_did():
    """`due_at` is what finally gives check 7 — the timing rule — something to
    say. It has been dead code since it was written, because a settlement
    receipt has no due date and the tape was the only feeder."""
    (ob, _), = obligations_from_entitlements(
        _Biz(), [_row(due_at=AT + 10 * 86_400, early_pay_discount=0.02)]
    )
    assert ob.due_at == AT + 10 * 86_400
    assert ob.early_pay_discount == 0.02
    assert ob.invoice_ref == "ACME-1001"


def test_usage_we_could_not_measure_is_none_and_never_zero():
    """Zero asserts the entitlement went unused, which would make every bill
    look fraudulent. `None` says we could not check, and the ladder escalates
    rather than accusing."""
    row = _row()
    del row["used_quantity"]
    (_ob, used), = obligations_from_entitlements(_Biz(), [row])
    assert used is None


def test_a_row_with_no_payee_amount_or_resource_is_dropped():
    bad = [
        _row(payee=""),
        _row(resource=""),
        _row(billed_usdc=None),
        _row(billed_usdc=0.0),
        _row(billed_usdc=-5.0),
    ]
    assert obligations_from_entitlements(_Biz(), bad) == []


def test_the_period_comes_from_the_bill_not_the_clock():
    """A window taken from `now` mints a new id on every tick, so the duplicate
    check never matches and the same charge is paid again and again."""
    (ob, _), = obligations_from_entitlements(_Biz(), [_row()])
    assert ob.period_start == AT - MONTH
    assert ob.period_end == AT - 3_600
    assert ob.obligation_id == obligation_key("acme", VENDOR, "/saas/seats", AT - 3_600)


def test_a_period_already_settled_is_not_billed_again():
    (ob, _), = obligations_from_entitlements(_Biz(), [_row()])
    paid = [{
        "at": AT, "obligation_id": ob.obligation_id, "vendor": VENDOR,
        "resource": "/saas/seats", "intent": PAY, "paid_usdc": 100.0,
        "period_start": ob.period_start, "period_end": ob.period_end,
    }]
    assert obligations_from_entitlements(_Biz(), [_row()], settled_through(paid)) == []


def test_next_month_is_a_new_bill():
    """The renewal. Same vendor, same service, a later window — which is a new
    id and therefore payable, where a period-less id would be refused as a
    duplicate for ever."""
    (first, _), = obligations_from_entitlements(_Biz(), [_row()])
    paid = [{
        "at": AT, "obligation_id": first.obligation_id, "vendor": VENDOR,
        "resource": "/saas/seats", "intent": PAY, "paid_usdc": 100.0,
        "period_end": first.period_end,
    }]
    renewal = _row(period_start=AT, period_end=AT + MONTH)
    (second, _), = obligations_from_entitlements(_Biz(), [renewal], settled_through(paid))
    assert second.obligation_id != first.obligation_id


def test_the_category_can_come_from_the_row():
    """A business declares several; a subscription is not necessarily booked to
    the first one. `_principle` checks the posting against the declared list, so
    the row must be able to say."""
    (ob, _), = obligations_from_entitlements(_Biz(), [_row(category="software")])
    assert ob.category == "software"
    (fallback, _), = obligations_from_entitlements(_Biz(), [_row()])
    assert fallback.category == "software", "the business's first, when the row is silent"


# --- the whole point: seats nobody opens, priced ---------------------------


def test_seats_nobody_opens_are_found_and_priced_in_usdc():
    """THE AGORANOMOS CHECK, with the money in it.

    Fifty seats billed, twelve open. `discrepancies` has always been able to say
    "one bill disagreed with our meter"; what it could not say is that
    seventy-six of a hundred USDC was for something that did not happen. Only
    one of those two sentences gets answered.
    """
    (ob, used), = obligations_from_entitlements(_Biz(), [_row()])
    d = run_obligation(
        ob, receipts=[], catalog={}, commitment=False, metered_quantity=used,
        dry_run=True, log_path="/dev/null", now=AT,
    )
    assert d.intent == ESCALATE
    assert d.recommended_intent == "refuse"
    assert d.discrepancy == 38.0, "in seats"
    assert d.discrepancy_usdc == 76.0, "and in money"
    assert "76 USDC of this bill is for something that did not happen" in d.rule


def test_a_saas_bill_has_no_market_at_all():
    """Why #03 had to come first, shown rather than asserted.

    A hundred-dollar seat bill cannot be benchmarked: `par` needs two
    independent sellers of the same unit and there is exactly one vendor of this
    subscription. So the ladder meets the unbenchmarked ceiling and escalates —
    correctly, because a price nobody can check should not be paid at size on
    the agent's own authority.

    That is not a defect in the meter. It is the reason an agreement is the only
    thing that can price a recurring bill, and it is why the commitment landed
    before this feeder did.
    """
    (ob, used), = obligations_from_entitlements(_Biz(), [_row(used_quantity=50.0)])
    d = run_obligation(
        ob, receipts=[], catalog={}, commitment=False, metered_quantity=used,
        dry_run=True, log_path="/dev/null", now=AT,
    )
    assert d.intent == ESCALATE
    assert "unbenchmarked" in d.rule and "ceiling" in d.rule


def test_an_entitlement_fully_used_and_under_agreement_is_paid():
    """The control, and the two entries working together: #06 finds the waste,
    #03 makes the legitimate bill payable. A check that fires on every bill is
    as useless as one that never fires, and an agent that refuses its own
    vendors is not a product.
    """
    from index_api.commitments import Commitment

    agreement = Commitment(
        commitment_id="c-saas", business="acme", payee=VENDOR,
        resource="/saas/seats", unit="seat-month",
        unit_price_usdc=2.0, max_quantity=50.0,
        starts_at=AT - 2 * MONTH, ends_at=AT + MONTH,
    )
    (ob, used), = obligations_from_entitlements(_Biz(), [_row(used_quantity=50.0)])
    d = run_obligation(
        ob, receipts=[], catalog={}, commitment=agreement, metered_quantity=used,
        dry_run=True, log_path="/dev/null", now=AT,
    )
    assert d.intent == PAY, d.rule
    assert d.commitment_verdict == "within"
    assert d.discrepancy == 0.0 and d.discrepancy_usdc == 0.0


def test_the_waste_is_found_even_under_an_agreement():
    """The orderings hold: the meter is check 3 and the agreement is 4b, so a
    bill inside its agreement is still refused when the seats are not open.
    An agreement to buy fifty seats is not an agreement to pay for thirty-eight
    nobody used."""
    from index_api.commitments import Commitment

    agreement = Commitment(
        commitment_id="c-saas", business="acme", payee=VENDOR,
        resource="/saas/seats", unit="seat-month",
        unit_price_usdc=2.0, max_quantity=50.0,
        starts_at=AT - 2 * MONTH, ends_at=AT + MONTH,
    )
    (ob, used), = obligations_from_entitlements(_Biz(), [_row()])
    d = run_obligation(
        ob, receipts=[], catalog={}, commitment=agreement, metered_quantity=used,
        dry_run=True, log_path="/dev/null", now=AT,
    )
    assert d.intent == ESCALATE
    assert d.discrepancy_usdc == 76.0
    assert "did not happen" in d.rule
    assert d.commitment_verdict == "", "check 3 returned before 4b was reached"


def test_usage_we_cannot_see_escalates_rather_than_accusing():
    """"We hold no record of consuming this" is not evidence of overbilling. The
    agent asks a person instead of refusing a bill that may be perfectly fair."""
    row = _row()
    del row["used_quantity"]
    (ob, used), = obligations_from_entitlements(_Biz(), [row])
    d = run_obligation(
        ob, receipts=[], catalog={}, commitment=False, metered_quantity=used,
        dry_run=True, log_path="/dev/null", now=AT,
    )
    assert d.intent == ESCALATE
    assert "unmetered" in d.rule
    assert d.discrepancy_usdc is None, "no count, so no figure — not a zero"


def test_the_waste_is_summed_as_money_that_stayed():
    """`overbilled_usdc` is realised: the bill did not go out. It must never be
    added to `saved_usdc`, which is measured against another seller's offer and
    bought nothing."""
    from index_api.statement import summarise

    (ob, used), = obligations_from_entitlements(_Biz(), [_row()])
    d = run_obligation(
        ob, receipts=[], catalog={}, commitment=False, metered_quantity=used,
        dry_run=True, log_path="/dev/null", now=AT,
    )
    s = summarise([{**d.as_record(), "paid_usdc": 0.0, "tx": None}])
    assert s["overbilled_usdc"] == 76.0
    assert s["saved_usdc"] == 0.0
    assert s["consumption_discrepancies"] == 1, "the count still exists beside it"


def test_a_bill_the_owner_approved_is_not_counted_as_waste_avoided():
    from index_api.statement import summarise

    settled = {
        "at": AT, "obligation_id": "ob-approved", "vendor": VENDOR,
        "resource": "/saas/seats", "intent": PAY, "actor": "owner",
        "billed_usdc": 100.0, "paid_usdc": 100.0,
        "discrepancy": 38.0, "discrepancy_usdc": 76.0,
    }
    assert summarise([settled])["overbilled_usdc"] == 0.0
