"""A throttled oracle read is not "no print".

`/onchain/{index_id}` is what the MCP plugin's get_rate/check_spend and the Aomi
App ask for every agent question. It read the chain uncached, and the client
mapped a 429 to None, so agents were told `404 no on-chain print` while the print
sat on chain (verify-live-testnet, 2026-10-14). Now the route serves the warm
cache, keeps the last good print on a failed read, and answers 503 ("ask again")
only when the chain is down and nothing was ever read.
"""

from __future__ import annotations

from acr_core import ALL_INDEX_IDS
from fastapi.testclient import TestClient
from index_api import app as app_mod
from index_api.onchain import READ_ALL_TTL_S, OracleReader

IID = ALL_INDEX_IDS[0]
PRINT = {"index_id": IID, "value": 0.4977, "ci_lo": 0.49, "ci_hi": 0.5, "attack_cost_per_bp": 0.01,
         "timestamp": 1, "posted_at": 1791981428}


class FakeOracle:
    def __init__(self) -> None:
        self.mode = "ok"  # ok | throttled | none
        self.calls = 0

    def read_latest(self, index_id, *, strict: bool = False):
        self.calls += 1
        if self.mode == "throttled":
            if strict:
                raise RuntimeError("429 Too Many Requests")
            return None
        if self.mode == "none":
            return None
        return {**PRINT, "index_id": index_id}


def _reader() -> tuple[OracleReader, FakeOracle]:
    r = OracleReader(rpc_url="http://unused.invalid", oracle_address="0x" + "33" * 20)
    fake = FakeOracle()
    r._client = fake  # type: ignore[assignment]
    return r, fake


def test_a_throttled_refresh_keeps_every_print():
    reader, fake = _reader()
    assert set(reader.read_all(use_cache=False)) == set(ALL_INDEX_IDS)
    fake.mode = "throttled"
    assert set(reader.read_all(use_cache=False)) == set(ALL_INDEX_IDS)
    assert reader.read(IID)["value"] == PRINT["value"]


def test_latest_serves_the_warm_cache_without_asking_the_chain():
    reader, fake = _reader()
    reader.read_all(use_cache=False)
    before = fake.calls
    assert reader.latest(IID)["value"] == PRINT["value"]
    assert fake.calls == before


def test_a_stale_cache_is_still_better_than_a_throttle():
    reader, fake = _reader()
    reader.read_all(use_cache=False)
    reader._cache_at -= READ_ALL_TTL_S + 1
    fake.mode = "throttled"
    assert reader.latest(IID)["value"] == PRINT["value"]


def _route(monkeypatch, reader):
    monkeypatch.setattr(app_mod, "get_reader", lambda: reader)
    return TestClient(app_mod.app).get(f"/onchain/{IID}")


def test_the_route_says_ask_again_not_no_print_when_the_chain_is_down(monkeypatch):
    reader, fake = _reader()
    fake.mode = "throttled"
    r = _route(monkeypatch, reader)
    assert r.status_code == 503, r.text
    assert "ask again" in r.json()["detail"]


def test_the_route_still_404s_a_real_absence(monkeypatch):
    reader, fake = _reader()
    fake.mode = "none"
    assert _route(monkeypatch, reader).status_code == 404


def test_the_route_answers_from_the_cache_through_a_throttle(monkeypatch):
    reader, fake = _reader()
    reader.read_all(use_cache=False)
    fake.mode = "throttled"
    r = _route(monkeypatch, reader)
    assert r.status_code == 200 and r.json()["value"] == PRINT["value"]
