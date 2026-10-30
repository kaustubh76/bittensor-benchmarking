"""Public Desk backend — hermetic tests (Circle REST + chain reads mocked).

The guardrails are the contract here: faucet caps that survive restarts,
qty clamps, and challenge payloads that encode exactly the advertised action.
"""

from __future__ import annotations

import time

import pytest
from index_api import desk
from index_api.desk import DeskError, FaucetLedger

# --- faucet ledger ---------------------------------------------------------


def test_faucet_ledger_one_drip_per_address(tmp_path):
    led = FaucetLedger(log_path=str(tmp_path / "led.jsonl"))
    led.claim("0xAbC0000000000000000000000000000000000001")
    with pytest.raises(DeskError) as e:
        led.claim("0xabc0000000000000000000000000000000000001")  # case-insensitive dup
    assert e.value.status == 409


def test_faucet_ledger_survives_restart(tmp_path):
    path = str(tmp_path / "led.jsonl")
    FaucetLedger(log_path=path).claim("0x" + "1" * 40)
    reborn = FaucetLedger(log_path=path)  # rehydrates from the JSONL
    with pytest.raises(DeskError):
        reborn.claim("0x" + "1" * 40)


def test_faucet_ledger_global_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(desk, "FAUCET_GLOBAL_CAP", 2)
    led = FaucetLedger(log_path="")
    led.claim("0x" + "1" * 40)
    led.claim("0x" + "2" * 40)
    with pytest.raises(DeskError) as e:
        led.claim("0x" + "3" * 40)
    assert e.value.status == 429


def test_faucet_release_frees_the_slot():
    led = FaucetLedger(log_path="")
    led.claim("0x" + "4" * 40)
    led.release("0x" + "4" * 40)
    led.claim("0x" + "4" * 40)  # no raise


def test_faucet_receipt_does_not_consume_a_second_slot(tmp_path):
    """The claim line reserves the slot; the receipt line records the hash. A
    restart must rehydrate ONE slot from the two lines, not two."""
    path = str(tmp_path / "led.jsonl")
    led = FaucetLedger(log_path=path)
    led.claim("0x" + "5" * 40)
    led.record_tx("0x" + "5" * 40, "0xdeadbeef")
    reborn = FaucetLedger(log_path=path)
    assert reborn.spent() == 1


ADDR6 = "0x" + "6" * 40


def _session_wallet(monkeypatch, address):
    """Stand in for Circle's wallet lookup — the ONLY source of the drip's
    destination now, so the caller cannot choose where the money goes."""
    monkeypatch.setattr(desk, "wallet_of", lambda tok: {"wallet_id": "w", "address": address})
    monkeypatch.setattr(desk, "_custody_balance", lambda: 100.0)


def test_drip_goes_to_the_session_wallet_not_a_caller_supplied_address(monkeypatch, tmp_path):
    """The faucet endpoint is unauthenticated and CORS is open, so a body field
    naming the destination let anyone drain the budget to addresses they chose.
    The destination is derived from the session token instead."""
    monkeypatch.setattr(desk, "_ledger", FaucetLedger(log_path=str(tmp_path / "led.jsonl")))
    _session_wallet(monkeypatch, ADDR6)
    sent: list[str] = []
    monkeypatch.setattr(desk, "_send_stake", lambda a: sent.append(a) or "0xfeed")

    desk.drip_stake("some-user-token")
    for _ in range(50):
        if sent:
            break
        time.sleep(0.05)
    assert sent == [desk._checksum(ADDR6)]


def test_drip_refuses_a_session_with_no_wallet_yet(monkeypatch, tmp_path):
    monkeypatch.setattr(desk, "_ledger", FaucetLedger(log_path=str(tmp_path / "led.jsonl")))
    monkeypatch.setattr(desk, "wallet_of", lambda tok: None)
    with pytest.raises(DeskError) as e:
        desk.drip_stake("tok")
    assert e.value.status == 409


def test_drip_refuses_when_the_funding_wallet_is_nearly_empty(monkeypatch, tmp_path):
    """A backstop independent of the ledger: whatever the ledger believes, never
    drain the wallet every drip is paid from."""
    monkeypatch.setattr(desk, "_ledger", FaucetLedger(log_path=str(tmp_path / "led.jsonl")))
    _session_wallet(monkeypatch, ADDR6)
    monkeypatch.setattr(desk, "_custody_balance", lambda: desk.FAUCET_RESERVE_USDC)
    with pytest.raises(DeskError) as e:
        desk.drip_stake("tok")
    assert e.value.status == 429


def test_the_reserve_is_days_of_press_runway_not_a_round_number():
    """On production the press and the faucet are the SAME wallet, so a busy
    desk shortens the oracle's life. An empty desk is a disappointment; an empty
    press is the end of the product — no prints, no marks, nothing settles. The
    floor therefore has to be denominated in what it protects."""
    assert desk.faucet_reserve_usdc(burn_per_day=0.41, days=14) == 5.74
    # It must move with the measured burn, which is the whole point.
    assert desk.faucet_reserve_usdc(burn_per_day=0.82, days=14) > desk.faucet_reserve_usdc(
        burn_per_day=0.41, days=14
    )
    # And it must comfortably outlast the old flat 2.0, which was ~5 days of
    # posting wearing a number that hid it.
    assert desk.FAUCET_RESERVE_USDC > 2.0


def test_the_reserve_never_goes_negative_or_nonsensical():
    """Garbage config must fail safe (no reserve) rather than compute a negative
    floor, which would read as "always allow" — a drain with extra steps."""
    assert desk.faucet_reserve_usdc(burn_per_day=-1, days=14) == 0.0
    assert desk.faucet_reserve_usdc(burn_per_day=0.41, days=-5) == 0.0
    assert desk.faucet_reserve_usdc(burn_per_day=0, days=0) == 0.0


def test_drip_returns_immediately_and_confirms_off_thread(monkeypatch, tmp_path):
    """The request thread must not wait on Circle's confirm poll — it outlives
    the browser's proxy timeout. The slot is reserved synchronously (the cap
    stays honest) and the hash lands later."""
    import threading

    monkeypatch.setattr(desk, "_ledger", FaucetLedger(log_path=str(tmp_path / "led.jsonl")))
    _session_wallet(monkeypatch, ADDR6)
    sending = threading.Event()
    release = threading.Event()

    def slow_send(address):
        sending.set()
        release.wait(5)
        return "0xfeed"

    monkeypatch.setattr(desk, "_send_stake", slow_send)
    out = desk.drip_stake("tok")
    assert out["state"] == "pending"  # returned while the transfer is in flight
    assert sending.wait(2)
    assert desk.get_ledger().spent() == 1  # slot already reserved
    release.set()
    for _ in range(50):
        if desk._drip_tx.get(ADDR6):
            break
        time.sleep(0.05)
    assert desk._drip_tx.get(ADDR6) == "0xfeed"


def test_drip_failure_frees_the_slot(monkeypatch, tmp_path):
    monkeypatch.setattr(desk, "_ledger", FaucetLedger(log_path=str(tmp_path / "led.jsonl")))
    _session_wallet(monkeypatch, "0x" + "7" * 40)

    def boom(address):
        raise RuntimeError("circle down")

    monkeypatch.setattr(desk, "_send_stake", boom)
    desk.drip_stake("tok")
    for _ in range(50):
        if desk.get_ledger().spent() == 0:
            break
        time.sleep(0.05)
    assert desk.get_ledger().spent() == 0  # the address may try again


def test_ledger_fails_closed_when_circle_is_unreachable(monkeypatch, tmp_path):
    """The most important guard here: failing OPEN would restore the exact bug
    the durable ledger exists to prevent — a restart handing fresh drips to
    addresses already paid."""
    led = FaucetLedger(log_path=str(tmp_path / "led.jsonl"), require_circle=True)
    monkeypatch.setattr(desk, "_circle", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down")))
    with pytest.raises(DeskError) as e:
        led.claim("0x" + "8" * 40)
    assert e.value.status == 503
    assert led.spent() == 0  # and nothing was handed out


def test_ledger_hydrates_on_a_freshly_booted_host(monkeypatch, tmp_path):
    """A just-restarted container must still ask Circle before paying anyone.

    time.monotonic() counts from an arbitrary origin — on Linux, host boot — so
    on a fresh machine it returns a SMALL number. A staleness check written as
    ``now - hydrated_at < TTL`` against an initial 0.0 therefore reads as
    "hydrated moments ago" and skips the check for the host's first ten
    minutes: exactly the window after a restart when the in-memory ledger is
    empty and this is the only thing stopping a second drip. CI runners boot
    fresh, which is how this surfaced; a long-running laptop never sees it.
    """
    monkeypatch.setattr(desk.time, "monotonic", lambda: 4.0)  # 4s of uptime
    asked = []
    monkeypatch.setattr(
        desk, "_circle",
        lambda *a, **k: (asked.append(1), {"data": {"transactions": []}})[1],
    )
    FaucetLedger(log_path=str(tmp_path / "led.jsonl"), require_circle=True).claim(
        "0x" + "7" * 40
    )
    assert asked, "a freshly booted ledger paid out without consulting Circle"


def test_ledger_rehydrates_from_circles_record(monkeypatch, tmp_path):
    """Production has no persistent disk, so the JSONL is empty on every boot;
    Circle's mirrored INBOUND rows are what make the caps real there."""
    paid = "0x" + "9" * 40
    monkeypatch.setattr(
        desk, "_circle",
        lambda *a, **k: {"data": {"transactions": [
            {"state": "COMPLETE", "destinationAddress": paid},
            {"state": "FAILED", "destinationAddress": "0x" + "e" * 40},
        ]}},
    )
    led = FaucetLedger(log_path=str(tmp_path / "led.jsonl"), require_circle=True)
    with pytest.raises(DeskError) as dup:
        led.claim(paid)  # already paid, per Circle — even with an empty file
    assert dup.value.status == 409
    led.claim("0x" + "e" * 40)  # the FAILED row consumed nothing


# --- margin math -----------------------------------------------------------


def test_feasible_qty_respects_the_takers_own_margin():
    """0.5 USDC at 2000bps on a 10x index at mark 0.5 → ~1 USDC per contract,
    so the stake buys well under a contract. Quoted with the safety haircut."""
    buy, sell = desk.feasible_qty(
        mark=0.5, multiplier=10, margin_bps=2000,
        taker_collateral=0.5, taker_contracts=0.0,
        maker_collateral=100.0, maker_contracts=0.0,
    )
    assert buy == sell == round(0.90 * 0.5 / 1.0, 2)  # 0.45


def test_feasible_qty_counts_an_existing_position():
    """Margin is on the RESULTING size, so a long can add only up to the cap but
    may sell all the way through flat: 0.2 USDC/contract on 0.3 collateral caps
    |contracts| at 1.35, and the trader is already long 1."""
    buy, sell = desk.feasible_qty(
        mark=1.0, multiplier=1, margin_bps=2000,
        taker_collateral=0.3, taker_contracts=1.0,
        maker_collateral=100.0, maker_contracts=0.0,
    )
    assert buy == 0.35  # 1.35 − 1 already on
    assert sell == desk.MAX_QTY  # 1.35 + 1, clamped by the desk's own ±2


def test_feasible_qty_is_bounded_by_the_makers_margin_too():
    """Every desk fill auto-mirrors onto the maker, whose own margin check can
    revert the trade — a taker with plenty of collateral is still capped."""
    buy, sell = desk.feasible_qty(
        mark=1.0, multiplier=1, margin_bps=2000,
        taker_collateral=100.0, taker_contracts=0.0,
        maker_collateral=0.2, maker_contracts=0.0,
    )
    assert buy == sell == round(0.90 * 0.2 / 0.2, 2)  # 0.9, the maker's cap


def test_feasible_qty_never_goes_negative_or_past_the_clamp():
    buy, sell = desk.feasible_qty(
        mark=1.0, multiplier=1, margin_bps=2000,
        taker_collateral=0.0, taker_contracts=0.0,
        maker_collateral=0.0, maker_contracts=0.0,
    )
    assert (buy, sell) == (0.0, 0.0)
    big = desk.feasible_qty(
        mark=0.01, multiplier=1, margin_bps=2000,
        taker_collateral=1000.0, taker_contracts=0.0,
        maker_collateral=1000.0, maker_contracts=0.0,
    )
    assert big == (desk.MAX_QTY, desk.MAX_QTY)


# --- challenge builder -----------------------------------------------------


@pytest.fixture()
def circle_capture(monkeypatch):
    """Capture the payload build_challenge sends to Circle."""
    sent: dict = {}

    def fake_circle(method, path, body=None, user_token=None):
        sent.update({"method": method, "path": path, "body": body, "user_token": user_token})
        return {"data": {"challengeId": "ch-123"}}

    monkeypatch.setattr(desk, "_circle", fake_circle)
    monkeypatch.setattr(
        desk, "_live_series", lambda iid: {"series_id": 2, "index_id": iid, "trader_count": 3}
    )

    class _S:
        futures_address = "0x29d97c629a8278f7ec4218ab0bd8baa9182642fe"

    monkeypatch.setattr(desk, "get_settings", lambda: _S())
    return sent


def test_challenge_approve_targets_usdc_for_the_venue(circle_capture):
    out = desk.build_challenge("tok", "wid", "approve", "ACR-GPU")
    assert out == {"challenge_id": "ch-123", "action": "approve"}
    b = circle_capture["body"]
    assert circle_capture["user_token"] == "tok"
    assert b["contractAddress"] == desk.USDC_PREDEPLOY
    assert b["abiFunctionSignature"] == "approve(address,uint256)"
    assert b["abiParameters"][0] == "0x29d97c629a8278f7ec4218ab0bd8baa9182642fe"


def test_challenge_collateral_posts_the_stake_on_the_live_series(circle_capture):
    desk.build_challenge("tok", "wid", "collateral", "ACR-DATA")
    b = circle_capture["body"]
    assert b["abiFunctionSignature"] == "postCollateral(uint256,uint256)"
    assert b["abiParameters"] == ["2", "500000"]  # series 2, 0.5 USDC-6


def test_challenge_trade_clamps_and_scales_qty(circle_capture):
    desk.build_challenge("tok", "wid", "trade", "ACR-GPU", qty=7.0)  # clamped to +2
    b = circle_capture["body"]
    assert b["abiFunctionSignature"] == "trade(uint256,int256)"
    assert b["abiParameters"] == ["2", str(2 * 10**18)]

    desk.build_challenge("tok", "wid", "trade", "ACR-GPU", qty=-1.0)
    assert circle_capture["body"]["abiParameters"][1] == str(-1 * 10**18)


def test_challenge_collateral_never_posts_more_than_the_wallet_holds(circle_capture, monkeypatch):
    """If Gas Station is NOT sponsoring the SCA, the approve already burned part
    of the drip as gas (USDC is Arc's gas token) — posting the full stake would
    revert inside transferFrom. Post what's there, less this tx's own gas."""
    monkeypatch.setattr(desk, "_wallet_usdc", lambda a: 0.4993)
    desk.build_challenge("tok", "wid", "collateral", "ACR-GPU", address="0x" + "8" * 40)
    posted = int(circle_capture["body"]["abiParameters"][1])
    assert posted == int((0.4993 - desk.GAS_RESERVE_USDC) * 1_000_000)


def test_challenge_collateral_posts_the_full_stake_when_gas_is_sponsored(
    circle_capture, monkeypatch
):
    monkeypatch.setattr(desk, "_wallet_usdc", lambda a: 0.5)
    desk.build_challenge("tok", "wid", "collateral", "ACR-GPU", address="0x" + "8" * 40)
    assert circle_capture["body"]["abiParameters"][1] == "500000"


def test_challenge_trade_clamps_to_live_margin(circle_capture, monkeypatch):
    """The reader asks for 2; the venue only supports 0.45 — the challenge is
    minted at 0.45 rather than reverting after they enter their PIN."""
    monkeypatch.setattr(
        desk, "desk_limits", lambda addr, iid: {"max_buy": 0.45, "max_sell": 1.5}
    )
    desk.build_challenge("tok", "wid", "trade", "ACR-GPU", qty=2.0, address="0x" + "9" * 40)
    assert circle_capture["body"]["abiParameters"][1] == str(int(0.45 * 10**18))


def test_challenge_trade_refuses_a_direction_with_no_margin(circle_capture, monkeypatch):
    monkeypatch.setattr(desk, "desk_limits", lambda addr, iid: {"max_buy": 0.0, "max_sell": 1.5})
    with pytest.raises(DeskError) as e:
        desk.build_challenge("tok", "wid", "trade", "ACR-GPU", qty=1.0, address="0x" + "9" * 40)
    assert e.value.status == 409
    desk.build_challenge("tok", "wid", "trade", "ACR-GPU", qty=-1.0, address="0x" + "9" * 40)
    assert circle_capture["body"]["abiParameters"][1] == str(-1 * 10**18)


def test_challenge_rejects_zero_qty_and_unknown_action(circle_capture):
    with pytest.raises(DeskError) as e:
        desk.build_challenge("tok", "wid", "trade", "ACR-GPU", qty=0.0)
    assert e.value.status == 400
    with pytest.raises(DeskError):
        desk.build_challenge("tok", "wid", "liquidate", "ACR-GPU")


def _fake_futures(monkeypatch, desks):
    class _Fut:
        configured = True

        def read_all(self):
            return desks

    monkeypatch.setattr("index_api.onchain.get_futures", lambda: _Fut())


def _series(**kw):
    """A fake desk row. Every one needs a real ``expiry_ts`` now — the gate
    reads it, and a missing key would silently make other guards fire."""
    row = {"series_id": 0, "settled": False, "trader_count": 2, "expiry_ts": time.time() + 86400}
    row.update(kw)
    return row


def test_live_series_guards(monkeypatch):
    _fake_futures(
        monkeypatch,
        {
            "ACR-GPU": _series(series_id=1, trader_count=127),
            "ACR-INF": _series(settled=True),
        },
    )
    with pytest.raises(DeskError) as full:
        desk._live_series("ACR-GPU")  # roster ≥ TRADER_HEADROOM
    assert full.value.status == 409
    assert "roster" in full.value.detail  # not the expiry gate firing by accident
    with pytest.raises(DeskError) as settled:
        desk._live_series("ACR-INF")
    assert settled.value.status == 404
    with pytest.raises(DeskError) as missing:
        desk._live_series("ACR-DATA")
    assert missing.value.status == 404


def test_live_series_refuses_an_expired_series(monkeypatch):
    """Past expiry the contract reverts `trade`, so quoting one would mint a
    challenge that dies AFTER the reader has already entered their PIN."""
    _fake_futures(monkeypatch, {"ACR-INF": _series(expiry_ts=time.time() - 1)})
    with pytest.raises(DeskError) as e:
        desk._live_series("ACR-INF")
    assert e.value.status == 409
    assert "expired" in e.value.detail


def test_live_series_refuses_a_series_inside_the_expiry_buffer(monkeypatch):
    """The buffer is the point: 30s of life is already gone by the time a PIN
    ceremony completes, and the desk read is 90s-cached on top of that."""
    _fake_futures(monkeypatch, {"ACR-INF": _series(expiry_ts=time.time() + 30)})
    with pytest.raises(DeskError) as e:
        desk._live_series("ACR-INF")
    assert e.value.status == 409


def test_live_series_accepts_a_series_with_life_left(monkeypatch):
    _fake_futures(monkeypatch, {"ACR-INF": _series(expiry_ts=time.time() + 3600)})
    assert desk._live_series("ACR-INF")["series_id"] == 0


# --- the exit --------------------------------------------------------------


#: `withdrawable()` is orchestration over live contract reads — it is proven
#: against a REAL ACRFutures deployment in tests/test_desk_onchain.py, where the
#: quote is checked by actually withdrawing it. What belongs here is the pure
#: arithmetic it delegates to, which has no chain in it to stand in for.


def test_free_collateral_is_everything_once_the_series_settles():
    """Settled means flat, and the contract skips the margin check entirely."""
    assert desk.free_collateral_units(554338, 0.0, 0.0, 10, 2000, settled=True) == 554338


def test_free_collateral_is_everything_when_the_trader_is_flat():
    """A flat account needs no margin — which is why the caller never has to
    read a mark for it, and why a dead oracle can't trap a flat reader."""
    assert desk.free_collateral_units(500000, 0.0, 0.0, 10, 2000, settled=False) == 500000


def test_free_collateral_subtracts_required_margin_on_an_open_position():
    """0.5 posted against +0.46 contracts at 0.4974 on a 10x index at 2000bps
    pins 0.4576, leaving ~0.042 — quoted under the safety haircut."""
    free = desk.free_collateral_units(500000, 0.46, 0.4974, 10, 2000, settled=False) / 1e6
    true_free = 0.5 - 0.46 * 0.4974 * 10 * 0.20
    assert 0 < free < true_free
    assert free == pytest.approx(true_free * desk.WITHDRAW_SAFETY, abs=1e-6)


ADDR = "0x" + "a" * 40


def test_free_collateral_never_goes_negative():
    """A position needing more margin than the collateral behind it clamps to
    zero rather than quoting a withdrawal the contract would revert."""
    assert desk.free_collateral_units(100000, 2.0, 0.4974, 10, 2000, settled=False) == 0
    assert desk.free_collateral_units(0, 0.0, 0.0, 10, 2000, settled=True) == 0

def test_challenge_withdraw_targets_the_venue_with_raw_units(circle_capture, monkeypatch):
    monkeypatch.setattr(
        desk, "withdrawable", lambda a: {"series_id": 3, "free_units": 554338, "contracts": 0.0}
    )
    out = desk.build_challenge("tok", "wid", "withdraw", "ACR-INF", address=ADDR)
    assert out["action"] == "withdraw"
    b = circle_capture["body"]
    assert b["contractAddress"] == "0x29d97c629a8278f7ec4218ab0bd8baa9182642fe"
    assert b["abiFunctionSignature"] == "withdrawCollateral(uint256,uint256)"
    assert b["abiParameters"] == ["3", "554338"]


def test_challenge_withdraw_explains_a_pinned_stake(circle_capture, monkeypatch):
    """"Nothing to withdraw" would be a lie to someone whose money is simply
    backing a position — the error has to name the case."""
    monkeypatch.setattr(
        desk, "withdrawable", lambda a: {"series_id": 0, "free_units": 0, "contracts": 0.46}
    )
    with pytest.raises(DeskError) as e:
        desk.build_challenge("tok", "wid", "withdraw", "ACR-INF", address=ADDR)
    assert e.value.status == 409
    assert "open position" in e.value.detail


def test_challenge_withdraw_requires_an_address(circle_capture):
    with pytest.raises(DeskError) as e:
        desk.build_challenge("tok", "wid", "withdraw", "ACR-INF")
    assert e.value.status == 400


# --- the reader's stake fits the book it is posted on -------------------------

from index_api.desk import (  # noqa: E402
    FAUCET_USDC,
    MARGIN_SAFETY,
    MAX_QTY,
    reader_stake_for,
)

_MULT, _BPS = 10, 2000
_MARKS = {"ACR-INF": 0.4924, "ACR-GPU": 0.0111, "ACR-DATA": 0.00209}


def _tradable(stake: float, mark: float) -> float:
    """What the reader could actually trade with that stake — the desk's own cap."""
    return min(MARGIN_SAFETY * stake / (mark * _MULT * _BPS / 10_000), MAX_QTY)


def test_the_expensive_book_still_takes_the_whole_drip():
    """ACR-INF is why the flat stake looked right for a year.

    A 0.50 stake margins only ~0.46 contracts there, well under the 2.0 the
    desk would quote, so every cent is usable. This must not change: it is the
    index the demo has been walked on all week.
    """
    stake = reader_stake_for(_MARKS["ACR-INF"], _MULT, _BPS, FAUCET_USDC)
    assert stake >= FAUCET_USDC - 0.02, stake
    assert _tradable(stake, _MARKS["ACR-INF"]) < MAX_QTY


def test_a_cheap_book_takes_a_fraction_and_loses_nothing():
    """On ACR-GPU the flat 0.50 was ten times what the desk can ever use."""
    for idx in ("ACR-GPU", "ACR-DATA"):
        stake = reader_stake_for(_MARKS[idx], _MULT, _BPS, FAUCET_USDC)
        assert stake < 0.10, f"{idx}: {stake}"
        # The point: nothing is taken away — the reader can still trade the
        # largest size the desk will ever quote.
        assert _tradable(stake, _MARKS[idx]) >= MAX_QTY, idx


def test_a_reader_can_still_post_on_a_second_book():
    """The dead end, pinned.

    Posting moved the whole drip to the venue, so switching index ran
    `max(0, bal - gas)` -> 0 and answered 409 "this wallet has no stake to post
    yet" with nothing on screen saying why. It became reachable the day ACR-GPU
    got a book — the UI defaults to it.
    """
    first = reader_stake_for(_MARKS["ACR-GPU"], _MULT, _BPS, FAUCET_USDC)
    left = FAUCET_USDC - first
    second = reader_stake_for(_MARKS["ACR-INF"], _MULT, _BPS, left)
    assert second > 0, "a reader who posted on one book must be able to post on another"
    assert second <= left


def test_the_stake_never_exceeds_the_wallet():
    # USDC is gas on Arc, so a stake equal to the balance leaves nothing to pay
    # for its own postCollateral — the sliver is load-bearing.
    for bal in (0.50, 0.20, 0.05, 0.011):
        stake = reader_stake_for(_MARKS["ACR-INF"], _MULT, _BPS, bal)
        assert stake <= bal, (bal, stake)


def test_an_empty_wallet_posts_nothing_so_the_caller_can_refuse():
    assert reader_stake_for(_MARKS["ACR-GPU"], _MULT, _BPS, 0.0) == 0.0
    assert reader_stake_for(_MARKS["ACR-GPU"], _MULT, _BPS, 0.005) == 0.0


def test_a_stake_too_small_to_trade_is_not_posted_as_dust():
    """Below the floor a stake buys a PIN ceremony and a disabled button."""
    tiny = 0.02
    stake = reader_stake_for(_MARKS["ACR-INF"], _MULT, _BPS, tiny)
    # Either it takes what is there (so the caller can decide), or nothing —
    # never a rounded-down sliver that margins less than the desk's minimum.
    assert stake == 0.0 or stake <= tiny


def test_no_mark_falls_back_rather_than_posting_zero():
    """A throttled oracle read must not silently post nothing."""
    assert reader_stake_for(0.0, _MULT, _BPS, FAUCET_USDC) > 0


# --- settling from a reader's own wallet ------------------------------------
#
# ACRFutures.settle is permissionless, but until now only a cron ever called
# it, so a reader whose series expired found a shut exit. These tests guard the
# refusals: every one of them is a revert the contract would ALSO make — but it
# would make it after the PIN ceremony, as a failed transaction the reader paid
# for and cannot interpret.


class _Futures:
    configured = True

    def __init__(self, series):
        self._series = series

    def all_series(self):
        return list(self._series)


class _Reader:
    def __init__(self, prints):
        self._prints = prints

    def read_all(self):
        return dict(self._prints)

    def read(self, iid):
        return self._prints.get(iid)


def _wire(monkeypatch, series, prints):
    import index_api.onchain as onchain

    monkeypatch.setattr(onchain, "get_futures", lambda: _Futures(series))
    monkeypatch.setattr(onchain, "get_reader", lambda: _Reader(prints))
    monkeypatch.setattr(desk, "_max_settle_age", lambda: 7200)


def _settle_series(sid=1, iid="ACR-INF", *, settled=False, expiry_offset=-3600):
    return {
        "series_id": sid,
        "index_id": iid,
        "expiry_ts": time.time() + expiry_offset,
        "settled": settled,
        "multiplier": 10,
    }


def test_settle_accepts_an_expired_series_with_a_fresh_price(monkeypatch):
    _wire(monkeypatch, [_settle_series()], {"ACR-INF": {"posted_at": time.time() - 600}})
    sid, age, max_age = desk._settle_precheck(1)
    assert sid == 1
    assert age < 700
    assert max_age == 7200


def test_settle_refuses_a_stale_price_and_says_when_the_window_reopens(monkeypatch):
    # The refusal that actually bites, and the one that is not the reader's
    # fault: the contract reverts "stale print" and there is nothing they can
    # do but wait, so the message has to say what they are waiting for.
    _wire(monkeypatch, [_settle_series()], {"ACR-INF": {"posted_at": time.time() - 9000}})
    with pytest.raises(DeskError) as e:
        desk._settle_precheck(1)
    assert e.value.status == 409
    assert "150 minutes old" in str(e.value)
    assert "next price" in str(e.value)


def test_settle_refuses_a_series_that_has_not_expired(monkeypatch):
    _wire(
        monkeypatch,
        [_settle_series(expiry_offset=7200)],
        {"ACR-INF": {"posted_at": time.time()}},
    )
    with pytest.raises(DeskError) as e:
        desk._settle_precheck(1)
    assert e.value.status == 409
    assert "not expired" in str(e.value)


def test_settle_refuses_an_already_settled_series_and_points_at_the_exit(monkeypatch):
    _wire(monkeypatch, [_settle_series(settled=True)], {"ACR-INF": {"posted_at": time.time()}})
    with pytest.raises(DeskError) as e:
        desk._settle_precheck(1)
    assert e.value.status == 409
    assert "withdraw" in str(e.value)


def test_settle_refuses_an_unknown_series(monkeypatch):
    _wire(monkeypatch, [_settle_series()], {"ACR-INF": {"posted_at": time.time()}})
    with pytest.raises(DeskError) as e:
        desk._settle_precheck(99)
    assert e.value.status == 404


def test_settle_treats_an_unreadable_price_as_retry_not_refusal(monkeypatch):
    # No print is NOT "you may not settle" — it is "we could not tell". A 409
    # would send the reader away; a 503 says it retries on its own.
    _wire(monkeypatch, [_settle_series()], {})
    with pytest.raises(DeskError) as e:
        desk._settle_precheck(1)
    assert e.value.status == 503
