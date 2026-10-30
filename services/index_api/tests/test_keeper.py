"""The venue keeper's safety rules.

It shares a process with the press, so the tests that matter are the ones about
what it REFUSES to do. Hermetic: no chain, no Circle, no credentials.
"""

from __future__ import annotations

import pytest
from index_api import keeper


@pytest.fixture(autouse=True)
def _reset_cooldowns(monkeypatch):
    monkeypatch.setattr(keeper, "_last_heartbeat", 0.0)
    monkeypatch.setattr(keeper, "_last_roll_check", 0.0)
    monkeypatch.delenv("ACR_KEEPER", raising=False)


class _LocalSigner:
    """Stands in for LocalKeySigner — a raw key, which the keeper must refuse."""

    address = "0x" + "a" * 40


class CircleWalletSigner:  # noqa: N801 — the NAME is the assertion
    """Named EXACTLY like the real class, because `_custody_signer` checks
    `type(sg).__name__`. An underscore-prefixed fake silently fails that
    check and makes the refusal tests pass for the wrong reason."""

    address = "0x" + "b" * 40


def test_the_kill_switch_stops_everything(monkeypatch):
    """One env var must stand the keeper down without a deploy — the thing you
    reach for when the venue is misbehaving and the press must keep printing."""
    monkeypatch.setenv("ACR_KEEPER", "0")
    assert keeper.enabled() is False
    assert keeper.heartbeat_once(None) is None
    assert keeper.roll_if_needed(None) is None


def test_it_refuses_a_raw_key_and_does_not_fall_back(monkeypatch):
    """The whole reason this module exists. `build_role_signer` will happily
    return a LOCAL signer when a role has no Circle wallet — and some hosts have
    an ambient ACR_POSTER_PRIVATE_KEY lying around. Silently signing the venue's
    chores with it would reintroduce exactly what the migration removed."""
    monkeypatch.setattr(
        "acr_oracle_client.build_role_signer", lambda role, settings=None: _LocalSigner()
    )
    assert keeper._custody_signer("taker") is None
    assert keeper.heartbeat_once(None) is None
    assert keeper.roll_if_needed(None) is None


def test_no_signer_at_all_is_a_quiet_no_op(monkeypatch):
    """An offline or unconfigured deployment must not raise inside the press's
    warm loop."""
    monkeypatch.setattr(
        "acr_oracle_client.build_role_signer", lambda role, settings=None: None
    )
    assert keeper.heartbeat_once(None) is None


def test_custody_is_accepted(monkeypatch):
    monkeypatch.setattr(
        "acr_oracle_client.build_role_signer",
        lambda role, settings=None: CircleWalletSigner(),
    )
    assert keeper._custody_signer("maker") is not None


def test_the_cooldown_bounds_a_failing_chore(monkeypatch):
    """Without a floor, a chore that cannot succeed would be retried on every
    60-second warm tick, burning gas and RPC budget to keep failing."""
    import time

    monkeypatch.setattr(
        "acr_oracle_client.build_role_signer",
        lambda role, settings=None: CircleWalletSigner(),
    )
    monkeypatch.setattr(keeper, "_last_heartbeat", time.time())  # just ran
    assert keeper.heartbeat_once(None) is None, "it ran again inside the cooldown"
    monkeypatch.setattr(keeper, "_last_roll_check", time.time())
    assert keeper.roll_if_needed(None) is None


def test_a_chore_failure_never_costs_the_press_a_beat():
    """The isolation guarantee, checked against the code that provides it: the
    keeper's exceptions are caught INSIDE the warm loop's own handler, so a
    broken chore cannot skip the print republish that follows it."""
    import asyncio

    from index_api import app

    async def _boom(futures):
        raise RuntimeError("venue on fire")

    class _Keeper:
        enabled = staticmethod(lambda: True)
        heartbeat_once = staticmethod(lambda f: (_ for _ in ()).throw(RuntimeError("boom")))
        roll_if_needed = staticmethod(lambda f: None)

    import sys

    real = sys.modules.get("index_api.keeper")
    sys.modules["index_api.keeper"] = _Keeper  # type: ignore[assignment]
    try:
        asyncio.run(app._run_keeper(None))  # must NOT raise
    finally:
        if real is not None:
            sys.modules["index_api.keeper"] = real


def test_the_roll_refuses_a_venue_it_does_not_own():
    """`openSeries` is onlyOwner, so this is the difference between rolling and
    a transaction that reverts on every cooldown forever.

    Until 2026-08-03 the venue's owner was the retiring EOA and this was always
    False — the keeper could only shout for a human. The handover to the maker's
    own Circle wallet is what made an unattended roll possible, and the guard
    has to survive that changing back: a fork, a redeploy pointed at another
    venue, another ownership transfer.
    """
    maker = "0x9D44A7Dd4e7bF173B3F13ee41E1B60C8e92388d2"
    assert keeper.may_open_series(maker, maker.lower()), "case must not decide this"
    assert not keeper.may_open_series("0x33189c643774ED2713EbFf5A6923e5fa42b96eE8", maker)
    # An unreadable owner is not permission. Empty must never mean "go ahead".
    assert not keeper.may_open_series("", maker)
    assert not keeper.may_open_series(maker, "")


def test_a_roll_needs_collateral_AND_gas_from_one_balance():
    """On Arc USDC is the gas token, so both come out of the same wallet.

    Checking them separately is how you open a series you cannot then
    collateralize — and an uncollateralized series is a desk that looks live and
    reverts on first contact, which is worse than not rolling at all.
    """
    need = keeper.GAS_FLOOR_USDC + keeper.ROLL_COLLATERAL
    assert keeper.roll_budget_ok(need)
    assert keeper.roll_budget_ok(need + 1)
    assert not keeper.roll_budget_ok(need - 0.01)
    # Enough for the collateral alone is NOT enough — that is the whole point.
    assert not keeper.roll_budget_ok(keeper.ROLL_COLLATERAL)


def test_a_missing_maker_inventory_is_never_read_as_flat():
    """The bug that stopped the venue trading for eleven hours.

    `descale_series` carries no `maker_inventory`, so sizing off
    `read_all_series()` believed the maker was FLAT. `feasible_qty` clamps
    against the auto-mirrored maker's margin as well as the taker's, so once the
    maker had gone short 2.31 contracts every trade exceeded the maker-side cap
    and reverted — while `.get("maker_inventory", 0.0)` made the wrong number
    look like a read one.
    """
    # What read_all_series actually returns: no such field.
    raw_series = {"series_id": 3, "multiplier": 10, "settled": False,
                  "maker": "0x" + "c" * 40}
    assert keeper.maker_inventory_or_none(raw_series) is None, (
        "a series read must not masquerade as a flat maker"
    )
    assert keeper.maker_inventory_or_none(None) is None
    # What read_desk returns, including a genuinely flat book — which must be
    # distinguishable from not having looked.
    assert keeper.maker_inventory_or_none({"maker_inventory": -2.31}) == -2.31
    assert keeper.maker_inventory_or_none({"maker_inventory": 0.0}) == 0.0


# --- the venue's shape is derived from the chain, never configured ------------


def _series(sid: int, index_id: str, expiry: int, settled: bool = False) -> dict:
    return {
        "series_id": sid,
        "index_id": index_id,
        "expiry_ts": expiry,
        "settled": settled,
        "multiplier": 10,
    }


NOW = 1_786_000_000


def test_an_index_with_no_series_is_simply_absent():
    """The failure that killed the old rotation, made unreachable.

    `futures-heartbeat.yml` records why a fixed list was removed: rotating over
    all three meant "two hours in three picked an index with no series at all
    and the workflow failed — noise that would have masked a real outage."
    Deriving the roster means an index without a book cannot be picked.
    """
    roster = keeper.live_indices([_series(3, "ACR-INF", NOW + 86_400)], NOW)
    assert roster == ["ACR-INF"]


def test_settled_and_expired_series_do_not_count_as_live():
    rows = [
        _series(0, "ACR-INF", NOW + 86_400, settled=True),  # settled
        _series(1, "ACR-GPU", NOW - 10),  # expired
        _series(2, "ACR-DATA", NOW + 86_400),  # the only live one
    ]
    assert keeper.live_indices(rows, NOW) == ["ACR-DATA"]


def test_the_roster_grows_as_books_are_opened():
    rows = [
        _series(3, "ACR-INF", NOW + 86_400),
        _series(4, "ACR-GPU", NOW + 86_400),
        _series(5, "ACR-DATA", NOW + 86_400),
    ]
    assert keeper.live_indices(rows, NOW) == ["ACR-DATA", "ACR-GPU", "ACR-INF"]


def test_the_allowlist_orders_and_pins_but_never_invents():
    """An operator can pin the rotation without a deploy — but an allowlisted
    index that has no series still cannot be traded."""
    rows = [_series(3, "ACR-INF", NOW + 86_400), _series(4, "ACR-GPU", NOW + 86_400)]
    assert keeper.live_indices(rows, NOW, ["ACR-GPU", "ACR-INF"]) == ["ACR-GPU", "ACR-INF"]
    assert keeper.live_indices(rows, NOW, ["ACR-DATA"]) == []
    assert keeper.live_indices([], NOW, ["ACR-INF"]) == []


def test_a_roll_is_budgeted_for_the_stake_it_will_actually_post():
    """A flat budget check would stand the keeper down on a book that costs
    pennies, or wave through one it cannot fund."""
    assert keeper.roll_budget_ok(1.10, 0.05) is True  # a cheap ACR-GPU book
    assert keeper.roll_budget_ok(1.10, 1.50) is False  # an ACR-INF-sized one
    assert keeper.roll_budget_ok(2.60, 1.50) is True


def test_collateral_is_sized_to_the_index_not_to_a_constant():
    """Margin scales with the mark, so one constant cannot fit three books."""
    from index_api.desk import MAX_QTY, collateral_for_full_book, feasible_qty

    inf = collateral_for_full_book(0.4924, 10, 2000)
    gpu = collateral_for_full_book(0.0111, 10, 2000)
    assert inf > 2.0, "ACR-INF genuinely needs a couple of USDC"
    assert gpu < 0.1, "ACR-GPU needs pennies — a flat 1.50 would be ~30x over"
    # The sizing must satisfy the very function the desk quotes readers with,
    # or the book would offer a size it cannot fill.
    buy, sell = feasible_qty(0.0111, 10, 2000, gpu, 0.0, gpu, 0.0)
    assert buy >= MAX_QTY and sell >= MAX_QTY
    assert collateral_for_full_book(0.0, 10, 2000) == 0.0


class TestStatus:
    """What the Terminal is allowed to say about the keeper.

    The verdicts used to go only to the server log, so a live venue could not
    answer "is anything still minding this book?" on any surface. These tests
    guard the two ways that answer could lie.
    """

    @pytest.fixture(autouse=True)
    def _clear(self, monkeypatch):
        monkeypatch.setattr(keeper, "_last_run", {})

    def test_a_disabled_keeper_says_off_not_zero(self, monkeypatch):
        # "off" and "stalled" are different facts. Reporting a disabled keeper
        # as a chore that has never run would make a deliberate configuration
        # look like an outage on every surface that renders it.
        monkeypatch.setenv("ACR_KEEPER", "0")
        st = keeper.status()
        assert st == {"enabled": False}

    def test_an_enabled_keeper_reports_every_chore(self):
        # The exact set, not a subset: a chore that stopped being reported would
        # vanish from /health silently, and "nobody is minding it" is precisely
        # what this surface exists to say out loud.
        st = keeper.status()
        assert st["enabled"] is True
        assert set(st) == {"enabled", "heartbeat", "roll", "mirror", "humans"}
        for chore in ("heartbeat", "roll", "mirror"):
            assert set(st[chore]) == {
                "checked_at",
                "checked_age_s",
                "verdict",
                "last_fire_at",
                "last_fire_age_s",
                "every_s",
                "next_due_s",
            }

    def test_a_cooldown_tick_still_counts_as_checked(self):
        # Both chores return None while on cooldown — the healthy majority of
        # ticks. If only verdicts were recorded, a keeper doing its job would
        # be indistinguishable from one that died an hour ago.
        keeper.record("heartbeat", None)
        hb = keeper.status()["heartbeat"]
        assert hb["checked_at"] is not None
        assert hb["checked_age_s"] < 5
        assert hb["verdict"] is None

    def test_a_chore_never_seen_reports_nothing_rather_than_now(self):
        # Unread must not render as fresh. An unrun chore reporting age 0 would
        # read on the strip as "checked a moment ago" — the exact inversion.
        hb = keeper.status()["heartbeat"]
        assert hb["checked_at"] is None
        assert hb["checked_age_s"] is None
        assert hb["last_fire_age_s"] is None

    def test_a_failure_is_recorded_as_a_verdict(self):
        keeper.record("roll", "failed: boom")
        assert keeper.status()["roll"]["verdict"] == "failed: boom"


def test_a_settlement_kicks_one_mirror_pass_that_skips_the_cooldown(monkeypatch):
    """Twelve real settlements vanished on 2026-09-13: they lived only in memory
    until the keeper's next 120 s tick, and a restart came first. A settlement
    now schedules a pass a few seconds later; a burst coalesces into one; the
    pass bypasses the tick's cooldown; and with no event loop (a script, a sync
    test) it says so instead of raising — the payment already succeeded."""
    import asyncio

    monkeypatch.setenv("ACR_KEEPER", "1")
    calls: list[bool] = []

    def fake_mirror_once(_futures=None, *, force=False):
        calls.append(force)
        return "mirrored 1"

    monkeypatch.setattr(keeper, "mirror_once", fake_mirror_once)
    monkeypatch.setattr(keeper, "_mirror_kick", None)

    assert keeper.kick_mirror() is False, "no running loop: nothing scheduled, nothing raised"

    async def burst():
        assert keeper.kick_mirror(delay_s=0.05) is True
        assert keeper.kick_mirror(delay_s=0.05) is True, "a second settlement rides the pending pass"
        assert keeper.kick_mirror(delay_s=0.05) is True
        await asyncio.sleep(0.3)

    asyncio.run(burst())
    assert calls == [True], "one pass for the burst, and it was forced past the cooldown"


def test_the_forced_pass_still_refuses_to_overlap_a_running_one(monkeypatch):
    monkeypatch.setenv("ACR_KEEPER", "1")
    assert keeper._mirror_lock.acquire(blocking=False)
    try:
        assert keeper.mirror_once(force=True) is None, "a pass in flight means this one stands down"
    finally:
        keeper._mirror_lock.release()


# ── humans: the press resolves each rotation window itself ───────────────────
# Until 2026-10-16 this was a laptop chore (`make resolve-humans ARGS=--commit`)
# and it had silently stopped: the last resolved window was 2958, five weeks
# back, so every surface read "0 humans". The press already holds the salt, the
# mirror address and the one authorized signer (Circle custody).

class TestHumans:
    W = 2963
    WALLETS = ["0x" + c * 40 for c in "1234"]

    class Book:
        source = "fixture"

        def roster(self):
            from types import SimpleNamespace

            w = TestHumans.WALLETS
            return [
                SimpleNamespace(nullifier="0x" + "aa" * 32, sandbox=True, wallets=w[:3]),
                SimpleNamespace(nullifier="0x" + "bb" * 32, sandbox=True, wallets=w[3:]),
            ]

    @pytest.fixture()
    def chain(self, monkeypatch):
        """A mirror that answers like the real one, and counts what it is asked."""
        from types import SimpleNamespace

        state = SimpleNamespace(window=self.W, host_window=self.W, clusters={}, reads=0, records=[],
                                authorized=True, salt_ok=True, built=0)

        class Client:
            def __init__(self, settings=None, signer=None, **_):
                state.built += 1
                self.signer = signer

            def chain_window(self):
                state.reads += 1
                return state.window

            def signer_authorized(self):
                state.reads += 1
                return state.authorized

            def salt_matches(self, salt):
                state.reads += 1
                return state.salt_ok

            def cluster_of(self, wallet, window):
                state.reads += 1
                return state.clusters.get((wallet, window))

            def record(self, wallet, cluster, window, sandbox, dry_run=False):
                state.records.append((wallet, window))
                state.clusters[(wallet, window)] = cluster
                return "0x" + "ee" * 32

        monkeypatch.setenv("ACR_KEEPER_HUMANS", "1")
        monkeypatch.setattr(keeper, "_last_humans", 0.0)
        monkeypatch.setattr(keeper, "_humans_done_window", None)
        monkeypatch.setattr(keeper, "get_settings", lambda: SimpleNamespace(
            humanid_mirror_address="0x7f41faA38F35F1FABfc76Df5B1618fC8d0c0d8e5", humanid_salt="0x" + "22" * 32))
        monkeypatch.setattr(keeper, "_custody_signer", lambda role: CircleWalletSigner())
        monkeypatch.setattr("acr_oracle_client.humanid.HumanIdMirrorClient", Client)
        monkeypatch.setattr("acr_oracle_client.humanid.current_window", lambda: state.host_window)
        monkeypatch.setattr("acr_oracle_client.agentbook.build_agentbook", lambda settings=None: self.Book())
        return state

    def test_off_unless_asked_for(self, chain, monkeypatch):
        monkeypatch.delenv("ACR_KEEPER_HUMANS")
        assert keeper.humans_once() is None
        assert chain.built == 0, "a deployment that did not opt in must not even build a client"

    def test_it_resolves_only_the_unresolved_then_idles_for_the_window(self, chain):
        from acr_oracle_client.humanid import cluster_id

        done = self.WALLETS[0]
        chain.clusters[(done, self.W)] = cluster_id("0x" + "aa" * 32, "0x" + "22" * 32, self.W)
        verdict = keeper.humans_once()
        assert verdict == f"resolved 3 wallet(s) for window {self.W} (1 already on chain)"
        assert [w for w, _ in chain.records] == self.WALLETS[1:]

        reads, built = chain.reads, chain.built
        assert keeper.humans_once(force=True) is None
        assert (chain.reads, chain.built) == (reads, built), "a resolved window must cost no reads at all"

    def test_an_already_resolved_window_says_so_once(self, chain):
        keeper.humans_once()
        chain.records.clear()
        keeper._humans_done_window = None  # a restart: the memo is gone, the chain is not
        assert keeper.humans_once(force=True) == f"window {self.W}: all 4 wallet(s) already resolved"
        assert chain.records == []

    def test_a_new_window_is_resolved_again(self, chain):
        keeper.humans_once()
        chain.records.clear()
        chain.window = chain.host_window = self.W + 1
        assert keeper.humans_once(force=True) == f"resolved 4 wallet(s) for window {self.W + 1}"
        assert {w for w, win in chain.records if win == self.W + 1} == set(self.WALLETS)

    def test_the_host_clock_running_ahead_does_not_mark_a_window_done(self, chain):
        keeper.humans_once()
        chain.host_window = self.W + 1  # the host thinks the window rolled; the chain has not yet
        assert keeper.humans_once(force=True) is None
        assert keeper._humans_done_window == self.W
        chain.window = self.W + 1  # the chain catches up: the next check resolves
        assert keeper.humans_once(force=True).startswith("resolved 4 wallet(s)")

    def test_a_signer_the_mirror_does_not_trust_is_a_verdict_not_a_write(self, chain):
        chain.authorized = False
        verdict = keeper.humans_once()
        assert "not in the mirror's signer set" in verdict and chain.records == []

    def test_a_salt_that_does_not_match_is_a_verdict_not_a_write(self, chain):
        chain.salt_ok = False
        assert "salt" in keeper.humans_once() and chain.records == []

    def test_it_refuses_a_raw_key(self, chain, monkeypatch):
        monkeypatch.setattr(keeper, "_custody_signer", lambda role: None)
        assert "Circle custody" in keeper.humans_once()
        assert chain.records == []

    def test_a_service_that_opted_in_but_lacks_the_salt_says_so(self, chain, monkeypatch):
        # Opting in is explicit, so silence would hide a misconfiguration for weeks.
        from types import SimpleNamespace

        monkeypatch.setattr(keeper, "get_settings", lambda: SimpleNamespace(humanid_mirror_address="0xabc", humanid_salt=""))
        verdict = keeper.humans_once()
        assert verdict.startswith("not resolving") and "ACR_HUMANID_SALT" in verdict
        assert chain.built == 0

    def test_a_refusal_is_not_overwritten_by_the_quiet_ticks_after_it(self, chain, monkeypatch):
        # The loop looks every few minutes and most looks return None (cooldown).
        # Recording that None would turn "not resolving: …" into what /ops renders
        # as "resolved, nothing to do": a confident false claim within five minutes.
        import asyncio

        from index_api import app as app_mod

        verdicts = iter(["not resolving: 0xabc is not in the mirror's signer set", None, None])
        monkeypatch.setattr(keeper, "humans_once", lambda *a, **k: next(verdicts))
        monkeypatch.setattr(keeper, "_last_run", {})
        for _ in range(3):
            asyncio.run(app_mod._humans_tick())
        assert keeper._last_run["humans"]["verdict"].startswith("not resolving")

    def test_the_cooldown_holds_between_checks(self, chain):
        keeper.humans_once()
        keeper._humans_done_window = None
        assert keeper.humans_once() is None, "a second check inside the cooldown must not run"

    def test_a_failing_tick_is_recorded_and_never_raised(self, chain, monkeypatch):
        import asyncio

        from index_api import app as app_mod

        def boom(*_a, **_k):
            raise RuntimeError("Circle refused the API key (HTTP 403)")

        monkeypatch.setattr(keeper, "humans_once", boom)
        monkeypatch.setattr(keeper, "_last_run", {})
        asyncio.run(app_mod._humans_tick())
        assert keeper._last_run["humans"]["verdict"].startswith("failed: Circle refused")
