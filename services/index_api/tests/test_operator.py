"""The spend operator's decision — every branch, and the ORDER they fire in.

The order is the design, so it gets its own tests: a duplicate that is also
over-par and over-budget must refuse as a duplicate, a meter discrepancy must
outrank a pricing question, and the budget must be reached last. A suite that
only checked each rule in isolation would pass on an implementation that
evaluates them in any order, which is how an agent ends up rerouting an invoice
it had already paid.
"""

from __future__ import annotations

import json

import pytest
from index_api.operator import (
    ESCALATE,
    HOLD,
    PAY,
    REFUSE,
    REROUTE,
    Obligation,
    decide,
    is_duplicate,
    log_decision,
    meter_quantity,
    run_obligation,
)
from index_api.par import Quote, par_from_quotes

VENDOR = "0x" + "aa" * 20
OTHER = "0x" + "bb" * 20
THIRD = "0x" + "cc" * 20
ZERO = "0x" + "00" * 20
RES = "https://acr.example/compute/inference"
NOW = 1_790_000_000.0


def _par(*prices_by_seller, exclude=VENDOR):
    quotes = [
        Quote(seller=s, price_usdc=p, source="catalog", resource=RES)
        for s, p in prices_by_seller
    ]
    return par_from_quotes(RES, quotes, exclude_seller=exclude)


def _ob(**kw) -> Obligation:
    base = dict(
        obligation_id="ob-1",
        vendor=VENDOR,
        billed_usdc=1.0,
        category="infra",
        resource=RES,
    )
    base.update(kw)
    return Obligation(**base)


AT_PAR = ((OTHER, 1.0), (THIRD, 1.0))
CHEAPER = ((OTHER, 0.5), (THIRD, 0.6))


# --- the happy path --------------------------------------------------------

def test_a_fair_bill_inside_budget_is_paid_and_says_why():
    d = decide(
        _ob(),
        par=_par(*AT_PAR),
        remaining_usdc=100.0,
        per_tx_limit_usdc=50.0,
        now=NOW,
    )
    assert d.intent == PAY
    assert d.escalated is False
    assert "at par" in d.rule and "2 observed sellers" in d.rule
    assert d.par_usdc == pytest.approx(1.0)


# --- 1 · duplicates, first -------------------------------------------------

def test_a_reference_we_already_settled_is_refused():
    d = decide(_ob(), par=_par(*AT_PAR), settled_refs={"ob-1"}, now=NOW)
    assert d.intent == REFUSE and "already settled" in d.rule


def test_a_resent_invoice_keeps_its_own_reference_and_is_caught_by_it():
    """The two duplicate shapes differ: a re-sent invoice keeps the vendor's ref
    and gets a fresh id from us. Checking only our id catches half of them."""
    ob = _ob(obligation_id="ob-NEW", invoice_ref="INV-77")
    assert is_duplicate(ob, {"INV-77"}) is True
    d = decide(ob, par=_par(*AT_PAR), settled_refs={"INV-77"}, now=NOW)
    assert d.intent == REFUSE and "already settled" in d.rule


def test_a_duplicate_that_is_also_overpriced_and_over_budget_refuses_as_a_duplicate():
    """Order. Rerouting a bill we already paid sends money to a stranger."""
    d = decide(
        _ob(billed_usdc=9.0),
        par=_par(*CHEAPER),
        settled_refs={"ob-1"},
        remaining_usdc=0.0,
        per_tx_limit_usdc=0.5,
        now=NOW,
    )
    assert d.intent == REFUSE
    assert "already settled" in d.rule
    assert d.reroute_to == "" and d.escalated is False


# --- 2 · payable at all ----------------------------------------------------

def test_the_zero_address_is_not_a_payee():
    """An unparsed payee field renders as 0x000…0, and a payment to it is burnt
    rather than refused."""
    d = decide(_ob(vendor=ZERO), par=_par(*AT_PAR, exclude=ZERO), now=NOW)
    assert d.intent == REFUSE and "no payee" in d.rule


def test_a_bill_for_nothing_is_not_a_bill():
    d = decide(_ob(billed_usdc=0.0), par=_par(*AT_PAR), now=NOW)
    assert d.intent == REFUSE and "no amount" in d.rule


# --- 3 · the meter, before the price ---------------------------------------

def test_a_bill_for_more_than_we_counted_goes_to_a_human():
    """Prior Art #06, the agoranomoi: the vendor supplies the goods and the
    measuring cup, so the agent keeps its own."""
    d = decide(
        _ob(vendor_quantity=1_000.0),
        metered_quantity=900.0,
        par=_par(*AT_PAR),
        remaining_usdc=100.0,
        now=NOW,
    )
    assert d.intent == ESCALATE and d.escalated is True
    assert "metered below billed" in d.rule
    assert d.discrepancy == pytest.approx(100.0)


def test_no_record_of_consuming_it_is_a_question_not_an_accusation():
    """None is not zero. Zero would assert we consumed nothing and make every
    bill look fraudulent."""
    d = decide(
        _ob(vendor_quantity=1_000.0),
        metered_quantity=None,
        par=_par(*AT_PAR),
        remaining_usdc=100.0,
        now=NOW,
    )
    assert d.intent == ESCALATE and "unmetered" in d.rule
    assert "below billed" not in d.rule, "we did not accuse them of overbilling"


def test_a_difference_inside_tolerance_does_not_stop_the_payment():
    d = decide(
        _ob(vendor_quantity=1_001.0),
        metered_quantity=1_000.0,
        par=_par(*AT_PAR),
        remaining_usdc=100.0,
        per_tx_limit_usdc=50.0,
        now=NOW,
    )
    assert d.intent == PAY
    assert d.discrepancy == pytest.approx(1.0)


def test_a_vendor_who_underbilled_is_noted_and_paid():
    d = decide(
        _ob(vendor_quantity=500.0),
        metered_quantity=1_000.0,
        par=_par(*AT_PAR),
        remaining_usdc=100.0,
        per_tx_limit_usdc=50.0,
        now=NOW,
    )
    assert d.intent == PAY
    assert any("under our count" in n for n in d.notes)


def test_the_meter_outranks_the_price():
    """Order. A bill for work nobody did is not a pricing question, so it must
    not come back as 'reroute to a cheaper seller'."""
    d = decide(
        _ob(billed_usdc=9.0, vendor_quantity=1_000.0),
        metered_quantity=100.0,
        par=_par(*CHEAPER),
        remaining_usdc=100.0,
        now=NOW,
    )
    assert d.intent == ESCALATE and "metered below billed" in d.rule
    assert d.reroute_to == ""


# --- 4 · a price we cannot check -------------------------------------------

def test_a_small_unbenchmarked_bill_is_paid_with_the_gap_recorded():
    """A brand-new vendor has no market yet and somebody still has to be paid."""
    d = decide(
        _ob(billed_usdc=0.5),
        par=_par((OTHER, 1.0)),  # one seller -> ONE_SELLER
        remaining_usdc=100.0,
        per_tx_limit_usdc=50.0,
        now=NOW,
        unbenchmarked_max_usdc=1.0,
    )
    assert d.intent == PAY
    assert any("unbenchmarked (ONE_SELLER)" in n for n in d.notes)
    assert d.par_usdc is None, "no benchmark may be reported as a number"


def test_a_large_unbenchmarked_bill_goes_to_a_human():
    """The unpriceable case must not quietly become the expensive one."""
    d = decide(
        _ob(billed_usdc=5.0),
        par=_par((OTHER, 1.0)),
        remaining_usdc=100.0,
        now=NOW,
        unbenchmarked_max_usdc=1.0,
    )
    assert d.intent == ESCALATE and "unbenchmarked (ONE_SELLER)" in d.rule
    assert "ceiling" in d.rule


def test_no_par_at_all_is_handled_like_an_unbenchmarked_one():
    d = decide(_ob(billed_usdc=5.0), par=None, remaining_usdc=100.0, now=NOW)
    assert d.intent == ESCALATE and "NO_PAR" in d.rule


# --- 5 · the price ---------------------------------------------------------

def test_an_overpriced_bill_with_a_cheaper_seller_is_rerouted():
    d = decide(
        _ob(billed_usdc=1.0),
        par=_par(*CHEAPER),
        remaining_usdc=100.0,
        per_tx_limit_usdc=50.0,
        now=NOW,
    )
    assert d.intent == REROUTE
    assert d.reroute_to == OTHER
    assert d.saving_usdc == pytest.approx(0.5)
    assert "cheapest offer" in d.rule and "saving" in d.rule


def test_an_overpriced_bill_with_nowhere_else_to_go_is_escalated():
    """No alternative means no saving to claim, so this is a judgement call and
    it is not the agent's."""
    par = _par((OTHER, 0.5))  # one independent seller -> unbenchmarked
    d = decide(
        _ob(billed_usdc=0.9), par=par, remaining_usdc=100.0, now=NOW,
        unbenchmarked_max_usdc=0.1,
    )
    assert d.intent == ESCALATE


# --- 6 · the timing --------------------------------------------------------

def test_a_bill_not_due_for_weeks_with_no_discount_is_held():
    d = decide(
        _ob(due_at=NOW + 30 * 86_400),
        par=_par(*AT_PAR),
        remaining_usdc=100.0,
        per_tx_limit_usdc=50.0,
        now=NOW,
    )
    assert d.intent == HOLD
    assert "not due for 30 days" in d.rule


def test_an_early_pay_discount_beats_holding_the_cash():
    d = decide(
        _ob(due_at=NOW + 30 * 86_400, early_pay_discount=0.02),
        par=_par(*AT_PAR),
        remaining_usdc=100.0,
        per_tx_limit_usdc=50.0,
        now=NOW,
    )
    assert d.intent == PAY
    assert any("2% discount" in n for n in d.notes)


def test_a_bill_due_inside_the_window_is_paid():
    d = decide(
        _ob(due_at=NOW + 86_400),
        par=_par(*AT_PAR),
        remaining_usdc=100.0,
        per_tx_limit_usdc=50.0,
        now=NOW,
    )
    assert d.intent == PAY


# --- 7 · the budget, last --------------------------------------------------

def test_a_payment_at_the_per_transaction_limit_is_the_owners_to_make():
    d = decide(
        _ob(billed_usdc=50.0),
        par=_par((OTHER, 50.0), (THIRD, 50.0)),
        remaining_usdc=1_000.0,
        per_tx_limit_usdc=50.0,
        now=NOW,
    )
    assert d.intent == ESCALATE and d.escalated is True
    assert "the owner signs this one" in d.rule


def test_a_payment_past_the_period_cap_is_escalated_not_trimmed():
    d = decide(
        _ob(billed_usdc=10.0),
        par=_par((OTHER, 10.0), (THIRD, 10.0)),
        remaining_usdc=4.0,
        per_tx_limit_usdc=50.0,
        now=NOW,
    )
    assert d.intent == ESCALATE
    assert "over budget" in d.rule
    assert d.paid_usdc == 0.0, "never a partial payment nobody authorized"


def test_the_price_outranks_the_budget():
    """Order. An overpriced bill that is also over budget should come back as
    'there is a cheaper seller', which is actionable, rather than as 'no money',
    which is not."""
    d = decide(
        _ob(billed_usdc=1.0),
        par=_par(*CHEAPER),
        remaining_usdc=0.0,
        per_tx_limit_usdc=0.5,
        now=NOW,
    )
    assert d.intent == REROUTE and d.reroute_to == OTHER


# --- the record ------------------------------------------------------------

def test_the_hashed_record_cannot_contain_its_own_payment():
    """PolicyWallet stores the hash BEFORE the money moves, so a record carrying
    its own transaction hash could never be committed in time."""
    d = decide(_ob(), par=_par(*AT_PAR), remaining_usdc=100.0, per_tx_limit_usdc=50.0, now=NOW)
    rec = d.as_record()
    assert "tx" not in rec and "paid_usdc" not in rec
    assert rec["rule"] and rec["intent"] == PAY
    assert rec["billed_usdc"] == 1.0


def test_every_outcome_names_the_check_that_fired():
    """A record without a rule is a decision nobody can review."""
    cases = [
        decide(_ob(), par=_par(*AT_PAR), settled_refs={"ob-1"}, now=NOW),
        decide(_ob(vendor=ZERO), par=None, now=NOW),
        decide(_ob(billed_usdc=0.0), par=None, now=NOW),
        decide(_ob(vendor_quantity=10.0), metered_quantity=1.0, par=_par(*AT_PAR), now=NOW),
        decide(_ob(billed_usdc=5.0), par=None, remaining_usdc=100.0, now=NOW),
        decide(_ob(), par=_par(*CHEAPER), remaining_usdc=100.0, per_tx_limit_usdc=50.0, now=NOW),
        decide(_ob(due_at=NOW + 9e6), par=_par(*AT_PAR), remaining_usdc=100.0,
               per_tx_limit_usdc=50.0, now=NOW),
        decide(_ob(billed_usdc=99.0), par=_par((OTHER, 99.0), (THIRD, 99.0)),
               remaining_usdc=1e6, per_tx_limit_usdc=50.0, now=NOW),
        decide(_ob(), par=_par(*AT_PAR), remaining_usdc=100.0, per_tx_limit_usdc=50.0, now=NOW),
    ]
    for d in cases:
        assert d.rule, f"{d.intent} with no rule"
        assert d.intent in {PAY, HOLD, REROUTE, ESCALATE, REFUSE}


# --- the meter's own arithmetic -------------------------------------------

def test_no_receipts_at_all_is_none_rather_than_zero():
    assert meter_quantity([], VENDOR, RES) is None


def test_a_vendor_we_have_paid_but_without_a_quantity_counts_as_zero_not_unknown():
    """We have a record of consuming it, so the count is checkable; it is the
    quantity that is missing, and 0 is the honest sum of no quantities."""
    rows = [{"seller": VENDOR, "resource": RES, "settled_at": 1.0}]
    assert meter_quantity(rows, VENDOR, RES) == 0.0


def test_the_meter_sums_only_this_vendor_this_resource_and_this_window():
    rows = [
        {"seller": VENDOR, "resource": RES, "quantity": 10.0, "settled_at": 100.0},
        {"seller": VENDOR, "resource": RES, "quantity": 5.0, "settled_at": 200.0},
        {"seller": VENDOR, "resource": RES, "quantity": 99.0, "settled_at": 1.0},  # too old
        {"seller": OTHER, "resource": RES, "quantity": 7.0, "settled_at": 150.0},  # not them
        {"seller": VENDOR, "resource": "other", "quantity": 3.0, "settled_at": 150.0},
    ]
    assert meter_quantity(rows, VENDOR, RES, since=50.0) == pytest.approx(15.0)


def test_the_meter_matches_a_vendor_whatever_case_the_address_arrived_in():
    rows = [{"seller": VENDOR.upper(), "resource": RES, "quantity": 4.0, "settled_at": 9.0}]
    assert meter_quantity(rows, VENDOR.lower(), RES) == pytest.approx(4.0)


# --- the log ---------------------------------------------------------------

def test_the_decision_log_is_appendable_and_readable(tmp_path):
    """Appends, and creates the directory it was pointed at.

    This used to write ONE decision twice and assert two rows. It no longer
    does, because the log now records decisions rather than ticks: a row that
    only restates the last decision about the same bill is dropped. An unpaid
    obligation is re-decided on every run by design, and on an hourly schedule
    the old behaviour was twenty-four identical rows a day per open bill —
    `work.decisions` climbing while nothing had happened. So appendability is
    shown with two DIFFERENT decisions, which is what it was always about.
    """
    path = tmp_path / "nested" / "decisions.jsonl"
    first = decide(_ob(), par=_par(*AT_PAR), remaining_usdc=100.0,
                   per_tx_limit_usdc=50.0, now=NOW)
    log_decision(first, str(path))
    log_decision(first, str(path))  # the same tick again: not a second decision

    second = decide(_ob(obligation_id="ob-2"), par=_par(*AT_PAR), remaining_usdc=100.0,
                    per_tx_limit_usdc=50.0, now=NOW)
    log_decision(second, str(path))

    rows = [json.loads(ln) for ln in path.read_text().splitlines()]
    assert len(rows) == 2, "one row per decision, not one per write"
    assert [r["obligation_id"] for r in rows] == ["ob-1", "ob-2"]
    assert rows[0]["intent"] == PAY and rows[0]["rule"]


def test_a_log_that_cannot_be_written_does_not_stop_a_cleared_payment():
    """The authoritative record is the on-chain hash. A full disk must not turn
    a payment that cleared policy into a crash."""
    d = decide(_ob(), par=_par(*AT_PAR), remaining_usdc=100.0, per_tx_limit_usdc=50.0, now=NOW)
    log_decision(d, "/proc/definitely/not/writable/decisions.jsonl")


# --- the driver ------------------------------------------------------------

class _FakePolicy:
    """Stands in for PolicyClient: the two methods the driver actually uses."""

    def __init__(self, remaining=100.0, per_tx=50.0, budget=True):
        self._budget = (
            {
                "category": "infra",
                "cap_usdc": 1_000.0,
                "spent_usdc": 0.0,
                "remaining_usdc": remaining,
                "per_tx_limit_usdc": per_tx,
                "period_start": 0,
                "period_length": 0,
            }
            if budget
            else None
        )
        self.calls: list[tuple] = []

    def budget(self, category):
        return self._budget

    def spend(self, category, to, amount, record):
        self.calls.append((category, to, amount, record))
        return "0x" + "ab" * 32


CATALOG = {
    "items": [
        {"resource": RES, "accepts": [{"amount": "1000000", "payTo": VENDOR}]},
        {"resource": RES, "accepts": [{"amount": "1000000", "payTo": OTHER}]},
        {"resource": RES, "accepts": [{"amount": "1000000", "payTo": THIRD}]},
    ]
}
RECEIPTS = [
    {"seller": VENDOR, "resource": RES, "quantity": 1_000.0, "amount_usdc": 1.0,
     "settled_at": NOW - 100},
]


def test_a_dry_run_clears_policy_and_sends_nothing(tmp_path):
    pol = _FakePolicy()
    d = run_obligation(
        _ob(vendor_quantity=1_000.0),
        receipts=RECEIPTS,
        catalog=CATALOG,
        policy=pol,
        now=NOW,
        log_path=str(tmp_path / "d.jsonl"),
    )
    assert d.intent == PAY
    assert pol.calls == [], "dry run is the default and it means no payment"
    assert d.tx is None and d.paid_usdc == 0.0
    assert any("dry run" in n for n in d.notes)


def test_a_live_run_pays_through_the_contract_and_records_the_hash_input(tmp_path):
    pol = _FakePolicy()
    d = run_obligation(
        _ob(vendor_quantity=1_000.0),
        receipts=RECEIPTS,
        catalog=CATALOG,
        policy=pol,
        now=NOW,
        dry_run=False,
        log_path=str(tmp_path / "d.jsonl"),
    )
    assert d.intent == PAY
    assert len(pol.calls) == 1
    category, to, amount, record = pol.calls[0]
    assert (category, to, amount) == ("infra", VENDOR, 1.0)
    assert record["rule"] and record["intent"] == PAY
    assert "tx" not in record, "the commitment cannot contain its own transaction"
    assert d.tx is not None and d.paid_usdc == pytest.approx(1.0)


def test_no_budget_on_chain_is_no_authority_not_a_budget_of_zero(tmp_path):
    """Zero would read as 'this category is exhausted this period', which a human
    waits out. 'No budget' is something they have to go and fix."""
    pol = _FakePolicy(budget=False)
    d = run_obligation(
        _ob(),
        receipts=RECEIPTS,
        catalog=CATALOG,
        policy=pol,
        now=NOW,
        log_path=str(tmp_path / "d.jsonl"),
    )
    assert d.intent == ESCALATE and d.escalated is True
    assert "no budget on chain" in d.rule
    assert pol.calls == []


def test_an_escalation_never_reaches_the_wallet(tmp_path):
    pol = _FakePolicy(per_tx=0.5)  # 1.0 billed is at/above the limit
    d = run_obligation(
        _ob(),
        receipts=RECEIPTS,
        catalog=CATALOG,
        policy=pol,
        now=NOW,
        dry_run=False,
        log_path=str(tmp_path / "d.jsonl"),
    )
    assert d.intent == ESCALATE and "the owner signs this one" in d.rule
    assert pol.calls == [], "nothing is sent for a decision that is not the agent's"


def test_the_driver_meters_from_our_own_receipts_and_prices_off_the_catalog(tmp_path):
    pol = _FakePolicy()
    d = run_obligation(
        _ob(vendor_quantity=1_000.0),
        receipts=RECEIPTS,
        catalog=CATALOG,
        policy=pol,
        now=NOW,
        log_path=str(tmp_path / "d.jsonl"),
    )
    assert d.metered_quantity == pytest.approx(1_000.0), "our own count"
    assert d.par_usdc == pytest.approx(1.0), "from the other two sellers"
    # The vendor's own listing must not be among the alternatives.
    assert d.best_usdc == pytest.approx(1.0)


def test_the_driver_catches_an_overbilling_vendor_end_to_end(tmp_path):
    """The whole product in one call: they billed for 5,000 units, our own
    receipts account for 1,000, and nothing is paid."""
    pol = _FakePolicy()
    d = run_obligation(
        _ob(vendor_quantity=5_000.0),
        receipts=RECEIPTS,
        catalog=CATALOG,
        policy=pol,
        now=NOW,
        dry_run=False,
        log_path=str(tmp_path / "d.jsonl"),
    )
    assert d.intent == ESCALATE
    assert "metered below billed" in d.rule
    assert d.discrepancy == pytest.approx(4_000.0)
    assert pol.calls == []


def test_the_driver_runs_without_a_wallet_at_all(tmp_path):
    """No policy means no budget check — the price and meter decisions still
    stand, which is what a business evaluating the operator sees first.

    AND THE RECORD SAYS SO. This test used to assert the intent and never read
    the sentence, while `_pay_rule` ended every `pay` line ", inside budget"
    unconditionally. So this exact path — and every `dry` keeper tick, which
    passes `policy=None` — wrote a row claiming a budget check that had not
    run. Asserted here rather than in a test of its own, so the suite count
    the claims audit gates does not move.
    """
    d = run_obligation(
        _ob(vendor_quantity=1_000.0),
        receipts=RECEIPTS,
        catalog=CATALOG,
        policy=None,
        now=NOW,
        log_path=str(tmp_path / "d.jsonl"),
    )
    assert d.intent == PAY
    assert d.par_usdc == pytest.approx(1.0)
    assert d.per_tx_limit_usdc is None and d.remaining_usdc is None
    assert "inside budget" not in d.rule
    assert "no budget was consulted" in d.rule


def test_every_driver_outcome_is_written_down(tmp_path):
    path = tmp_path / "audit.jsonl"
    run_obligation(_ob(), receipts=RECEIPTS, catalog=CATALOG,
                   policy=_FakePolicy(), now=NOW, log_path=str(path))
    run_obligation(_ob(obligation_id="ob-2"), receipts=RECEIPTS, catalog=CATALOG,
                   policy=_FakePolicy(budget=False), now=NOW, log_path=str(path))

    rows = [json.loads(ln) for ln in path.read_text().splitlines()]
    assert len(rows) == 2
    assert rows[0]["intent"] == PAY
    assert rows[1]["intent"] == ESCALATE
    assert all(r["rule"] for r in rows), "including the refusals"


def test_a_reroute_rule_never_contradicts_itself(tmp_path):
    """Measured on real fills: a bill can sit BELOW the median and still have a
    cheaper offer under it, so a rule phrased as "over par by N bp" printed
    "over par by -246 bp". The sentence now quotes the gap to the cheapest
    offer, which is the comparison the decision was actually made on."""
    # 2.00 is under the median of [1.00, 3.00, 3.00] = 3.00, yet 1.00 is on offer.
    par = _par((OTHER, 1.0), (THIRD, 3.0), ("0x" + "dd" * 20, 3.0))
    d = decide(
        _ob(billed_usdc=2.0), par=par, remaining_usdc=100.0,
        per_tx_limit_usdc=50.0, now=NOW,
    )
    assert d.intent == REROUTE
    assert d.over_par_bp is not None and d.over_par_bp < 0, "genuinely below the median"
    assert "-" not in d.rule.split("bp")[0], "no negative basis points in the prose"
    assert "cheapest offer" in d.rule
    assert d.saving_usdc == pytest.approx(1.0)


# --- 4 · the counterparty, before the price -------------------------------

def _screen(risk, matched=(), reason="", screened=True):
    from index_api.counterparty import CounterpartyVerdict

    return CounterpartyVerdict(
        address=VENDOR, risk=risk, backend="test",
        matched=tuple(matched), reason=reason, screened=screened,
    )


def test_a_flagged_counterparty_is_not_a_payment_the_agent_makes():
    """RFB 2 wants screening built into the path, not run once at onboarding."""
    d = decide(
        _ob(), par=_par(*AT_PAR), screen=_screen("flagged", ("us_ofac_sdn",)),
        remaining_usdc=100.0, per_tx_limit_usdc=50.0, now=NOW,
    )
    assert d.intent == ESCALATE and d.escalated is True
    assert "counterparty flagged" in d.rule and "us_ofac_sdn" in d.rule
    assert d.screen_risk == "flagged"
    assert d.screen_matched == ["us_ofac_sdn"]


def test_the_counterparty_outranks_the_price():
    """Order. A vendor we may not pay is not a pricing question, so a flagged
    address must not come back as "there is a cheaper seller"."""
    d = decide(
        _ob(billed_usdc=9.0), par=_par(*CHEAPER),
        screen=_screen("flagged", ("eu_fsf",)),
        remaining_usdc=100.0, now=NOW,
    )
    assert d.intent == ESCALATE and "counterparty flagged" in d.rule
    assert d.reroute_to == ""


def test_the_counterparty_outranks_the_budget():
    """"Over budget" is the wrong headline for a sanctioned address."""
    d = decide(
        _ob(billed_usdc=99.0), par=_par((OTHER, 99.0), (THIRD, 99.0)),
        screen=_screen("flagged", ("us_ofac_sdn",)),
        remaining_usdc=0.0, per_tx_limit_usdc=1.0, now=NOW,
    )
    assert "counterparty flagged" in d.rule
    assert "budget" not in d.rule and "owner signs" not in d.rule


def test_the_meter_still_outranks_the_counterparty():
    """A bill for work nobody did is refused before we ask who they are: the
    cheaper, more damning check comes first."""
    d = decide(
        _ob(vendor_quantity=1_000.0), metered_quantity=10.0,
        par=_par(*AT_PAR), screen=_screen("flagged", ("us_ofac_sdn",)),
        remaining_usdc=100.0, now=NOW,
    )
    assert "metered below billed" in d.rule


def test_a_clear_counterparty_is_recorded_on_the_decision():
    """In the hashed record, so the commitment says what was known about the
    counterparty when the money moved."""
    d = decide(
        _ob(), par=_par(*AT_PAR), screen=_screen("clear"),
        remaining_usdc=100.0, per_tx_limit_usdc=50.0, now=NOW,
    )
    assert d.intent == PAY
    assert d.screen_risk == "clear"
    assert "screen_risk" in d.as_record()


def test_an_unscreened_payment_records_the_gap_rather_than_hiding_it():
    d = decide(
        _ob(), par=_par(*AT_PAR),
        screen=_screen("unknown", reason="the screen could not answer", screened=False),
        remaining_usdc=100.0, per_tx_limit_usdc=50.0, now=NOW,
    )
    assert d.intent == PAY, "an outage does not stop the business by default"
    assert d.screen_risk == "unknown"
    assert any("not screened" in n for n in d.notes)


def test_no_screen_at_all_is_also_written_down():
    d = decide(
        _ob(), par=_par(*AT_PAR), screen=None,
        remaining_usdc=100.0, per_tx_limit_usdc=50.0, now=NOW,
    )
    assert any("no counterparty screen" in n for n in d.notes)
    assert d.screen_risk == "", "absent, not guessed"


def test_screening_required_turns_an_outage_into_an_escalation(monkeypatch):
    from index_api import counterparty as cp

    monkeypatch.setattr(cp, "REQUIRED", True)
    d = decide(
        _ob(), par=_par(*AT_PAR),
        screen=_screen("unknown", reason="timed out", screened=False),
        remaining_usdc=100.0, per_tx_limit_usdc=50.0, now=NOW,
    )
    assert d.intent == ESCALATE
    assert "counterparty unknown" in d.rule


# --- which market a bill is judged against ---------------------------------
#
# Two price populations exist for one unit and they sit two orders of magnitude
# apart: the fleet this deployment operates, priced off `IndexSpec.reference_level`
# which `anchors/GAP.md` measures 10x to 1250x above market, and the real
# published prices in `anchors/`. Medianing them together would produce exactly
# the incomparable number `par.py` exists to refuse.

UNIT = "$/1k tokens"
#: THREE rows, not two, and the reason is `par.py`'s documented quirk:
#: `weighted_median` is the 50% quantile, so on an even sample it returns the
#: LOWER middle price — with two rows the median IS the cheapest, `par == best`,
#: and the at-the-rate case this fixture exists to exercise can never arise.
MARKET_ROWS = [
    {"id": "a/cheap", "price_usd_per_unit": 0.0002, "quality": {"model_class": "mid"}},
    {"id": "b/mid", "price_usd_per_unit": 0.0004, "quality": {"model_class": "mid"}},
    {"id": "c/dear", "price_usd_per_unit": 0.0006, "quality": {"model_class": "mid"}},
]


def _anchors(tmp_path):
    d = tmp_path / "anchors" / "ACR-INF"
    # `exist_ok`, because the two-populations test prices twice under one
    # tmp_path and the basket is the same both times.
    d.mkdir(parents=True, exist_ok=True)
    (d / "2026-10-06.json").write_text(json.dumps({
        "index_id": "ACR-INF", "unit": UNIT,
        "fetched_at": "2026-10-06T00:00:00+00:00",
        "rows": MARKET_ROWS, "missing": [],
    }))
    return tmp_path / "anchors"


#: Fleet-scale fills for the same unit — three sellers around 0.5, which is
#: `reference_level` and roughly 1250x the market rows above.
FLEET_FILLS = [
    {"seller": OTHER, "unit": UNIT, "quantity": 10.0, "amount_usdc": 4.8,
     "resource": RES, "settled_at": NOW - 100},
    {"seller": THIRD, "unit": UNIT, "quantity": 10.0, "amount_usdc": 5.2,
     "resource": RES, "settled_at": NOW - 100},
]


def _priced(tmp_path, vendor, billed, monkeypatch):
    import index_api.par as par_mod

    monkeypatch.setattr(par_mod, "_ANCHOR_DIR", _anchors(tmp_path))
    ob = _ob(vendor=vendor, billed_usdc=billed, unit=UNIT, vendor_quantity=10.0)
    return run_obligation(
        ob, receipts=FLEET_FILLS, catalog={}, commitment=False,
        metered_quantity=10.0, now=NOW, log_path=str(tmp_path / "d.jsonl"),
    )


def test_our_own_seller_is_judged_against_our_own_fleet(tmp_path, monkeypatch):
    """Like-for-like. A fleet bill and the fleet's quotes are pinned to the same
    scale, so that comparison is sound inside the loop — and it is the only one
    that is. Benchmarking it against real market prices would report every
    settlement this project has ever made as a ~100,000 bp overpay."""
    from index_api.par import FLEET_SELLERS

    ours = sorted(FLEET_SELLERS)[0]
    d = _priced(tmp_path, ours, 5.2, monkeypatch)
    assert d.par_usdc is not None and d.par_usdc > 0.1, (
        f"a fleet bill was priced against the market basket: par={d.par_usdc}"
    )
    assert d.par_sellers == 2


def test_a_real_vendor_is_judged_against_real_published_prices(tmp_path, monkeypatch):
    """RFB 3's "what the market is actually paying", and the reason `par.py`
    exists: before the basket a real invoice had no benchmark at this scale at
    all, and measuring it against the index read as roughly -9,990 bp."""
    d = _priced(tmp_path, "0x" + "9a" * 20, 0.002 * 10, monkeypatch)
    assert d.par_usdc is not None and d.par_usdc < 0.001, (
        f"a real invoice was priced against our own fleet: par={d.par_usdc}"
    )
    assert d.par_sellers == 3


def test_the_two_populations_never_land_in_one_median(tmp_path, monkeypatch):
    """The failure this split exists to prevent. One median over both would sit
    between 0.0006 and 4.8 — a number describing no market that exists."""
    from index_api.par import FLEET_SELLERS

    ours = _priced(tmp_path, sorted(FLEET_SELLERS)[0], 5.2, monkeypatch)
    theirs = _priced(tmp_path, "0x" + "9a" * 20, 0.002 * 10, monkeypatch)
    assert ours.par_sellers == 2 and theirs.par_sellers == 3, (
        f"the two did not read different populations: {ours.par_sellers} vs {theirs.par_sellers}"
    )
    assert ours.par_usdc / theirs.par_usdc > 100, (
        "the two pars are not two populations apart, so something merged them"
    )


def test_a_market_reference_is_never_recorded_as_a_reroute(tmp_path, monkeypatch):
    """`openai/gpt-4o-mini` is a price a reader can check and an address nobody
    can pay. Writing it into `reroute_to` would record a routing the wallet
    cannot execute, and `applyReroute` would be handed a string to send USDC to."""
    d = _priced(tmp_path, "0x" + "9a" * 20, 0.002 * 10, monkeypatch)
    assert d.intent == ESCALATE and d.escalated is True
    assert d.recommended_intent == REFUSE
    assert d.reroute_to == "", f"a market reference became a payee: {d.reroute_to!r}"
    assert "addresses we can pay" in d.rule
    # And the rule quotes the RATE, not the cheapest row — against a basket
    # nothing is routable, so "something cheaper exists" is not the question.
    assert "going market rate" in d.rule
    assert d.over_par_bp and d.over_par_bp > 0


def test_a_bill_at_the_going_market_rate_is_not_escalated(tmp_path, monkeypatch):
    """The bug this split fixes, measured on the real basket before it was.

    Everywhere else the verdict keys off the CHEAPEST offer, because that is
    money genuinely available somewhere we can send it. Against a market basket
    nothing is available anywhere, so a bill priced at exactly the median was
    escalated for being dearer than `a/cheap` — which is a different model, not
    a better price. The basket medians mid-class rows on purpose.
    """
    # Median of the three rows is 0.0004; the cheapest is 0.0002. Bill at the
    # median exactly: 5000 bp above `best`, 0 bp above the rate.
    d = _priced(tmp_path, "0x" + "9a" * 20, 0.0004 * 10, monkeypatch)
    # Not `!= ESCALATE`, which is what this asserted first and which PASSED on a
    # broken implementation: falling out of the market branch dropped into the
    # reroute below, so the bill was routed to `a/cheap` — a model id the wallet
    # cannot pay. Assert the outcome, not the absence of one outcome.
    assert d.intent == PAY, f"a bill at the going rate was not paid: {d.intent} — {d.rule}"
    assert d.reroute_to == "", f"a market reference became a payee: {d.reroute_to!r}"
    assert any("going market rate" in n for n in d.notes), d.notes


def test_owner_inbox_escalates_even_an_affordable_payable_bill(tmp_path, monkeypatch):
    from index_api import payments

    monkeypatch.setattr(payments, "owner_inbox_business", lambda business: True)
    policy = _FakePolicy()
    decision = run_obligation(
        _ob(vendor_quantity=1_000.0),
        receipts=RECEIPTS,
        catalog=CATALOG,
        policy=policy,
        now=NOW,
        dry_run=False,
        log_path=str(tmp_path / "owner-inbox.jsonl"),
    )
    assert decision.intent == ESCALATE
    assert decision.recommended_intent == PAY
    assert decision.escalated
    assert policy.calls == []
    assert decision.tx is None and decision.paid_usdc == 0
