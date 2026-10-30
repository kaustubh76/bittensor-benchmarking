"""The Desk feed pass — a USDC transfer the chain saw becomes a right the gate honours.

Everything that can be held without a chain is held here: which Transfer logs
count, every refusal in `verify_payment`, the mint's plumbing, and the gate's
behaviour with a DESK-SESSION header. The chain and Circle are stand-ins; the
EIP-712 signing runs for real through a local key.
"""

from __future__ import annotations

import time
import types
from types import SimpleNamespace

import pytest
from acr_core import reset_settings
from index_api import feedpass
from index_api.feedpass import PassError, nonce_for, transfer_in_receipt, verify_payment

USDC = "0x3600000000000000000000000000000000000000"
SELLER = "0x" + "ab" * 20
WALLET = "0x" + "cd" * 20
OTHER = "0x" + "ef" * 20
TX = "0x" + "11" * 32


def _topic(addr: str) -> bytes:
    return bytes.fromhex("00" * 12 + addr[2:])


def _transfer_log(token: str, frm: str, to: str, units: int) -> dict:
    return {
        "address": token,
        "topics": [bytes.fromhex(feedpass.TRANSFER_TOPIC[2:]), _topic(frm), _topic(to)],
        "data": units.to_bytes(32, "big"),
    }


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ACR_X402_PAY_TO", SELLER)
    monkeypatch.setenv("ACR_ATTESTOR_ADDRESS", "0x" + "77" * 20)
    monkeypatch.setenv("ACR_PASS_PRICE_USDC", "1.0")
    monkeypatch.setenv("ACR_PASS_WINDOW_S", "86400")
    monkeypatch.setenv("ACR_PASS_CLAIM_MAX_AGE_S", "3600")
    reset_settings()
    feedpass._access_memo.clear()
    yield
    reset_settings()


# --- which logs count ---------------------------------------------------------

def test_transfer_in_receipt_sums_only_usdc_from_this_wallet_to_the_seller():
    rcpt = {"logs": [
        _transfer_log(USDC, WALLET, SELLER, 600_000),
        _transfer_log(USDC, WALLET, SELLER, 400_000),   # two legs add up
        _transfer_log(USDC, OTHER, SELLER, 5_000_000),  # someone else's money
        _transfer_log(USDC, WALLET, OTHER, 5_000_000),  # paid to the wrong place
        _transfer_log(OTHER, WALLET, SELLER, 5_000_000),  # a different token
        {"address": USDC, "topics": [bytes(32)], "data": b""},  # not a Transfer
    ]}
    assert transfer_in_receipt(rcpt, usdc=USDC, sender=WALLET, recipient=SELLER) == 1_000_000


def test_transfer_in_receipt_is_case_insensitive_and_zero_when_empty():
    rcpt = {"logs": [_transfer_log(USDC.upper(), WALLET.lower(), SELLER.upper(), 7)]}
    assert transfer_in_receipt(rcpt, usdc=USDC, sender=WALLET, recipient=SELLER) == 7
    assert transfer_in_receipt({"logs": []}, usdc=USDC, sender=WALLET, recipient=SELLER) == 0


# --- every refusal, from the chain's answer --------------------------------------

class _W3:
    """A chain that answers exactly what the test says it does."""

    def __init__(self, receipt, block_ts: int):
        self.eth = SimpleNamespace(
            get_transaction_receipt=lambda h: receipt,
            get_block=lambda n: {"timestamp": block_ts},
            chain_id=31337,
        )


def _ok_receipt(units: int = 1_000_000, status: int = 1) -> dict:
    return {"status": status, "blockNumber": 42, "logs": [_transfer_log(USDC, WALLET, SELLER, units)]}


def test_verify_payment_accepts_a_fresh_full_transfer():
    now = time.time()
    units, ts = verify_payment(_W3(_ok_receipt(), int(now) - 60), TX, sender=WALLET, now=now)
    assert units == 1_000_000 and ts == int(now) - 60


def test_verify_payment_refuses_a_failed_transaction():
    with pytest.raises(PassError) as e:
        verify_payment(_W3(_ok_receipt(status=0), int(time.time())), TX, sender=WALLET)
    assert e.value.status == 402 and "did not succeed" in e.value.detail


def test_verify_payment_refuses_a_short_payment_and_says_by_how_much():
    with pytest.raises(PassError) as e:
        verify_payment(_W3(_ok_receipt(units=999_999), int(time.time())), TX, sender=WALLET)
    assert e.value.status == 402 and "0.999999 USDC" in e.value.detail


def test_verify_payment_refuses_a_payment_from_another_wallet():
    rcpt = {"status": 1, "blockNumber": 1, "logs": [_transfer_log(USDC, OTHER, SELLER, 5_000_000)]}
    with pytest.raises(PassError):
        verify_payment(_W3(rcpt, int(time.time())), TX, sender=WALLET)


def test_verify_payment_refuses_an_old_receipt():
    """A pass is bought, not remembered: a transfer from two hours ago buys nothing today."""
    now = time.time()
    with pytest.raises(PassError) as e:
        verify_payment(_W3(_ok_receipt(), int(now) - 7_200), TX, sender=WALLET, now=now)
    assert "too old" in e.value.detail


def test_verify_payment_refuses_an_unknown_transaction():
    class _Missing:
        eth = SimpleNamespace(get_transaction_receipt=lambda h: (_ for _ in ()).throw(ValueError("not found")))

    with pytest.raises(PassError) as e:
        verify_payment(_Missing(), TX, sender=WALLET)
    assert e.value.status == 404


def test_verify_payment_refuses_when_no_seller_is_configured(monkeypatch):
    monkeypatch.setenv("ACR_X402_PAY_TO", "")
    reset_settings()
    with pytest.raises(PassError) as e:
        verify_payment(_W3(_ok_receipt(), int(time.time())), TX, sender=WALLET)
    assert e.value.status == 503


# --- the mint's plumbing: one payment, one pass ------------------------------------

def test_nonce_is_the_transaction_hash():
    assert nonce_for(TX) == int("11" * 16, 16)
    assert nonce_for(TX) == nonce_for(TX[2:]) and nonce_for(TX) != nonce_for("0x" + "22" * 32)


class _w3:
    """The smallest chain `mint` needs: one that can be asked for a receipt.

    `mint` waits for the redeem's receipt because `relay_redeem` returns as soon
    as the transaction is ACCEPTED. Reading access straight after raced the block
    that grants it — reproduced on anvil, where a receipt with status 1 and an
    AccessGranted log still produced `has_access: False`. A stub that cannot be
    asked for a receipt is a stub that cannot see that bug.
    """

    def __init__(self, status: int = 1) -> None:
        outer = self

        class _Eth:
            def wait_for_transaction_receipt(self, tx_hash, timeout=60):  # noqa: ARG002
                outer.asked = tx_hash
                return types.SimpleNamespace(status=outer.status)

        self.status = status
        self.asked = None
        self.eth = _Eth()


def test_mint_signs_for_the_window_with_the_hash_as_nonce_and_reports_the_chain(monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(feedpass, "verify_payment", lambda w3, tx, *, sender, now=None: (1_000_000, 0))
    import acr_oracle_client.feed_access as fa

    def _sign(signer, w3, attestor, **kw):
        seen["sign"] = kw
        return 27, b"r" * 32, b"s" * 32

    def _relay(signer, w3, attestor, **kw):
        seen["relay"] = kw
        return "0xrelay"

    monkeypatch.setattr(fa, "sign_feed_access", _sign)
    monkeypatch.setattr(fa, "relay_redeem", _relay)
    monkeypatch.setattr(fa, "access_of", lambda w3, a, who: (True, 1_700_000_000))
    before = int(time.time())
    out = feedpass.mint(_w3(status=1), object(), wallet=WALLET, tx_hash=TX)
    assert seen["sign"]["nonce"] == nonce_for(TX)
    assert seen["sign"]["payer"] == WALLET and seen["sign"]["beneficiary"] == WALLET
    assert before + 86_400 <= seen["sign"]["paid_until"] <= before + 86_400 + 5
    assert seen["sign"]["amount_units"] == 1_000_000
    assert seen["relay"]["v"] == 27
    assert out == {"wallet": WALLET, "has_access": True, "paid_until": 1_700_000_000,
                   "payment_tx": TX, "attest_tx": "0xrelay"}


def test_mint_maps_a_reused_nonce_to_409(monkeypatch):
    monkeypatch.setattr(feedpass, "verify_payment", lambda *a, **k: (1_000_000, 0))
    import acr_oracle_client.feed_access as fa

    monkeypatch.setattr(fa, "sign_feed_access", lambda *a, **k: (27, b"r" * 32, b"s" * 32))
    monkeypatch.setattr(fa, "relay_redeem", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("execution reverted: nonce used")))
    with pytest.raises(PassError) as e:
        feedpass.mint(object(), object(), wallet=WALLET, tx_hash=TX)
    assert e.value.status == 409 and "already bought" in e.value.detail


def test_mint_refuses_without_an_attestor(monkeypatch):
    monkeypatch.setenv("ACR_ATTESTOR_ADDRESS", "")
    reset_settings()
    with pytest.raises(PassError) as e:
        feedpass.mint(object(), object(), wallet=WALLET, tx_hash=TX)
    assert e.value.status == 503


def test_the_real_signature_recovers_to_the_press_key():
    """The one part that must run for real: EIP-712 over the shared types, checked
    against our own signer before anything would be broadcast."""
    from acr_oracle_client.feed_access import sign_feed_access
    from acr_oracle_client.signer import LocalKeySigner

    signer = LocalKeySigner("0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80")
    w3 = SimpleNamespace(eth=SimpleNamespace(chain_id=31337))
    v, r, s = sign_feed_access(signer, w3, "0x" + "77" * 20, payer=WALLET, beneficiary=WALLET,
                               paid_until=1_800_000_000, amount_units=1_000_000, nonce=nonce_for(TX))
    assert v in (27, 28) and len(r) == 32 and len(s) == 32


# --- the gate ---------------------------------------------------------------------

def test_access_is_memoised_and_a_throttled_rpc_reads_as_no_pass(monkeypatch):
    import acr_oracle_client.feed_access as fa

    calls = {"n": 0}

    def _access_of(w3, a, who):
        calls["n"] += 1
        return True, 123

    monkeypatch.setattr(fa, "access_of", _access_of)
    assert feedpass.access(object(), WALLET) == (True, 123)
    assert feedpass.access(object(), WALLET.upper()) == (True, 123)
    assert calls["n"] == 1, "the second read within the TTL is served from memory"
    feedpass.forget(WALLET)
    monkeypatch.setattr(fa, "access_of", lambda *a: (_ for _ in ()).throw(TimeoutError()))
    assert feedpass.access(object(), WALLET) == (False, 0)


def test_a_desk_session_with_a_pass_reads_a_gated_endpoint_without_paying(monkeypatch):
    from fastapi.testclient import TestClient
    from index_api import x402
    from index_api.app import app

    monkeypatch.setattr(x402, "pass_receipt", lambda tok: x402.PaymentReceipt(
        payer=WALLET, amount_usdc=0.0, tx_ref="pass:1800000000", network="eip155:5042002", scheme="feed-pass"
    ) if tok == "good-token" else None)
    c = TestClient(app)
    assert c.get("/prints").status_code == 402, "no header, no pass: the paywall stands"
    assert c.get("/prints", headers={"DESK-SESSION": "bad-token"}).status_code == 402
    r = c.get("/prints", headers={"DESK-SESSION": "good-token"})
    assert r.status_code == 200, r.text


def test_pass_receipt_is_none_without_a_wallet_or_a_pass(monkeypatch):
    from index_api import desk, x402

    monkeypatch.setattr(desk, "wallet_of", lambda tok: None)
    assert x402.pass_receipt("t") is None
    monkeypatch.setattr(desk, "wallet_of", lambda tok: {"wallet_id": "w", "address": WALLET})
    monkeypatch.setattr(feedpass, "access", lambda w3, a: (False, 0))
    assert x402.pass_receipt("t") is None
    monkeypatch.setattr(feedpass, "access", lambda w3, a: (True, 1_800_000_000))
    rc = x402.pass_receipt("t")
    assert rc is not None and rc.amount_usdc == 0.0 and rc.scheme == "feed-pass" and rc.tx_ref == "pass:1800000000"


def test_a_reverted_attestation_is_not_reported_as_a_pass(monkeypatch):
    """The failure this wait exists to catch. `relay_redeem` returns when the
    transaction is ACCEPTED, so a redeem that reverts once mined used to come back
    as a 200 with `has_access: False` — the money moved, the attestation did not,
    and nothing said so. It is a 502 now."""
    import acr_oracle_client.feed_access as fa

    monkeypatch.setattr(feedpass, "verify_payment", lambda w3, tx, *, sender, now=None: (1_000_000, 0))
    monkeypatch.setattr(fa, "sign_feed_access", lambda *a, **k: (27, b"r" * 32, b"s" * 32))
    monkeypatch.setattr(fa, "relay_redeem", lambda *a, **k: "0xrelay")
    monkeypatch.setattr(fa, "access_of", lambda w3, a, who: (False, 0))

    with pytest.raises(PassError) as e:
        feedpass.mint(_w3(status=0), object(), wallet=WALLET, tx_hash=TX)
    assert e.value.status == 502 and "reverted" in e.value.detail


def test_a_relay_that_never_mines_is_a_502_not_a_silent_no_pass(monkeypatch):
    """And a receipt that never arrives must not read as "you have no pass"."""
    import acr_oracle_client.feed_access as fa

    class _Timeout(_w3):
        def __init__(self) -> None:
            super().__init__()

            class _Eth:
                def wait_for_transaction_receipt(self, tx_hash, timeout=60):  # noqa: ARG002
                    raise TimeoutError("not mined within 60s")

            self.eth = _Eth()

    monkeypatch.setattr(feedpass, "verify_payment", lambda w3, tx, *, sender, now=None: (1_000_000, 0))
    monkeypatch.setattr(fa, "sign_feed_access", lambda *a, **k: (27, b"r" * 32, b"s" * 32))
    monkeypatch.setattr(fa, "relay_redeem", lambda *a, **k: "0xrelay")
    with pytest.raises(PassError) as e:
        feedpass.mint(_Timeout(), object(), wallet=WALLET, tx_hash=TX)
    assert e.value.status == 502 and "never mined" in e.value.detail
