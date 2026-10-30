"""The futures desk the terminal polls: a failed read never replaces a good one,
and a stale cache is served while ONE refresh runs behind it.

Measured 2026-10-14 on the testnet press: `/futures` took 2-26 s while `/health`
answered in 0.4 s (each visitor past the TTL paid the chain scan, and each
concurrent one paid it again), and the client's swallowed RPC errors cached an
EMPTY desk, so the terminal showed an empty venue between two full ones
(desks per poll `[3, 0]`). These run against a fake client, because the point is
the reader's cache policy, not the chain; tests/test_desk_onchain.py covers the
real reads against anvil.
"""

from __future__ import annotations

import threading
import time

from acr_core import ALL_INDEX_IDS
from acr_oracle_client.futures import select_series_for_index
from index_api.onchain import FUTURES_TTL_S, FuturesReader

IID = ALL_INDEX_IDS[0]
SERIES = [{"series_id": 0, "index_id": IID, "exists": True, "settled": False, "maker": "0xm", "multiplier": 10}]
FILL = {"series_id": 0, "taker": "0xt", "qty": 1.0, "side": "buy", "mark": 1.0, "block": 7, "tx": "0xaa"}


class FakeClient:
    """Answers like `FuturesClient`: a failed read raises under `strict`, else
    comes back empty, exactly as the real client swallows RPC errors."""

    def __init__(self) -> None:
        self.fail = False
        self.trades = [dict(FILL)]
        self.calls = {"series": 0, "desk": 0, "trades": 0}
        self.gate: threading.Event | None = None

    def _wait(self) -> None:
        if self.gate is not None:
            assert self.gate.wait(5), "the test never released the read"

    def read_all_series(self, *, strict: bool = False) -> list[dict]:
        self.calls["series"] += 1
        self._wait()
        if self.fail:
            if strict:
                raise ConnectionError("429 Too Many Requests")
            return []
        return [dict(s) for s in SERIES]

    def read_desk(self, index_id, *, all_series=None, strict: bool = False):
        self.calls["desk"] += 1
        if self.fail:
            if strict:
                raise ConnectionError("429 Too Many Requests")
            return None
        s = select_series_for_index(all_series or [], index_id)
        return None if s is None else {**s, "maker_inventory": -3.0}

    def recent_trades(self, *, strict: bool = False) -> list[dict]:
        self.calls["trades"] += 1
        self._wait()
        if self.fail:
            if strict:
                raise ConnectionError("429 Too Many Requests")
            return []
        return [dict(t) for t in self.trades]


def _reader() -> tuple[FuturesReader, FakeClient]:
    reader = FuturesReader(rpc_url="http://unused.invalid", futures_address="0x" + "11" * 20)
    client = FakeClient()
    reader._client = client  # type: ignore[assignment]
    return reader, client


def _age(reader: FuturesReader, by: float) -> None:
    """Make every read, and every attempt at one, `by` seconds older."""
    reader._cache_at -= by
    reader._series_at -= by
    reader._trades_at -= by
    reader._tried = {k: v - by for k, v in reader._tried.items()}


def _settle(reader: FuturesReader) -> None:
    deadline = time.monotonic() + 5
    while reader._inflight and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not reader._inflight


def test_a_throttled_refresh_keeps_the_desk_and_the_tape():
    reader, client = _reader()
    assert list(reader.read_all(use_cache=False)) == [IID]
    assert len(reader.recent_trades(use_cache=False)) == 1

    client.fail = True  # the warm loop's next tick hits a 429
    assert list(reader.read_all(use_cache=False)) == [IID]
    assert len(reader.recent_trades(use_cache=False)) == 1
    assert reader.roster()["desks"][IID]["maker_inventory"] == -3.0


def test_a_kept_desk_is_not_called_fresh():
    reader, client = _reader()
    reader.read_all(use_cache=False)
    fresh_at = reader._cache_at
    client.fail = True
    reader.read_all(use_cache=False)
    assert reader._cache_at == fresh_at


def test_a_stale_desk_is_served_at_once_and_refreshed_behind_once():
    reader, client = _reader()
    reader.read_all(use_cache=False)
    reader.recent_trades(use_cache=False)
    _age(reader, FUTURES_TTL_S + 1)
    before = dict(client.calls)

    client.gate = threading.Event()  # every chain read now hangs until released
    t0 = time.monotonic()
    rosters = [reader.roster() for _ in range(5)]
    assert time.monotonic() - t0 < 1.0, "a stale cache made the request wait on the chain"
    assert all(list(r["desks"]) == [IID] and len(r["trades"]) == 1 for r in rosters)

    time.sleep(0.2)
    assert client.calls["series"] - before["series"] == 1, "five requests started more than one desk refresh"
    assert client.calls["trades"] - before["trades"] == 1, "five requests started more than one tape refresh"

    client.gate.set()
    deadline = time.monotonic() + 5
    while reader._inflight and time.monotonic() < deadline:
        time.sleep(0.02)
    assert not reader._inflight
    assert time.monotonic() - reader._cache_at < FUTURES_TTL_S


def test_a_quiet_tape_is_read_once_not_on_every_request():
    reader, client = _reader()
    client.trades = []
    assert reader.recent_trades() == []
    assert reader.recent_trades() == []
    assert client.calls["trades"] == 1


def test_desks_never_read_are_read_again_rather_than_shown_empty():
    # A failed first desk read caches nothing, so the next request reads again:
    # an empty desk is what the terminal shows as "no venue", and it is not true.
    reader, client = _reader()
    client.fail = True
    assert reader.read_all() == {}
    client.fail = False
    assert list(reader.read_all()) == [IID]


def test_an_unread_tape_never_makes_a_request_wait():
    # Measured on the press 2026-10-14: the tape's first page throttled on every
    # attempt, and every /futures request walked it again (2-42 s).
    reader, client = _reader()
    client.fail = True
    assert reader.recent_trades() == []
    assert client.calls["trades"] == 1
    client.gate = threading.Event()  # a chain read would now hang
    t0 = time.monotonic()
    for _ in range(5):
        assert reader.recent_trades() == []
    assert time.monotonic() - t0 < 0.5, "a request waited on the chain for an unread tape"
    assert client.calls["trades"] == 1, "the tape was re-walked inside the retry floor"

    client.fail = False
    client.gate.set()
    _age(reader, FuturesReader._RETRY_S + 1)
    assert reader.recent_trades() == []  # still served at once
    _settle(reader)
    assert client.calls["trades"] == 2
    assert len(reader.recent_trades()) == 1, "the retry behind did not fill the tape"


def test_concurrent_cold_readers_share_one_scan():
    reader, client = _reader()
    client.gate = threading.Event()
    out: list[dict] = []
    threads = [threading.Thread(target=lambda: out.append(reader.read_all())) for _ in range(5)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    client.gate.set()
    for t in threads:
        t.join(5)
    assert [list(o) for o in out] == [[IID]] * 5
    assert client.calls["series"] == 1, "five cold readers paid for five scans"


def test_the_roster_says_how_old_each_part_is():
    reader, client = _reader()
    client.fail = True
    assert reader.roster()["freshness"]["tape_age_s"] is None  # unread, not empty
    reader2, _ = _reader()
    f = reader2.roster()["freshness"]
    assert set(f) == {"desks_age_s", "tape_age_s", "series_age_s"}
    assert all(v is not None and v < 5 for v in f.values())
