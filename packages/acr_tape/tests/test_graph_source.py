"""GraphSource tests — the indexed settlement tape.

Exercised against a recorded GraphQL payload rather than a live endpoint, so the
suite stays hermetic and the decode is pinned independently of whether a
subgraph happens to be deployed.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from acr_core import ModelClass, Service
from acr_tape import GraphSource

WAD = 10**18


def _settlement(
    sid: str, index: str, seller: str, payer: str, unit_price: float, quantity: float, ts: int
) -> dict[str, Any]:
    return {
        "id": sid,
        "index": index,
        "payer": {"id": payer},
        "seller": {"id": seller},
        "amount": str(int(unit_price * quantity * 1_000_000)),
        "quantity": str(int(quantity * WAD)),
        "unitPrice": str(int(unit_price * WAD)),
        "slippageBp": "100",
        "settledAt": str(ts),
        "synthetic": True,
    }


class _Recorded(GraphSource):
    """A GraphSource whose transport replays a recorded payload."""

    def __init__(self, settlements=None, attestations=None, fail: bool = False, errors=None):
        super().__init__(url="https://example.invalid/subgraph", api_key="")
        self._settlements = settlements or []
        self._attestations = attestations or []
        self._fail = fail
        self._errors = errors
        self.calls: list[dict] = []

    def _query(self, query: str, variables: dict) -> dict:  # type: ignore[override]
        self.calls.append(dict(variables))
        if self._fail or self._errors:
            return {}
        skip = int(variables["skip"])
        settlements = "settlements(" in query
        rows = self._settlements if settlements else self._attestations
        page = rows[skip : skip + int(variables["first"])]
        # Attestations are read through `sellers { latestAttestation }`, one
        # current record per seller — not the `attestations` event log.
        return {"settlements" if settlements else "sellers": page}


def test_settlements_decode_into_tape_events():
    src = _Recorded(
        settlements=[
            _settlement("0x01", "ACR-INF", "0xseller", "0xpayer", 0.52, 0.01, 1_785_000_000),
        ]
    )
    events = list(src.stream())
    assert len(events) == 1
    e = events[0]
    assert e.service is Service.INFERENCE
    assert e.seller == "0xseller"
    assert e.buyer == "0xpayer"
    assert e.price == pytest.approx(0.52)
    assert e.size == pytest.approx(0.01)
    # notional is the derived product, and it is what the estimator weights by.
    assert e.notional == pytest.approx(0.0052)


def test_timestamps_are_normalized_to_the_first_settlement():
    """The store windows in seconds from 0, so an absolute epoch would misalign
    every window — the same reason ArcSource interpolates and ReceiptSource
    subtracts its own t0."""
    src = _Recorded(
        settlements=[
            _settlement("0x01", "ACR-INF", "0xs", "0xp", 0.5, 0.01, 1_785_000_000),
            _settlement("0x02", "ACR-INF", "0xs", "0xp", 0.5, 0.01, 1_785_000_600),
        ]
    )
    events = sorted(src.stream(), key=lambda e: e.ts)
    assert events[0].ts == 0.0
    assert events[1].ts == 600.0
    # The absolute time survives on settled_ts, so nothing is lost.
    assert events[0].settled_ts == 1_785_000_000


def test_sellers_keep_their_own_unit_prices():
    """The property the whole subgraph exists to deliver. If this collapses to
    one price, TCA has nothing to measure."""
    src = _Recorded(
        settlements=[
            _settlement("0x01", "ACR-INF", "0xdear", "0xp", 0.60, 0.01, 1_785_000_000),
            _settlement("0x02", "ACR-INF", "0xcheap", "0xp", 0.50, 0.01, 1_785_000_060),
        ]
    )
    prices = {e.seller: e.price for e in src.stream()}
    assert prices["0xdear"] == pytest.approx(0.60)
    assert prices["0xcheap"] == pytest.approx(0.50)


def test_an_unknown_index_is_skipped_not_guessed():
    src = _Recorded(
        settlements=[
            _settlement("0x01", "ACR-NOPE", "0xs", "0xp", 0.5, 0.01, 1_785_000_000),
            _settlement("0x02", "ACR-GPU", "0xs", "0xp", 0.011, 0.5, 1_785_000_060),
        ]
    )
    events = list(src.stream())
    assert [e.service for e in events] == [Service.GPU]


@pytest.mark.parametrize(
    "bad",
    [
        {"quantity": "0"},          # a zero quantity has no unit price
        {"unitPrice": "0"},         # nor a zero price
        {"settledAt": "0"},         # nor an unstamped settlement
        {"quantity": "not-a-int"},  # nor a malformed row
    ],
)
def test_unusable_rows_are_dropped_rather_than_failing_the_window(bad):
    row = _settlement("0x01", "ACR-INF", "0xs", "0xp", 0.5, 0.01, 1_785_000_000)
    row.update(bad)
    good = _settlement("0x02", "ACR-INF", "0xs", "0xp", 0.5, 0.01, 1_785_000_060)
    assert len(list(_Recorded(settlements=[row, good]).stream())) == 1


def test_an_unreachable_subgraph_yields_an_empty_tape():
    """Degrade, never raise — the API must not 500 because an indexer is down."""
    assert list(_Recorded(settlements=[_settlement(
        "0x01", "ACR-INF", "0xs", "0xp", 0.5, 0.01, 1_785_000_000)], fail=True).stream()) == []


def test_an_unset_url_is_disabled_not_broken():
    src = GraphSource(url="", api_key="")
    assert list(src.stream()) == []
    assert src.attestations() == []


def test_attestations_decode_with_the_registrys_own_enum_codes():
    src = _Recorded(
        attestations=[
            {
                "id": "0xseller",
                "latestAttestation": {
                    "id": "0xatt",
                    "service": 1,
                    "modelClass": 0,
                    "latencySloMs": "2500",
                    "schemaId": "0xdeadbeef",
                    "timestamp": "1785000000",
                },
            }
        ]
    )
    (a,) = src.attestations()
    assert a.service is Service.GPU
    assert a.model_class is ModelClass.FRONTIER
    assert a.latency_slo_ms == 2500.0
    assert a.schema_id == "0xdeadbeef"


def test_an_unknown_enum_code_defaults_the_same_way_the_chain_decode_does():
    src = _Recorded(
        attestations=[
            {
                "id": "0xs",
                "latestAttestation": {
                    "id": "0xatt", "service": 99, "modelClass": 99,
                    "latencySloMs": "100", "schemaId": "0x", "timestamp": "1",
                },
            }
        ]
    )
    (a,) = src.attestations()
    assert a.service is Service.INFERENCE
    assert a.model_class is ModelClass.OPEN


def test_paging_walks_past_the_first_page():
    rows = [
        _settlement(f"0x{i:04x}", "ACR-INF", "0xs", "0xp", 0.5, 0.01, 1_785_000_000 + i)
        for i in range(1001)
    ]
    src = _Recorded(settlements=rows)
    assert len(list(src.stream())) == 1001
    assert [c["skip"] for c in src.calls] == [0, 1000]


def test_the_query_is_valid_json_and_asks_only_for_benchmarked_rows():
    """An unbenchmarked settlement has no slippage to contribute; letting one in
    would put a row with no arrival price into a tape that is only meaningful
    relative to one."""
    from acr_tape.graph_source import _SETTLEMENTS_QUERY

    assert "benchmarked: true" in _SETTLEMENTS_QUERY
    json.dumps({"query": _SETTLEMENTS_QUERY})  # must survive transport encoding


def test_one_current_record_per_seller_not_one_per_posting():
    """Parity with ``RegistryClient.all_attestations()``, which walks
    ``sellerAt(i)`` and reads the seller's CURRENT record.

    Reading the `attestations` event log instead returned every historical
    posting — on the live registry, 12 rows for 6 sellers. A seller that had
    re-attested then entered the hedonic feature matrix once per re-attestation,
    weighted several times and mixed with its own superseded metadata.
    """
    src = _Recorded(
        attestations=[
            {
                "id": "0xseller1",
                "latestAttestation": {
                    "id": "0xatt-new", "service": 0, "modelClass": 1,
                    "latencySloMs": "250", "schemaId": "0x", "timestamp": "2000",
                },
            },
            # A seller seen only through settlements has never attested. It must
            # be skipped, not decoded into a default-valued attestation that the
            # hedonic stage would treat as real quality metadata.
            {"id": "0xseller2", "latestAttestation": None},
        ]
    )
    out = src.attestations()
    assert len(out) == 1
    assert out[0].seller == "0xseller1"
    assert out[0].ts == 2000.0
    assert len({a.seller for a in out}) == len(out)
