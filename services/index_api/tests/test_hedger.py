"""The hedger's public state — derived from public data, and honest about gaps.

Hermetic: a fake reader stands in for the chain, so these run with no RPC, no
Circle credentials and no agent wallet.
"""

from __future__ import annotations

import pytest
from index_api import hedger

AGENT = "0x1dc707e330d7b1cd9b2d0a0c0346ce2d19d35bc9"
PAYER = "0x71e140d9540d3163ead1591f81b454c6d5bd26c0"


class _Client:
    def __init__(self, position=None, collateral=0.0):
        self._position = position
        self._collateral = collateral

    def position_of(self, sid, who):
        return self._position

    def collateral_of(self, sid, who):
        return self._collateral


class _Futures:
    def __init__(self, trades=None, desk=None, client=None):
        self._trades = trades or []
        self._desk = desk
        self._client = client or _Client()

    def recent_trades(self):
        return self._trades

    def read_desk(self, index_id, **kw):
        return self._desk


def _fill(taker: str, tx: str = "0xabc") -> dict:
    return {"series_id": 3, "taker": taker, "qty": 1.0, "side": "buy",
            "mark": 0.5, "block": 1, "tx": tx, "seen_at": 0.0}


@pytest.fixture(autouse=True)
def _agent_env(monkeypatch):
    monkeypatch.setenv("ACR_HEDGER_ADDRESS", AGENT)
    monkeypatch.setenv("ACR_HEDGER_PAYER", PAYER)


def test_unconfigured_reports_itself_rather_than_inventing_an_agent(monkeypatch):
    """A deployment with no agent must say so. An empty-but-configured-looking
    panel would claim an autonomous trader that does not exist."""
    monkeypatch.delenv("ACR_HEDGER_ADDRESS", raising=False)
    state = hedger.build_hedger_state(_Futures())
    assert state["configured"] is False
    assert state["agent"] is None and state["fills"] == []


def test_only_this_agents_fills_are_counted():
    """The tape carries every trader's fills. Attributing someone else's trade
    to the agent would overstate exactly the thing this panel exists to show."""
    mine, theirs = _fill(AGENT.upper(), "0x1"), _fill("0xdead" + "0" * 36, "0x2")
    state = hedger.build_hedger_state(_Futures(trades=[mine, theirs]))
    assert [f["tx"] for f in state["fills"]] == ["0x1"], "matched on a case-sensitive address"


def test_spend_is_attributed_to_the_backing_eoa_not_the_smart_account():
    """The agent's TWO identities, and the reason this is easy to get wrong: the
    settlement records the backing EOA (EIP-3009 needs an ecrecover-able
    signature) while the trade records the smart account. Matching the ledger on
    the smart account would report a paying agent as having paid nothing."""
    receipts = {"receipts": [
        {"payer": PAYER, "amount_usdc": 0.0001},
        {"payer": PAYER, "amount_usdc": 0.0001},
        {"payer": "0x784e" + "0" * 36, "amount_usdc": 0.0001},  # a different buyer
    ]}
    state = hedger.build_hedger_state(_Futures(), receipts)
    assert state["paid_queries"] == 2
    assert state["spent_usdc"] == pytest.approx(0.0002)


def test_an_unread_ledger_is_unknown_not_zero():
    """`0 paid` and `we did not look` are different claims. Reporting the second
    as the first is the failure this codebase keeps meeting."""
    state = hedger.build_hedger_state(_Futures())
    assert state["paid_queries"] is None
    assert state["spent_usdc"] is None


def test_a_throttled_collateral_read_stays_unknown():
    """`collateral_of` returning None means the chain would not say. Flattening
    that to 0.0 would show a funded agent as broke."""
    fut = _Futures(desk={"series_id": 3}, client=_Client(collateral=None))
    state = hedger.build_hedger_state(fut)
    assert state["collateral_usdc"] is None


def test_the_gap_is_the_decision():
    """The panel shows mandate minus reality; computing it server-side keeps the
    UI from re-deriving it and drifting from what the agent itself used."""
    fut = _Futures(desk={"series_id": 3}, client=_Client(position={"contracts": 1.81}))
    state = hedger.build_hedger_state(fut)
    assert state["position_contracts"] == 1.81
    assert state["gap_contracts"] == pytest.approx(hedger.TARGET_CONTRACTS - 1.81)


def test_no_position_means_no_gap_rather_than_a_full_one():
    """An unread position with a computed gap would tell the agent to trade the
    whole mandate on the strength of a failed read."""
    fut = _Futures(desk={"series_id": 3}, client=_Client(position=None))
    state = hedger.build_hedger_state(fut)
    assert state["position_contracts"] is None and state["gap_contracts"] is None


#: Every key /hedger promises. `HedgerState` in apps/terminal/lib/types.ts is
#: typed against this exact set, `apps/terminal/app/api/hedger/route.ts` carries
#: a literal of it, and ops.py reads several by name. A field dropped here is a
#: panel rendering "…" forever over a value that exists, and a dashboard branch
#: that never fires — which is exactly what happened: ops.py read `position` for
#: a payload that has always said `position_contracts`, and nothing failed,
#: because no test named the key set.
HEDGER_KEYS = {
    "configured", "agent", "payer", "index_id", "target_contracts", "venue",
    "wallet_kind", "series_id", "multiplier", "mark", "mark_block",
    "position_contracts", "gap_contracts", "collateral_usdc",
    "fills", "receipts", "paid_queries", "spent_usdc",
}


def test_the_payload_key_set_is_the_contract():
    fut = _Futures(desk={"series_id": 3, "multiplier": 10},
                   client=_Client(position={"contracts": 2.0}))
    assert set(hedger.build_hedger_state(fut)) == HEDGER_KEYS


def test_the_unconfigured_payload_carries_the_same_key_set(monkeypatch):
    """A switched-off deployment must return the same SHAPE, not a subset. The
    panel's off state reads `index_id`, `target_contracts`, `venue` and
    `series_id` off it, and a key that exists only sometimes is a key the UI
    learns to optional-chain and then to distrust. `gap_contracts` used to be
    attached past the early return, so this branch returned one key fewer."""
    monkeypatch.setenv("ACR_HEDGER_ADDRESS", "")
    assert set(hedger.build_hedger_state(_Futures())) == HEDGER_KEYS


def test_the_agents_own_receipts_ride_along_newest_first():
    """The spend counter was computed from these rows and then threw them away,
    so the panel could show the position an agent took and not one print it
    bought. Sorted here rather than trusted: the committed archive is
    append-ordered by capture, not by settlement, so the newest of its four real
    hedger rows sits third in the file."""
    receipts = {"receipts": [
        {"payer": PAYER, "amount_usdc": 0.0001, "tx_ref": "old",
         "network": "eip155:5042002", "scheme": "exact", "settled_at": 100.0,
         "resource": ""},
        {"payer": PAYER, "amount_usdc": 0.0001, "tx_ref": "new",
         "network": "eip155:5042002", "scheme": "exact", "settled_at": 300.0,
         "seq": 7},
        {"payer": "0x784e" + "0" * 36, "amount_usdc": 9.0, "tx_ref": "theirs",
         "network": "eip155:5042002", "scheme": "exact", "settled_at": 400.0},
    ]}
    rows = hedger.build_hedger_state(_Futures(), receipts)["receipts"]
    assert [r["tx_ref"] for r in rows] == ["new", "old"]
    # Projected to the INTERSECTION of the two ledger paths: `seq` is live-only
    # and `resource` is archive-only, and a key present on one path and absent on
    # the other is how a panel learns to render "…" for a value that is there.
    assert set(rows[0]) == {"tx_ref", "amount_usdc", "network", "settled_at"}


def test_an_unread_ledger_leaves_receipts_unknown_not_empty():
    """[] says 'this agent has never paid'. None says 'we did not look'. The
    panel prints a different sentence for each, so the press keeps them apart —
    and all three spend fields move together, or the card would show a spend
    counter above a table claiming there is nothing to count."""
    state = hedger.build_hedger_state(_Futures())
    assert state["receipts"] is None
    assert state["paid_queries"] is None and state["spent_usdc"] is None


def test_the_mark_is_the_venues_own_last_fill_on_the_agents_series():
    """ACRFutures fills every trade at `oracle.latestValue(indexId)` and emits
    it, so the newest Traded on this series IS an oracle print the contract
    used. It must come from the AGENT's series, not from whichever book traded
    most recently — and `read_desk` carries no mark at all, so the tape is the
    only source. Note the tape has one even when the agent's own fills do not:
    the heartbeat keeps trading while an agent at its mandate holds."""
    theirs = {**_fill("0xdead" + "0" * 36, "0x9"), "series_id": 5, "mark": 0.002}
    mine = {**_fill("0xc972" + "0" * 36, "0x1"), "series_id": 3,
            "mark": 0.49112, "block": 55880191}
    fut = _Futures(trades=[theirs, mine], desk={"series_id": 3, "multiplier": 10},
                   client=_Client(position={"contracts": 2.0}))
    state = hedger.build_hedger_state(fut)
    assert state["mark"] == pytest.approx(0.49112)
    assert state["mark_block"] == 55880191 and state["multiplier"] == 10
    assert state["fills"] == [], "the mark is the venue's, not this agent's own"


def test_ops_reports_the_position_under_the_name_the_payload_uses(monkeypatch):
    """The join between two modules, pinned. `ops.py` read `st["position"]`
    against a payload that has always said `position_contracts`, so the branch
    was dead and /ops never once reported the number the agent exists to move.
    Nothing failed, because nothing named the key. A rename on either side now
    fails here instead of going quiet."""
    from index_api import app as app_mod
    from index_api import marketplace as mkt_mod
    from index_api import onchain as onchain_mod
    from index_api import ops

    monkeypatch.setattr(onchain_mod, "get_futures", lambda *a, **k: _Futures())
    monkeypatch.setattr(app_mod, "get_facilitator", lambda *a, **k: None)
    monkeypatch.setattr(mkt_mod, "build_receipts", lambda *a, **k: None)
    monkeypatch.setattr(
        hedger, "build_hedger_state",
        lambda *a, **k: {"configured": True, "position_contracts": 1.75,
                         "gap_contracts": 0.25, "target_contracts": 2.0,
                         "paid_queries": 4, "spent_usdc": 0.0004},
    )
    rec = ops.Recorder()
    rec.section("hedger", "The hedger")
    ops._hedger(rec)
    labels = [c["label"] for c in rec.sections[0]["checks"]]
    assert any("1.750" in lab for lab in labels), labels
    assert any("4 paid read" in lab for lab in labels), labels


def test_the_addresses_resolve_from_settings_when_the_environment_is_silent(monkeypatch):
    """Render inlines both as real environment variables; a checkout has them
    only in `.env`, which ONLY ACRSettings reads. Reading raw os.environ alone is
    why the committed bundle archived a live venue with no agent standing on it.
    Environment still wins wherever both are set."""
    monkeypatch.setenv("ACR_HEDGER_ADDRESS", "")
    monkeypatch.setenv("ACR_HEDGER_PAYER", "")
    monkeypatch.setattr(
        hedger, "get_settings",
        lambda: type("S", (), {"futures_address": "", "hedger_address": AGENT,
                               "hedger_payer": PAYER})(),
    )
    state = hedger.build_hedger_state(_Futures())
    assert state["configured"] and state["agent"] == AGENT
    assert state["payer"] == PAYER
