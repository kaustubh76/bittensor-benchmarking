"""Floor-buyer demo tests — the real two-act x402 loop, in-process (hermetic)."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from index_api import buyer_demo
from index_api.x402 import (
    CircleFacilitator,
    DevFacilitator,
    reset_facilitator,
    set_facilitator,
)


def _dev_gate() -> DevFacilitator:
    fac = DevFacilitator()
    set_facilitator(fac)
    return fac


def test_two_act_loop_settles_real_receipts():
    """execute() drives bare-402 → priced retry → 200 through the app's own
    gate; every payment lands on the facilitator's public ledger."""
    from index_api.app import app

    fac = _dev_gate()
    buyer_demo.reset()
    try:
        assert buyer_demo.try_start(3, "0xfloor-test")
        asyncio.run(buyer_demo.execute(app, 3, 0.0, "0xfloor-test", paths=["/vol/ACR-INF"]))
        st = buyer_demo.status()
        assert st["state"] == "done"
        assert st["done"] == st["total"] == 3
        assert st["spent_usdc"] == pytest.approx(0.0003)
        assert st["recent"][0]["tx_ref"].startswith("dev-")
        # The receipts are REAL gate settlements, not demo bookkeeping.
        assert fac.paid_queries == 3
        assert all(r.payer == "0xfloor-test" for r in fac.recent)
        # And they surface on the public ledger endpoint.
        led = TestClient(app).get("/marketplace/receipts").json()
        assert led["paid_queries"] == 3
        assert led["receipts"][0]["payer"] == "0xfloor-test"
    finally:
        buyer_demo.reset()
        reset_facilitator()


def test_start_endpoint_refuses_circle_gate():
    """The mock header fails closed on the Circle gate — start must refuse
    rather than run a loop of guaranteed 402s."""
    from acr_core.config import ACRSettings
    from index_api.app import app

    set_facilitator(
        CircleFacilitator(
            settings=ACRSettings(
                x402_facilitator_url="https://fac.example", x402_pay_to="0xSeller", _env_file=None
            )
        )
    )
    buyer_demo.reset()
    try:
        r = TestClient(app).post("/demo/buyer/start", json={"count": 2})
        assert r.status_code == 409
        assert "apps/agent" in r.json()["detail"]
        assert buyer_demo.status()["state"] == "idle"  # slot never claimed
    finally:
        buyer_demo.reset()
        reset_facilitator()


def test_single_flight():
    from index_api.app import app

    _dev_gate()
    buyer_demo.reset()
    try:
        assert buyer_demo.try_start(5, "0xfloor-a")
        assert not buyer_demo.try_start(5, "0xfloor-b")  # slot held
        r = TestClient(app).post("/demo/buyer/start", json={"count": 2})
        assert r.status_code == 409
        assert "floor" in r.json()["detail"]
    finally:
        buyer_demo.reset()
        reset_facilitator()


def test_price_from_challenge_paths():
    headers_empty: dict = {}
    body = {"accepts": [{"amount": "250", "maxAmountRequired": "250"}]}
    assert buyer_demo._price_from_challenge(body, headers_empty) == pytest.approx(0.00025)
    assert buyer_demo._price_from_challenge({}, {"X-402-Price": "0.0005"}) == pytest.approx(0.0005)
    with pytest.raises(RuntimeError):
        buyer_demo._price_from_challenge({}, {})


def test_status_endpoint_shape():
    from index_api.app import app

    buyer_demo.reset()
    st = TestClient(app).get("/demo/buyer/status").json()
    assert st == {
        "state": "idle", "payer": "", "total": 0, "done": 0,
        "spent_usdc": 0.0, "recent": [], "error": None,
    }
