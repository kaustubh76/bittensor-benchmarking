"""Re-run the agent's reasoning from its own record, and get the same answer.

`contracts/src/PolicyWallet.sol` says the decision hash is "what makes the
off-chain ledger **replayable** rather than merely stored". That was not true
when it was written. The record pinned every OUTPUT of the pricing step — par,
best, basis points, saving — and none of its inputs or thresholds. A reviewer
could prove a row had not been edited, and could re-check the arithmetic between
those four numbers, but could not recompute them, could not tell which market
produced them, and could not tell what limits they were judged against.

The worst of the gaps was the denomination: given a bill of 1.5 against a par of
0.002, nothing in the record said whether the bill had been divided by a
quantity or compared whole — a thousandfold fork with no field to settle it.

Prior Art #01 asks for exactly this and nothing less: *"a reviewer can replay the
agent's reasoning instead of reconstructing it from bank statements."* A hash
proves nobody edited the story. Replay proves the story is the one the ladder
tells.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from index_api.operator import (
    ESCALATE,
    PAY,
    Obligation,
    decide,
    replay,
)

ARCHIVE = Path("services/index_api/index_api/operator_decisions.jsonl")
SANDBOX = Path("services/index_api/index_api/operator_decisions.sandbox.jsonl")


def _rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


# --- the round trip ---------------------------------------------------------


def test_a_fresh_decision_replays_to_itself():
    """The property, on a decision whose every input is known."""
    ob = Obligation(
        obligation_id="ob-1",
        vendor="0x" + "ab" * 20,
        billed_usdc=0.5,
        business="acme",
        category="infra",
        kind="x402",
        resource="/compute/x",
        vendor_quantity=100.0,
        unit="$/1k tokens",
    )
    first = decide(ob, metered_quantity=100.0, remaining_usdc=50.0,
                   per_tx_limit_usdc=10.0, now=1_000.0)
    again = replay(first.as_record())
    assert again.intent == first.intent
    assert again.rule == first.rule


def test_the_clock_comes_from_the_record_not_from_now():
    """`at` is the one non-deterministic input the record already pinned.
    Replaying against the present would make every held bill come due."""
    ob = Obligation(
        obligation_id="ob-due", vendor="0x" + "ab" * 20, billed_usdc=0.5,
        business="acme", category="infra", resource="/x",
        due_at=2_000_000.0,
    )
    held = decide(ob, remaining_usdc=50.0, per_tx_limit_usdc=10.0, now=1_000_000.0)
    assert held.intent == "hold", held.rule
    assert replay(held.as_record()).intent == "hold"


def test_the_denomination_survives_the_round_trip():
    """The thousandfold fork. A per-unit benchmark compares $/1k-tokens and must
    be multiplied back by the quantity before it is money; a per-call one
    compares the whole bill. Without the field on the record, a replay could
    pick the other one and look correct."""
    from index_api.par import Par

    ob = Obligation(
        obligation_id="ob-unit", vendor="0x" + "ab" * 20, billed_usdc=1.0,
        business="acme", category="infra", resource="/x",
        vendor_quantity=1_000.0, unit="$/1k tokens",
    )
    per_unit = Par(
        resource="/x", available=True, denomination="unit",
        par_usdc=0.001, best_usdc=0.001, best_seller="0x" + "cd" * 20, sellers=3,
    )
    d = decide(ob, par=per_unit, metered_quantity=1_000.0,
               remaining_usdc=50.0, per_tx_limit_usdc=10.0, now=1_000.0)
    assert d.par_denomination == "unit", "the record must say which market"
    back = replay(d.as_record())
    assert back.intent == d.intent and back.rule == d.rule
    assert back.over_par_bp == d.over_par_bp, "and the arithmetic re-derives"


def test_the_thresholds_travel_with_the_decision():
    """Three of the four come from the environment at import, so without them on
    the record a reviewer cannot recover them from the repo either."""
    ob = Obligation(
        obligation_id="ob-ceiling", vendor="0x" + "ab" * 20, billed_usdc=5.0,
        business="acme", category="infra", resource="/x",
    )
    # A ceiling of 10 pays it; a ceiling of 1 escalates it. Same bill.
    loose = decide(ob, remaining_usdc=50.0, per_tx_limit_usdc=10.0,
                   unbenchmarked_max_usdc=10.0, now=1_000.0)
    tight = decide(ob, remaining_usdc=50.0, per_tx_limit_usdc=10.0,
                   unbenchmarked_max_usdc=1.0, now=1_000.0)
    assert loose.intent == PAY and tight.intent == ESCALATE
    assert replay(loose.as_record()).intent == PAY, "the ceiling it cleared is on the record"
    assert replay(tight.as_record()).intent == ESCALATE


def test_the_screen_gate_travels_too():
    """`ACR_SCREEN_REQUIRED` flips `unknown` from payable to refused. It is not a
    parameter, it changes the branch, and it used to leave no trace at all — so
    a replay could reach the opposite verdict and look correct."""
    from index_api.counterparty import CounterpartyVerdict

    ob = Obligation(
        obligation_id="ob-unk", vendor="0x" + "ab" * 20, billed_usdc=0.5,
        business="acme", category="infra", resource="/x",
    )
    unknown = CounterpartyVerdict(
        address=ob.vendor, risk="unknown", backend="denylist",
        reason="no denylist is configured", screened=False,
    )
    d = decide(ob, screen=unknown, remaining_usdc=50.0, per_tx_limit_usdc=10.0, now=1_000.0)
    assert d.screen_required is not None, "the gate is recorded"
    assert d.screen_reason, "and so is the screen's own basis"
    assert replay(d.as_record()).intent == d.intent


# --- against everything we actually ship -----------------------------------


@pytest.mark.parametrize("path", [ARCHIVE, SANDBOX], ids=["archive", "sandbox"])
def test_every_shipped_record_replays_to_its_own_verdict(path):
    """THE TEST THAT EARNS THE WORD IN THE CONTRACT, and the one that caught a
    fixture lying.

    Rows written before the inputs were recorded cannot be replayed — they are
    skipped by name rather than silently, because "we could not check" and "it
    checked out" are different answers and this file exists to keep them apart.
    """
    rows = _rows(path)
    assert rows, f"{path} should ship rows"

    checked = skipped = 0
    mismatches = []
    for row in rows:
        # A row that predates the replay fields has no thresholds on it, so the
        # ladder would be re-run against today's defaults rather than the ones
        # it was judged on. Not a replay; an opinion.
        if row.get("unbenchmarked_max_usdc") is None:
            skipped += 1
            continue
        checked += 1
        got = replay(row)
        if got.intent != row.get("intent"):
            mismatches.append(
                f"{row.get('obligation_id')}: recorded {row.get('intent')!r}, "
                f"replays as {got.intent!r} ({got.rule})"
            )

    assert not mismatches, "a record that does not reproduce:\n  " + "\n  ".join(mismatches)
    # Not an assertion about the count — the archive is what it is — but the
    # numbers must be reported, or a run that replayed nothing passes silently.
    print(f"\n{path.name}: replayed {checked}, skipped {skipped} (predate the fields)")


def test_at_least_one_shipped_record_really_replays():
    """The guard on the test above, which would otherwise pass by skipping.

    Every row of the committed archive predates the replay fields, so on its own
    the parametrised test can report "0 replayed, 19 skipped" and go green —
    which is a check that cannot fail, the shape this repo keeps finding. The
    sandbox fixtures are generated by `scripts/gen_sandbox_decisions.py` through
    `decide()` itself, so they replay by construction; this asserts somebody is
    actually being checked.
    """
    replayable = [
        r for p in (ARCHIVE, SANDBOX) for r in _rows(p)
        if r.get("unbenchmarked_max_usdc") is not None
    ]
    assert replayable, (
        "no shipped record carries the fields a replay needs, so the replay test "
        "above proves nothing — regenerate the fixtures, or archive a decision "
        "made since the fields landed"
    )
    for row in replayable:
        assert replay(row).intent == row.get("intent"), row.get("obligation_id")


def test_a_fixture_that_the_ladder_would_not_reach_is_caught():
    """What this machinery is for, demonstrated on the row that was wrong.

    The hand-written `inv-0044` claimed *"140 USDC is at or above the 100
    per-payment limit, so the owner signs this one"* while carrying
    `par_usdc: null`. With no benchmark, a 140 USDC bill meets check 5 — the
    unbenchmarked ceiling — and never reaches the per-payment rule at check 8.
    The sentence was plausible and the decision was unreachable.
    """
    hand_written = {
        "obligation_id": "sandbox:inv-0044", "vendor": "0x" + "d4" * 20,
        "billed_usdc": 140.0, "business": "sandbox", "category": "contractors",
        "resource": "/work/milestone-2", "intent": ESCALATE,
        "rule": "140 USDC is at or above the 100 per-payment limit",
        "par_usdc": None, "screen_risk": "clear",
        "remaining_usdc": 400.0, "per_tx_limit_usdc": 100.0,
        "unbenchmarked_max_usdc": 1.0, "meter_tolerance": 0.02,
        "pay_window_s": 259200.0, "material_bp": 25.0, "at": 1_790_900_000.0,
    }
    got = replay(hand_written)
    assert got.intent == ESCALATE, "it does escalate — but not for the stated reason"
    assert "unbenchmarked" in got.rule, got.rule
    assert "per-payment" not in got.rule, (
        "the recorded rule names a check the ladder never reached"
    )
