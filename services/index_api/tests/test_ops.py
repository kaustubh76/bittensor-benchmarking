"""The operator console — the sections it renders, and that it cannot raise.

`/ops` is the one surface an operator opens when something smells wrong, so the
contract is narrow and strict: every section reports, nothing throws, and a read
that did not land renders as its own tier rather than as a passing check with no
number attached.
"""

from __future__ import annotations


def test_the_console_reports_the_agent_gate_and_the_screen():
    """`_gate` answers "who takes the money" and says nothing about who is CALLING
    or what inspects what they send. While those were the same section, a screen with
    no call sites was invisible to the one surface an operator actually opens."""
    from index_api.ops import SECTIONS, run_all

    assert "agent" in {name for name, _title, _fn in SECTIONS}

    out = run_all()
    agent = next((s for s in out["sections"] if s["name"] == "agent"), None)
    assert agent is not None, "the agent section did not render"
    labels = " ".join(c["label"] for c in agent["checks"])
    assert "audience:" in labels
    assert "screen backend:" in labels, "the backend is the one thing a reader cannot infer"
    assert "inspections:" in labels
    # Never raises: a console that can take the press down with it is worse than no
    # console, which is why `_guard` exists and why this asserts a shape not a value.
    assert all(c["ok"] in (True, False, None) for c in agent["checks"])


def test_the_press_wallet_is_watched_with_a_hard_floor(monkeypatch):
    """On 2026-09-12 the press wallet — which signs EVERY on-chain write — ran to
    0.006 USDC while the console did not look at it and the verifier only warned.
    Two things are pinned here: the wallet appears in the funding section at all,
    and its floor is a FAILURE rather than a warning, because a wallet that cannot
    pay gas is not "degraded" — it is every pillar stopping within the hour."""
    from index_api import ops
    from index_api.ops import Recorder

    class _Signer:
        address = "0x8366968f84a343CF70941EBe858428643d825cb0"

    class _Eth:
        def get_balance(self, _addr):
            return int(0.25 * 1e18)  # well below the 1.0 floor

    class _W3:
        """Stands in for the CLASS, because `_funding` calls `Web3.HTTPProvider(...)`
        before it ever constructs an instance — a bare factory lambda here reports
        "no RPC" and the section bails before reaching the wallet."""

        eth = _Eth()

        def __init__(self, *_a, **_k):
            pass

        @staticmethod
        def HTTPProvider(*_a, **_k):  # noqa: N802 - mirrors web3's name
            return object()

        @staticmethod
        def to_checksum_address(a):
            return a

        @staticmethod
        def from_wei(v, _unit):
            return v / 1e18

    import web3

    monkeypatch.setattr(web3, "Web3", _W3)
    import acr_oracle_client

    monkeypatch.setattr(acr_oracle_client, "build_role_signer",
                        lambda role, s=None: _Signer() if role == "poster" else None)

    rec = Recorder()
    rec.section("funding", "Wallet runway")
    ops._funding(rec)
    press = [c for c in rec.sections[0]["checks"] if c["label"].startswith("press:")]
    assert press, "the press wallet must appear in the funding section"
    c = press[0]
    assert c["ok"] is False, "0.25 USDC is below the critical floor and must FAIL"
    assert c["warn"] is False, "the floor is a failure, not a warning — that was the whole bug"
    assert "critical floor" in (c["detail"] or "")


def test_a_post_reserves_twice_its_cost_and_that_is_where_writes_stop():
    """Pinned to the chain, not to a formula someone believed. Measured on Arc
    mainnet 2026-10-13: the last post that landed was estimated at 181,800 gas
    with a 40 gwei max fee — 0.007272 USDC reserved up front — and cost 0.003584.
    The wallet held 0.005272: enough to pay for a post, too little to send one."""
    from index_api.desk import post_reservation_usdc

    reserve = post_reservation_usdc(20 * 10**9)
    assert abs(reserve - 0.007272) / 0.007272 < 0.01, reserve
    assert post_reservation_usdc(None) == reserve, "an unreadable base fee falls back to Arc's measured 20 gwei"
    assert 0.003584 < 0.005272 < reserve, "the outage: affordable to pay, impossible to send"


def test_the_ledger_says_posts_are_refused_now_below_the_reservation(monkeypatch):
    """The old sentence — writes stop "when this reaches zero" — was on screen for
    the whole ~49-hour mainnet outage, at 0.005 USDC, while every post was being
    refused. Below the reservation the ledger must say so in those words."""
    from index_api import ops
    from index_api.ops import Recorder

    class _Signer:
        address = "0x1547B0d3C334F264D28B087566B512AB5CD85379"

    class _Eth:
        def get_balance(self, _addr):
            return 5272576941191814  # the measured mainnet balance

        def get_block(self, _which):
            return {"baseFeePerGas": 20 * 10**9}

    class _W3:
        eth = _Eth()

        def __init__(self, *_a, **_k):
            pass

        @staticmethod
        def HTTPProvider(*_a, **_k):  # noqa: N802 - mirrors web3's name
            return object()

        @staticmethod
        def to_checksum_address(a):
            return a

        @staticmethod
        def from_wei(v, _unit):
            return v / 1e18

    import web3

    monkeypatch.setattr(web3, "Web3", _W3)
    import acr_oracle_client

    monkeypatch.setattr(acr_oracle_client, "build_role_signer",
                        lambda role, s=None: _Signer() if role == "poster" else None)
    rec = Recorder()
    rec.section("funding", "Wallet runway")
    ops._funding(rec)
    (c,) = [c for c in rec.sections[0]["checks"] if c["label"].startswith("press:")]
    assert c["ok"] is False
    assert "REFUSED NOW" in c["detail"], c["detail"]
    assert "reaches zero" not in c["detail"], "the sentence that hid the outage is gone"


def test_the_console_fails_when_the_rotation_window_has_no_humans(monkeypatch):
    """At 00:00 UTC on the boundary every resolution expires at once and only a
    manual chore brings them back. The one surface an operator opens must go RED
    then — not warn, and not leave it to a chip saying "out of date"."""
    from index_api import ops, tca
    from index_api.ops import Recorder

    monkeypatch.setattr(tca, "_cfg", lambda: ("https://example.invalid", ""))
    monkeypatch.setattr(tca, "graph_query", lambda *a, **k: {"humanClusters": []})
    def _only(rec):
        (sec,) = rec.sections
        (c,) = sec["checks"]
        return c

    rec = Recorder()
    ops._humans_this_window(rec, 2959)
    c = _only(rec)
    assert c["ok"] is False and "0 cluster(s)" in c["label"] and "resolve-humans" in c["detail"]

    monkeypatch.setattr(tca, "graph_query", lambda *a, **k: {"humanClusters": [{"id": "0x1", "walletCount": 3}, {"id": "0x2", "walletCount": 1}]})
    rec = Recorder()
    ops._humans_this_window(rec, 2959)
    c = _only(rec)
    assert c["ok"] is True and "2 cluster(s), 4 wallet(s)" in c["label"]

    # No subgraph: unknown, never a false pass.
    monkeypatch.setattr(tca, "_cfg", lambda: ("", ""))
    rec = Recorder()
    ops._humans_this_window(rec, 2959)
    assert _only(rec)["ok"] is None


def test_the_console_names_which_graph_path_carries_the_queries(monkeypatch):
    """Studio's development URL is capped at 3,000 queries a day and counted on no
    dashboard; the gateway is counted and billed. The console says which is in
    use and, on the dev URL, warns when the pace would cross the cap."""
    from acr_tape import graph_client
    from index_api import ops
    from index_api.ops import Recorder

    def _only(rec):
        (sec,) = rec.sections
        (c,) = sec["checks"]
        return c

    graph_client._reset_for_tests()
    now = graph_client.time.time()
    # A burst in the first minute of uptime is NOT a pace: unknown, never a warning.
    graph_client._recent.extend([now] * 100)
    monkeypatch.setattr(graph_client, "_ledger", {**graph_client._ledger, "queries": 100, "started_at": now - 30})
    rec = Recorder()
    ops._subgraph_transport(rec, "https://api.studio.thegraph.com/query/1/x/v0.2.0")
    c = _only(rec)
    assert c["ok"] is None and "measuring" in c["label"]
    # The same hundred in the last hour, ten minutes in: 2,400/day, over 70% of 3,000 → warn.
    monkeypatch.setattr(graph_client, "_ledger", {**graph_client._ledger, "queries": 100, "started_at": now - 700})
    rec = Recorder()
    ops._subgraph_transport(rec, "https://api.studio.thegraph.com/query/1/x/v0.2.0")
    c = _only(rec)
    assert c["ok"] is False and "via studio-dev" in c["label"] and "2,400/day" in c["label"] and "step 6" in c["detail"]

    graph_client._recent.clear()
    graph_client._recent.extend([now] * 10)
    rec = Recorder()
    ops._subgraph_transport(rec, "https://gateway.thegraph.com/api/subgraphs/id/QmX")
    c = _only(rec)
    assert c["ok"] is True and "via gateway" in c["label"] and "240/day" in c["label"] and "usage page" in c["detail"]

    rec = Recorder()
    ops._subgraph_transport(rec, "")
    assert _only(rec)["ok"] is None
    graph_client._reset_for_tests()


def test_memory_is_watched_against_the_tier_limit(monkeypatch):
    """OOM-killed at 512 MiB on 2026-09-13 with nothing saying it was close."""
    from index_api import ops
    from index_api.ops import Recorder

    def _only(rec):
        (sec,) = rec.sections
        (c,) = sec["checks"]
        return c

    assert isinstance(ops.rss_mib(), float), "the real reading works on this platform"
    monkeypatch.setattr(ops, "rss_mib", lambda: 300.0)
    rec = Recorder()
    ops._memory(rec)
    c = _only(rec)
    assert c["ok"] is True and "300 MiB of 512" in c["label"]
    monkeypatch.setattr(ops, "rss_mib", lambda: 450.0)
    rec = Recorder()
    ops._memory(rec)
    c = _only(rec)
    assert c["ok"] is False and "OOM" in c["detail"]
    monkeypatch.setattr(ops, "rss_mib", lambda: None)
    rec = Recorder()
    ops._memory(rec)
    assert _only(rec)["ok"] is None


def test_the_memory_guard_acts_past_the_line_and_says_what_it_freed(monkeypatch):
    """OOM-killed at 512 MiB after the hourly press left ~150 MiB of fragmented
    heap behind. Past 90 % the warm tick drops the graph cache and returns freed
    heap to the OS, and /ops shows the last action."""
    from acr_tape import graph_client
    from index_api import memory

    memory.last_action = None
    readings = iter([300.0])
    monkeypatch.setattr(memory, "rss_mib", lambda: next(readings, 300.0))
    assert memory.guard(limit_mib=512) is None, "below the line: nothing happens"

    readings = iter([480.0, 480.0, 420.0, 420.0])
    monkeypatch.setattr(memory, "rss_mib", lambda: next(readings, 420.0))
    graph_client._reset_for_tests()
    graph_client._cache["k"] = (graph_client.time.time(), {"x": 1}, 10)
    a = memory.guard(limit_mib=512)
    assert a and a["before_mib"] == 480 and a["dropped_cache_bytes"] == 10
    assert graph_client.cache_bytes() == 0, "the cache was dropped"
    assert memory.last_action is a
    assert isinstance(memory.trim(), (float, type(None))), "trim never raises, glibc or not"


def test_the_console_shows_the_humans_chore_only_where_it_runs(monkeypatch):
    """The press resolves each window itself where `ACR_KEEPER_HUMANS=1`. Where
    it runs, the console must say whether it is alive and surface a refusal
    ("not resolving: …", "failed: …") instead of letting window after window go
    unresolved in silence, which is how five weeks passed before 2026-10-16."""
    from index_api import keeper, ops
    from index_api.ops import Recorder

    def humans_checks(status):
        monkeypatch.setattr(keeper, "status", lambda: status)
        rec = Recorder()
        ops._keeper(rec)
        return [c for sec in rec.sections for c in sec["checks"] if c["label"].startswith("humans")]

    fresh = {"checked_age_s": 30.0, "verdict": None, "on": True}
    base = {"enabled": True, "heartbeat": {"checked_age_s": 10.0}, "roll": {"checked_age_s": 10.0}}

    assert humans_checks({**base, "humans": {"on": False}}) == []

    (ok,) = humans_checks({**base, "humans": fresh})
    assert ok["ok"] is True and "resolved" not in ok["detail"], "no verdict yet is not a resolved window"

    (refused,) = humans_checks({**base, "humans": {**fresh, "verdict": "not resolving: 0xabc is not in the mirror's signer set"}})
    assert refused["ok"] is False and "signer set" in refused["detail"]

    (failed,) = humans_checks({**base, "humans": {**fresh, "verdict": "failed: Circle refused the API key (HTTP 403)"}})
    assert failed["ok"] is False

    (silent,) = humans_checks({**base, "humans": {"on": True, "checked_age_s": None, "verdict": None}})
    assert silent["ok"] is None
