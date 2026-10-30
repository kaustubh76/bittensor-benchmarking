"""The committed register itself — not the feeder that reads it.

`tests/test_entitlements.py` has fifteen tests on
`obligations_from_entitlements`, all of them against rows built in the test. They
passed for weeks while **the register did not exist**: `entitlements.py` declares
`ARCHIVE_PATH` and `LOG_PATH`, `_read_one` treats `FileNotFoundError` as `[]`, and
`operator_keeper` added an empty list to its obligations on every tick. No log
line, no health signal, no failing gate — the function was tested and the file it
reads was absent.

That absence had consequences the module's own docstring names, and all of them
were true:

    check 7, the timing     only this feeder sets `due_at`, so `hold` had never
                            once been written to a decision log
    settled_on_time         0 of 0, because the denominator needs a due date
    the duplicate check     `is_duplicate` matches on id OR `invoice_ref`, and
                            only this feeder supplies an `invoice_ref`
    early_pay_discount      read nowhere else in the product

So these tests are about the FILE, and about the rungs being reachable through
it. A test that builds its own rows cannot tell you the register is there.

WHY THE ROWS ARE `sandbox`. The register is scoped per business, and the only
rows committed here belong to `sandbox` — the business `businesses.py` marks
`"sandbox": true` and `traction.py` filters out of every published figure, for
the reason it states: *"showing the UI and claiming usage are different things."*
`operator_commitments.jsonl` is sandbox-only for the same reason. These rows
prove the ladder can reach its own rungs; they deliberately do **not** move
`/operator/traction`, where `hold` still reads 0 because no real customer has
sent us a dated invoice. Making that number move needs a customer, not a commit.
"""

from __future__ import annotations

from index_api import businesses, entitlements
from index_api.operator import HOLD, obligations_from_entitlements, run_obligation

#: Far enough past the three-day pay window that check 7 has something to say,
#: and fixed rather than computed: a period taken from the clock mints a new
#: obligation id on every tick and the same charge gets paid twice.
NOW = 1_791_400_000.0


def _sandbox():
    rows = [b for b in businesses.load() if b.slug == "sandbox"]
    assert rows, "the sandbox business is missing from businesses.json"
    return rows[0]


def test_the_committed_register_is_not_empty() -> None:
    """The one thing fifteen passing tests could not tell us."""
    rows = entitlements.load()
    assert rows, (
        f"{entitlements.ARCHIVE_PATH.name} has no rows — the feeder is wired to "
        "nothing, and every consequence in this module's docstring is live"
    )


def test_every_row_carries_what_the_feeder_requires() -> None:
    """`obligations_from_entitlements` drops a row with no payee, resource or
    amount, and logs a warning rather than raising. A register whose rows are
    silently dropped is indistinguishable from an empty one, which is the state
    this whole file exists to detect."""
    for row in entitlements.load():
        ref = row.get("invoice_ref") or row.get("resource") or "<unnamed>"
        assert str(row.get("business") or "").strip(), f"{ref}: no business, so it is nobody's bill"
        assert str(row.get("payee") or "").strip(), f"{ref}: no payee"
        assert str(row.get("resource") or "").strip(), f"{ref}: no resource"
        amount = row.get("billed_usdc")
        assert isinstance(amount, (int, float)) and amount > 0, f"{ref}: no positive amount"
        assert float(row.get("period_end") or 0.0) > 0, f"{ref}: no period end to key the id on"


def test_every_row_is_addressed_to_a_business_the_registry_knows() -> None:
    """A bill for a slug that is not onboarded reaches no wallet and appears in
    no figure, so it would sit on the register looking like work."""
    known = {b.slug for b in businesses.load()}
    for row in entitlements.load():
        slug = str(row.get("business") or "").strip().lower()
        assert slug in known, f"{slug!r} is not in businesses.json"


def test_the_hold_rung_is_reachable_through_the_register() -> None:
    """Check 7, which had never fired.

    The assertion is on the INTENT rather than on the sentence, because the
    sentence carries a day count that moves. What must not regress is that some
    row on the register reaches the calendar rung at all — and reaching it means
    passing the meter first, which is why the row that demonstrates this is
    metered exactly as billed. A row even 2.5% under tolerance escalates at
    check 3 and the timing rung is never consulted.
    """
    biz = _sandbox()
    rows = entitlements.for_business("sandbox")
    intents = []
    for ob, used in obligations_from_entitlements(biz, rows):
        d = run_obligation(
            ob, receipts=[], catalog={}, metered_quantity=used,
            dry_run=True, log_path="/dev/null", now=NOW,
        )
        intents.append(d.intent)
    assert HOLD in intents, (
        f"no row on the register reaches check 7 — got {sorted(set(intents))}. "
        "A bill must be metered clean AND dated beyond the pay window to get there."
    )


def test_an_early_pay_discount_changes_the_decision() -> None:
    """`early_pay_discount` is read in exactly one place — the `elif` beside the
    timing rung — so a register with no discounted row leaves the field dead.

    Two rows on the register are dated the same distance out and differ only by
    the discount. If they ever decide the same way, the field has stopped
    mattering and the brief's "worth paying early for the discount" is not a
    thing this product does.
    """
    biz = _sandbox()
    rows = entitlements.for_business("sandbox")
    pairs = obligations_from_entitlements(biz, rows)

    far = [(ob, used) for ob, used in pairs if ob.due_at and ob.due_at > NOW + 30 * 86_400]
    assert len(far) >= 2, "need two far-dated rows to compare, with and without a discount"
    assert any(ob.early_pay_discount > 0 for ob, _ in far), "no far-dated row offers a discount"
    assert any(ob.early_pay_discount <= 0 for ob, _ in far), "no far-dated row lacks one"

    by_discount: dict[bool, str] = {}
    for ob, used in far:
        d = run_obligation(
            ob, receipts=[], catalog={}, metered_quantity=used,
            dry_run=True, log_path="/dev/null", now=NOW,
        )
        by_discount[ob.early_pay_discount > 0] = d.intent

    assert by_discount[False] == HOLD, "a far-dated bill with no discount should wait"
    assert by_discount[True] != HOLD, "a discount should buy the bill out of the wait"


def test_every_row_carries_an_invoice_ref() -> None:
    """The second half of `is_duplicate`, which matches on id OR `invoice_ref`.

    An obligation id is derived from (business, payee, resource, period_end), so
    it catches the same bill arriving twice unchanged. `invoice_ref` is what
    catches the vendor re-sending one charge under a new period — the case the id
    cannot see — and only this feeder supplies it.
    """
    for row in entitlements.load():
        assert str(row.get("invoice_ref") or "").strip(), (
            f"{row.get('resource')}: no invoice_ref, so the duplicate check has "
            "only the derived id to go on"
        )
