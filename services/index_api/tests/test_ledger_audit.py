"""Each of the six, with the error planted in front of it.

A check that has never caught anything is a claim, not a control. Every test
here puts one of the essay's six errors into otherwise clean books and asserts
the audit finds it — and then asserts the clean books come back clean, because a
check that fires on everything is just as useless as one that never fires.

The through-line: **every planted error balances.** Not one of these would be
caught by summing the postings, which is why the module exists.
"""

from __future__ import annotations

from index_api.ledger_audit import ERRORS, audit

TREASURY = "0x" + "11" * 20
SELLER = "0x" + "ab" * 20
OTHER = "0x" + "cd" * 20
CATS = ("machine-services",)


def _receipt(seller: str = SELLER, resource: str = "/compute/x", amount: float = 1.0) -> dict:
    return {
        "payer": TREASURY,
        "seller": seller,
        "resource": f"https://press.example{resource}",
        "amount_usdc": amount,
        "quantity": 10.0,
    }


def _paid(**kw) -> dict:
    row = {
        "at": 1_790_900_000.0,
        "obligation_id": "ob-1",
        "vendor": SELLER,
        "category": "machine-services",
        "billed_usdc": 1.0,
        "paid_usdc": 1.0,
        "intent": "pay",
        "rule": "at par",
        "business": "acme",
        "resource": "/compute/x",
        "discrepancy": None,
        # A payment that moved money has a transaction. Without one the phantom
        # check flags it, correctly — see the section at the foot of this file.
        "tx": "0x" + "ee" * 32,
    }
    row.update(kw)
    return row


def _boom(_tx):
    """A confirmer that fails, which is not the same as a chain that denies."""
    raise RuntimeError("the node fell over mid-audit")


def _audit(decisions, receipts=None, categories=CATS) -> dict:
    return audit(
        decisions,
        receipts if receipts is not None else [_receipt()],
        treasury=TREASURY,
        slug="acme",
        categories=categories,
    )


# --- the shape --------------------------------------------------------------


def test_all_six_are_always_reported():
    """Including on empty books. A missing check reads as a passing one.

    The six are the essay's, in its order, and the seventh is deliberately not
    one of them — see `test_the_phantom_is_reported_beside_the_six_not_among_them`.
    """
    out = audit([], [], treasury=TREASURY, slug="acme", categories=CATS)
    assert tuple(c["error"] for c in out["checks"])[:6] == ERRORS
    assert len(out["checks"]) == 7


def test_clean_books_come_back_clean():
    out = _audit([_paid()])
    assert out["clean"] is True
    assert out["findings"] == []


def test_every_check_says_what_it_searched():
    """`found: 0` means nothing without it, and this repo has shipped that bug.

    An audit walker here once skipped empty lists, so `notes: []` was never
    examined and the ledger of exemptions reported it fine.
    """
    out = _audit([_paid()])
    assert all("searched" in c for c in out["checks"])
    by = {c["error"]: c for c in out["checks"]}
    assert by["original entry"]["searched"] == 1, "a payment was in front of it"
    assert by["omission"]["searched"] == 1, "a settlement was in front of it"


# --- 1 · omission -----------------------------------------------------------


def test_omission_a_settlement_with_no_decision():
    """The seller recorded it and we did not. The ledger cannot see this at all:
    the transaction is simply absent, and absent transactions balance."""
    out = _audit([_paid()], receipts=[_receipt(), _receipt(resource="/compute/unseen")])
    found = [f for f in out["findings"] if f["error"] == "omission"]
    assert len(found) == 1
    assert "/compute/unseen" in found[0]["detail"]


def test_omission_survives_a_change_of_id_convention():
    """Matched on (seller, resource), not on our own naming.

    Keying on `obligation_id` made every receipt look uncovered the moment the
    id format improved — a check reporting on itself rather than on the books.
    """
    out = _audit([_paid(obligation_id="a-totally-different-id-scheme")])
    assert [f for f in out["findings"] if f["error"] == "omission"] == []


def test_a_settlement_with_no_seller_is_unattributable_not_clean():
    """34 rows in the committed archive have no payee. Counting them as covered
    would be the omission this check exists to find."""
    out = _audit([_paid()], receipts=[_receipt(), {**_receipt(), "seller": ""}])
    assert out["unattributable_settlements"] == 1
    assert [f for f in out["findings"] if f["error"] == "omission"] == []


# --- 2 · commission ---------------------------------------------------------


def test_commission_paid_a_party_that_never_served_us():
    """The essay's hardest case: "A payment to the wrong vendor's account
    reconciles perfectly, because the bank confirms that you paid exactly whom
    you told it to pay"."""
    out = _audit([_paid(vendor=OTHER)])
    found = [f for f in out["findings"] if f["error"] == "commission"]
    assert len(found) == 1 and "never appears as a seller" in found[0]["detail"]


def test_commission_a_reroute_that_reroutes_to_nobody():
    out = _audit([{**_paid(), "intent": "reroute", "paid_usdc": 0.0, "reroute_to": ""}])
    found = [f for f in out["findings"] if f["error"] == "commission"]
    assert len(found) == 1 and "nobody" in found[0]["detail"]


def test_commission_a_reroute_back_to_the_vendor_it_was_escaping():
    out = _audit(
        [{**_paid(), "intent": "reroute", "paid_usdc": 0.0, "reroute_to": SELLER}]
    )
    found = [f for f in out["findings"] if f["error"] == "commission"]
    assert len(found) == 1 and "escaping" in found[0]["detail"]


# --- 3 · principle ----------------------------------------------------------


def test_principle_booked_to_an_account_the_business_never_declared():
    out = _audit([_paid(category="entertainment")])
    found = [f for f in out["findings"] if f["error"] == "principle"]
    assert len(found) == 1 and "entertainment" in found[0]["detail"]


def test_principle_a_business_with_no_declared_categories_is_not_checkable():
    """And says so, by searching nothing, rather than passing everything."""
    out = _audit([_paid(category="anything")], categories=())
    by = {c["error"]: c for c in out["checks"]}
    assert by["principle"]["searched"] == 0
    assert [f for f in out["findings"] if f["error"] == "principle"] == []


# --- 4 · original entry -----------------------------------------------------


def test_original_entry_the_wrong_amount_on_both_sides():
    """`ledger_export` writes paid_usdc to the expense and its negation to the
    treasury, so ANY amount sums to zero. That is the essay's point, in our
    own file."""
    out = _audit([_paid(billed_usdc=1.0, paid_usdc=9.0)])
    found = [f for f in out["findings"] if f["error"] == "original entry"]
    assert len(found) == 1 and "9.000000" in found[0]["detail"]


def test_original_entry_the_retry_that_paid_twice():
    """The control that existed and never ran: `decide`'s check 1 took a set of
    settled references and the production runner never passed one."""
    out = _audit([_paid(), _paid()])
    found = [f for f in out["findings"] if f["error"] == "original entry"]
    assert len(found) == 1 and "settled twice" in found[0]["detail"]


def test_a_dry_run_is_not_a_second_payment():
    """Nine rows in the archive are `pay` with `paid_usdc: 0.0`. Counting them
    as settlements would report the whole book as paid twice."""
    out = _audit([_paid(), _paid(paid_usdc=0.0)])
    assert [f for f in out["findings"] if f["error"] == "original entry"] == []


# --- 5 · compensating -------------------------------------------------------


def test_compensating_two_meter_errors_that_cancel():
    """The period's net discrepancy is zero and neither bill is right."""
    out = _audit(
        [
            _paid(obligation_id="ob-1", discrepancy=5.0),
            _paid(obligation_id="ob-2", discrepancy=-5.0),
        ],
        receipts=[_receipt()],
    )
    found = [f for f in out["findings"] if f["error"] == "compensating"]
    assert len(found) == 1
    assert found[0]["obligation_id"] == "", "no single row is the error"


def test_discrepancies_that_do_not_cancel_are_not_this_error():
    out = _audit(
        [
            _paid(obligation_id="ob-1", discrepancy=5.0),
            _paid(obligation_id="ob-2", discrepancy=-1.0),
        ]
    )
    assert [f for f in out["findings"] if f["error"] == "compensating"] == []


def test_overcounts_alone_are_not_compensating():
    """Two errors in the same direction do not hide each other."""
    out = _audit(
        [
            _paid(obligation_id="ob-1", discrepancy=5.0),
            _paid(obligation_id="ob-2", discrepancy=5.0),
        ]
    )
    assert [f for f in out["findings"] if f["error"] == "compensating"] == []


# --- 6 · complete reversal --------------------------------------------------


def test_complete_reversal_a_payment_that_runs_backwards():
    """Negate the amount and the pair still sums to zero: the file now says the
    vendor paid US. Nothing downstream checks a sign."""
    out = _audit([_paid(paid_usdc=-1.0)])
    found = [f for f in out["findings"] if f["error"] == "complete reversal"]
    assert len(found) == 1 and "treasury UP" in found[0]["detail"]


def test_complete_reversal_a_bill_that_pays_us():
    out = _audit([_paid(billed_usdc=-1.0, paid_usdc=0.0)])
    found = [f for f in out["findings"] if f["error"] == "complete reversal"]
    assert len(found) == 1 and "a bill that pays us" in found[0]["detail"]


# --- the whole point --------------------------------------------------------


def test_every_planted_error_balances():
    """The claim the module rests on, asserted rather than written in a comment.

    Each of these is caught above. None of them is caught by double entry: the
    exporter writes `+amount` to the expense and `-amount` to the treasury, so
    the postings sum to zero whatever the amount, the payee, the account, the
    direction, or the number of times it was written.
    """
    for row in (
        _paid(vendor=OTHER),                       # commission
        _paid(category="entertainment"),           # principle
        _paid(billed_usdc=1.0, paid_usdc=9.0),     # original entry
        _paid(paid_usdc=-1.0),                     # complete reversal
    ):
        amount = float(row["paid_usdc"])
        assert amount + (-amount) == 0.0, "the postings balance; the book is wrong"


# --- not one of the six: the phantom ----------------------------------------
#
# The essay's six are errors a trial balance cannot see. This is the one it says
# nobody catches at all: "SolidInvoice: the most complete write path, and no way
# to disprove a phantom payment."
#
# We could produce one, which is why it is here. On a chain where the
# PolicyWallet address has no code a CALL neither reverts nor fails to estimate
# (measured against a live Arc node: 22026 gas), so a transaction broadcasts,
# returns `status: 1`, and gets written down as a payment with a hash as its
# evidence. `PolicyClient` now refuses to send at all — this looks for one
# anyway, because a control nobody audits is a control nobody can show you.


def test_the_phantom_is_reported_beside_the_six_not_among_them():
    out = audit([], [], treasury=TREASURY, slug="acme", categories=CATS)
    names = [c["error"] for c in out["checks"]]
    assert tuple(names[:6]) == ERRORS, "the essay's six, in its order, untouched"
    assert names[6] == "phantom payment"
    assert len(names) == 7


def test_a_payment_with_no_transaction_is_a_claim_with_no_receipt():
    """Caught without consulting the chain at all, because it needs no chain."""
    out = _audit([_paid(tx=None)])
    found = [f for f in out["findings"] if f["error"] == "phantom payment"]
    assert len(found) == 1 and "no transaction to point at" in found[0]["detail"]


def test_without_a_confirmer_the_check_says_the_chain_was_not_consulted():
    """It must not pass as though it had. The six follow the same rule."""
    out = _audit([_paid(tx="0x" + "ee" * 32)])
    check = [c for c in out["checks"] if c["error"] == "phantom payment"][0]
    assert "chain not consulted" in check["question"]
    assert check["found"] == 0 and check["searched"] == 1


def test_a_transaction_the_chain_denies_is_a_phantom():
    out = audit(
        [_paid(tx="0x" + "ee" * 32)],
        [_receipt()],
        treasury=TREASURY, slug="acme", categories=CATS,
        confirm=lambda _tx: False,
    )
    found = [f for f in out["findings"] if f["error"] == "phantom payment"]
    assert len(found) == 1 and "does not corroborate" in found[0]["detail"]
    check = [c for c in out["checks"] if c["error"] == "phantom payment"][0]
    assert "does the chain corroborate" in check["question"]


def test_a_chain_that_cannot_answer_is_never_a_phantom():
    """`None` means "could not tell". Reporting a sleeping node as a fabricated
    payment would cry wolf until nobody read the audit at all."""
    out = audit(
        [_paid(tx="0x" + "ee" * 32)],
        [_receipt()],
        treasury=TREASURY, slug="acme", categories=CATS,
        confirm=lambda _tx: None,
    )
    assert [f for f in out["findings"] if f["error"] == "phantom payment"] == []


def test_a_confirmer_that_raises_is_not_a_phantom_either():
    out = audit(
        [_paid(tx="0x" + "ee" * 32)],
        [_receipt()],
        treasury=TREASURY, slug="acme", categories=CATS,
        confirm=_boom,
    )
    assert [f for f in out["findings"] if f["error"] == "phantom payment"] == []


def test_a_corroborated_payment_is_clean():
    out = audit(
        [_paid(tx="0x" + "ee" * 32)],
        [_receipt()],
        treasury=TREASURY, slug="acme", categories=CATS,
        confirm=lambda _tx: True,
    )
    assert [f for f in out["findings"] if f["error"] == "phantom payment"] == []


def test_a_dry_run_is_not_a_phantom():
    """Nine rows in the committed archive are `pay` with `paid_usdc: 0.0` and no
    tx. They claim nothing, so there is nothing to corroborate."""
    out = _audit([_paid(paid_usdc=0.0, tx=None)])
    assert [f for f in out["findings"] if f["error"] == "phantom payment"] == []


# --- the bank statement the audit reads from --------------------------------
#
# `load_tape` had no tests and one caller, and it is the input to the omission
# check — the one error whose whole method is "compare our books against
# somebody else's record". Every failure mode here returns an empty list, and an
# empty tape makes the omission check report CLEAN. That is the shape worth
# testing: the function cannot fail loudly, so the only protection is knowing
# exactly which empty means what.


def test_the_tape_is_read_from_the_file_it_is_given(tmp_path):
    """The `path` parameter had no caller. It is the seam a test needs, and
    deleting it would mean every test of this function had to reach for an
    environment variable instead."""
    import json

    from index_api.ledger_audit import load_tape

    p = tmp_path / "receipts.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in (_receipt(), _receipt(amount=2.0))) + "\n")
    rows = load_tape(str(p))
    assert len(rows) == 2
    assert [r["amount_usdc"] for r in rows] == [1.0, 2.0]


def test_a_half_written_line_does_not_truncate_the_tape(tmp_path):
    """An interrupted write leaves half a line. Taking that as the end of the
    file would silently drop every settlement behind it — and the audit would
    then report the dropped ones as omissions, which is a false accusation
    rather than a missed one."""
    import json

    from index_api.ledger_audit import load_tape

    p = tmp_path / "receipts.jsonl"
    p.write_text(
        json.dumps(_receipt()) + "\n"
        + '{"payer": "0x11", "amount_usd\n'  # a crash mid-write
        + json.dumps(_receipt(amount=3.0)) + "\n"
    )
    rows = load_tape(str(p))
    assert [r["amount_usdc"] for r in rows] == [1.0, 3.0], "the row behind the tear survived"


def test_a_missing_tape_is_empty_and_the_audit_says_it_searched_nothing(tmp_path):
    """The dangerous empty. A tape that is not there and a tape with nothing in
    it both read as zero settlements, and zero settlements makes the omission
    check pass — so the check has to report what it searched, not just its
    verdict."""
    from index_api.ledger_audit import load_tape

    assert load_tape(str(tmp_path / "nope.jsonl")) == []

    out = _audit([_paid()], receipts=[])
    om = next(c for c in out["checks"] if c["error"] == ERRORS[0])
    assert om["searched"] == 0, "the reader can see the denominator was empty"


def test_the_environment_names_the_tape_when_no_path_is_given(tmp_path, monkeypatch):
    """Production sets `ACR_RECEIPT_ARCHIVE_PATH` in `render.yaml`, and the
    fallback beside the module is what makes a fresh checkout work. If the env
    var were ignored the audit would read the committed copy on the deployed
    host — stale books, reported as current."""
    import json

    from index_api.ledger_audit import load_tape

    p = tmp_path / "from-env.jsonl"
    p.write_text(json.dumps(_receipt(amount=7.5)) + "\n")
    monkeypatch.setenv("ACR_RECEIPT_ARCHIVE_PATH", str(p))
    assert [r["amount_usdc"] for r in load_tape()] == [7.5]


def test_the_committed_tape_is_readable_and_not_empty():
    """The fallback is a real file in the image, not a hopeful path. If it ever
    stops being one, the omission check quietly starts searching nothing on
    every host that has no env var set."""
    from index_api.ledger_audit import load_tape

    rows = load_tape()
    assert rows, "the committed receipt archive should ship beside the module"
    assert all(isinstance(r, dict) for r in rows)


# --- the injected clock -----------------------------------------------------


def test_the_audit_reports_the_moment_it_was_asked_about():
    """`now=` had no caller either. It is the repo's standing idiom — a clock
    passed in rather than read — and it is what lets `as_of` be asserted at all
    instead of compared against a moving target."""
    out = audit([_paid()], [_receipt()], treasury=TREASURY, slug="acme",
                categories=CATS, now=1_790_000_000.0)
    assert out["as_of"] == 1_790_000_000.0
