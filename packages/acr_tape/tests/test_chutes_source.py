"""ChutesSource — the first ACR tape source carrying non-simulated prices.

Closes the gap that `tape_source: sim` represented: until this source existed,
every number the estimator had ever produced came from `acr_sim`, and the README
said so. These tests pin the three things that make the real tape usable and the
two that make it dangerous.

The dangerous pair is why the assertions below are blunt about it:

* the funding graph is a star (one aggregate buyer), so `clean()` destroys the
  window unless `single_venue=True` — and `requires_single_venue` exists so a
  caller cannot forget;
* the upstream rows are daily, so every event in a day would share one timestamp
  and `pipeline._bar_series` would see a `1e-9` bar duration. The source spreads
  sellers deterministically across the day instead.

All of it runs off the committed fixture. No network, no API key.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from acr_core import ModelClass, Service
from acr_tape import ChutesSource, TapeSource, model_class_for

FIXTURE = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "bt" / "chutes_usage.json"

pytestmark = pytest.mark.skipif(
    not FIXTURE.exists(),
    reason="no Bittensor fixture (run `uv run python scripts/bt_snapshot.py --capture-fixture`)",
)


@pytest.fixture(scope="module")
def src() -> ChutesSource:
    return ChutesSource()


def test_is_a_tape_source(src):
    assert isinstance(src, TapeSource)


def test_yields_inference_events_in_ts_order(src):
    events = src.collect()
    assert events, "fixture produced no events"
    assert all(e.service is Service.INFERENCE for e in events)
    ts = [e.ts for e in events]
    assert ts == sorted(ts), "stream() must yield in economic-timestamp order"


def test_prices_are_positive_and_plausible_for_inference(src):
    """Realized Chutes prices land near $0.0003/1k tokens, not near ACR-INF's
    nominal 0.50 — which is the 1250x gap `anchors/GAP.md` already declares."""
    px = [e.price for e in src.collect()]
    assert all(p > 0 for p in px)
    mid = sorted(px)[len(px) // 2]
    # Two decades either side of the measured median: a loose band on purpose,
    # since this tracks a live market, but tight enough to catch a unit error
    # (a $/1M-token value here would be ~1000x out).
    assert 1e-6 < mid < 1e-1, f"median {mid} is outside any plausible $/1k-token range"


def test_sizes_are_token_thousands(src):
    """`size` is tokens/1000 so that price*size reconstructs billed USD."""
    rows = json.loads(FIXTURE.read_text())
    by_id = {f"chutes:{r['date']}:{str(r['chute_id'])[:12]}": r for r in rows}
    checked = 0
    for e in src.collect():
        r = by_id.get(e.event_id)
        if r is None:
            continue
        assert e.notional == pytest.approx(r["usd_amount"], rel=1e-9)
        checked += 1
    assert checked > 0, "no event matched a fixture row"


def test_declares_that_it_needs_single_venue_estimation(src):
    """The star-graph problem is advertised, not left to documentation."""
    assert src.requires_single_venue is True
    assert src.native_step_s == 86_400.0


def test_one_aggregate_buyer_is_the_literal_shape(src):
    buyers = {e.buyer for e in src.collect()}
    assert buyers == {"chutes:aggregate"}


def test_timestamps_within_a_day_are_spread_not_identical(src):
    """A `1e-9` bar duration makes the deconvolution span the whole sample.

    Guarding the property rather than the implementation: within any day that
    has more than one seller, the timestamps must differ.
    """
    events = src.collect()
    by_day: dict[int, list[float]] = {}
    for e in events:
        by_day.setdefault(int(e.ts // 86_400), []).append(e.ts)
    multi = [v for v in by_day.values() if len(v) > 1]
    assert multi, "fixture has no day with more than one event"
    for day_ts in multi:
        assert len(set(day_ts)) > 1, "all events in a day share one timestamp"


def test_timestamps_are_reproducible_across_instances():
    """`hash()` is salted per process; the tape must not be."""
    a = [e.ts for e in ChutesSource().collect()]
    b = [e.ts for e in ChutesSource().collect()]
    assert a == b


def test_attestations_cover_every_seller_and_carry_real_latency(src):
    sellers = {e.seller for e in src.collect()}
    att = src.attestations()
    assert {a.seller for a in att} == sellers
    assert all(a.latency_slo_ms > 0 for a in att)
    # average_ttft is a real measurement, so at least one seller must differ
    # from the 250ms fallback or we are not reading it at all.
    assert any(a.latency_slo_ms != 250.0 for a in att)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Qwen/Qwen3-235B-A22B-Thinking-2507-TEE", ModelClass.FRONTIER),
        ("Qwen/Qwen3.8-2.4T-A95B", ModelClass.FRONTIER),  # 2.4T -> 2400B
        ("Qwen/Qwen3.8-27B-TEE", ModelClass.MID),
        ("Qwen/Qwen3-4B", ModelClass.SMALL),
        ("some-model-with-no-size", ModelClass.OPEN),
        ("", ModelClass.OPEN),
    ],
)
def test_model_class_is_read_from_the_name(name, expected):
    assert model_class_for(name) is expected


def test_mixture_of_experts_uses_total_not_active_params():
    """`397B-A17B` carries total and active params; total is what it costs to serve."""
    assert model_class_for("Qwen/Qwen3.5-397B-A17B-TEE") is ModelClass.FRONTIER


def test_missing_file_is_an_empty_tape_not_a_crash(tmp_path):
    """A source whose input is absent degrades the way ArcSource does."""
    src = ChutesSource(path=tmp_path / "nope.json")
    assert src.collect() == []
    assert src.attestations() == []


def test_rows_can_be_injected_without_touching_disk():
    src = ChutesSource(
        rows=[
            {
                "date": "2026-10-29",
                "chute_id": "abc123",
                "name": "test/Model-70B",
                "usd_amount": 10.0,
                "tokens_total": 1_000_000,
                "price_per_1k_tokens": 0.01,
                "average_ttft": 1.5,
            }
        ]
    )
    (e,) = src.collect()
    assert e.price == 0.01
    assert e.size == 1000.0
    assert e.model_class is ModelClass.MID
    (a,) = src.attestations()
    assert a.latency_slo_ms == pytest.approx(1500.0)
