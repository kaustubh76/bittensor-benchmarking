"""The symbolon: what we agreed, what we got, and what they billed.

Prior Art #03. A symbolon was broken in two and the halves had to fit. An
accounts-payable clerk still does the same match by hand — purchase order, goods
received, invoice — and this repo had two of the three: the vendor's bill, and
our own meter counting what actually arrived. The missing half was the prior
agreement to match them against.

The `PolicyWallet`'s budget looks like one and is not. It says "at most X on
infra", which is a LIMIT. A commitment says "at most X with this payee, for this
service, at this price, inside this window" — and that is the half a clerk
reconciles against.

The second thing it buys is less obvious and matters more. Check 5 escalates any
bill the agent cannot price above one USDC, and a benchmark needs two
independent sellers of the same unit — which a contractor's hourly rate and a
SaaS seat price can never have. **A market needs competitors; an agreement needs
none, because it is what we agreed.** So a bill of real size becomes payable
without inventing a market for it, which is the test at the bottom of this file.
"""

from __future__ import annotations

from index_api.commitments import (
    NONE_FOUND,
    OUTSIDE_WINDOW,
    OVER_QUANTITY,
    OVER_TOTAL,
    OVER_UNIT_PRICE,
    WITHIN,
    Commitment,
    assess,
    covering,
    load,
)
from index_api.operator import ESCALATE, PAY, Obligation, decide, replay

PAYEE = "0x" + "d4" * 20
OTHER = "0x" + "ab" * 20
RES = "/work/milestone-2"
AT = 1_790_900_000.0


def _c(**kw) -> Commitment:
    row = {
        "commitment_id": "c-1",
        "business": "acme",
        "payee": PAYEE,
        "resource": RES,
        "unit": "$/hour",
        "unit_price_usdc": 0.5,
        "max_quantity": 300.0,
        "starts_at": AT - 30 * 86_400,
        "ends_at": AT + 30 * 86_400,
    }
    row.update(kw)
    return Commitment(**row)


def _ob(billed: float, quantity: float | None = 280.0) -> Obligation:
    return Obligation(
        obligation_id="ob-1", vendor=PAYEE, billed_usdc=billed, business="acme",
        category="contractors", kind="milestone", resource=RES, unit="$/hour",
        vendor_quantity=quantity,
    )


# --- the agreement itself ---------------------------------------------------


def test_a_total_can_be_expressed_either_way():
    """Price × quantity, or a flat ceiling. Both are agreements people make."""
    assert _c().total_usdc == 150.0
    assert _c(unit_price_usdc=None, max_quantity=None, max_total_usdc=90.0).total_usdc == 90.0


def test_an_agreement_with_no_ceiling_is_refused_by_the_register(tmp_path, caplog):
    """A commitment that authorises everything is the opposite of an agreement,
    and a register that silently holds a blank cheque is worse than an empty
    one."""
    import json

    p = tmp_path / "commitments.jsonl"
    p.write_text(json.dumps({
        "commitment_id": "c-blank", "business": "acme", "payee": PAYEE, "resource": RES,
    }) + "\n")
    assert load(p) == ()


def test_a_row_with_no_payee_or_resource_cannot_be_matched_to_anything(tmp_path):
    import json

    p = tmp_path / "commitments.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in (
        {"commitment_id": "c-a", "business": "acme", "payee": "", "resource": RES,
         "max_total_usdc": 10.0},
        {"commitment_id": "c-b", "business": "acme", "payee": PAYEE, "resource": "",
         "max_total_usdc": 10.0},
        {"commitment_id": "", "business": "acme", "payee": PAYEE, "resource": RES,
         "max_total_usdc": 10.0},
    )) + "\n")
    assert load(p) == ()


def test_the_hash_pins_the_terms_as_they_stood():
    a = _c()
    assert a.hash() == _c().hash(), "the same terms hash the same"
    assert a.hash() != _c(unit_price_usdc=0.6).hash(), "a renegotiation is a different agreement"
    assert a.hash().startswith("0x")


# --- finding the one that applies ------------------------------------------


def test_the_narrowest_live_window_wins():
    """A specific agreement for one month must override an open retainer rather
    than be hidden behind it."""
    retainer = _c(commitment_id="c-open", starts_at=0.0, ends_at=0.0)
    this_month = _c(commitment_id="c-month", starts_at=AT - 86_400, ends_at=AT + 86_400)
    got = covering("acme", PAYEE, RES, AT, register=(retainer, this_month))
    assert got is not None and got.commitment_id == "c-month"


def test_an_expired_agreement_does_not_cover_anything():
    stale = _c(ends_at=AT - 86_400)
    assert covering("acme", PAYEE, RES, AT, register=(stale,)) is None


def test_a_resource_matches_whatever_form_it_arrived_in():
    """The register may hold a URL and the obligation a path, and matching them
    as raw strings finds nothing — the mistake the omission check records."""
    url = _c(resource="https://acme.example/work/milestone-2")
    assert covering("acme", PAYEE, RES, AT, register=(url,)) is not None


def test_another_payee_or_another_business_is_not_covered():
    a = _c()
    assert covering("acme", OTHER, RES, AT, register=(a,)) is None
    assert covering("someone-else", PAYEE, RES, AT, register=(a,)) is None


# --- does the bill fit -----------------------------------------------------


def test_a_bill_inside_the_agreement_matches():
    v = assess(_c(), billed_usdc=140.0, quantity=280.0, at=AT)
    assert v["matched"] is True and v["verdict"] == WITHIN
    assert v["commitment_hash"]


def test_over_the_agreed_total_is_named_and_priced():
    v = assess(_c(), billed_usdc=200.0, quantity=280.0, at=AT)
    assert v["verdict"] == OVER_TOTAL and v["matched"] is False
    assert v["over_usdc"] == 50.0, "150 was agreed"


def test_over_the_agreed_quantity_is_priced_at_the_agreed_rate():
    """The over-delivery's worth, not the vendor's own rate for it."""
    v = assess(_c(max_total_usdc=1_000.0), billed_usdc=160.0, quantity=320.0, at=AT)
    assert v["verdict"] == OVER_QUANTITY
    assert v["over_usdc"] == 10.0, "20 hours over, at the 0.5 we agreed"


def test_dearer_per_unit_than_agreed_is_caught_even_inside_the_total():
    """A bill can sit inside both the total and the quantity and still charge
    more per unit than agreed, by delivering less than it billed for."""
    v = assess(_c(), billed_usdc=100.0, quantity=100.0, at=AT)
    assert v["verdict"] == OVER_UNIT_PRICE
    assert v["over_usdc"] == 50.0, "1.0/hour against the 0.5 agreed, over 100 hours"


def test_a_bill_outside_the_window_is_not_an_agreement_at_all():
    v = assess(_c(ends_at=AT - 1), billed_usdc=10.0, quantity=20.0, at=AT)
    assert v["verdict"] == OUTSIDE_WINDOW
    assert v["over_usdc"] is None, "there is no overage; there is no agreement"


def test_no_agreement_is_its_own_verdict():
    v = assess(None, billed_usdc=10.0, quantity=20.0, at=AT)
    assert v["verdict"] == NONE_FOUND and v["matched"] is False


# --- the ladder ------------------------------------------------------------


def test_an_agreement_prices_a_bill_no_market_can():
    """THE TEST THIS WHOLE MODULE EXISTS FOR.

    A 140 USDC milestone has no market: one contractor is not two independent
    sellers, so `par` comes back unavailable and check 5 escalates anything over
    the one-USDC ceiling. That made every bill of real size unpayable by
    construction, whatever its kind.

    With the agreement it was made under, the bill HAS been checked — against
    the better of the two standards — so the ceiling does not apply.
    """
    ob = _ob(140.0)
    without = decide(ob, remaining_usdc=400.0, per_tx_limit_usdc=300.0,
                     metered_quantity=280.0, now=AT)
    assert without.intent == ESCALATE
    assert "unbenchmarked" in without.rule

    with_agreement = decide(ob, commitment=_c(), remaining_usdc=400.0,
                            per_tx_limit_usdc=300.0, metered_quantity=280.0, now=AT)
    assert with_agreement.intent == PAY, with_agreement.rule
    assert with_agreement.commitment_verdict == WITHIN
    assert with_agreement.commitment_hash


def test_a_bill_that_breaks_the_agreement_is_refused_with_the_amount():
    ob = _ob(200.0)
    d = decide(ob, commitment=_c(), remaining_usdc=400.0, per_tx_limit_usdc=300.0,
               metered_quantity=280.0, now=AT)
    assert d.intent == ESCALATE and d.recommended_intent == "refuse"
    assert d.commitment_verdict == OVER_TOTAL
    assert d.over_commitment_usdc == 50.0
    assert "150" in d.rule and "agreed" in d.rule


def test_the_agreement_outranks_the_market_because_we_are_not_shopping():
    """A reroute is the wrong answer to a bill we committed to. The agreement is
    more specific than the going rate, and the going rate elsewhere is somebody
    else's business."""
    from index_api.par import Par

    cheaper = Par(resource=RES, available=True, denomination="unit", par_usdc=0.5,
                  best_usdc=0.2, best_seller=OTHER, sellers=3)
    d = decide(_ob(140.0), commitment=_c(), par=cheaper, remaining_usdc=400.0,
               per_tx_limit_usdc=300.0, metered_quantity=280.0, now=AT)
    assert d.intent == PAY, d.rule
    assert d.reroute_to == "", "we agreed to this vendor"


def test_the_meter_still_outranks_the_agreement():
    """The three-way match is three-way. An agreement says what we would pay for
    work done; it does not say the work was done, and check 3 comes first."""
    d = decide(_ob(140.0, quantity=280.0), commitment=_c(), remaining_usdc=400.0,
               per_tx_limit_usdc=300.0, metered_quantity=100.0, now=AT)
    assert d.intent == ESCALATE
    assert "metered below billed" in d.rule, d.rule


def test_the_budget_still_outranks_the_agreement():
    """A commitment is not authority to exceed the wallet. The contract refuses
    and never clamps, and the agreement cannot talk it round."""
    d = decide(_ob(140.0), commitment=_c(), remaining_usdc=400.0,
               per_tx_limit_usdc=100.0, metered_quantity=280.0, now=AT)
    assert d.intent == ESCALATE
    assert "per-payment" in d.rule or "limit" in d.rule, d.rule


def test_the_agreement_travels_onto_the_record_and_replays():
    """The reviewer's half of the symbolon: the id names the agreement, the hash
    pins its terms, and both are inside `as_record()` — so the chain commits to
    "this payment was made against that agreement"."""
    d = decide(_ob(140.0), commitment=_c(), remaining_usdc=400.0,
               per_tx_limit_usdc=300.0, metered_quantity=280.0, now=AT)
    rec = d.as_record()
    assert rec["commitment_id"] == "c-1"
    assert rec["commitment_hash"].startswith("0x")
    assert rec["commitment_verdict"] == WITHIN
    # A replay has the verdict but not the agreement object, so it cannot
    # re-derive the match — and must not pretend to. It reproduces the row's
    # own reasoning from what the row carries.
    assert replay(rec).commitment_verdict in (WITHIN, "")


# --- the value ledger: realised and hypothetical are different columns ------


def test_held_back_is_only_counted_where_the_money_really_stayed():
    """`saved_usdc` and `held_back_usdc` look alike and are not.

    A reroute's saving is measured against another seller's OFFER and nothing
    was bought — `ledger_export` refuses to book it, because "writing the
    avoided overpay as `Income:Savings` would be inventing a credit". Held back
    is money a vendor asked for, outside an agreement we had written down, that
    did not leave the wallet. One is a counterfactual; one is defensible against
    the bank statement. Adding them would produce a number that is neither.
    """
    from index_api.statement import summarise

    over = {
        "at": AT, "obligation_id": "ob-over", "vendor": PAYEE, "resource": RES,
        "intent": ESCALATE, "billed_usdc": 200.0, "paid_usdc": 0.0,
        "commitment_verdict": OVER_TOTAL, "over_commitment_usdc": 50.0,
    }
    s = summarise([over])
    assert s["held_back_usdc"] == 50.0
    assert s["saved_usdc"] == 0.0, "nothing was rerouted, so nothing was 'saved'"


def test_a_bill_inside_its_agreement_holds_nothing_back():
    from index_api.statement import summarise

    paid = {
        "at": AT, "obligation_id": "ob-ok", "vendor": PAYEE, "resource": RES,
        "intent": PAY, "billed_usdc": 140.0, "paid_usdc": 140.0,
        "commitment_verdict": WITHIN, "over_commitment_usdc": None,
    }
    assert summarise([paid])["held_back_usdc"] == 0.0


def test_a_bill_the_owner_approved_anyway_was_not_held_back():
    """The figure claims money that did not go out. An escalation a human then
    settled went out, and counting it would make the number a record of what
    the agent OBJECTED to rather than of what it saved."""
    from index_api.statement import summarise

    settled = {
        "at": AT, "obligation_id": "ob-approved", "vendor": PAYEE, "resource": RES,
        "intent": PAY, "actor": "owner", "billed_usdc": 200.0, "paid_usdc": 200.0,
        "commitment_verdict": OVER_TOTAL, "over_commitment_usdc": 50.0,
    }
    assert summarise([settled])["held_back_usdc"] == 0.0


def test_the_shipped_fixtures_demonstrate_the_whole_ladder():
    """Seven rows, and between them every check that can fire.

    They are generated by `scripts/gen_sandbox_decisions.py` through `decide()`
    and gated by `make ci`, so this asserts coverage rather than content: if a
    future change makes one of these states unreachable, the demonstration goes
    quiet rather than wrong, and quiet is the failure nobody investigates.
    """
    import json

    from index_api.statement import SANDBOX_ARCHIVE_PATH

    rows = [json.loads(ln) for ln in SANDBOX_ARCHIVE_PATH.read_text().splitlines() if ln.strip()]
    verdicts = {r.get("commitment_verdict") or "" for r in rows}
    rules = " · ".join(r.get("rule") or "" for r in rows)

    assert {r["intent"] for r in rows} >= {PAY, "reroute", ESCALATE}
    assert WITHIN in verdicts, "a bill honoured under an agreement"
    assert OVER_TOTAL in verdicts, "and one held back by it"
    assert "metered below billed" in rules, "the meter"
    assert "counterparty flagged" in rules, "the screen"
    assert "cheapest offer" in rules, "the market"
    assert "per-payment limit" in rules, "the budget"


def test_an_agreement_vouches_for_a_payee_the_tape_cannot():
    """The commission check asks "was this party meant to be paid?", and the
    settlement tape was its only evidence.

    The tape records x402 machine-service settlements and nothing else, so a
    contractor or a subscription vendor can never appear on it — and the check
    would report "paid somebody who never served this treasury" about every one
    of them. The audit's hardest finding, fired at the wrong target, on every
    obligation that is not a machine call.

    A commitment answers the question better than a receipt does: the receipt
    says money moved, the agreement says we intended it to — and its hash is on
    the decision and inside the transaction that paid it.
    """
    from index_api.ledger_audit import audit

    treasury = "0x" + "11" * 20
    # THE TAPE HAS TO SAY SOMETHING FIRST. `_commission` accuses only when
    # `served` is non-empty — `if served and vendor not in served` — because a
    # check that has searched nothing must not accuse any more than it may
    # report clean. So the false positive needs a treasury that HAS bought
    # machine services, which is every real one.
    tape = [{
        "payer": treasury, "seller": OTHER, "resource": "/compute/x",
        "amount_usdc": 1.0, "quantity": 10.0, "settled_at": AT - 86_400,
    }]
    paid_contractor = {
        "at": AT, "obligation_id": "ob-1", "vendor": PAYEE, "resource": RES,
        "category": "contractors", "intent": PAY, "billed_usdc": 140.0,
        "paid_usdc": 140.0, "tx": "0x" + "ee" * 32,
    }

    # No agreement, no receipt: the check fires, and rightly — nothing anywhere
    # says this payee was meant to be paid.
    out = audit([paid_contractor], tape, treasury=treasury, slug="acme",
                categories=("contractors",))
    assert [f for f in out["findings"] if f["error"] == "commission"], (
        "an unvouched payee should still be reported"
    )

    # With the agreement on the record, it does not.
    vouched = {**paid_contractor, "commitment_id": "c-1", "commitment_verdict": WITHIN}
    out = audit([vouched], tape, treasury=treasury, slug="acme",
                categories=("contractors",))
    assert not [f for f in out["findings"] if f["error"] == "commission"], (
        "an agreement is better evidence than a receipt"
    )


def test_a_broken_agreement_does_not_vouch_for_anybody():
    """Only `within` vouches. A bill that breached its agreement is the opposite
    of evidence that the payee was meant to be paid that much."""
    from index_api.ledger_audit import audit

    breached = {
        "at": AT, "obligation_id": "ob-2", "vendor": PAYEE, "resource": RES,
        "category": "contractors", "intent": PAY, "billed_usdc": 200.0,
        "paid_usdc": 200.0, "tx": "0x" + "ff" * 32,
        "commitment_id": "c-1", "commitment_verdict": OVER_TOTAL,
    }
    treasury = "0x" + "11" * 20
    tape = [{
        "payer": treasury, "seller": OTHER, "resource": "/compute/x",
        "amount_usdc": 1.0, "quantity": 10.0, "settled_at": AT - 86_400,
    }]
    out = audit([breached], tape, treasury=treasury, slug="acme",
                categories=("contractors",))
    assert [f for f in out["findings"] if f["error"] == "commission"]
