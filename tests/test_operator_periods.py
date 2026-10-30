"""A bill covers a PERIOD, and the second run has to be able to pay.

An obligation had no window. `_obligations` filtered on `payer` and nothing
else, and `obligation_key` had no time component — so `(seller, resource)` named
a bill for all of history, and once it was paid that pair sat in `settled_refs`
forever. Every later settlement from the same seller for the same service summed
into an id that was already there and was refused as a duplicate.

Nobody could see it, for two reasons that are both worth remembering. A human
runs this once, so there was never a second run. And the test suite asserted the
half that worked — `test_operator_duplicates.py` pins "a retried bill is refused
once it has really been paid" and "an unpaid history refuses nothing", both
correct — and never asked what happens to consumption that is genuinely new.

On a schedule it is fatal and silent: the first tick pays, every tick after it
refuses everything, and the only symptom is a number that stops going up.

The two halves have to hold at once, which is why they are tested together: new
consumption must be PAID, and a retry inside one window must still be REFUSED.
Either alone is easy and useless.
"""

from __future__ import annotations

import sys
from pathlib import Path

from index_api.operator import (
    PAY,
    REFUSE,
    Obligation,
    decide,
    is_duplicate,
    obligation_key,
    settled_refs_from,
    settled_through,
)

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from operator_run import _obligations  # noqa: E402

TREASURY = "0x" + "11" * 20
SELLER = "0x" + "ab" * 20
RESOURCE = "/compute/inf-mid"


class _Biz:
    slug = "acme"
    treasury = TREASURY
    categories = ("machine-services",)


def _receipt(at: float, amount: float = 1.0, qty: float = 10.0, resource: str = RESOURCE) -> dict:
    return {
        "payer": TREASURY,
        "seller": SELLER,
        "resource": resource,
        "amount_usdc": amount,
        "quantity": qty,
        "unit": "$/1k tokens",
        "settled_at": at,
    }


def _paid(ob: Obligation, at: float) -> dict:
    """The log row a real payment of `ob` leaves behind."""
    return {
        "at": at,
        "obligation_id": ob.obligation_id,
        "vendor": ob.vendor,
        "resource": ob.resource,
        "intent": PAY,
        "billed_usdc": ob.billed_usdc,
        "paid_usdc": ob.billed_usdc,
        "period_start": ob.period_start,
        "period_end": ob.period_end,
    }


# --- the window -------------------------------------------------------------


def test_a_bill_covers_only_what_settled_after_the_last_one(monkeypatch):
    """THE BUG, end to end: pay a period, then consume more, and the new
    consumption must produce a payable bill rather than a duplicate."""
    first = _obligations(_Biz(), [_receipt(100.0), _receipt(200.0)], {}, {})
    assert len(first) == 1
    assert first[0].billed_usdc == 2.0
    assert (first[0].period_start, first[0].period_end) == (100.0, 200.0)

    log = [_paid(first[0], at=250.0)]
    through = settled_through(log)

    # More consumption arrives.
    second = _obligations(
        _Biz(), [_receipt(100.0), _receipt(200.0), _receipt(300.0, amount=5.0)], {}, through
    )
    assert len(second) == 1, "the new consumption is a bill of its own"
    assert second[0].billed_usdc == 5.0, "and covers ONLY what is new"
    assert (second[0].period_start, second[0].period_end) == (300.0, 300.0)

    # The half that must keep working: it is not the bill we already paid.
    assert second[0].obligation_id != first[0].obligation_id
    assert not is_duplicate(second[0], settled_refs_from(log)), (
        "new consumption refused as a duplicate is the whole defect"
    )


def test_nothing_new_is_no_obligation_at_all(monkeypatch):
    """Not a zero-amount bill, which would be a decision to make about nothing —
    and on a schedule, one per tick forever."""
    obs = _obligations(_Biz(), [_receipt(100.0)], {}, {})
    through = settled_through([_paid(obs[0], at=150.0)])
    assert _obligations(_Biz(), [_receipt(100.0)], {}, through) == []


def test_a_receipt_exactly_on_the_boundary_is_not_billed_twice():
    """The boundary is the end of the paid period, so a settlement AT it was in
    that bill. Counting it again is how two periods overlap."""
    obs = _obligations(_Biz(), [_receipt(100.0), _receipt(200.0)], {}, {})
    through = settled_through([_paid(obs[0], at=250.0)])
    assert through[(SELLER.lower(), RESOURCE)] == 200.0
    assert _obligations(_Biz(), [_receipt(200.0)], {}, through) == []


def test_the_window_is_per_pair_not_per_business():
    """Paying one seller must not silence another."""
    other = "0x" + "cd" * 20
    rows = [_receipt(100.0), {**_receipt(100.0), "seller": other}]
    obs = {o.vendor: o for o in _obligations(_Biz(), rows, {}, {})}
    assert set(obs) == {SELLER, other}

    through = settled_through([_paid(obs[SELLER], at=150.0)])
    left = _obligations(_Biz(), rows, {}, through)
    assert [o.vendor for o in left] == [other]


# --- the meter has to cover the same window ---------------------------------


def test_the_meter_and_the_bill_cover_the_same_window():
    """`run_obligation(since=)` defaulted to 0.0 and the runner never passed it,
    so the meter counted from the epoch. That was accidentally consistent while
    the bill was all-time too; with a window it becomes a false discrepancy on
    every row — the agent accusing every vendor of overbilling."""
    from index_api.operator import meter_quantity

    rows = [_receipt(100.0, qty=10.0), _receipt(300.0, qty=7.0)]
    obs = _obligations(_Biz(), rows, {}, {})
    through = settled_through([_paid(obs[0], at=350.0)])

    # Nothing new yet, so nothing to bill; now a later settlement arrives.
    rows.append(_receipt(400.0, amount=2.0, qty=3.0))
    ob = _obligations(_Biz(), rows, {}, through)[0]
    assert ob.vendor_quantity == 3.0

    metered = meter_quantity(rows, ob.vendor, ob.resource, ob.period_start)
    assert metered == ob.vendor_quantity, "the two counts must be of the same period"


# --- the id, and the nineteen rows already on the record --------------------


def test_a_bill_with_no_period_keeps_exactly_the_id_it_had():
    """No period means the id is unchanged, so nothing already on the record
    becomes unrecognisable."""
    assert obligation_key("acme", SELLER, RESOURCE, 0.0) == obligation_key(
        "acme", SELLER, RESOURCE
    )
    assert obligation_key("acme", SELLER, RESOURCE) == "acme:0xabababab:compute-inf-mid"


def test_every_real_payment_on_the_record_is_still_recognised():
    """THE MIGRATION PROPERTY, measured against the committed archive rather
    than asserted about it.

    `settled_refs_from` reads ids off the log to decide what has already been
    paid, so an id the code can no longer reproduce is a bill the next run pays
    a second time — the error of original entry *Agents and Ledgers* names, and
    the one check 1 exists to prevent.

    Found while writing this: EIGHT of the nineteen archived ids are in a older
    convention (`rsplit("/", 1)[-1]`, before the whole path was used — the fix
    that stopped `/curve/ACR-INF` and `/vol/ACR-INF` colliding). All eight are
    dry runs with `paid_usdc: 0.0`, which `settled_refs_from` ignores by design,
    so no money is at risk. That is worth a test rather than a note, because the
    margin is "the stale ones happen to be the ones that do not count".
    """
    from index_api.statement import read_decisions

    paid = [
        r for r in read_decisions(business="acr-fleet")
        if str(r.get("intent") or "") == PAY and float(r.get("paid_usdc") or 0.0) > 0
    ]
    assert paid, "the archive should carry real payments"
    for r in paid:
        rebuilt = obligation_key(
            "acr-fleet", str(r.get("vendor") or ""), str(r.get("resource") or ""),
            float(r.get("period_end") or 0.0),
        )
        assert rebuilt == r.get("obligation_id"), (
            f"a paid bill the duplicate check can no longer recognise: {r.get('obligation_id')}"
        )


def test_a_period_makes_a_different_id():
    a = obligation_key("acme", SELLER, RESOURCE, 200.0)
    b = obligation_key("acme", SELLER, RESOURCE, 300.0)
    assert a != b and a.endswith("@200") and b.endswith("@300")


def test_a_row_with_no_period_is_settled_through_the_moment_it_was_paid():
    """The bootstrap. Every row in the committed archive predates windows, and
    those bills summed every receipt on the tape when they were paid — so
    "settled through the payment" is the true reading, and anything that settled
    later is genuinely new."""
    legacy = {
        "at": 500.0,
        "obligation_id": "acme:0xabababab:compute-inf-mid",
        "vendor": SELLER,
        "resource": RESOURCE,
        "intent": PAY,
        "billed_usdc": 1.0,
        "paid_usdc": 1.0,
    }
    assert settled_through([legacy]) == {(SELLER.lower(), RESOURCE): 500.0}

    # And it really does stop the re-payment: everything on the old tape is
    # behind the boundary, so there is nothing left to bill.
    assert _obligations(_Biz(), [_receipt(100.0), _receipt(200.0)], {}, settled_through([legacy])) == []


def test_a_dry_run_is_not_a_period_boundary():
    """Keying on `intent == pay` alone would let a cleared-but-unsent decision
    close a window the money never covered — and the consumption inside it would
    never be billed again. Same rule `settled_refs_from` lives by."""
    obs = _obligations(_Biz(), [_receipt(100.0)], {}, {})
    row = _paid(obs[0], at=150.0)
    row["paid_usdc"] = 0.0
    assert settled_through([row]) == {}


def test_the_latest_paid_period_wins():
    """Two payments for one pair: the boundary is the later one, not whichever
    row happens to come last in the file."""
    rows = [
        {"vendor": SELLER, "resource": RESOURCE, "intent": PAY, "paid_usdc": 1.0,
         "at": 900.0, "period_end": 800.0},
        {"vendor": SELLER, "resource": RESOURCE, "intent": PAY, "paid_usdc": 1.0,
         "at": 400.0, "period_end": 300.0},
    ]
    assert settled_through(rows) == {(SELLER.lower(), RESOURCE): 800.0}


# --- the half that must keep working ----------------------------------------


def test_a_retry_inside_one_window_is_still_refused():
    """The duplicate check still has to do its job. Nothing about periods makes
    paying the same window twice acceptable."""
    ob = _obligations(_Biz(), [_receipt(100.0)], {}, {})[0]
    log = [_paid(ob, at=150.0)]
    again = _obligations(_Biz(), [_receipt(100.0)], {}, {})[0]
    assert again.obligation_id == ob.obligation_id
    d = decide(again, settled_refs=settled_refs_from(log), remaining_usdc=100.0)
    assert d.intent == REFUSE and "already settled" in d.rule


def test_the_decision_carries_the_window_onto_the_record():
    """Otherwise the next run cannot find the boundary and falls back to the
    payment time — which is a looser bound than the truth."""
    ob = _obligations(_Biz(), [_receipt(100.0), _receipt(200.0)], {}, {})[0]
    d = decide(ob, remaining_usdc=100.0)
    assert (d.period_start, d.period_end) == (100.0, 200.0)
    assert d.as_record()["period_end"] == 200.0, "and it is part of what was hashed"


def test_a_settlement_with_no_timestamp_is_still_billed_once():
    """A zero bound means NO bound, not a bound at the epoch.

    Written as `at <= bound` the window filter also dropped every receipt with
    no `settled_at` — `at` is 0.0 for those — so a pair we had never paid was
    never billed at all. `test_operator_duplicates.py` caught it on the first
    run, which is the argument for keeping tests that assert the boring case.

    It is billed once, in the first period, and the boundary that payment sets
    excludes it afterwards.
    """
    bare = _receipt(100.0)
    del bare["settled_at"]
    obs = _obligations(_Biz(), [bare], {}, {})
    assert len(obs) == 1 and obs[0].billed_usdc == 1.0
    assert obs[0].period_end == 0.0, "it cannot be placed in a period"
    assert obs[0].obligation_id == obligation_key("acme", SELLER, RESOURCE), (
        "so the bill keeps the periodless id"
    )

    through = settled_through([_paid(obs[0], at=500.0)])
    assert _obligations(_Biz(), [bare], {}, through) == [], "and is not billed twice"


def test_the_old_convention_really_would_have_refused_it():
    """The counterfactual, run rather than asserted.

    Under the periodless id, the bill for new consumption is the SAME id as the
    bill already paid — so check 1 refuses it, forever. This is what the
    unattended operator would have done on every tick after its first, and the
    reason the id had to change rather than the duplicate check being relaxed.
    """
    # The old world: the bill that was paid and the bill for new consumption
    # are both named without a period, so they are the same name.
    old_id = obligation_key("acme", SELLER, RESOURCE)
    old_log = [{
        "at": 150.0, "obligation_id": old_id, "vendor": SELLER, "resource": RESOURCE,
        "intent": PAY, "billed_usdc": 1.0, "paid_usdc": 1.0,
    }]
    periodless = Obligation(
        obligation_id=old_id, vendor=SELLER, billed_usdc=5.0,
        business="acme", resource=RESOURCE,
    )
    assert is_duplicate(periodless, settled_refs_from(old_log)), (
        "this is the defect: genuinely new consumption, refused as a duplicate"
    )

    # The same consumption, named with its period, is payable — and the
    # bootstrap in `settled_through` reads that same legacy row to find the
    # boundary, so nothing already paid comes back either.
    windowed = _obligations(
        _Biz(),
        [_receipt(100.0), _receipt(300.0, amount=5.0)],
        {},
        settled_through(old_log),
    )[0]
    assert windowed.billed_usdc == 5.0, "only what settled after the legacy payment"
    assert not is_duplicate(windowed, settled_refs_from(old_log))


# --- the log records decisions, not ticks -----------------------------------


def test_an_unchanged_decision_is_not_written_twice(tmp_path):
    """On a schedule, an unpaid bill is re-decided every tick — deliberately,
    because the screen is re-asked each time. A rerouted bill is never settled,
    so it comes back for ever: twenty-four identical rows a day per open bill,
    and `work.decisions` climbing while nothing happened.
    """
    from index_api.operator import log_decision
    from index_api.statement import read_decisions

    log = tmp_path / "decisions.jsonl"
    ob = _obligations(_Biz(), [_receipt(100.0)], {}, {})[0]

    first = decide(ob, remaining_usdc=100.0)
    log_decision(first, str(log))
    log_decision(decide(ob, remaining_usdc=100.0), str(log))
    log_decision(decide(ob, remaining_usdc=100.0), str(log))
    assert len(read_decisions(path=str(log))) == 1, "three identical ticks, one decision"


def test_a_changed_mind_is_always_written(tmp_path):
    """The suppression must not hide the thing the re-decision exists for. A
    different verdict, price, meter or intent is new information."""
    from index_api.operator import log_decision
    from index_api.statement import read_decisions

    log = tmp_path / "decisions.jsonl"
    ob = _obligations(_Biz(), [_receipt(100.0)], {}, {})[0]

    log_decision(decide(ob, remaining_usdc=100.0), str(log))
    changed = decide(ob, remaining_usdc=100.0)
    changed.screen_risk = "flagged"
    log_decision(changed, str(log))
    rows = read_decisions(path=str(log))
    assert len(rows) == 2
    assert rows[-1]["screen_risk"] == "flagged"


def test_two_different_bills_never_suppress_each_other(tmp_path):
    """The comparison is per obligation id. Interleaved decisions about two
    bills must both land."""
    from index_api.operator import log_decision
    from index_api.statement import read_decisions

    log = tmp_path / "decisions.jsonl"
    other = "0x" + "cd" * 20
    rows = [_receipt(100.0), {**_receipt(100.0), "seller": other}]
    a, b = _obligations(_Biz(), rows, {}, {})
    for ob in (a, b, a, b):
        log_decision(decide(ob, remaining_usdc=100.0), str(log))
    logged = read_decisions(path=str(log))
    assert len(logged) == 2, "each bill decided once, repeats suppressed"
    assert {r["vendor"] for r in logged} == {a.vendor, b.vendor}


# --- which channel the money went out through -------------------------------


def test_the_record_says_which_channel_paid():
    """A payment without its channel is a claim without a basis — the same
    argument `screen_backend` won.

    Circle's developer-controlled wallet and a raw local key build IDENTICAL
    calldata, so no reader of the chain, the log or the ledger could tell which
    one paid. Five payments on Arc testnet went out from a raw EOA while
    `build_role_signer("taker")` now resolves to Circle, and nothing anywhere
    recorded the difference.
    """
    from index_api.operator import run_obligation

    class _Policy:
        def __init__(self, kind):
            self.kind = kind

        def signer_kinds(self):
            return {"agent": self.kind, "owner": "local"}

        def budget(self, _category):
            return {"remaining_usdc": 100.0, "per_tx_limit_usdc": 50.0}

        def spend(self, *_a, **_k):
            return "0x" + "ee" * 32

    ob = _obligations(_Biz(), [_receipt(100.0)], {}, {})[0]
    d = run_obligation(ob, receipts=[_receipt(100.0)], catalog={},
                       policy=_Policy("circle"), dry_run=True, log_path="/dev/null")
    assert d.paid_via == "circle"

    d = run_obligation(ob, receipts=[_receipt(100.0)], catalog={},
                       policy=_Policy("local"), dry_run=True, log_path="/dev/null")
    assert d.paid_via == "local"


def test_the_channel_is_part_of_what_was_hashed():
    """Set before `as_record()` runs, so the commitment on chain says which
    channel was authorised rather than which one a later reader assumes."""
    from index_api.operator import UNHASHED

    assert "paid_via" not in UNHASHED
    ob = _obligations(_Biz(), [_receipt(100.0)], {}, {})[0]
    d = decide(ob, remaining_usdc=100.0)
    d.paid_via = "circle"
    assert d.as_record()["paid_via"] == "circle"


def test_a_policy_client_that_cannot_say_does_not_block_the_payment():
    """A reporting field must never be able to stop money that cleared policy."""
    from index_api.operator import run_obligation

    class _Mute:
        def signer_kinds(self):
            raise RuntimeError("circle api down")

        def budget(self, _category):
            return {"remaining_usdc": 100.0, "per_tx_limit_usdc": 50.0}

        def spend(self, *_a, **_k):
            return "0x" + "ee" * 32

    class _Loud(_Mute):
        """Identical, except it CAN say which signer kinds it has."""

        def signer_kinds(self):
            return ("local",)

    kw = dict(receipts=[_receipt(100.0)], catalog={}, dry_run=True, log_path="/dev/null")
    ob = _obligations(_Biz(), [_receipt(100.0)], {}, {})[0]

    d = run_obligation(ob, policy=_Mute(), **kw)
    assert d.paid_via == "", "a client that cannot say must leave the field empty"

    # Asserted against the LOUD run rather than against a literal intent. The
    # claim is that a reporting field cannot CHANGE the decision — pinning `PAY`
    # instead made the test fail the day the benchmark started pricing this
    # fixture against the real market, which was the ladder working, not a
    # regression in what this test is about.
    loud = run_obligation(ob, policy=_Loud(), **kw)
    assert d.intent == loud.intent, "a reporting field changed the decision"
    # Not asserting `loud.paid_via != ""`: this is a DRY run, so no channel is
    # recorded on either side. `paid_via` is written when money moves, and the
    # point here is only that an unreadable client cannot alter the verdict.
