"""A Circle key the chain cannot use fails in one sentence, not a traceback.

On 2026-10-16 `make resolve-humans ARGS=--commit` died on testnet with sixty
lines of SDK traceback ending in `403 {"code":3,"message":"Forbidden"}`. The
`.env` targeted testnet but held a `LIVE_API_KEY`, which Circle refuses for a
testnet wallet. Nothing said so: the answer was in the key's own prefix.
"""
from __future__ import annotations

import pytest
from acr_oracle_client.signer import CircleAuthError, CircleWalletSigner, circle_key_mismatch

MAINNET, TESTNET = 5042, 5042002


@pytest.mark.parametrize("key, chain", [
    ("TEST_API_KEY:abc:def", TESTNET),
    ("LIVE_API_KEY:abc:def", MAINNET),
    (" TEST_API_KEY:abc:def ", TESTNET),
])
def test_a_key_of_the_chains_kind_is_fine(key, chain):
    assert circle_key_mismatch(key, chain) is None


@pytest.mark.parametrize("key, chain, kind, want", [
    ("LIVE_API_KEY:abc:def", TESTNET, "LIVE_API_KEY", "TEST_API_KEY"),  # the 2026-10-16 case
    ("TEST_API_KEY:abc:def", MAINNET, "TEST_API_KEY", "LIVE_API_KEY"),
])
def test_a_key_of_the_other_kind_is_named(key, chain, kind, want):
    why = circle_key_mismatch(key, chain)
    assert why is not None
    assert kind in why and want in why and str(chain) in why
    assert "abc" not in why and "def" not in why, "the key itself must never be echoed"


@pytest.mark.parametrize("key", ["", "#commented", "not-a-circle-key", "SANDBOX:x:y"])
def test_an_unrecognised_key_shape_is_not_judged(key):
    # Circle will answer for it; guessing here would refuse a key we cannot read.
    assert circle_key_mismatch(key, TESTNET) is None


class ForbiddenException(Exception):
    """Stands in for circle.web3...exceptions.ForbiddenException, by name and
    status, which is how the signer recognises it without importing the SDK."""

    status = 403


class _RefusingClient:
    def __init__(self, exc: Exception):
        self.exc = exc

    def wallet_address(self, wallet_id):
        raise self.exc

    def sign_typed_data(self, wallet_id, typed_data_json):
        raise self.exc

    def contract_execution(self, wallet_id, contract_address, call_data, idempotency_key):
        raise self.exc


def test_a_refused_key_becomes_one_sentence():
    signer = CircleWalletSigner(wallet_id="52251d34-9575-5bab", client=_RefusingClient(ForbiddenException("(403)\nReason: Forbidden\n" + "x" * 2000)))
    with pytest.raises(CircleAuthError) as info:
        _ = signer.address
    msg = str(info.value)
    assert "\n" not in msg and len(msg) < 400
    assert "403" in msg and "52251d34" in msg
    assert "revoked" in msg and "other environment" in msg


def test_an_unauthorized_status_is_recognised_without_the_sdk_class():
    class Whatever(Exception):
        status = 401

    signer = CircleWalletSigner(wallet_id="w", client=_RefusingClient(Whatever("nope")))
    with pytest.raises(CircleAuthError):
        _ = signer.address


def test_a_refusal_while_writing_is_named_too():
    signer = CircleWalletSigner(wallet_id="w", client=_RefusingClient(ForbiddenException("x")))
    signer._address = "0x" + "11" * 20  # the read worked; the write is what Circle refuses
    with pytest.raises(CircleAuthError):
        signer.send_transaction(None, {"to": "0x" + "22" * 20, "data": "0x"})


def test_any_other_failure_is_left_alone():
    signer = CircleWalletSigner(wallet_id="w", client=_RefusingClient(TimeoutError("slow")))
    with pytest.raises(TimeoutError):
        _ = signer.address
