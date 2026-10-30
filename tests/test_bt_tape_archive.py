"""The cumulative Bittensor archive, and the two ways it could quietly fail.

`scripts/bt_snapshot.py` has a forward-polling mode which is the only way either
subnet grows a real time series: Chutes `/invocations/usage` retains about twelve
days, so anything older exists only if we kept it. That makes this archive the
one artifact in the repo where a bug is **irreversible** — a gap wider than the
retention window is a permanent hole in the history of a product whose entire
purpose is a continuous series.

Three bugs lived here.

**It wrote where nothing could survive.** `TAPE_DIR` was `data/bt_tape`, and
`data/` is in BOTH `.gitignore` and `.dockerignore`, on a host with no persistent
disk. Every observation reached neither the repo nor the image. The x402 receipt
archive had already hit this exact wall and moved under `services/` for exactly
this reason; `test_archive_path_survives_a_deploy` is here so the lesson stays
learned instead of being re-learned a third time.

**It double-counted.** Both feeds overlap on every call — usage returns a rolling
window, Lium returns the whole fleet — so a plain append duplicated every row
after the first run.

**A truncated read was not a retry.** `http.client.IncompleteRead` is not an
`OSError`, so `fetch`'s three attempts never saw it — on the 21 MB endpoint, the
one likeliest to truncate. Measured live, and it matters more now that this
capture runs unattended on a schedule. See the last block in this file.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

import bt_snapshot  # noqa: E402

_USAGE = [
    {
        "date": "2026-10-18",
        "chute_id": "c1",
        "name": "deepseek-ai/DeepSeek-V3",
        "price_per_1k_tokens": 0.01,
        "tokens_total": 1000,
    },
    {
        "date": "2026-10-19",
        "chute_id": "c1",
        "name": "deepseek-ai/DeepSeek-V3",
        "price_per_1k_tokens": 0.02,
        "tokens_total": 2000,
    },
]
_LIUM = [{"id": "e1", "price_per_gpu": 3.0}, {"id": "e2", "price_per_gpu": 2.0}]


def _lines(p: Path) -> list[dict]:
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


# -- the structural guard ---------------------------------------------------


def test_archive_path_survives_a_deploy():
    """The archive must reach both the repo and the image.

    Asserted against the ignore files themselves rather than against a hardcoded
    path, so moving the archive is allowed and moving it back under an ignored
    directory is not.
    """
    rel = bt_snapshot.TAPE_DIR.relative_to(_ROOT)
    top = rel.parts[0]
    for name in (".gitignore", ".dockerignore"):
        patterns = {
            ln.strip().rstrip("/")
            for ln in (_ROOT / name).read_text().splitlines()
            if ln.strip() and not ln.startswith("#")
        }
        assert top not in patterns, f"{rel} is excluded by {name} — it would not survive"


# -- dedup ------------------------------------------------------------------


def test_a_second_run_appends_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(bt_snapshot, "TAPE_DIR", tmp_path)
    bt_snapshot.append_tape(_USAGE, _LIUM, captured_on="2026-10-19")
    first = _lines(tmp_path / "chutes_usage.jsonl")
    assert len(first) == 2

    bt_snapshot.append_tape(_USAGE, _LIUM, captured_on="2026-10-19")
    assert _lines(tmp_path / "chutes_usage.jsonl") == first
    assert len(_lines(tmp_path / "lium_executors.jsonl")) == 2


def test_a_new_day_is_appended(tmp_path, monkeypatch):
    monkeypatch.setattr(bt_snapshot, "TAPE_DIR", tmp_path)
    bt_snapshot.append_tape(_USAGE, _LIUM, captured_on="2026-10-19")
    fresh = [{"date": "2026-10-20", "chute_id": "c1", "price_per_1k_tokens": 0.03}]
    bt_snapshot.append_tape(fresh, _LIUM, captured_on="2026-10-20")

    usage = _lines(tmp_path / "chutes_usage.jsonl")
    assert sorted({r["date"] for r in usage}) == ["2026-10-18", "2026-10-19", "2026-10-20"]
    # same executors, a different day: a price SERIES, not a duplicate
    assert len(_lines(tmp_path / "lium_executors.jsonl")) == 4


def test_lium_rows_are_stamped_with_the_capture_day(tmp_path, monkeypatch):
    """Lium returns a price but no date. Unstamped, the archive cannot answer
    "what did this cost on day X" — the only question it exists to answer."""
    monkeypatch.setattr(bt_snapshot, "TAPE_DIR", tmp_path)
    bt_snapshot.append_tape([], _LIUM, captured_on="2026-10-19")
    rows = _lines(tmp_path / "lium_executors.jsonl")
    assert {r["captured_on"] for r in rows} == {"2026-10-19"}


def test_a_corrupt_archive_line_does_not_block_appending(tmp_path, monkeypatch):
    """A half-written line must cost only itself.

    The corrupt line STAYS in the file — rewriting an append-only archive to
    tidy it is a worse risk than leaving it — so the contract is that both the
    writer and the reader step over it. Asserted from the consumer's side,
    because that is the side that has to keep working.
    """
    monkeypatch.setattr(bt_snapshot, "TAPE_DIR", tmp_path)
    path = tmp_path / "chutes_usage.jsonl"
    path.write_text("{truncated\n")
    bt_snapshot.append_tape(_USAGE, [], captured_on="2026-10-19")

    good = []
    for ln in path.read_text().splitlines():
        try:
            good.append(json.loads(ln))
        except ValueError:
            pass
    assert len(good) == 2, "both fresh rows were appended past the corrupt line"

    from acr_tape import ChutesSource

    assert len(ChutesSource(path=path).collect()) == 2


# -- the seed ---------------------------------------------------------------


def test_seeding_preserves_the_oldest_fixture_day(tmp_path, monkeypatch):
    """The fixture holds days upstream has already dropped.

    Seeding is what makes the series start there instead of today, and it has to
    stay idempotent or running it twice doubles the history.
    """
    monkeypatch.setattr(bt_snapshot, "TAPE_DIR", tmp_path)
    assert bt_snapshot.seed_from_fixture() == 0

    usage = _lines(tmp_path / "chutes_usage.jsonl")
    dates = sorted({r["date"] for r in usage})
    assert dates[0] == "2026-10-18", "the day that has aged off upstream"
    n = len(usage)

    assert bt_snapshot.seed_from_fixture() == 0
    assert len(_lines(tmp_path / "chutes_usage.jsonl")) == n


# ── the transport fault that skipped the retry ──────────────────────────────
#
# Measured on a live run, 2026-10-30:
#
#   http.client.IncompleteRead: IncompleteRead(13469752 bytes read,
#                                              7978473 more expected)
#
# on `/invocations/stats/llm`. `IncompleteRead`'s MRO is
# `HTTPException -> Exception`, so it is neither an `OSError` nor a `URLError`:
# it walked through `fetch`'s handler, past `try_fetch` (which catches only
# `RuntimeError`), and out of `main` as a traceback. On the 21 MB payload — the
# largest, the likeliest to truncate, and the one that feeds the ACR-INF tape.
#
# A half-read is the definition of what three attempts are for, and it was the
# one transport fault that skipped them. Which matters more now than it did:
# this capture runs unattended on a schedule, so the failure mode is a day
# missing from a 12-day retention window, with nobody at the terminal.


def _raiser(exc, calls):
    def fake(req, timeout=0):
        calls.append(getattr(req, "full_url", str(req)))
        raise exc

    return fake


def test_a_truncated_large_payload_is_retried_not_a_traceback(monkeypatch):
    import http.client

    calls: list[str] = []
    monkeypatch.setattr(bt_snapshot.time, "sleep", lambda *_: None)
    monkeypatch.setattr(
        bt_snapshot.urllib.request,
        "urlopen",
        _raiser(http.client.IncompleteRead(b"x" * 10, 20), calls),
    )
    with pytest.raises(RuntimeError) as e:
        bt_snapshot.fetch("https://api.chutes.ai/invocations/stats/llm")
    # All three attempts, and the cause named in the message rather than lost.
    assert len(calls) == 3
    assert "IncompleteRead" in str(e.value)


def test_a_truncated_body_that_decodes_as_broken_json_is_also_retried(monkeypatch):
    """The same read, one layer up: a half-received JSON array raises
    `JSONDecodeError`, which is a `ValueError` and was equally unretried."""
    calls: list[str] = []
    monkeypatch.setattr(bt_snapshot.time, "sleep", lambda *_: None)
    monkeypatch.setattr(
        bt_snapshot.urllib.request,
        "urlopen",
        _raiser(json.JSONDecodeError("Expecting value", "[{", 2), calls),
    )
    with pytest.raises(RuntimeError):
        bt_snapshot.fetch("https://api.chutes.ai/invocations/usage")
    assert len(calls) == 3


def test_a_degradable_endpoint_still_degrades_on_that_fault(monkeypatch):
    """`try_fetch` is what keeps one dead upstream from voiding the other four,
    and it only catches `RuntimeError` — so the fix has to land in `fetch`,
    which is where it landed."""
    import http.client

    monkeypatch.setattr(bt_snapshot.time, "sleep", lambda *_: None)
    monkeypatch.setattr(
        bt_snapshot.urllib.request,
        "urlopen",
        _raiser(http.client.IncompleteRead(b"", 1), []),
    )
    assert bt_snapshot.try_fetch("https://api.chutes.ai/pricing", "chutes /pricing") is None
