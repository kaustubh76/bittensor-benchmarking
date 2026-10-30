"""Check 1 of 8, which never ran, and the id it needs to be trustworthy.

*Agents and Ledgers* names six errors a trial balance cannot see. One of them is
the error of original entry — "the wrong amount on both sides, **or paid twice
after a retry**". This repo had the control for it written and wired to nothing:
`decide()` has taken a `settled_refs` argument since the day it was written, and
`scripts/operator_run.py`, its only production caller, never passed one. Check 1
ran against an empty set on every real run.

Two things had to be true before that check could be switched on, and the second
is why this file exists at all:

  * money has to have moved — a dry run that cleared policy is not a settlement,
    and treating it as one turns the duplicate check into a denial of service;
  * the reference has to identify ONE obligation — and it did not.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

from index_api.operator import (  # noqa: E402
    ESCALATE,
    HOLD,
    PAY,
    REFUSE,
    Obligation,
    decide,
    settled_refs_from,
)
from operator_run import _obligations  # noqa: E402


class _Biz:
    slug = "acme"
    treasury = "0x" + "11" * 20
    categories = ("infra",)


def _receipt(seller: str, resource: str, amount: float = 1.0, qty: float = 10.0) -> dict:
    return {
        "payer": _Biz.treasury,
        "seller": seller,
        "resource": f"https://press.example{resource}",
        "amount_usdc": amount,
        "quantity": qty,
        "unit": "$/1k tokens",
    }


# --- the id -----------------------------------------------------------------


def test_one_seller_two_resources_are_two_obligations():
    """The regression that was already in the committed archive.

    `obligation_id` was built from `resource.rsplit("/", 1)[-1]`, so
    `/curve/ACR-INF` and `/vol/ACR-INF` — two different purchases from one
    seller — collapsed to `acme:0x…:ACR-INF`. Two rows in
    `operator_decisions.jsonl` share that id today.

    A colliding id is worse than a verbose one. It merges two obligations into a
    single escalation-queue row, pairs an owner's resolution with whichever bill
    sorted first, and — once check 1 is live — refuses a bill nobody has paid.
    """
    seller = "0x" + "ab" * 20
    obs = _obligations(
        _Biz(),
        [_receipt(seller, "/curve/ACR-INF"), _receipt(seller, "/vol/ACR-INF")],
        {},
    )
    ids = [o.obligation_id for o in obs]
    assert len(obs) == 2, "one seller, two resources, two obligations"
    assert len(set(ids)) == 2, f"ids collided: {ids}"
    assert all("curve" in i or "vol" in i for i in ids), ids


def test_a_root_resource_still_gets_a_name():
    """`/` must not produce a trailing-colon id that reads as a missing field."""
    seller = "0x" + "cd" * 20
    (ob,) = _obligations(_Biz(), [_receipt(seller, "/")], {})
    assert ob.obligation_id.endswith(":root"), ob.obligation_id


# --- what counts as settled -------------------------------------------------


def _row(**kw) -> dict:
    base = {
        "intent": PAY,
        "paid_usdc": 2.0,
        "obligation_id": "ob-1",
        "invoice_ref": "",
    }
    base.update(kw)
    return base


def test_a_dry_run_is_not_a_settlement():
    """The inversion this check has to avoid.

    Every real row in the archive today is `intent: "pay"` with
    `paid_usdc: 0.0` — policy cleared, nothing sent. Keying on the intent alone
    would mark all nine as paid, and the first live run would refuse every bill
    it exists to settle.
    """
    assert settled_refs_from([_row(paid_usdc=0.0)]) == set()
    assert settled_refs_from([_row(paid_usdc=2.0)]) == {"ob-1"}


def test_only_payments_count():
    for intent in ("reroute", "hold", "refuse", ESCALATE):
        assert settled_refs_from([_row(intent=intent)]) == set(), intent


def test_both_halves_of_a_duplicate_are_recoverable_from_the_log():
    """`is_duplicate` matches our id OR the vendor's ref, so both must be stored.

    The record carried only the id, so the re-sent-invoice half of the check
    could never fire from a log: a vendor resending the same bill under a fresh
    id was invisible. "Checking one catches half."
    """
    refs = settled_refs_from([_row(obligation_id="ob-9", invoice_ref="INV-77")])
    assert refs == {"ob-9", "INV-77"}


def test_a_malformed_amount_is_not_a_settlement():
    assert settled_refs_from([_row(paid_usdc="nonsense")]) == set()
    assert settled_refs_from([_row(paid_usdc=None)]) == set()


# --- the check, end to end --------------------------------------------------


def _ob(**kw) -> Obligation:
    base = {
        "obligation_id": "ob-1",
        "vendor": "0x" + "ab" * 20,
        "billed_usdc": 2.0,
        "business": "acme",
        "category": "infra",
    }
    base.update(kw)
    return Obligation(**base)


def test_a_retried_bill_is_refused_once_it_has_really_been_paid():
    history = [_row(obligation_id="ob-1", paid_usdc=2.0)]
    d = decide(_ob(), settled_refs=settled_refs_from(history))
    assert d.intent == REFUSE
    assert "already settled" in d.rule


def test_the_same_bill_resent_under_a_new_id_is_refused_too():
    history = [_row(obligation_id="ob-1", invoice_ref="INV-77", paid_usdc=2.0)]
    d = decide(_ob(obligation_id="ob-NEW", invoice_ref="INV-77"),
               settled_refs=settled_refs_from(history))
    assert d.intent == REFUSE


def test_an_unpaid_history_refuses_nothing():
    """The failure mode on the other side: a check that refuses everything."""
    history = [_row(obligation_id="ob-1", paid_usdc=0.0)]
    d = decide(_ob(), settled_refs=settled_refs_from(history))
    assert d.intent != REFUSE or "already settled" not in d.rule


# --- the recommendation, so agreement has something to measure --------------


def test_every_escalation_says_what_it_would_have_done():
    """"How often the human agreed" needs a counterfactual to agree WITH.

    The agent used to escalate and record nothing but the fact of escalating, so
    an owner's approval could be compared against nothing at all.
    """
    # `unbenchmarked_max_usdc` is lifted in the two budget cases on purpose:
    # at its 1.0 USDC default a 150 USDC bill never reaches check 8, because
    # check 5 stops it first for being a price we cannot check. The ordering is
    # right; the test has to respect it to reach the check it is about.
    big = {"unbenchmarked_max_usdc": 10_000.0}

    # Above the per-payment limit: the bill is sound, the authority is not.
    d = decide(_ob(billed_usdc=150.0), per_tx_limit_usdc=100.0, **big)
    assert d.intent == ESCALATE and d.recommended_intent == PAY

    # No money left this period: waiting, not refusing — the period rolls.
    d = decide(_ob(billed_usdc=150.0), remaining_usdc=10.0, **big)
    assert d.intent == ESCALATE and d.recommended_intent == HOLD

    # A price we cannot check, above the ceiling: wait for a benchmark rather
    # than refuse a bill that may be perfectly fair.
    d = decide(_ob(billed_usdc=150.0))
    assert d.intent == ESCALATE and d.recommended_intent == HOLD
    assert "unbenchmarked" in d.rule

    # Billed for more than we counted: the bill is wrong.
    d = decide(_ob(vendor_quantity=100.0), metered_quantity=1.0)
    assert d.intent == ESCALATE and d.recommended_intent == REFUSE

    # No record of consuming it at all.
    d = decide(_ob(vendor_quantity=100.0), metered_quantity=None)
    assert d.intent == ESCALATE and d.recommended_intent == REFUSE


def test_an_agent_decision_is_marked_as_one():
    assert decide(_ob()).actor == "agent"


def test_the_due_date_and_the_vendor_ref_survive_onto_the_record():
    """Both existed on the obligation and were dropped at this boundary.

    `due_at` is why nothing could compute "settled on time"; `invoice_ref` is
    why only half the duplicate check could ever run.
    """
    d = decide(_ob(due_at=1_790_000_000.0, invoice_ref="INV-5"))
    assert d.due_at == 1_790_000_000.0
    assert d.invoice_ref == "INV-5"
