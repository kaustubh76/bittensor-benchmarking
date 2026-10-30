"""The mainnet guard — the service refuses to serve on Arc mainnet misconfigured.

Both gates fail OPEN in `auto` mode. On testnet that is a convenience; on mainnet a
missing variable gives prints away and dissolves the human tier, with a log warning
as the only signal. These tests hold the property that a misconfigured mainnet
deploy is a stack trace at startup, never a quiet week of free prints.
"""

from __future__ import annotations

import pytest
from acr_core import (
    MAINNET_CHAIN_ID,
    ACRSettings,
    MainnetGuardError,
    assert_mainnet_ready,
    reset_settings,
)
from acr_core import (
    testnet_surfaces_enabled as surfaces_enabled,
)
from acr_core.mainnet_guard import violations

MAINNET_OK = dict(
    arc_chain_id=MAINNET_CHAIN_ID,
    x402_mode="circle",
    x402_facilitator_url="https://gateway-api.circle.com",
    x402_pay_to="0x" + "ab" * 20,
    humanid_mode="agentkit",
    humanid_app_id="app_1234",
    cors_origins="https://arc-compute-rate.vercel.app",
    arc_rpc_url="https://rpc.mainnet.arc.io",
)


def _s(**over) -> ACRSettings:
    return ACRSettings(_env_file=None, **over)


# --- the guard is a no-op everywhere but mainnet -----------------------------

def test_testnet_defaults_are_not_a_violation():
    """Local development must not have to configure Circle or World."""
    assert violations(_s()) == []
    assert_mainnet_ready(_s())  # does not raise


def test_mainnet_defaults_are_refused_with_every_reason_listed():
    """One pass, every violation — a deploy should not fix them one boot at a time."""
    bad = violations(_s(arc_chain_id=MAINNET_CHAIN_ID))
    # x402 mode, facilitator not a URL, facilitator not mainnet's, pay-to, humanid
    # mode, app id, CORS, RPC (defaults to localhost)
    assert len(bad) == 8
    with pytest.raises(MainnetGuardError) as e:
        assert_mainnet_ready(_s(arc_chain_id=MAINNET_CHAIN_ID))
    for needle in ("ACR_X402_MODE", "ACR_X402_FACILITATOR_URL", "ACR_X402_PAY_TO",
                   "ACR_HUMANID_MODE", "ACR_HUMANID_APP_ID", "ACR_CORS_ORIGINS"):
        assert needle in str(e.value)


def test_a_fully_configured_mainnet_passes():
    assert violations(_s(**MAINNET_OK)) == []
    assert_mainnet_ready(_s(**MAINNET_OK))


# --- each gate must be EXPLICIT: auto is the fail-open mode ------------------

@pytest.mark.parametrize("mode", ["auto", "dev", "", "AUTO"])
def test_x402_auto_or_dev_is_refused_on_mainnet(mode):
    """`auto` falls back to the FREE dev gate when Circle looks unconfigured.
    On mainnet that fallback must be impossible, so the mode must be `circle`."""
    bad = violations(_s(**{**MAINNET_OK, "x402_mode": mode}))
    assert len(bad) == 1 and "ACR_X402_MODE" in bad[0] and "FREE dev gate" in bad[0]


@pytest.mark.parametrize("mode", ["auto", "dev", ""])
def test_humanid_auto_or_dev_is_refused_on_mainnet(mode):
    """The dev verifier grants the human tier to anyone who asks."""
    bad = violations(_s(**{**MAINNET_OK, "humanid_mode": mode}))
    assert len(bad) == 1 and "ACR_HUMANID_MODE" in bad[0]


def test_an_explicit_mode_with_no_backend_is_still_refused():
    """`circle` with no facilitator would fail closed at request time — but a
    paywall that answers 500 is not a product either. Catch it at boot."""
    bad = violations(_s(**{**MAINNET_OK, "x402_facilitator_url": "", "humanid_app_id": ""}))
    assert {b.split(" ")[0] for b in bad} == {"ACR_X402_FACILITATOR_URL", "ACR_HUMANID_APP_ID"}


def test_wildcard_cors_is_refused_on_mainnet():
    bad = violations(_s(**{**MAINNET_OK, "cors_origins": "*"}))
    assert len(bad) == 1 and "ACR_CORS_ORIGINS" in bad[0]


# --- the chain profile: what a mainnet environment must not carry over --------

def test_a_localhost_or_plain_http_rpc_is_refused_on_mainnet():
    for rpc in ("http://127.0.0.1:8545", "http://rpc.mainnet.arc.io", "https://localhost:8545", ""):
        bad = violations(_s(**{**MAINNET_OK, "arc_rpc_url": rpc}))
        assert len(bad) == 1 and "ACR_ARC_RPC_URL" in bad[0], rpc


def test_a_testnet_gateway_wallet_left_in_the_environment_is_refused():
    """The single most likely copy-paste failure: the testnet GatewayWallet in a
    mainnet env. Every payment would fail verify with a reason that reads like a
    bad signature. The profile knows mainnet's address; the guard holds it."""
    testnet_gw = "0x0077777d7EBA4688BDeF3E311b846F25870A19B9"
    bad = violations(_s(**{**MAINNET_OK, "x402_gateway_wallet": testnet_gw}))
    assert len(bad) == 1 and "GatewayWallet" in bad[0] and "0x77777777Dcc4" in bad[0]


def test_mainnet_defaults_resolve_from_the_profile_and_pass():
    """With nothing chain-shaped set, the profile fills explorer and Gateway wallet
    with the documented mainnet values — so the guard passes, and a mainnet env
    never has to contain a literal it could get wrong."""
    s = _s(**MAINNET_OK)
    assert s.chain_name == "Arc"
    assert s.explorer_base == "https://explorer.arc.io"
    assert s.x402_gateway_wallet == "0x77777777Dcc4d5A8B6E418Fd04D8997ef11000eE"
    assert s.circle_blockchain == "ARC" and s.gateway_chain == "arc"
    # The preview ended (probed 2026-09-21): Gateway lists Arc without the header.
    assert s.private_mainnet is False
    assert s.public_rpc_url == "https://rpc.mainnet.arc.io"
    assert violations(s) == []


def test_private_mainnet_header_can_be_forced_on_by_env():
    assert _s(**{**MAINNET_OK, "arc_private_mainnet": True}).private_mainnet is True


def test_the_wallet_rpc_is_never_the_servers_keyed_one():
    """A provider URL with a key in it is fine for the server and a leak for the
    payload; the visitor's wallet gets the profile's public endpoint."""
    s = _s(**{**MAINNET_OK, "arc_rpc_url": "https://arc-mainnet.g.alchemy.com/v2/SECRET"})
    assert s.public_rpc_url == "https://rpc.mainnet.arc.io"
    assert violations(s) == []
    bad = violations(_s(**{**MAINNET_OK, "public_rpc_url": "http://rpc.mainnet.arc.io"}))
    assert len(bad) == 1 and "ACR_PUBLIC_RPC_URL" in bad[0]


def test_the_testnet_facilitator_is_refused_on_mainnet():
    bad = violations(_s(**{**MAINNET_OK, "x402_facilitator_url": "https://gateway-api-testnet.circle.com"}))
    assert len(bad) == 1 and "gateway-api.circle.com" in bad[0]


def test_testnet_profile_is_unchanged_by_all_of_this():
    s = _s()
    assert (s.chain_name, s.circle_blockchain, s.gateway_chain, s.private_mainnet) == (
        "Arc Testnet", "ARC-TESTNET", "arcTestnet", False)
    assert s.explorer_base == "https://testnet.arcscan.app"
    assert s.x402_gateway_wallet == "0x0077777d7EBA4688BDeF3E311b846F25870A19B9"
    assert s.public_rpc_url == "https://rpc.testnet.arc.io"


# --- the testnet-only money surfaces ----------------------------------------

def test_testnet_surfaces_are_on_by_default_off_mainnet():
    assert surfaces_enabled(_s()) is True


def test_testnet_surfaces_can_be_turned_off_on_testnet():
    """A staging host that should not drip."""
    assert surfaces_enabled(_s(testnet_surfaces=False)) is False


def test_no_environment_turns_the_surfaces_on_on_mainnet():
    """The property that makes it a kill-switch: a typo cannot flip it."""
    assert surfaces_enabled(_s(arc_chain_id=MAINNET_CHAIN_ID, testnet_surfaces=True)) is False


# --- through the real app -----------------------------------------------------

def _env(monkeypatch, **over):
    for k, v in over.items():
        monkeypatch.setenv(k, str(v))
    reset_settings()


@pytest.mark.parametrize("route", ["/desk/faucet", "/demo/buyer/start", "/demo/attack/start"])
def test_the_money_surfaces_answer_404_on_mainnet(monkeypatch, route):
    """404, not 403: a route that admits it exists invites the next attempt."""
    from fastapi.testclient import TestClient
    from index_api.app import app

    _env(monkeypatch, ACR_ARC_CHAIN_ID=MAINNET_CHAIN_ID, ACR_TESTNET_SURFACES="1")
    r = TestClient(app).post(route, json={})
    assert r.status_code == 404
    assert "not available on this network" in r.json()["detail"]
    reset_settings()


def test_the_money_surfaces_answer_404_when_switched_off_on_testnet(monkeypatch):
    from fastapi.testclient import TestClient
    from index_api.app import app

    _env(monkeypatch, ACR_TESTNET_SURFACES="0")
    r = TestClient(app).post("/desk/faucet", json={})
    assert r.status_code == 404
    reset_settings()


def test_a_misconfigured_mainnet_refuses_to_boot(monkeypatch):
    """The lifespan runs the guard before any background task starts."""
    from fastapi.testclient import TestClient
    from index_api.app import app

    _env(monkeypatch, ACR_ARC_CHAIN_ID=MAINNET_CHAIN_ID)
    with pytest.raises(MainnetGuardError):
        with TestClient(app):
            pass
    reset_settings()
