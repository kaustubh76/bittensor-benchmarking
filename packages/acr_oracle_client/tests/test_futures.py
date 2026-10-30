"""acr_oracle_client futures tests (offline / pure-helper path).

The web3 reads/writes are ``# pragma: no cover - live chain``; these cover the
decode + series-selection logic and the WAD/USDC scaling that must stay in lock
step with the ``ACRFutures`` contract (and its Foundry parity test).
"""

from __future__ import annotations

from acr_oracle_client import (
    FuturesClient,
    bytes32_to_index_id,
    descale_position,
    descale_series,
    index_id_to_bytes32,
    select_series_for_index,
)

WAD = 10**18


def test_bytes32_index_id_roundtrip():
    assert bytes32_to_index_id(index_id_to_bytes32("ACR-INF")) == "ACR-INF"


def test_descale_series_decodes_units():
    raw = (index_id_to_bytes32("ACR-GPU"), 1_700_000_000, 1000, "0xMaker", True, False, 0)
    s = descale_series(3, raw)
    assert s["series_id"] == 3
    assert s["index_id"] == "ACR-GPU"
    assert s["multiplier"] == 1000
    assert s["settled"] is False
    assert s["settlement_price"] == 0.0


def test_descale_series_settlement_price_wad():
    raw = (index_id_to_bytes32("ACR-INF"), 1, 1000, "0xM", True, True, 6 * 10**17)
    assert descale_series(0, raw)["settlement_price"] == 0.6


def test_descale_position_applies_multiplier():
    # contracts -2, avg 0.40, realized -0.30 WAD value*contracts (the parity
    # vector's end state) → realized USDC = -0.30 * multiplier.
    raw = (-2 * WAD, 4 * 10**17, -3 * 10**17)
    p = descale_position(raw, multiplier=1000)
    assert p["contracts"] == -2.0
    assert abs(p["avg_price"] - 0.40) < 1e-12
    assert abs(p["realized_pnl_usdc"] - (-300.0)) < 1e-9  # -0.30 * 1000


def test_select_series_prefers_latest_unsettled():
    series = [
        {"series_id": 0, "index_id": "ACR-INF", "exists": True, "settled": True},
        {"series_id": 1, "index_id": "ACR-INF", "exists": True, "settled": False},
        {"series_id": 2, "index_id": "ACR-GPU", "exists": True, "settled": False},
    ]
    assert select_series_for_index(series, "ACR-INF")["series_id"] == 1
    assert select_series_for_index(series, "ACR-GPU")["series_id"] == 2
    assert select_series_for_index(series, "ACR-DATA") is None


def test_select_series_falls_back_to_settled():
    series = [{"series_id": 5, "index_id": "ACR-INF", "exists": True, "settled": True}]
    assert select_series_for_index(series, "ACR-INF")["series_id"] == 5


def test_client_offline_reads_are_safe():
    fc = FuturesClient(futures_address=None)
    assert fc.configured is False
    assert fc.read_all_series() == []
    assert fc.read_desk("ACR-INF") is None
    assert fc.position_of(0, "0xabc") is None
    assert fc.collateral_of(0, "0xabc") is None
    assert fc.recent_trades() == []
    assert fc.can_write() is False


def test_range_errors_are_told_apart_from_throttling():
    """Narrowing cures a too-wide range and does nothing for a throttle, so the
    two must not be confused: measured once, treating a 429 as "too wide" turned
    a four-page 7.9h walk into 1264 blocks.

    Arc MAINNET refuses in TWO ways (measured 2026-09-28) and only the first was
    recognised, which made an over-wide window look transient — the same width
    was re-asked twice before the ladder narrowed.
    """
    from acr_oracle_client.futures import _is_range_error

    # -32602: a RESULT cap, and it names the range to retry with.
    assert _is_range_error(Exception(
        "request exceeded max allowed range: query exceeds max results 2000, "
        "retry with the range 23142054-23142123"
    ))
    # -32012: a BLOCK cap. Address-filtered, 5000 answers and 10000 does not.
    assert _is_range_error(Exception("{'code': -32012, 'message': 'requested range too large'}"))
    assert _is_range_error(Exception("requested range too large"))
    # Arc testnet's older shapes still count.
    assert _is_range_error(Exception("413 Payload Too Large"))
    # Throttling is NOT a range problem: same range, after a backoff.
    assert not _is_range_error(Exception("429 Too Many Requests"))
    assert not _is_range_error(Exception("HTTP 429"))
