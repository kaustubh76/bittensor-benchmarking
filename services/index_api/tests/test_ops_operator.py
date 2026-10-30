"""The systems ledger's spend-operator section.

Every other section of `/ops` watches something on chain or in a subgraph —
state that outlives the container. This one watches the two things the spend
operator added that nothing else can see: a decision log on a disk every deploy
erases, and a `PolicyWallet` address that may not exist on the chain we are on.

The tests are about what each verdict MEANS, because this section has three
tiers and putting a fact in the wrong one is the whole failure mode of a
dashboard. A wallet that is not on this chain is a hard failure — payments
through it return a transaction hash and move nothing. A human not reading their
queue is a warning — it is somebody else's attention, not our code. A node that
would not answer is unread, and must never render as either.
"""

from __future__ import annotations

import time

from index_api.businesses import Business

WALLET = "0x" + "a7" * 20


def _biz(slug: str = "acme", *, wallet: str = WALLET, sandbox: bool = False) -> Business:
    return Business(
        slug=slug,
        treasury="0x" + "11" * 20,
        policy_wallet=wallet,
        chain="testnet",
        sandbox=sandbox,
    )


def _row(**kw) -> dict:
    row = {
        "at": time.time() - 3600,
        "business": "acme",
        "obligation_id": "ob-1",
        "intent": "pay",
        "billed_usdc": 1.0,
        "paid_usdc": 1.0,
    }
    row.update(kw)
    return row


def _wire(monkeypatch, *, businesses, decisions=(), archived=(), live=(),
          status="ok", owner=True, explode=False, agent_kind="circle"):
    """Point the section at known state.

    `read_decisions` is called three ways by one function — unfiltered, at the
    archive, at the live log — so the fake dispatches on `path` rather than
    returning one list to all three. A fake that ignored `path` would make the
    at-risk count come out 0 no matter what, which is exactly the number this
    section exists to disbelieve.
    """
    from index_api import businesses as reg
    from index_api import operator as op
    from index_api import statement

    monkeypatch.setattr(reg, "real", lambda *_a, **_k: tuple(businesses))
    monkeypatch.setattr(op, "LOG_PATH", "/nonexistent/live.jsonl")

    def fake_read(path=None, **_kw):
        if path is None:
            return list(decisions)
        return list(live) if "live.jsonl" in str(path) else list(archived)

    monkeypatch.setattr(statement, "read_decisions", fake_read)

    class _Client:
        def __init__(self, **_kw):
            if explode:
                raise RuntimeError("no signer on this host")

        def wallet_status(self):
            return status

        def can_escalate(self):
            return owner

        def signer_kinds(self):
            return {"agent": agent_kind, "owner": "local" if owner else "none"}

    import acr_oracle_client.policy as pol

    monkeypatch.setattr(pol, "PolicyClient", _Client)


def _run() -> list[dict]:
    from index_api.ops import Recorder, _guard, _operator

    rec = Recorder()
    _guard(rec, "operator", "The spend operator", _operator)
    return rec.sections[0]["checks"]


def _find(checks: list[dict], needle: str) -> dict:
    hit = [c for c in checks if needle in c["label"]]
    assert hit, f"no check mentioning {needle!r} in {[c['label'] for c in checks]}"
    return hit[0]


# --- it renders at all ------------------------------------------------------


def test_the_section_is_registered():
    """A section function that is not in `SECTIONS` runs nowhere. The operator's
    state was invisible to the console for exactly this reason: the module had
    ten sections and none of them was the thing that spends money."""
    from index_api.ops import SECTIONS

    assert "operator" in {name for name, _t, _f in SECTIONS}


def test_one_business_cannot_take_the_page_down(monkeypatch):
    """`_guard` catches a crash, but it catches it for the WHOLE section — so a
    client that cannot be built for one business would hide the queue and the
    decision record too. The loop swallows per business and records an unread."""
    _wire(monkeypatch, businesses=[_biz()], explode=True)
    checks = _run()
    assert _find(checks, "wallet unread")["ok"] is None
    assert _find(checks, "decision record")["ok"] is not None, (
        "the rest of the section still reported"
    )


# --- the wallet, which is the money ----------------------------------------


def test_a_wallet_that_is_not_on_this_chain_is_a_hard_failure(monkeypatch):
    """The phantom payment, as an operator-visible fact.

    A CALL to a codeless address does not revert — measured on Arc at 22026 gas —
    so an operator pointed at a wallet that exists on the other chain gets a
    transaction hash back for a payment that moved nothing. That is not
    "degraded": every payment it makes is fiction.
    """
    _wire(monkeypatch, businesses=[_biz()], status="not_on_this_chain")
    c = _find(checks := _run(), "PolicyWallet")
    assert c["ok"] is False
    assert c["warn"] is False, "a wallet that cannot hold money is not a warning"
    assert "codeless" in (c["detail"] or ""), "the reader is told why it matters"
    assert checks, "and the section still rendered"


def test_a_node_that_would_not_answer_is_unread_not_broken(monkeypatch):
    """`no_rpc` is not cached by the client, because the node may come back.
    Rendering it as a missing wallet would cry outage on every throttle."""
    _wire(monkeypatch, businesses=[_biz()], status="no_rpc")
    assert _find(_run(), "wallet unread")["ok"] is None


def test_a_business_with_no_wallet_is_counted_but_not_probed(monkeypatch):
    """Measuring for a business without spending for them is a real product
    state — it is what an evaluation looks like — so it must not read as a
    failure."""
    _wire(monkeypatch, businesses=[_biz(wallet="")])
    checks = _run()
    assert "0 with a wallet to spend from" in _find(checks, "registered")["label"]
    assert not [c for c in checks if "PolicyWallet" in c["label"]]


def test_no_owner_key_is_a_warning_because_the_agent_still_runs(monkeypatch):
    """Without an owner signer the agent escalates and waits, which is the
    honest behaviour. Reporting it as a failure would say the product is broken
    when it is doing the one thing it promises."""
    _wire(monkeypatch, businesses=[_biz()], owner=False)
    c = _find(_run(), "owner key")
    assert (c["ok"], c["warn"]) == (False, True)


# --- the queue --------------------------------------------------------------


def test_a_fresh_escalation_is_the_product_working(monkeypatch):
    """An escalation is the agent reaching the end of its authority and
    stopping. A queue with something in it is not a fault."""
    _wire(monkeypatch, businesses=[_biz()],
          decisions=[_row(intent="escalate", at=time.time() - 3600)])
    c = _find(_run(), "escalation queue")
    assert c["ok"] is True
    assert "1 obligation(s)" in c["label"]


def test_a_queue_nobody_reads_is_a_warning(monkeypatch):
    """Past the staleness threshold the claim changes from "the agent stopped"
    to "nobody is answering", which is a fact about the operator and the only
    thing this check can honestly assert."""
    from index_api.ops import OPERATOR_QUEUE_STALE_H

    old = time.time() - (OPERATOR_QUEUE_STALE_H + 2) * 3600
    _wire(monkeypatch, businesses=[_biz()], decisions=[_row(intent="escalate", at=old)])
    c = _find(_run(), "escalation queue")
    assert (c["ok"], c["warn"]) == (False, True)
    assert "owner key" in (c["detail"] or ""), "and what clearing one needs"


def test_the_sandboxs_fixtures_are_not_an_operator_backlog(monkeypatch):
    """THE BUG THIS TEST EXISTS FOR, found by running the section against real
    state: all three "pending escalations" it reported were the sandbox's
    hand-written fixtures.

    They are there so the queue UI has something to render, which means they are
    DESIGNED to sit unanswered forever — no age threshold can ever clear them,
    and counting them presents a demonstration as real operation. That is a
    traction overclaim pointed the other way, and the registry's `sandbox` flag
    exists to stop both directions.
    """
    ancient = time.time() - 400 * 3600
    _wire(
        monkeypatch,
        businesses=[_biz()],  # `real()` already excludes the sandbox
        decisions=[
            _row(business="sandbox", intent="escalate", at=ancient, obligation_id="sandbox:inv-0043"),
            _row(business="acme", intent="pay"),
        ],
    )
    c = _find(_run(), "escalation queue")
    assert c["ok"] is True, "a fixture must not raise an operator alarm"
    assert "0 obligation(s)" in c["label"]
    assert "sandboxes excluded" in c["label"], "and a reader is told it was filtered"


# --- the record, and the disk that eats it ----------------------------------


def test_live_decisions_the_archive_does_not_hold_are_reported_as_at_risk(monkeypatch):
    """The hazard the section was added for. `data/` is untracked AND in
    `.dockerignore`, so a decision written only there is gone on the next
    redeploy — and the page would have reported the system healthy, because
    everything else it looks at lives on chain."""
    _wire(
        monkeypatch,
        businesses=[_biz()],
        archived=[_row(obligation_id="ob-1", at=100.0)],
        live=[_row(obligation_id="ob-1", at=100.0), _row(obligation_id="ob-2", at=200.0)],
    )
    c = _find(_run(), "decision record")
    assert (c["ok"], c["warn"]) == (False, True)
    assert "1 a redeploy would destroy" in c["label"]
    assert "archive-decisions" in (c["detail"] or ""), "the fix is named, not implied"


def test_at_risk_is_counted_with_the_archivers_own_rule(monkeypatch):
    """An escalation and its resolution share an `obligation_id` by design, so a
    key of the id alone would call the resolution already-archived and let the
    half of the record that proves a human was involved die on the disk.

    `decision_key` lives in `statement.py` for this: the archiver imports the
    same function, so this count cannot drift from what the archiver will
    actually take.
    """
    _wire(
        monkeypatch,
        businesses=[_biz()],
        archived=[_row(obligation_id="ob-1", intent="escalate", at=100.0)],
        live=[
            _row(obligation_id="ob-1", intent="escalate", at=100.0),
            _row(obligation_id="ob-1", intent="pay", at=200.0, actor="owner"),
        ],
    )
    assert "1 a redeploy would destroy" in _find(_run(), "decision record")["label"]


def test_a_sandbox_row_on_the_disk_is_not_at_risk(monkeypatch):
    """The archiver excludes sandboxes, so counting one as at-risk would raise a
    warning `make archive-decisions` can never clear — which trains an operator
    to ignore the line that matters."""
    _wire(
        monkeypatch,
        businesses=[_biz()],
        archived=[],
        live=[_row(business="sandbox", obligation_id="sandbox:inv-0044")],
    )
    assert "0 a redeploy would destroy" in _find(_run(), "decision record")["label"]


def test_an_operator_that_has_decided_nothing_says_so(monkeypatch):
    """Not an unknown: every file was read and none held a decision. That is the
    state of every first deploy, and rendering it as unread would send an
    operator looking for a fault that is not there."""
    _wire(monkeypatch, businesses=[_biz()])
    c = _find(_run(), "nothing decided")
    assert c["ok"] is True


def test_the_payment_channel_is_reported_and_a_raw_key_is_a_warning(monkeypatch):
    """Circle's developer-controlled wallet and a raw local key produce
    IDENTICAL calldata, so nothing downstream can tell them apart — and five
    payments on Arc testnet went out from a raw EOA while a fifth of the score
    is Circle tool usage. A warning rather than a failure: a raw key pays
    correctly, it just leaves the keys on the host.
    """
    _wire(monkeypatch, businesses=[_biz()], agent_kind="circle")
    c = _find(_run(), "the agent pays through")
    assert c["ok"] is True and "circle" in c["label"]

    _wire(monkeypatch, businesses=[_biz()], agent_kind="local")
    c = _find(_run(), "the agent pays through")
    assert (c["ok"], c["warn"]) == (False, True)
    assert "ACR_CIRCLE_TAKER_WALLET_ID" in (c["detail"] or ""), "and what to set"
