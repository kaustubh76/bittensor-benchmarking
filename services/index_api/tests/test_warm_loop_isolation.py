"""One failing read must not silence every chore behind it.

`_warm_chain` is the press's sixty-second loop: it keeps the on-chain read
caches warm, pings itself awake, mirrors settlements on chain, runs the venue
keeper's chores and hands heap back before the tier's limit kills the process.
Those all used to live inside ONE try/except, in order — so a single throttled
Arc read raised, the outer handler caught it, and everything after it was
skipped for that tick. Every tick. The only trace was a line in the server log.

Arc answers 429. `_run_keeper` already carried the reasoning for its own
handler — "the caller's handler would also catch this, but then a keeper failure
would skip the rest of that tick" — and the two reads ahead of it never got it.

What made this worth a test rather than a note: the deployed press reports
`checked_at: null` for all three keeper chores after ten minutes of uptime on a
sixty-second loop, with `keeper.enabled: true`. The recording code dates from
2026-09-05, so the running image is not too old to have it.
"""

from __future__ import annotations

import asyncio

from index_api import app as app_mod


class _Reader:
    configured = True

    def __init__(self, boom: bool) -> None:
        self.boom = boom
        self.reads = 0

    def read_all(self, use_cache: bool = True):  # noqa: ARG002
        self.reads += 1
        if self.boom:
            raise RuntimeError("429 Too Many Requests")
        return {}


class _Futures:
    configured = True

    def __init__(self, boom: bool) -> None:
        self.boom = boom

    def read_all(self, use_cache: bool = True):  # noqa: ARG002
        if self.boom:
            raise RuntimeError("429 Too Many Requests")
        return {}

    def recent_trades(self, use_cache: bool = True):  # noqa: ARG002
        return []


def _run_one_tick(monkeypatch, *, reader_boom=False, futures_boom=False) -> dict:
    """Drive exactly one pass of the warm loop and report what ran."""
    ran: dict[str, int] = {"mirror": 0, "keeper": 0, "memory": 0, "self": 0}

    reader, futures = _Reader(reader_boom), _Futures(futures_boom)
    monkeypatch.setattr(app_mod, "get_reader", lambda: reader)
    monkeypatch.setattr(app_mod, "get_futures", lambda: futures)
    monkeypatch.setattr(app_mod, "CHAIN_WARM_SECONDS", 0.01)
    monkeypatch.setattr(app_mod, "_poster", None)

    async def _touch():
        ran["self"] += 1

    async def _mirror():
        ran["mirror"] += 1

    async def _keeper(_f):
        ran["keeper"] += 1

    monkeypatch.setattr(app_mod, "_touch_self", _touch)
    monkeypatch.setattr(app_mod, "_run_mirror", _mirror)
    monkeypatch.setattr(app_mod, "_run_keeper", _keeper)
    monkeypatch.setattr(app_mod.memory, "guard", lambda: ran.__setitem__("memory", ran["memory"] + 1))

    async def drive():
        stop = asyncio.Event()
        task = asyncio.create_task(app_mod._warm_chain(stop))
        # Long enough for one tick at the 10 ms interval set above.
        await asyncio.sleep(0.15)
        stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())
    ran["reads"] = reader.reads
    return ran


def test_every_chore_runs_on_a_healthy_tick(monkeypatch):
    """The control. Without it, a test asserting "the mirror still ran" could
    pass against a loop that never ran anything at all."""
    ran = _run_one_tick(monkeypatch)
    assert ran["self"] >= 1
    assert ran["mirror"] >= 1
    assert ran["keeper"] >= 1
    assert ran["memory"] >= 1


def test_a_throttled_oracle_read_does_not_skip_the_mirror(monkeypatch):
    """THE BUG. The oracle read is the FIRST thing after the self-ping, so an
    unisolated failure there skipped the mirror, the keeper and the memory guard
    — the chore that feeds the tape, the chore that settles the venue, and the
    one that stops a 512 MiB tier from OOM-killing the process."""
    ran = _run_one_tick(monkeypatch, reader_boom=True)
    assert ran["reads"] >= 1, "the read was attempted"
    assert ran["mirror"] >= 1, "and the mirror still ran"
    assert ran["keeper"] >= 1
    assert ran["memory"] >= 1


def test_a_throttled_venue_read_does_not_skip_the_keeper(monkeypatch):
    """The venue's chores are cooldown-driven and do not need the reads to have
    landed. A throttled tape must not stop a settlement."""
    ran = _run_one_tick(monkeypatch, futures_boom=True)
    assert ran["keeper"] >= 1
    assert ran["mirror"] >= 1
    assert ran["memory"] >= 1


def test_the_loop_survives_both_reads_failing(monkeypatch):
    """Arc throttles everything at once, not one read at a time."""
    ran = _run_one_tick(monkeypatch, reader_boom=True, futures_boom=True)
    assert ran["mirror"] >= 1 and ran["keeper"] >= 1 and ran["memory"] >= 1


def test_the_loop_survives_its_own_setup(monkeypatch):
    """THE REGRESSION, pinned directly.

    `_warm_chain` computed its outer guard as
    `bool(testnet_surfaces_enabled().receipt_mirror_address)` — a function that
    takes a required argument and returns a bool. The call raised TypeError
    before `while not stop.is_set()`, and because the loop is an
    `asyncio.create_task` whose exception nobody awaits until shutdown, the task
    died at every boot in total silence for four weeks.

    Driving it with an already-set stop event exercises exactly the setup path
    and nothing else: it must return, not raise.
    """
    monkeypatch.setattr(app_mod, "get_reader", lambda: _Reader(False))
    monkeypatch.setattr(app_mod, "get_futures", lambda: _Futures(False))

    async def drive():
        stop = asyncio.Event()
        stop.set()
        await app_mod._warm_chain(stop)

    asyncio.run(drive())  # a raise here is the bug


def test_a_mirror_only_deployment_still_runs_the_loop(monkeypatch):
    """What the broken line was *for*. The mirror depends on neither the oracle
    reader nor the venue, so a deployment with only a mirror address configured
    must not be turned away by the outer guard — and the guard could not read
    that address at all."""
    from acr_core import get_settings

    class _Unconfigured:
        configured = False

        def read_all(self, use_cache: bool = True):  # noqa: ARG002
            raise AssertionError("nothing to read")

    monkeypatch.setattr(app_mod, "get_reader", lambda: _Unconfigured())
    monkeypatch.setattr(app_mod, "get_futures", lambda: _Unconfigured())
    monkeypatch.setattr(app_mod, "SELF_URL", "")
    monkeypatch.setattr(
        app_mod, "get_settings",
        lambda: type("_S", (), {"receipt_mirror_address": "0x" + "ab" * 20})(),
    )
    assert get_settings is not None  # the real one is untouched

    ran = {"mirror": 0}

    async def _mirror():
        ran["mirror"] += 1

    async def _touch():
        pass

    monkeypatch.setattr(app_mod, "_run_mirror", _mirror)
    monkeypatch.setattr(app_mod, "_touch_self", _touch)
    monkeypatch.setattr(app_mod, "CHAIN_WARM_SECONDS", 0.01)
    monkeypatch.setattr(app_mod.memory, "guard", lambda: None)

    async def drive():
        stop = asyncio.Event()
        task = asyncio.create_task(app_mod._warm_chain(stop))
        await asyncio.sleep(0.12)
        stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(drive())
    assert ran["mirror"] >= 1, "a mirror-only deployment must still feed the tape"
