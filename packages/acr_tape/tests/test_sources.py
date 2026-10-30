"""acr_tape source tests."""

from __future__ import annotations

import json
import types

from acr_core import INDEX_REGISTRY, Service, index_for_service
from acr_sim import SimConfig
from acr_tape import ArcSource, ReceiptSource, SimSource, TapeSource
from acr_tape.arc_source import EIP3009_AUTHORIZATION_ABI, USDC_TRANSFER_ABI


def _log(args: dict, block_number: int = 100):
    """An EventData-shaped entry, as web3's event.get_logs() returns."""
    return {
        "args": args,
        "transactionHash": bytes.fromhex("ab" * 32),
        "logIndex": 3,
        "blockNumber": block_number,
    }


def test_sim_source_streams_events_and_attestations():
    src = SimSource(config=SimConfig(seed=2, horizon=3600.0, events_per_service=400))
    assert isinstance(src, TapeSource)
    events = src.collect()
    assert len(events) > 0
    # Sorted by economic timestamp.
    assert all(events[i].ts <= events[i + 1].ts for i in range(len(events) - 1))
    assert len(src.attestations()) > 0


def test_sim_source_range_filters():
    src = SimSource(config=SimConfig(seed=2, horizon=3600.0, events_per_service=400))
    window = src.range(1000.0, 2000.0)
    assert all(1000.0 <= e.ts < 2000.0 for e in window)


def test_arc_source_degrades_gracefully_without_connection():
    # No RPC / address -> empty tape, no exception. This is the "thin testnet" path.
    src = ArcSource(rpc_url="http://127.0.0.1:1", x402_address=None)
    assert src.collect() == []
    assert src.attestations() == []


def _write_receipts(path, rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_receipt_source_empty_when_unset_or_missing(tmp_path):
    # No path configured -> empty tape (never raises).
    assert ReceiptSource(log_path="").collect() == []
    # Configured path that doesn't exist yet -> empty tape.
    assert ReceiptSource(log_path=str(tmp_path / "nope.jsonl")).collect() == []


def test_receipt_source_decodes_real_settlements_honestly(tmp_path):
    p = tmp_path / "x402_receipts.jsonl"
    _write_receipts(
        p,
        [
            {"payer": "0xbuyer", "amount_usdc": 0.0001, "tx_ref": "uuid-2",
             "network": "eip155:5042002", "scheme": "exact", "settled_at": 200.0,
             "resource": "/curve/ACR-GPU"},
            {"payer": "0xbuyer", "amount_usdc": 0.0001, "tx_ref": "uuid-1",
             "network": "eip155:5042002", "scheme": "exact", "settled_at": 100.0,
             "resource": "/vol/ACR-INF"},
            # dev/sim rows must be ignored — no fabricated economic signal.
            {"payer": "0xbuyer", "amount_usdc": 0.0001, "tx_ref": "dev-1",
             "scheme": "dev", "settled_at": 150.0, "resource": "/prints"},
        ],
    )
    src = ReceiptSource(log_path=str(p), seller="0xseller")
    assert isinstance(src, TapeSource)
    events = src.collect()

    # Only the two real settlements; dev row dropped.
    assert len(events) == 2
    # Sorted by economic timestamp and normalized to seconds-from-first (the store
    # windows from 0) — uuid-1 @100 → 0.0, uuid-2 @200 → 100.0.
    assert [e.ts for e in events] == [0.0, 100.0]
    # Service resolved precisely from the paid resource path.
    assert events[0].service == Service.INFERENCE  # /vol/ACR-INF
    assert events[1].service == Service.GPU  # /curve/ACR-GPU
    # HONEST: price == the index reference level (no fabricated dispersion),
    # size derived so price*size recovers the settled notional exactly.
    for e in events:
        ref = index_for_service(e.service).reference_level
        assert e.price == ref
        assert abs(e.price * e.size - 0.0001) < 1e-12
        assert e.seller == "0xseller" and e.buyer == "0xbuyer"
    # No attestations of its own (the store merges the on-chain registry).
    assert src.attestations() == []


def test_receipt_source_unknown_resource_falls_back_to_default(tmp_path):
    p = tmp_path / "r.jsonl"
    _write_receipts(
        p,
        [{"payer": "0xb", "amount_usdc": 0.0001, "tx_ref": "u", "scheme": "exact",
          "settled_at": 10.0, "resource": "/prints"}],  # not index-specific
    )
    src = ReceiptSource(log_path=str(p), default_service=Service.DATA)
    (e,) = src.collect()
    assert e.service == Service.DATA
    assert e.price == INDEX_REGISTRY["ACR-DATA"].reference_level


def test_default_abi_is_real_transfer_and_marker_exists():
    assert USDC_TRANSFER_ABI["name"] == "Transfer"
    names = [i["name"] for i in USDC_TRANSFER_ABI["inputs"]]
    assert names == ["from", "to", "value"]
    # The real EIP-3009 authorization marker (no value/size/service).
    assert EIP3009_AUTHORIZATION_ABI["name"] == "AuthorizationUsed"
    assert [i["name"] for i in EIP3009_AUTHORIZATION_ABI["inputs"]] == ["authorizer", "nonce"]


def test_decode_transfer_to_tapeevent():
    src = ArcSource(x402_address="0x3600000000000000000000000000000000000000")
    args = {"from": "0xBuyer", "to": "0xSeller", "value": 5_000_000}  # 5 USDC (6 decimals)
    ev = src._decode_log(_log(args))
    assert ev is not None
    assert ev.buyer == "0xBuyer" and ev.seller == "0xSeller"
    ref = index_for_service(Service.INFERENCE).reference_level
    assert abs(ev.notional - 5.0) < 1e-9  # value recovered exactly
    assert abs(ev.price - ref) < 1e-12  # price = reference level (no fabricated signal)
    assert ev.ts == 100.0  # block number at decode time (remapped in stream())


def test_config_driven_event_override():
    src = ArcSource(
        x402_address="0xabc",
        event_abi={"name": "Paid", "type": "event", "inputs": []},
        field_map={"buyer": "payer", "seller": "merchant", "value": "amount"},
        service_resolver=lambda a: Service.GPU,
    )
    args = {"payer": "0xP", "merchant": "0xM", "amount": 2_000_000}
    ev = src._decode_log(_log(args))
    assert ev is not None
    assert ev.buyer == "0xP" and ev.seller == "0xM" and ev.service == Service.GPU
    assert abs(ev.notional - 2.0) < 1e-9


def test_arc_defaults_from_settings():
    src = ArcSource()
    # Defaults to the Arc USDC system contract and the configured RPC.
    assert src.x402_address == "0x3600000000000000000000000000000000000000"
    assert src.event_name == "Transfer"


def _ev(ts: float, seller: str = "0xS"):
    from acr_core import TapeEvent

    return TapeEvent(
        event_id=f"e{ts}", ts=ts, service=Service.INFERENCE, seller=seller,
        buyer="0xB", price=1.0, size=1.0,
    )


def test_interpolate_times_maps_blocks_to_seconds_from_zero():
    # ts at decode time is a block NUMBER; interpolate to seconds from the
    # earliest block using only the two endpoint block timestamps.
    events = [_ev(100), _ev(102), _ev(101)]
    out = ArcSource._interpolate_times(events, 100, 1_700_000_000, 102, 1_700_000_090)
    # block 100→0s, 101→45s (linear midpoint), 102→90s; sorted, earliest at 0.
    assert [e.ts for e in out] == [0.0, 45.0, 90.0]


def test_interpolate_times_single_block_all_zero():
    out = ArcSource._interpolate_times([_ev(100), _ev(100)], 100, 1_700_000_000, 100, 1_700_000_000)
    assert [e.ts for e in out] == [0.0, 0.0]


def test_interpolate_times_empty_is_empty():
    assert ArcSource._interpolate_times([], 0, 0, 0, 0) == []


def test_resolve_from_block_bounds_to_lookback():
    src = ArcSource(x402_address="0xabc", lookback_blocks=5_000)
    w3 = types.SimpleNamespace(eth=types.SimpleNamespace(block_number=1_000_000))
    assert src._resolve_from_block(w3) == 995_000
    # An explicit from_block is honoured verbatim.
    src2 = ArcSource(x402_address="0xabc", from_block=42)
    assert src2._resolve_from_block(w3) == 42


def test_block_time_cache_fetches_once_per_block():
    calls = {"n": 0}

    def get_block(bn):
        calls["n"] += 1
        return {"timestamp": 1_700_000_000 + bn}

    w3 = types.SimpleNamespace(eth=types.SimpleNamespace(get_block=get_block))
    src = ArcSource(x402_address="0xabc")
    assert src._block_time(w3, 7) == 1_700_000_007
    assert src._block_time(w3, 7) == 1_700_000_007  # cached
    assert calls["n"] == 1


def test_by_seller_resolver_spreads_all_services():
    from acr_tape.arc_source import by_seller_service_resolver

    seen = {by_seller_service_resolver({"to": f"0x{i:040x}"}) for i in range(60)}
    assert seen == {Service.INFERENCE, Service.GPU, Service.DATA}
