"""What `ChutesSource` does when the upstream row is not the row it expected.

Every case here was a real crash or a real silent distortion, reproduced before
it was fixed. They share one shape: the tape is the boundary between a
third-party API that can change without telling us and an estimator that
publishes a number either way. A row that arrives slightly wrong should cost us
that row, never the window and never the process.

Hermetic by construction — every test passes `rows=` or writes its own file, so
nothing here can skip when a fixture moves.
"""

from __future__ import annotations

import json

from acr_tape import ChutesSource
from acr_tape.chutes_source import _MAX_LATENCY_MS, _UNKNOWN_LATENCY_MS

_GOOD = {
    "date": "2026-10-18",
    "chute_id": "c1",
    "name": "deepseek-ai/DeepSeek-V3",
    "price_per_1k_tokens": 0.01,
    "tokens_total": 1000,
    "average_ttft": 1.106,
}


def _row(**over) -> dict:
    return {**_GOOD, **over}


# -- numeric coercion -------------------------------------------------------


def test_a_string_price_does_not_raise():
    """`not price` is False for "0.01", so `price <= 0` used to raise TypeError.

    Out of the per-row loop, so it took the whole build with it. Upstream already
    string-encodes numerics elsewhere (Taostats `registration_cost`), which is
    why this is a fixture of the real world and not a hypothetical.
    """
    src = ChutesSource(rows=[_row(price_per_1k_tokens="0.01", tokens_total="1000")])
    assert len(src.collect()) == 1


def test_a_junk_price_drops_only_its_own_row():
    rows = [_row(), _row(chute_id="c2", price_per_1k_tokens="n/a")]
    assert len(ChutesSource(rows=rows).collect()) == 1


def test_a_non_finite_price_drops_its_row():
    rows = [_row(), _row(chute_id="c2", price_per_1k_tokens=float("inf"))]
    assert len(ChutesSource(rows=rows).collect()) == 1


# -- latency: the unit, and the tail ---------------------------------------


def test_ttft_is_seconds_not_ms():
    """Pinned deliberately: a silent unit flip moves every inference print.

    Chutes `average_ttft` is in SECONDS — the live p50 is ~1.1, and reading that
    as milliseconds would claim a 1.1ms time-to-first-token, which no model
    achieves. The conversion must multiply, so assert the DIRECTION, not just a
    number a future edit could satisfy by accident.
    """
    src = ChutesSource(rows=[_row(average_ttft=1.106)])
    src.collect()
    (att,) = src.attestations()
    assert att.latency_slo_ms == 1106.0
    assert att.latency_slo_ms > 1.106, "a seconds->ms conversion must scale UP"


def test_an_extreme_ttft_is_clamped():
    """The live max is ~1632s. Unclamped that is a 1,632,652ms SLO, which
    `hedonic` log-centers against REF_LATENCY_MS=250 — one dead chute becomes the
    highest-leverage observation in the window."""
    src = ChutesSource(rows=[_row(average_ttft=1632.652)])
    src.collect()
    (att,) = src.attestations()
    assert att.latency_slo_ms == _MAX_LATENCY_MS


def test_a_missing_ttft_is_imputed_and_counted():
    """The substitution is legitimate; doing it silently was not.

    `_UNKNOWN_LATENCY_MS` equals `hedonic.REF_LATENCY_MS` by construction, so
    such a row lands on the reference seller and contributes zero latency shift.
    That is the honest choice for "no information" — but it has to be countable,
    or the fraction of the tape assuming the reference seller is unknowable.
    """
    rows = [_row(), _row(chute_id="c2", name="other", average_ttft=None)]
    src = ChutesSource(rows=rows)
    src.collect()
    assert src.n_latency_imputed == 1
    imputed = [a for a in src.attestations() if a.latency_slo_ms == _UNKNOWN_LATENCY_MS]
    assert len(imputed) == 1


def test_rebuilding_does_not_double_count_imputations():
    src = ChutesSource(rows=[_row(average_ttft=None)])
    src.collect()
    src._build()
    assert src.n_latency_imputed == 1


# -- file loading -----------------------------------------------------------


def test_one_corrupt_jsonl_line_costs_only_that_line(tmp_path):
    """This path now reads the CUMULATIVE archive, which is append-only and
    grows forever, so a single truncated write must not erase every day before
    it."""
    p = tmp_path / "t.jsonl"
    p.write_text(
        json.dumps(_row())
        + "\n{truncated...\n"
        + json.dumps(_row(chute_id="c2", date="2026-10-19"))
        + "\n"
    )
    assert len(ChutesSource(path=p).collect()) == 2


def test_a_corrupt_json_file_is_empty_not_an_exception(tmp_path):
    p = tmp_path / "t.json"
    p.write_text("{not json")
    assert ChutesSource(path=p).collect() == []


def test_a_jsonl_line_that_is_not_an_object_is_skipped(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_text("[1,2,3]\n" + json.dumps(_row()) + "\n")
    assert len(ChutesSource(path=p).collect()) == 1


def test_a_missing_file_is_empty():
    assert ChutesSource(path="/nonexistent/nope.jsonl").collect() == []
