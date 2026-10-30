"""`anchors.fetch_lium` — the network path that prices ACR-GPU.

This is the function that turns Bittensor subnet 51 into anchor rows, and until
now it had no test at all: every SN51 assertion in the suite read the committed
fixture, so the fetch itself — retries, provenance digest, and what it does with
a payload that is not what it expected — was unexercised.

The case that matters most is the quiet one. `fetch_lium` used to coerce any
non-list payload to `[]` and return it **with a valid sha256**, so an upstream
that started wrapping its array in an envelope would produce a provenance-clean
anchor containing zero Lium rows. `--fetch` would write it, and only the separate
`--check` would later notice. A fetch that cannot see its rows has to fail, not
succeed emptily.

No network: `urlopen` is stubbed, following `test_graph_client.py`.
"""

from __future__ import annotations

import hashlib
import json
import sys
import urllib.error
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

import anchors  # noqa: E402

_ROWS = [
    {"id": "e1", "machine_name": "NVIDIA H100 80GB HBM3", "price_per_gpu": 3.0},
    {"id": "e2", "machine_name": "NVIDIA H100 80GB HBM3", "price_per_gpu": 2.5},
]


class _Resp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a) -> bool:
        return False


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """The retry backoff is 2s + 4s; nothing here needs to wait for it."""
    monkeypatch.setattr(anchors.time, "sleep", lambda *_: None)


def _stub(monkeypatch, payload, calls: list):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def fake(req, timeout=0):
        calls.append(getattr(req, "full_url", str(req)))
        return _Resp(body)

    monkeypatch.setattr(anchors.urllib.request, "urlopen", fake)
    return body


def test_a_good_payload_returns_rows_and_a_digest_of_the_bytes(monkeypatch):
    calls: list = []
    body = _stub(monkeypatch, _ROWS, calls)
    rows, sha = anchors.fetch_lium()

    assert rows == _ROWS
    assert len(calls) == 1
    # the provenance contract: sha256 of the RAW bytes, not of a reserialization
    assert sha == "0x" + hashlib.sha256(body).hexdigest()


def test_it_reads_the_declared_endpoint(monkeypatch):
    calls: list = []
    _stub(monkeypatch, _ROWS, calls)
    anchors.fetch_lium()
    assert calls == [anchors.LIUM_URL]


def test_an_enveloped_payload_fails_instead_of_returning_zero_rows(monkeypatch):
    """The silent-degradation bug, pinned.

    A dict is a plausible shape for an API to migrate to, and it used to produce
    an empty row set with a perfectly valid digest.
    """
    calls: list = []
    _stub(monkeypatch, {"executors": _ROWS, "total": 2}, calls)
    with pytest.raises(RuntimeError, match="failed after"):
        anchors.fetch_lium()
    assert len(calls) == 3, "a reshaped payload belongs in the retry, like any other fault"


def test_a_bare_object_also_fails(monkeypatch):
    calls: list = []
    _stub(monkeypatch, {}, calls)
    with pytest.raises(RuntimeError):
        anchors.fetch_lium()


def test_malformed_json_fails(monkeypatch):
    calls: list = []
    _stub(monkeypatch, b"{not json", calls)
    with pytest.raises(RuntimeError):
        anchors.fetch_lium()


def test_it_retries_a_transport_error_then_succeeds(monkeypatch):
    calls: list = []
    body = json.dumps(_ROWS).encode()

    def fake(req, timeout=0):
        calls.append(1)
        if len(calls) == 1:
            raise urllib.error.URLError("connection reset")
        return _Resp(body)

    monkeypatch.setattr(anchors.urllib.request, "urlopen", fake)
    rows, _ = anchors.fetch_lium()
    assert rows == _ROWS and len(calls) == 2


def test_it_gives_up_after_the_declared_attempts(monkeypatch):
    calls: list = []

    def fake(req, timeout=0):
        calls.append(1)
        raise urllib.error.URLError("down")

    monkeypatch.setattr(anchors.urllib.request, "urlopen", fake)
    with pytest.raises(RuntimeError, match="failed after 3 attempts"):
        anchors.fetch_lium()
    assert len(calls) == 3


# ── what `--fetch` actually writes ──────────────────────────────────────────
#
# The readers above and in `test_lium_anchor.py` exercise the floor against the
# committed anchor. This is the write path: `build_curated_anchor` is the only
# place a thin class is ever labelled, and it had no test, so the label could
# have been absent from every future anchor while every reader stayed green.


def _lium(monkeypatch, executors: list[dict]) -> dict:
    """Build the real ACR-GPU anchor off a synthetic executor list, no network."""
    monkeypatch.setattr(anchors, "fetch_lium", lambda *a, **k: (executors, "0xsha"))
    return anchors.build_curated_anchor("ACR-GPU")


def _ex(machine: str, price: float, i: int) -> dict:
    return {"id": f"e{i}", "machine_name": machine, "tier": "secure", "price_per_gpu": price}


_FAT = (
    [_ex("NVIDIA H100 80GB HBM3", 3.0, i) for i in range(4)]
    + [_ex("NVIDIA H200", 3.9, 10 + i) for i in range(4)]
    + [_ex("NVIDIA H200 NVL", 4.0, 20 + i) for i in range(4)]
    + [_ex("NVIDIA B200", 6.1, 30 + i) for i in range(4)]
    + [_ex("NVIDIA B300 SXM6 PC", 8.9, 40 + i) for i in range(4)]
    + [_ex("NVIDIA B300 SXM6 AC", 8.0, 50 + i) for i in range(4)]
)


def test_a_well_populated_fetch_marks_nothing_thin(monkeypatch):
    doc = _lium(monkeypatch, _FAT)
    assert doc["aggregate"]["n"] == 10
    assert doc["provenance"]["n_accelerator_classes_thin"] == 0
    assert doc["provenance"]["thin_classes"] == []
    assert all("thin" not in (r.get("quality") or {}) for r in doc["rows"])
    assert anchors._check_thin("ACR-GPU", doc) == []


def test_a_one_executor_class_is_labelled_in_the_row_and_the_provenance(monkeypatch):
    """One seller in Helsinki was the entire `NVIDIA H100 80GB HBM3` market on
    2026-10-30. The row still prices — see `rule.lium_thin_note` — but it says
    so, which is what was missing."""
    thin_only = [e for e in _FAT if e["machine_name"] != "NVIDIA H100 80GB HBM3"]
    thin_only.append(_ex("NVIDIA H100 80GB HBM3", 3.0, 99))
    doc = _lium(monkeypatch, thin_only)

    row = next(r for r in doc["rows"] if r["id"] == "lium/h100-80gb-hbm3")
    assert row["quality"]["thin"] is True
    assert row["quality"]["min_executors"] == 2
    assert row["raw"]["n_executors"] == 1
    # Priced, not dropped: the basket stays at 10 of 10 and so stays `usable`.
    assert row["price_usd_per_unit"] == 3.0 / 3600.0
    assert doc["aggregate"]["n"] == 10
    assert doc["provenance"]["thin_classes"] == ["lium/h100-80gb-hbm3"]
    assert doc["provenance"]["min_executors"] == 2


def test_a_fetch_that_thins_past_the_ceiling_is_caught_by_check(monkeypatch):
    """Three of six classes on one executor. `--check` is the thing that has to
    notice, because `aggregate.n` still reads 10 of 10."""
    fat = [
        e
        for e in _FAT
        if e["machine_name"] not in {"NVIDIA H100 80GB HBM3", "NVIDIA H200", "NVIDIA B200"}
    ]
    fat += [
        _ex("NVIDIA H100 80GB HBM3", 3.0, 101),
        _ex("NVIDIA H200", 3.9, 102),
        _ex("NVIDIA B200", 6.1, 103),
    ]
    doc = _lium(monkeypatch, fat)
    assert doc["aggregate"]["n"] == 10
    assert doc["provenance"]["n_accelerator_classes_thin"] == 3
    fails = anchors._check_thin("ACR-GPU", doc)
    assert len(fails) == 1 and "above the declared ceiling of 2" in fails[0]


def test_a_class_with_no_executors_is_missing_not_thin(monkeypatch):
    """The two states are different and must not be conflated: `missing` means
    nothing priced, `thin` means too few did."""
    doc = _lium(monkeypatch, [e for e in _FAT if e["machine_name"] != "NVIDIA H200 NVL"])
    assert [m["id"] for m in doc["missing"]] == ["lium/h200-nvl"]
    assert doc["provenance"]["thin_classes"] == []
