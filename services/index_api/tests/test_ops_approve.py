"""The escalation inbox's two actions — and what they refuse.

`operator/approve` is the only path by which an escalated bill gets paid, and it
pays from the OWNER's wallet. So the tests here are mostly about what it will not
do: pay an obligation nobody escalated, pay an amount or a payee the caller
supplied rather than the one the agent recorded, pay for a business with no
wallet, or pay without writing the outcome down.

The load-bearing assertion is `test_the_hash_is_the_one_the_agent_committed_to`.
The decision hash must come from the LOGGED record, hours after the agent wrote
it. If this console re-derived it from the form, the chain would hold a
commitment to whatever was posted and the euthyna claim would be hollow.
"""

from __future__ import annotations

import json

import pytest
from index_api import businesses as biz
from index_api import ops_actions
from index_api.operator import hashable_record
from index_api.ops_actions import ActionError, run

A = "0x" + "aa" * 20
VENDOR = "0x" + "bb" * 20
WALLET = "0x" + "cc" * 20
NOW = 1_790_000_000.0


class _FakeOwnerClient:
    """A PolicyClient stand-in: the three things the handlers touch."""

    def __init__(self, owner="0x" + "dd" * 20, escalate=True):
        self.owner_signer = type("S", (), {"address": owner})()
        self._escalate = escalate
        self.calls: list[tuple] = []

    def can_escalate(self):
        return self._escalate

    def budget(self, category):
        return {"category": category, "remaining_usdc": 900.0, "per_tx_limit_usdc": 10.0}

    def spend_as_owner(self, category, to, amount, record):
        self.calls.append((category, to, amount, record))
        return "0x" + "ee" * 32


def _row(**kw):
    base = dict(
        at=NOW - 100, obligation_id="inv-1", vendor=VENDOR, category="infra",
        billed_usdc=5.0, intent="escalate", rule="limit hit", business="acme",
        resource="svc", escalated=True, paid_usdc=0.0, tx=None,
        metered_quantity=None, vendor_quantity=None, discrepancy=None,
        par_usdc=None, best_usdc=None, over_par_bp=None, saving_usdc=None,
        reroute_to="", notes=[],
    )
    base.update(kw)
    return base


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """A registry with one business, a log, and a fake wallet client."""
    reg = tmp_path / "businesses.json"
    reg.write_text(json.dumps({"businesses": [
        {"slug": "acme", "treasury": A, "policy_wallet": WALLET,
         "categories": ["infra"], "consented": True},
        {"slug": "nowallet", "treasury": "0x" + "11" * 20},
    ]}))
    monkeypatch.setattr(biz, "REGISTRY_PATH", reg)

    logp = tmp_path / "decisions.jsonl"
    monkeypatch.setattr("index_api.operator.LOG_PATH", str(logp))
    monkeypatch.setattr("index_api.statement.LOG_PATH", str(logp))
    monkeypatch.setattr(ops_actions, "AUDIT_PATH", str(tmp_path / "audit.jsonl"))

    client = _FakeOwnerClient()
    real_resolver = ops_actions._business_wallet

    def seed(rows):
        logp.write_text("\n".join(json.dumps(r) for r in rows) + "\n")

    def use(c):
        """Swap in a fake wallet client, or `use(None)` for the real resolver —
        which is what the no-wallet refusal has to exercise."""
        monkeypatch.setattr(
            ops_actions, "_business_wallet",
            real_resolver if c is None
            else (lambda business: (biz.resolve(business), c)),
        )

    use(client)
    return seed, client, logp, use


# --- what it refuses -------------------------------------------------------

def test_approve_needs_a_business_and_an_obligation(wired):
    with pytest.raises(ActionError) as e:
        run("operator/approve", {}, True)
    assert e.value.status == 400


def test_an_obligation_nobody_escalated_cannot_be_approved(wired):
    seed, _, _, _ = wired
    seed([_row(intent="pay", rule="at par")])
    with pytest.raises(ActionError) as e:
        run("operator/approve", {"business": "acme", "obligation_id": "inv-1"}, True)
    assert e.value.status == 404
    assert "not waiting on anybody" in str(e.value)


def test_an_already_approved_obligation_cannot_be_approved_twice(wired):
    """The queue clears on resolution, so a second approval finds nothing. This is
    the duplicate-payment case the whole product is about."""
    seed, _, _, _ = wired
    seed([_row(), _row(at=NOW - 50, intent="pay", rule="owner approved", paid_usdc=5.0)])
    with pytest.raises(ActionError) as e:
        run("operator/approve", {"business": "acme", "obligation_id": "inv-1"}, True)
    assert e.value.status == 404


def test_a_business_with_no_wallet_says_so_rather_than_failing_obscurely(wired):
    seed, _, _, use = wired
    seed([_row(business="nowallet")])
    use(None)  # the real resolver, which is the thing being tested
    with pytest.raises(ActionError) as e:
        run("operator/approve",
            {"business": "nowallet", "obligation_id": "inv-1"}, True)
    assert e.value.status == 400
    assert "no PolicyWallet" in str(e.value)


def test_an_oversized_approval_is_capped_by_this_console(wired):
    """PolicyWallet's cap is the real authority; this is a second, smaller fence,
    because a bearer token is a weaker key than a wallet."""
    seed, _, _, _ = wired
    seed([_row(billed_usdc=10_000.0)])
    with pytest.raises(ActionError) as e:
        run("operator/approve", {"business": "acme", "obligation_id": "inv-1"}, True)
    assert e.value.status == 400
    assert "capped" in str(e.value)


def test_no_owner_key_means_nobody_here_can_approve(wired):
    seed, _, _, use = wired
    seed([_row()])
    use(_FakeOwnerClient(escalate=False))
    with pytest.raises(ActionError) as e:
        run("operator/approve", {"business": "acme", "obligation_id": "inv-1"}, True)
    assert "no owner key" in str(e.value)


# --- dry run first ---------------------------------------------------------

def test_a_dry_run_describes_the_payment_and_sends_nothing(wired):
    seed, client, _, _ = wired
    seed([_row()])
    out = run("operator/approve", {"business": "acme", "obligation_id": "inv-1"}, True)
    assert out["dry_run"] is True
    r = out["result"]
    assert "5.000000 USDC" in r["would"] and VENDOR in r["would"]
    assert r["rule_it_stopped_on"] == "limit hit"
    # `custody` names the SIGNER's class, not the client's: a raw key and a
    # Circle-custodied wallet are the distinction an operator needs here.
    assert r["owner"] == "0x" + "dd" * 20
    assert r["custody"] == type(client.owner_signer).__name__
    assert client.calls == [], "a dry run pays nothing"


# --- the live path ---------------------------------------------------------

def test_the_hash_is_the_one_the_agent_committed_to(wired):
    """From the LOGGED record, not the form. If this console re-derived the hash
    from what was posted, the chain would commit to the posted thing and the
    replayable-record claim would be hollow."""
    seed, client, _, _ = wired
    row = _row()
    seed([row])

    run("operator/approve", {"business": "acme", "obligation_id": "inv-1"}, False)

    assert len(client.calls) == 1
    category, to, amount, record = client.calls[0]
    assert (category, to, amount) == ("infra", VENDOR, 5.0)
    assert record == hashable_record(row), "the record is not the agent's own"
    assert "tx" not in record and "paid_usdc" not in record


def test_approving_appends_the_outcome_and_clears_the_queue(wired):
    """Appended, never an edit: the escalation stays exactly as the agent wrote
    it and the resolution sits after it."""
    from index_api.statement import pending_escalations, read_decisions

    seed, _, logp, _ = wired
    seed([_row()])
    run("operator/approve", {"business": "acme", "obligation_id": "inv-1"}, False)

    rows = read_decisions(str(logp), business="acme")
    assert len(rows) == 2, "the escalation is still there, with the outcome after it"
    assert rows[0]["intent"] == "escalate"
    assert rows[1]["intent"] == "pay"
    assert "owner approved" in rows[1]["rule"] and "limit hit" in rows[1]["rule"]
    assert rows[1]["paid_usdc"] == 5.0 and rows[1]["tx"]
    assert pending_escalations(rows) == [], "nothing is waiting any more"


def test_rejecting_records_the_refusal_pays_nothing_and_clears_the_queue(wired):
    from index_api.statement import pending_escalations, read_decisions

    seed, client, logp, _ = wired
    seed([_row()])
    run("operator/reject", {"business": "acme", "obligation_id": "inv-1",
                            "note": "wrong vendor"}, False)

    assert client.calls == [], "a rejection moves no money"
    rows = read_decisions(str(logp), business="acme")
    assert rows[-1]["intent"] == "refuse"
    assert "owner rejected: wrong vendor" in rows[-1]["rule"]
    assert pending_escalations(rows) == []


def test_a_rejection_with_no_reason_still_says_so(wired):
    from index_api.statement import read_decisions

    seed, _, logp, _ = wired
    seed([_row()])
    run("operator/reject", {"business": "acme", "obligation_id": "inv-1"}, False)
    assert "no reason given" in read_decisions(str(logp), business="acme")[-1]["rule"]


# --- every outcome is audited ---------------------------------------------

def test_both_actions_are_audited_including_the_refusals(wired):
    seed, _, _, _ = wired
    seed([_row()])
    run("operator/approve", {"business": "acme", "obligation_id": "inv-1"}, True)
    with pytest.raises(ActionError):
        run("operator/approve", {"business": "acme", "obligation_id": "nope"}, True)

    rows = ops_actions.recent(10)
    actions = [(r["action"], r["ok"]) for r in rows]
    assert ("operator/approve", True) in actions
    assert ("operator/approve", False) in actions, "a refusal is written down too"
