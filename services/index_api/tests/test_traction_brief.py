"""The three figures the hackathon's briefs name by word, and the four ways
each of them could flatter us.

RFB 4 asks for "obligations settled **on time** **without a human touching
them**" and "decisions made vs escalated, and **how often the human agreed**".
RFB 5 asks for "risk events caught **before** the transaction". None of the
three was answerable from the record as it stood: an owner's approval was
written as an ordinary payment, the agent recorded no recommendation to agree
with, and no decision carried a due date.

Every test here is about a denominator. The three most flattering numbers
available on a traction page are a perfect autonomy rate computed from a log
that cannot see a human, a perfect agreement rate over zero resolutions, and a
perfect punctuality rate over zero due dates — and all three are what you get by
being careless rather than by being good.
"""

from __future__ import annotations

from index_api.statement import agreement, autonomy, screening, summarise

VENDOR = "0x" + "bb" * 20


def _d(**kw) -> dict:
    row = {
        "at": 1_790_900_000.0,
        "obligation_id": "ob-1",
        "vendor": VENDOR,
        "category": "infra",
        "billed_usdc": 1.0,
        "paid_usdc": 1.0,
        "intent": "pay",
        "rule": "at par",
        "actor": "agent",
    }
    row.update(kw)
    return row


# --- autonomy ---------------------------------------------------------------


def test_an_owners_approval_is_not_the_agents_work():
    """The bug this field exists to kill.

    `_settle` wrote an approval as a plain `pay`, identical in every field to
    one the agent reached alone. Autonomy computed from that log is 100%,
    always, and says nothing.
    """
    a = autonomy([_d(), _d(obligation_id="ob-2", actor="owner")])
    assert a["settled_by_agent"] == 1
    assert a["settled_by_owner"] == 1


def test_a_row_written_before_actor_existed_counts_as_the_agents():
    """Every row in the committed archive predates the field. The agent did make
    those decisions, so defaulting them to `agent` is the true reading — and the
    alternative, dropping them, would quietly shrink the denominator."""
    row = _d()
    del row["actor"]
    assert autonomy([row])["settled_by_agent"] == 1


def test_an_escalation_is_not_a_settlement():
    """It is the opposite: the obligation is still open and waiting on a person."""
    a = autonomy([_d(intent="escalate")])
    assert a["settled_by_agent"] == 0 and a["settled_by_owner"] == 0


def test_holding_and_refusing_are_settlements_the_agent_reached():
    for intent in ("hold", "reroute", "refuse"):
        assert autonomy([_d(intent=intent)])["settled_by_agent"] == 1, intent


def test_a_bill_with_no_due_date_is_not_counted_punctual():
    """Otherwise "we do not know when this was due" becomes evidence of
    promptness, and every figure in the archive today has no due date."""
    a = autonomy([_d()])
    assert a["settled_with_a_due_date"] == 0
    assert a["settled_on_time"] == 0


def test_on_time_is_measured_against_the_due_date_it_has():
    early = autonomy([_d(at=100.0, due_at=200.0)])
    assert early == {**early, "settled_on_time": 1, "settled_with_a_due_date": 1}
    late = autonomy([_d(at=300.0, due_at=200.0)])
    assert late["settled_on_time"] == 0 and late["settled_with_a_due_date"] == 1


# --- agreement --------------------------------------------------------------


def test_agreement_compares_the_two_opinions():
    agreed = agreement([_d(actor="owner", intent="pay", recommended_intent="pay")])
    assert agreed == {"owner_resolutions": 1, "owner_agreed": 1}

    overruled = agreement([_d(actor="owner", intent="refuse", recommended_intent="pay")])
    assert overruled == {"owner_resolutions": 1, "owner_agreed": 0}


def test_an_unanswered_queue_is_not_unanimous_agreement():
    """The denominator is RESOLUTIONS, not escalations. Dividing by escalations
    would report a confident figure while the owner is on holiday."""
    a = agreement([_d(intent="escalate", recommended_intent="pay")])
    assert a == {"owner_resolutions": 0, "owner_agreed": 0}


def test_an_escalation_with_no_recommendation_is_excluded():
    """"No budget on chain" means the agent had no authority to form an opinion.
    Counting it would invent a disagreement the agent never voiced."""
    a = agreement([_d(actor="owner", intent="refuse", recommended_intent="")])
    assert a["owner_resolutions"] == 0


def test_the_agents_own_decisions_are_not_agreements_with_itself():
    a = agreement([_d(actor="agent", intent="pay", recommended_intent="pay")])
    assert a["owner_resolutions"] == 0


# --- screening --------------------------------------------------------------


def test_a_flagged_counterparty_that_was_not_paid_is_caught():
    s = screening([_d(intent="escalate", screen_risk="flagged")])
    assert s["risk_events_caught"] == 1


def test_unknown_is_never_counted_as_caught():
    """It means no screen could answer. Filing "we could not check" under "we
    caught something" inverts the only claim this figure makes."""
    s = screening([_d(intent="pay", screen_risk="unknown")])
    assert s["risk_events_caught"] == 0
    assert s["paid_unscreened"] == 1, "and it is not silently clean either"


def test_a_clear_screen_is_neither():
    s = screening([_d(intent="pay", screen_risk="clear")])
    assert s == {"risk_events_caught": 0, "paid_unscreened": 0}


def test_a_flagged_row_that_was_somehow_paid_is_not_a_catch():
    """It cannot happen — `decide` screens at step 4 and only assigns `pay`
    after step 8, returning early in between — and the count must not claim
    credit for it if the ordering is ever broken."""
    s = screening([_d(intent="pay", screen_risk="flagged")])
    assert s["risk_events_caught"] == 0


# --- all of it, through the summary the pages read --------------------------


def test_the_summary_carries_every_figure_the_briefs_ask_for():
    s = summarise([_d()])
    for key in (
        "settled_by_agent",
        "settled_by_owner",
        "settled_on_time",
        "settled_with_a_due_date",
        "owner_resolutions",
        "owner_agreed",
        "risk_events_caught",
        "paid_unscreened",
    ):
        assert key in s, key


def test_the_committed_archive_carries_a_real_human_settlement():
    """The archive this ships with is no longer agent-only, and that is the point.

    It held nine decisions, none touched by a person, no payment, no due date —
    so every figure here was honestly zero and none of them was PROVEN. A live
    run on Arc testnet then paid five obligations, escalated one above the
    0.05 USDC per-payment limit, and the owner settled that one with
    `spendAsOwner`. These assertions are against that record.
    """
    from index_api.statement import ARCHIVE_PATH, read_decisions

    s = summarise(read_decisions(str(ARCHIVE_PATH)))

    # A human really settled one, and the record can tell which.
    assert s["settled_by_owner"] >= 1, "an owner settlement must be distinguishable"
    assert s["settled_by_agent"] >= 1, "and so must the agent's own work"

    # The agreement figure has a real sample behind it, not an empty one.
    assert s["owner_resolutions"] >= 1
    assert 0 <= s["owner_agreed"] <= s["owner_resolutions"], "cannot agree more often than asked"

    # Money actually moved, which is what makes `moved_usdc` a claim at all.
    assert s["paid_usdc"] > 0

    # Still nothing to be punctual against: the runner sets no due dates, and
    # reporting 100% on-time from an empty denominator is the exact flattery
    # `autonomy()` refuses.
    assert s["settled_with_a_due_date"] == 0


def test_the_owner_settlement_carries_the_evidence_it_was_judged_on():
    """`_settle` used to write a thin stub: no screen, no meter, no benchmark.

    The facts did not change when a person agreed with them, and an audit that
    reads a settled obligation as unscreened is reading a hole we dug.
    """
    import json

    from index_api.statement import ARCHIVE_PATH

    rows = [json.loads(ln) for ln in ARCHIVE_PATH.read_text().splitlines() if ln.strip()]
    owner_rows = [r for r in rows if r.get("actor") == "owner"]
    assert owner_rows, "the archive should carry at least one owner settlement"
    r = owner_rows[-1]
    assert r["recommended_intent"], "nothing to compare the human's choice against"
    assert r["screen_risk"], "the counterparty verdict was carried forward"
    assert r["metered_quantity"] is not None, "so was the independent count"
    assert r["paid_usdc"] > 0 and r["tx"], "and it really paid, on chain"
