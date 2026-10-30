"""Cash against what is coming — the module, and the rung it added to the ladder.

RFB 4's list of what the agent decides opens with "whether there is enough
liquidity to cover what is due, and what is due next", and until this module
nothing in the operator asked it. What it had was a BUDGET: `cap - spent`, from
the contract's counters, which is permission and reads healthy on a wallet
holding nothing.

So the claims worth testing are the ones where the two questions diverge, plus
the three ways a cash figure goes quietly wrong: the wrong number of decimals,
a failed read rendered as zero, and an undated bill folded in as though its date
were merely later.

Hermetic. The balance arrives as an argument, the way `par` and `commitment` do.
"""

from __future__ import annotations

import pytest
from index_api.liquidity import HORIZON_S, Liquidity, assess, short_by
from index_api.operator import ESCALATE, HOLD, PAY, Obligation, decide
from index_api.par import Quote, par_from_quotes

VENDOR = "0x" + "aa" * 20
OTHER = "0x" + "bb" * 20
THIRD = "0x" + "cc" * 20
RES = "https://acr.example/compute/inference"
NOW = 1_790_000_000.0
DAY = 86_400.0


def _par(price: float = 1.0):
    """Two independent sellers at `price`.

    The price is a parameter because check 6 sits ABOVE the cash check: a bill
    priced at 6 against a par of 1 reroutes long before anything looks at the
    balance, so a cash test that left par at 1 would be testing the pricing
    rung and reporting on this one.
    """
    quotes = [
        Quote(seller=s, price_usdc=price, source="catalog", resource=RES)
        for s in (OTHER, THIRD)
    ]
    return par_from_quotes(RES, quotes, exclude_seller=VENDOR)


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


# --- what is counted, and what is deliberately not -------------------------

def test_a_dated_bill_inside_the_horizon_is_what_due_means():
    liq = assess(10.0, [_ob(billed_usdc=4.0, due_at=NOW + 5 * DAY)], NOW)
    assert liq.due_usdc == pytest.approx(4.0)
    assert liq.due_count == 1
    assert liq.soonest_at == pytest.approx(NOW + 5 * DAY)
    assert liq.undated == 0
    assert liq.covers_due is True


def test_an_undated_bill_is_counted_apart_and_never_inside_the_total():
    """"We do not know when this is due" is not "it is due later".

    Folding an undated bill into `due_usdc` would understate nothing and
    overstate the precision: the figure would look like a dated total while
    carrying a bill that might be owed tomorrow or next year. Counting them
    apart lets the figure say what it does not cover, which is the same reason
    the ledger audit prints `searched` beside `found`.
    """
    liq = assess(10.0, [_ob(billed_usdc=99.0), _ob(billed_usdc=99.0)], NOW)
    assert liq.due_usdc == 0.0
    assert liq.due_count == 0
    assert liq.undated == 2
    assert liq.soonest_at is None
    # And the owner is NOT told they are short, because nothing dated is owed.
    assert liq.covers_due is True


def test_a_bill_past_due_is_the_most_due_thing_there_is():
    """A horizon that only looked forward would drop the overdue bills, which
    are exactly the ones a person needs to see first."""
    liq = assess(1.0, [_ob(billed_usdc=7.0, due_at=NOW - 30 * DAY)], NOW)
    assert liq.due_usdc == pytest.approx(7.0)
    assert liq.due_count == 1
    assert liq.covers_due is False


def test_a_bill_beyond_the_horizon_is_not_yet_this_window_s_problem():
    far = assess(1.0, [_ob(billed_usdc=500.0, due_at=NOW + HORIZON_S + DAY)], NOW)
    assert far.due_usdc == 0.0 and far.due_count == 0 and far.undated == 0
    near = assess(1.0, [_ob(billed_usdc=500.0, due_at=NOW + HORIZON_S - DAY)], NOW)
    assert near.due_usdc == pytest.approx(500.0)


def test_the_horizon_is_its_own_constant_and_not_the_pay_window():
    """`PAY_WINDOW_S` answers "release the cash yet?" per bill; this answers "is
    the cash there at all". Sharing one constant would make tuning either one
    silently move the other."""
    from index_api.operator import PAY_WINDOW_S

    assert HORIZON_S != PAY_WINDOW_S
    assert HORIZON_S == pytest.approx(30 * DAY)


def test_an_obligation_may_arrive_as_a_dataclass_or_as_a_logged_row():
    """The ladder hands this `Obligation`s; the statement hands it dicts read
    back out of the decision log. Both carry the same two field names."""
    row = {"billed_usdc": 4.0, "due_at": NOW + DAY, "intent": "escalate"}
    assert assess(10.0, [row], NOW).due_usdc == pytest.approx(4.0)
    assert assess(10.0, [{"billed_usdc": 4.0}], NOW).undated == 1


# --- a failed read is not an empty wallet ----------------------------------

def test_an_unreadable_balance_stays_none_and_never_becomes_zero():
    """An unfunded wallet and an unreachable node are different things to do on
    a Monday morning. A confident 0.0 from a read that failed is the kind of
    number the traction page already refuses to print."""
    liq = assess(None, [_ob(billed_usdc=4.0, due_at=NOW + DAY)], NOW)
    assert liq.held_usdc is None
    assert liq.measured is False
    # Not False. "We do not know" is a third answer and the surface renders it.
    assert liq.covers_due is None
    assert liq.as_dict()["held_usdc"] is None
    assert liq.as_dict()["covers_due"] is None
    # The dated side is still real: only the cash side failed.
    assert liq.as_dict()["due_usdc"] == pytest.approx(4.0)


def test_a_zero_balance_is_a_measurement_and_says_so():
    liq = assess(0.0, [_ob(billed_usdc=1.0, due_at=NOW + DAY)], NOW)
    assert liq.measured is True and liq.covers_due is False


# --- short_by --------------------------------------------------------------

def test_nothing_measured_is_not_evidence_of_a_shortfall():
    assert short_by(None, 5.0) == 0.0
    assert short_by(assess(None, [], NOW), 5.0) == 0.0


def test_the_bill_itself_is_netted_off_both_sides():
    """Paying a bill removes it from the cash AND from the queue, so a shortfall
    means the OTHER dated bills cannot be met afterwards — not that this one
    cannot be paid, which is check 8's question."""
    bills = [
        _ob(obligation_id="a", billed_usdc=6.0, due_at=NOW + DAY),
        _ob(obligation_id="b", billed_usdc=6.0, due_at=NOW + 2 * DAY),
    ]
    liq = assess(10.0, bills, NOW)
    assert liq.due_usdc == pytest.approx(12.0)
    # Pay 6 from 10 and 4 is left against the other 6 still dated: 2 short.
    assert short_by(liq, 6.0) == pytest.approx(2.0)
    # With 12 held, both are covered and nothing is short.
    assert short_by(assess(12.0, bills, NOW), 6.0) == 0.0


def test_a_shortfall_is_never_negative():
    liq = assess(1_000.0, [_ob(billed_usdc=1.0, due_at=NOW + DAY)], NOW)
    assert short_by(liq, 1.0) == 0.0


# --- the rung ---------------------------------------------------------------

def test_the_cash_check_holds_a_bill_the_wallet_cannot_afford_alongside_the_rest():
    """ESCALATE with a recommendation of HOLD, not REFUSE.

    The bill may be perfectly sound and the owner may fund the wallet, so
    waiting is the honest recommendation when the only thing missing is money.
    Refusing would record a judgement about the invoice on the strength of our
    own bank balance.
    """
    others = [
        _ob(obligation_id="ob-1", billed_usdc=6.0, due_at=NOW + DAY),
        _ob(obligation_id="ob-2", billed_usdc=6.0, due_at=NOW + 2 * DAY),
    ]
    liq = assess(7.0, others, NOW)
    d = decide(
        _ob(billed_usdc=6.0, due_at=NOW + DAY),
        par=_par(6.0),
        remaining_usdc=1_000.0,
        per_tx_limit_usdc=1_000.0,
        liquidity=liq,
        now=NOW,
    )
    assert d.intent == ESCALATE and d.escalated is True
    assert d.recommended_intent == HOLD
    assert "not enough to cover what is due" in d.rule
    # The three numbers a person needs, in the sentence itself.
    assert "7 USDC held" in d.rule and "12 dated" in d.rule and "30 days" in d.rule
    # And the picture it was judged against is on the record, for replay.
    assert d.held_usdc == pytest.approx(7.0)
    assert d.due_usdc == pytest.approx(12.0)
    assert d.liquidity_horizon_s == pytest.approx(HORIZON_S)


def test_a_bill_bigger_than_the_balance_says_so_and_does_not_blame_a_queue():
    """The sharper of the two situations, and the one a single sentence got
    wrong. Nothing else is dated here: the bill is simply larger than the
    wallet, the transfer would revert, and a rule reading "0 dated in the next
    30 days, so paying 10 would leave 9.5 short" is arithmetically true while
    naming a cause that does not exist."""
    d = decide(
        _ob(billed_usdc=10.0),
        par=_par(10.0),
        remaining_usdc=1_000.0,
        per_tx_limit_usdc=1_000.0,
        liquidity=assess(0.5, [], NOW),
        now=NOW,
    )
    assert d.intent == ESCALATE and d.recommended_intent == HOLD
    assert d.rule == (
        "the wallet holds 0.5 USDC and this bill is 10, "
        "so the payment would revert on chain"
    )
    assert "dated" not in d.rule, "there is no queue to blame"


def test_the_rung_is_not_vacuous_the_same_bill_pays_without_the_injection():
    """The proof that the check is doing the work: identical inputs, no cash
    picture, and the bill goes out. A rung that fires either way would be
    decoration on a decision that was already made."""
    ob = _ob(billed_usdc=6.0, due_at=NOW + DAY)
    kw = dict(par=_par(6.0), remaining_usdc=1_000.0, per_tx_limit_usdc=1_000.0, now=NOW)
    short = assess(
        7.0,
        [ob, _ob(obligation_id="ob-2", billed_usdc=6.0, due_at=NOW + 2 * DAY)],
        NOW,
    )
    assert decide(ob, liquidity=short, **kw).intent == ESCALATE
    assert decide(ob, **kw).intent == PAY


def test_with_no_balance_read_the_decision_says_so_rather_than_passing_silently():
    """Check 4's example, not check 3's guard: a check that could not run leaves
    a note, because an unexplained `pay` reads as a cash check that passed."""
    d = decide(
        _ob(billed_usdc=1.0),
        par=_par(),
        remaining_usdc=1_000.0,
        per_tx_limit_usdc=1_000.0,
        now=NOW,
    )
    assert d.intent == PAY
    assert any("nothing checked this against cash" in n for n in d.notes)


def test_a_measured_balance_leaves_no_such_note():
    d = decide(
        _ob(billed_usdc=1.0),
        par=_par(),
        remaining_usdc=1_000.0,
        per_tx_limit_usdc=1_000.0,
        liquidity=assess(500.0, [], NOW),
        now=NOW,
    )
    assert d.intent == PAY
    assert not any("against cash" in n for n in d.notes)


def test_an_unreadable_balance_never_blocks_a_payment():
    """Fail OPEN here, deliberately. A node that timed out is not evidence of a
    shortfall, and refusing on no information is the mistake the counterparty
    check already argues against — it would stop every payment the moment an
    RPC wobbled."""
    d = decide(
        _ob(billed_usdc=6.0, due_at=NOW + DAY),
        par=_par(6.0),
        remaining_usdc=1_000.0,
        per_tx_limit_usdc=1_000.0,
        liquidity=assess(None, [_ob(obligation_id="o2", billed_usdc=99.0, due_at=NOW + DAY)], NOW),
        now=NOW,
    )
    assert d.intent == PAY
    # AND IT SAYS SO. This is the shape production actually produces — the
    # keeper always builds a picture, so a wallet that will not answer arrives
    # as `Liquidity(held_usdc=None, …)` and never as `None`. The first cut
    # tested `elif liquidity is None`, so this exact row — the one case where a
    # chain read really had failed — was written down with no note at all and
    # read as a cash check that passed.
    assert any("nothing checked this against cash" in n for n in d.notes), d.notes


# --- the order, which is the design ----------------------------------------

def test_a_bill_over_its_own_per_payment_limit_says_so_and_does_not_blame_the_cash():
    """Check 8 is policy and comes first: a bill that breaches its own limit
    must say which limit, not point at a balance that was never the reason."""
    d = decide(
        _ob(billed_usdc=60.0, due_at=NOW + DAY),
        par=_par(60.0),
        remaining_usdc=1_000.0,
        per_tx_limit_usdc=50.0,
        liquidity=assess(0.0, [_ob(billed_usdc=60.0, due_at=NOW + DAY)], NOW),
        now=NOW,
    )
    assert d.intent == ESCALATE
    assert "not enough to cover" not in d.rule


def test_a_duplicate_is_refused_before_the_cash_is_consulted():
    d = decide(
        _ob(billed_usdc=6.0, due_at=NOW + DAY),
        par=_par(6.0),
        remaining_usdc=1_000.0,
        per_tx_limit_usdc=1_000.0,
        settled_refs={"ob-1"},
        liquidity=assess(0.0, [_ob(billed_usdc=6.0, due_at=NOW + DAY)], NOW),
        now=NOW,
    )
    assert "already settled" in d.rule


# --- the record round-trips ------------------------------------------------

def test_a_liquidity_picture_survives_the_record_it_is_written_into():
    liq = Liquidity(
        held_usdc=3.0, due_usdc=9.0, due_count=2,
        soonest_at=NOW, undated=1, horizon_s=HORIZON_S,
    )
    out = liq.as_dict()
    assert out["horizon_days"] == pytest.approx(30.0)
    assert out["undated"] == 1
    assert out["covers_due"] is False
