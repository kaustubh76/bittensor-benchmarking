"""Config tests — Circle/x402/Arc fields default to the offline-safe values, and
the singleton reloads env after reset_settings()."""

from __future__ import annotations

from acr_core import get_settings, reset_settings
from acr_core.config import ACRSettings


def test_circle_and_x402_default_empty():
    # Empty defaults are what select the offline/dev paths — must stay empty.
    # _env_file=None tests the pure code defaults regardless of any local .env.
    s = ACRSettings(_env_file=None)
    assert s.circle_api_key == ""
    assert s.circle_entity_secret == ""
    assert s.circle_wallet_id == ""
    assert s.x402_facilitator_url == ""
    assert s.x402_pay_to == ""
    assert s.poster_private_key == ""
    assert s.tape_source == "sim"
    # Taostats is optional, and the EMPTY key is the load-bearing default: a
    # non-empty one would send CI at a live API on a 5-request/minute free tier.
    assert s.taostats_api_key == ""
    assert s.taostats_base_url == "https://api.taostats.io"
    assert s.taostats_max_calls_per_cycle == 3


def test_arc_defaults_and_caip2():
    s = ACRSettings(_env_file=None)
    assert s.arc_chain_id == 5042002
    assert s.usdc_address == "0x3600000000000000000000000000000000000000"
    assert s.caip2() == "eip155:5042002"


def test_caip2_override():
    s = ACRSettings(_env_file=None, arc_network_caip2="eip155:1")
    assert s.caip2() == "eip155:1"


def test_comment_pollution_is_blanked():
    # A .env whose empty placeholders carry an inline `# comment` must read as
    # unset — otherwise config selection silently picks the live path.
    s = ACRSettings(
        _env_file=None,
        x402_facilitator_url="   # Circle Gateway URL",
        x402_pay_to="  # our wallet",
        circle_api_key="# PREFIX:ID:SECRET",
    )
    assert s.x402_facilitator_url == ""
    assert s.x402_pay_to == ""
    assert s.circle_api_key == ""
    # Real values and int coercion are untouched.
    s2 = ACRSettings(_env_file=None, arc_chain_id=5042002, x402_pay_to="0xabc")
    assert s2.arc_chain_id == 5042002 and s2.x402_pay_to == "0xabc"


def test_comment_pollution_on_numeric_fields_uses_default():
    # The same inline-comment artifact on a NUMERIC field must fall back to the
    # field default, not raise (a blank string cannot parse as float/int — this
    # would crash every get_settings() call at app startup).
    s = ACRSettings(
        _env_file=None,
        x402_price_usdc="   # sub-cent price",
        x402_max_timeout_seconds="# seconds",
        refresh_seconds="  # refresh",
    )
    assert s.x402_price_usdc == 0.0001
    assert s.x402_max_timeout_seconds == 60
    assert s.refresh_seconds == 30.0
    # And a polluted str field with a non-empty default gets the default back.
    s2 = ACRSettings(_env_file=None, circle_base_url="# https://…")
    assert s2.circle_base_url == "https://api.circle.com"


def test_env_override_after_reset(monkeypatch):
    monkeypatch.setenv("ACR_X402_FACILITATOR_URL", "https://facilitator.example/v2")
    monkeypatch.setenv("ACR_ARC_CHAIN_ID", "5042002")
    reset_settings()
    try:
        s = get_settings()
        assert s.x402_facilitator_url == "https://facilitator.example/v2"
        assert s.arc_chain_id == 5042002
    finally:
        reset_settings()  # don't leak into other tests
