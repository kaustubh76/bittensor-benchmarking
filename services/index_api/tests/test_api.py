"""index_api tests — x402 gating, store views, poster (offline)."""

from __future__ import annotations

import pytest
from acr_sim import SimConfig
from acr_tape import SimSource
from fastapi.testclient import TestClient
from index_api.app import app, store
from index_api.poster import OraclePoster
from index_api.store import PrintStore


@pytest.fixture(scope="module", autouse=True)
def _seed_store():
    # Use a small, fast source for the app's module-level store.
    store.source = SimSource(config=SimConfig(seed=4, horizon=3600.0, events_per_service=800))
    store.refresh(ts=3600.0)
    yield


client = TestClient(app)


def test_root_is_public():
    r = client.get("/")
    assert r.status_code == 200
    assert "ACR-INF" in r.json()["indices"]


def test_prints_require_payment():
    r = client.get("/prints")
    assert r.status_code == 402
    assert r.headers.get("WWW-Authenticate") == "x402"


def test_prints_served_with_valid_payment():
    r = client.get("/prints", headers={"X-Payment": "x402 0xagent:0.0001"})
    assert r.status_code == 200
    body = r.json()
    assert "ACR-INF" in body["prints"]
    p = body["prints"]["ACR-INF"]
    assert p["ci_lo"] <= p["value"] <= p["ci_hi"]
    assert p["attack_cost_per_bp"] > 0


def test_insufficient_payment_rejected():
    r = client.get("/prints", headers={"X-Payment": "x402 0xagent:0.00000001"})
    assert r.status_code == 402


def test_curve_and_vol_and_scores():
    h = {"X-Payment": "x402 0xagent:0.001"}
    assert len(client.get("/curve/ACR-INF", headers=h).json()["curve"]) == 4
    assert client.get("/vol/ACR-INF", headers=h).json()["annualized_vol"] >= 0
    sellers = client.get("/seller-scores/ACR-INF", headers=h).json()["sellers"]
    assert sellers and 0.0 <= sellers[0]["score"] <= 1.0


def test_revenue_tracks_paid_queries():
    r = client.get("/revenue")
    assert r.json()["paid_queries"] >= 1


def test_onchain_endpoint_503_without_oracle():
    # No ACR_ORACLE_ADDRESS configured -> the on-chain reader is unconfigured.
    r = client.get("/onchain/ACR-INF")
    assert r.status_code == 503


def test_terminal_data_has_null_onchain_without_oracle():
    r = client.get("/terminal/data")
    assert r.status_code == 200
    body = r.json()
    assert body["oracle"] is None
    assert body["prints"]["ACR-INF"]["onchain"] is None


def test_poster_offline_returns_markers():
    st = PrintStore(source=SimSource(config=SimConfig(seed=5, horizon=3600.0, events_per_service=600)))
    poster = OraclePoster(st)
    refs = poster.post_once(ts=3600.0)
    assert len(refs) >= 1
    assert all(r.startswith("offline:") for r in refs)


def _rolling_store(seed: int = 4, n_windows: int = 4) -> PrintStore:
    src = SimSource(config=SimConfig(seed=seed, horizon=n_windows * 3600.0, events_per_service=n_windows * 1000))
    return PrintStore(source=src, window_s=3600.0, step_s=3600.0)


def test_prints_evolve_and_vol_becomes_real():
    st = _rolling_store()
    for _ in range(3):
        st.refresh()
    assert len(st.history["ACR-INF"]) == 3
    # Distinct windows -> the print actually moved -> realized vol is nonzero.
    assert st.vol("ACR-INF") > 0.0


def test_ts_strictly_increasing_across_window_wrap():
    st = _rolling_store(n_windows=4)
    prev = -1.0
    for _ in range(9):  # > n_windows, so the window cursor wraps
        st.refresh()
        ts = st.latest["ACR-INF"].ts
        assert ts > prev, f"print ts regressed at wrap: {ts} <= {prev}"
        prev = ts


def test_concurrent_reads_during_refresh_are_safe():
    # Copy-on-write means readers never see a partially-updated dict. A couple of
    # yielding readers overlapping a handful of refreshes exercises the swap
    # without spin-starving the refresh thread under the GIL.
    import threading
    import time

    st = _rolling_store(n_windows=2)  # small tape -> fast refresh
    st.refresh()
    errors: list[Exception] = []
    stop = threading.Event()

    def hammer():
        try:
            while not stop.is_set():
                snap = st.snapshot()
                assert "ACR-INF" in snap["prints"]
                time.sleep(0.001)  # yield so the refresh thread makes progress
        except Exception as exc:  # pragma: no cover - only on a real race bug
            errors.append(exc)

    readers = [threading.Thread(target=hammer) for _ in range(2)]
    for t in readers:
        t.start()
    for _ in range(8):
        st.refresh()
    stop.set()
    for t in readers:
        t.join()
    assert not errors, f"reads raced with refresh: {errors[:1]}"


def test_poster_isolates_single_index_failure():
    class _FlakyClient:
        def post(self, p):
            if p.index_id == "ACR-GPU":
                raise RuntimeError("boom")
            return None  # offline marker

        def can_post(self):
            return False

    st = _rolling_store(seed=6)
    st.refresh()
    poster = OraclePoster(st, client=_FlakyClient())
    refs = poster.post_latest()
    assert "error:ACR-GPU" in refs
    # The other indices still posted despite GPU failing.
    assert any(r.startswith("offline:") for r in refs)


def test_lifespan_starts_and_stops_cleanly(monkeypatch):
    # A large refresh interval so the timer never fires during the test.
    from index_api import app as app_mod

    monkeypatch.setattr(app_mod.get_settings(), "refresh_seconds", 3600.0)
    with TestClient(app) as c:
        assert c.get("/health").json()["status"] == "ok"


def test_terminal_data_includes_history_and_sellers():
    body = client.get("/terminal/data").json()
    # History rides the existing store deque — sparklines + the detail chart.
    pts = body["history"]["ACR-INF"]
    assert pts, "terminal payload should carry recent print history"
    assert pts[-1]["ci_lo"] <= pts[-1]["value"] <= pts[-1]["ci_hi"]
    # Sellers ride along for the human Registry view (machine endpoint stays gated).
    sellers = body["sellers"]["ACR-INF"]
    assert sellers
    assert {"seller", "score", "clean_share", "attested", "volume_usdc"} <= set(sellers[0])


def test_revenue_exposes_price_and_recent_receipts():
    client.get("/prints", headers={"X-Payment": "x402 0xreceipt-agent:0.0001"})
    body = client.get("/revenue").json()
    assert body["price_usdc"] == 0.0001
    assert any(r["payer"] == "0xreceipt-agent" for r in body["recent"])


def test_demo_attack_run_single_flight_and_verdict(monkeypatch):
    """The Attack Lab loop: start → 409 while running → series grows → verdict.

    Paced at 0s and shrunk to 6 sim-hours (attack in h2–4) so the whole run is
    a few seconds of estimator work; the shape is identical to the live demo.
    """
    import time

    import index_api.demo as demo
    from index_api import app as app_mod

    monkeypatch.setattr(demo, "PACE_S", 0.0)
    monkeypatch.setattr(demo, "HOURS_TOTAL", 6)
    monkeypatch.setattr(demo, "ATK_FROM", 2)
    monkeypatch.setattr(demo, "ATK_TO", 4)
    monkeypatch.setattr(app_mod.get_settings(), "refresh_seconds", 3600.0)

    # The run is an asyncio background task — it needs the app's loop alive
    # across requests, so drive everything through a lifespan-scoped client.
    with TestClient(app) as c:
        r = c.post("/demo/attack/start", json={"budget_usdc": 2000, "seed": 7})
        assert r.status_code == 200
        assert r.json()["state"] == "running"
        assert c.post("/demo/attack/start", json={}).status_code == 409

        deadline = time.time() + 90
        st = c.get("/demo/attack/status").json()
        while st["state"] == "running" and time.time() < deadline:
            time.sleep(0.2)
            st = c.get("/demo/attack/status").json()

    assert st["state"] == "done", st.get("error")
    assert st["hour"] == 6
    assert len(st["series"]) >= 4
    assert st["usdc_burned"] > 0
    assert st["n_adversarial"] > 0
    v = st["verdict"]
    # The product claim, verified on the same series the viewer watches:
    # naive VWAP is dragged far more than ACR under an identical attack.
    assert v["peak_vwap_err_bp"] > v["peak_acr_err_bp"]
    assert v["resistance"] > 1.0

    # --- the telemetry the page shows its work with -----------------------
    # These used to be computed 12 times a run and thrown away at the
    # `p, _ = estimate_index(...)` line. Without them /attack can only assert
    # that the index resists manipulation; with them it can show the cleaner
    # doing the resisting, hour by hour.
    assert st["phase"] == "done"
    assert st["elapsed_s"] is not None and st["elapsed_s"] > 0
    assert st["started_at"] is not None
    # Totals are known before the first hour is estimated, so every counter
    # can be a fraction of a stated whole rather than a number climbing to
    # nowhere.
    assert st["n_adversarial_total"] >= st["n_adversarial"] > 0
    assert st["usdc_total"] >= st["usdc_burned"] > 0
    # Why the budget knob does not move the outcome: the trade cap binds.
    assert st["budget_affords"] > st["trade_cap"] > 0

    for pt in st["series"]:
        assert pt["n_raw"] >= pt["n_obs"] > 0
        assert 0.0 <= pt["cleaned_pct"] <= 100.0
        assert pt["acr_ci_lo"] <= pt["acr"] <= pt["acr_ci_hi"]
        assert pt["attack_cost_per_bp"] > 0
        assert pt["step_ms"] is not None

    # The claim this whole page exists to make, as a property of the data:
    # a poisoned hour floods the tape and the cleaner removes far more of it.
    atk = [p for p in st["series"] if p["attack"]]
    quiet = [p for p in st["series"] if not p["attack"]]
    assert atk and quiet
    assert max(p["cleaned_pct"] for p in atk) > max(p["cleaned_pct"] for p in quiet)
    assert max(p["n_raw"] for p in atk) > max(p["n_raw"] for p in quiet)


def test_attack_status_is_honest_before_any_run_has_happened():
    """Idle must not look like a finished run of zeroes — the page rests on
    the archived exercise instead, and needs to be able to tell the two
    apart."""
    import index_api.demo as demo

    demo._run = demo.AttackRun()
    st = demo.status()
    assert st["state"] == "idle"
    assert st["phase"] == "idle"
    # Absent, not zero: a 0.0 elapsed or a 0 total would render as a real
    # measurement of a run that never happened.
    assert st["elapsed_s"] is None
    assert st["started_at"] is None
    assert st["n_adversarial_total"] is None
    assert st["usdc_total"] is None


def test_demo_attack_start_clamps_params(monkeypatch):
    from index_api import demo

    # No real simulation — this test only checks the endpoint's clamping.
    monkeypatch.setattr(demo, "execute", lambda *a: None)
    demo._run = demo.AttackRun()  # claim a fresh slot
    r = client.post(
        "/demo/attack/start",
        json={"budget_usdc": 1e9, "target_multiplier": 100.0, "seed": 1},
    )
    assert r.status_code == 200
    params = r.json()["params"]
    assert params["budget_usdc"] == 50_000.0
    assert params["target_multiplier"] == 5.0
    demo._run = demo.AttackRun()  # leave the slot clean for other tests


def test_x402_info_reports_dev_gate():
    """Ungated gate descriptor for the Terminal's API console. Runs LAST in this
    module: resetting the facilitator zeroes revenue counters that earlier
    tests assert on."""
    from index_api.x402 import reset_facilitator

    reset_facilitator()  # ordering-proof: force a fresh selection under scrubbed env
    try:
        body = client.get("/x402/info").json()
        assert body["facilitator"] == "dev"
        assert body["price_usdc"] == 0.0001
        assert body["payment_header"] == "PAYMENT-SIGNATURE"
        assert "/prints/{index_id}" in body["gated_endpoints"]
        # Six: the five flat-priced index routes and the fleet's per-seller
        # /compute/{label}, which charged real money for a day while this list
        # said five. test_marketplace names the difference exactly.
        assert len(body["gated_endpoints"]) == 6
        # The canonical header passes the dev gate end-to-end.
        r = client.get("/prints", headers={"PAYMENT-SIGNATURE": "x402 0xagent-sig:0.0001"})
        assert r.status_code == 200
    finally:
        reset_facilitator()
