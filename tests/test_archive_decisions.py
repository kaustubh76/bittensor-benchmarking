"""The preflight that stands between a redeploy and the decision log.

The free tier has no persistent disk: `data/operator_decisions.jsonl` is erased
by every deploy, and the only thing that stops a redeploy from throwing the
record away is `archive_decisions.py --check` refusing. So the exit code IS the
safety property, and every test here is about one question — does this exit code
mean "safe to deploy" when it should?

The bug these were written after: the check passed against a hostname that has
never existed. Render answers a plain 404 for an unknown service, which is the
same status an OLDER ACR image answers for `/operator/businesses` — and one of
those means "nothing to lose" while the other means "you are pointed at the
wrong machine". Read as the first, a typo in the host silently cleared the gate.

The three codes, kept apart deliberately:

  0  production holds nothing the archive does not — safe
  1  production is AHEAD of the archive — archive it first
  2  I could not tell — unread, and never reported as either of the above
"""

from __future__ import annotations

import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import archive_decisions as ad  # noqa: E402

API = "https://press.example"


def _row(**kw) -> dict:
    row = {
        "at": 1_790_900_000.0,
        "business": "acme",
        "obligation_id": "ob-1",
        "intent": "pay",
        "billed_usdc": 1.0,
        "paid_usdc": 1.0,
    }
    row.update(kw)
    return row


@pytest.fixture
def archive(tmp_path, monkeypatch):
    """An archive file the test owns, never the committed one."""
    p = tmp_path / "operator_decisions.jsonl"
    p.write_text("")
    monkeypatch.setattr(sys, "argv", ["archive_decisions.py"])
    return p


def _run(archive: Path, *extra: str) -> int:
    argv = ["archive_decisions.py", "--api", API, "--archive", str(archive), *extra]
    sys.argv = argv
    return ad.main()


def _wire(monkeypatch, *, businesses=None, live=None, raises=None, health=None):
    """Stand in for production.

    `_businesses` and `_live` are the two functions that reach the network, and
    `_is_our_press` is the control probe. Patched as module attributes because
    that is the seam `main` goes through.
    """
    def fake_businesses(_api):
        if raises is not None:
            raise raises
        return list(businesses or [])

    monkeypatch.setattr(ad, "_businesses", fake_businesses)
    monkeypatch.setattr(ad, "_live", lambda _api, slug: (list(live or []), False))
    monkeypatch.setattr(ad, "_is_our_press", lambda _api: health)


# --- 0: safe ----------------------------------------------------------------


def test_an_archive_that_already_holds_everything_is_safe(archive, monkeypatch):
    archive.write_text(json.dumps(_row()) + "\n")
    _wire(monkeypatch, businesses=["acme"], live=[_row()])
    assert _run(archive, "--check") == 0


def test_an_image_that_predates_the_operator_has_nothing_to_lose(archive, monkeypatch):
    """A legitimate 404, and the state of the FIRST deploy of the operator. A
    preflight that blocked here would make shipping it impossible."""
    _wire(
        monkeypatch,
        raises=ad.NoOperatorThere("no /operator/businesses"),
    )
    assert _run(archive, "--check") == 0


# --- 1: production is ahead -------------------------------------------------


def test_a_decision_only_production_holds_blocks_the_deploy(archive, monkeypatch):
    _wire(monkeypatch, businesses=["acme"], live=[_row(obligation_id="ob-new")])
    assert _run(archive, "--check") == 1


def test_a_resolution_sharing_an_id_with_its_escalation_is_not_already_archived(
    archive, monkeypatch
):
    """`decision_key` carries `intent` for exactly this: an escalation and the
    human's resolution of it share an `obligation_id` by design, and a key of
    the id alone would call the resolution archived and let the half of the
    record that proves a human was involved die on the disk."""
    archive.write_text(json.dumps(_row(intent="escalate")) + "\n")
    _wire(
        monkeypatch,
        businesses=["acme"],
        live=[_row(intent="escalate"), _row(intent="pay", at=1_790_900_100.0, actor="owner")],
    )
    assert _run(archive, "--check") == 1


# --- 2: I could not tell ----------------------------------------------------


def test_a_host_that_is_not_an_acr_press_is_refused(archive, monkeypatch):
    """THE BUG THESE TESTS EXIST FOR, measured against a Render hostname that has
    never existed: a plain 404 plus no `/health` read as "the image predates the
    operator", so the gate passed on a host nobody had ever deployed to."""
    _wire(monkeypatch, raises=ad.NotOurPress("not an ACR press"))
    assert _run(archive, "--check") == 2


def test_a_404_from_a_host_that_does_have_health_is_the_old_image(archive, monkeypatch):
    """The control probe is what keeps the two 404s apart, so it has to be the
    thing under test rather than a detail of the mock. `/health` is on every ACR
    image ever deployed; a hostname that is not ours has nothing there either."""
    calls: list[str] = []

    def fake_open(url, timeout=0):  # noqa: ARG001
        calls.append(url)
        if url.endswith("/operator/businesses"):
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)  # type: ignore[arg-type]
        raise AssertionError(f"unexpected read of {url}")

    monkeypatch.setattr(ad.urllib.request, "urlopen", fake_open)
    monkeypatch.setattr(ad, "_is_our_press", lambda _api: {"status": "ok", "chain_id": 5042002})
    assert _run(archive, "--check") == 0, "an older ACR image holds no decisions to lose"
    assert any("operator/businesses" in u for u in calls)

    monkeypatch.setattr(ad, "_is_our_press", lambda _api: None)
    assert _run(archive, "--check") == 2, "the same 404 from a host that is not ours"


def test_a_sleeping_host_is_unread_and_not_mistaken_for_being_ahead(archive, monkeypatch):
    """A free-tier cold start outlasts the timeout. This used to leave an
    unhandled traceback, which exits 1 — the code that means "production is
    ahead" — so the redeploy script told the operator to run an archiver that
    would fail the same way."""
    _wire(monkeypatch, raises=TimeoutError("The read operation timed out"))
    assert _run(archive, "--check") == 2


def test_a_host_that_stops_answering_part_way_through_is_refused(archive, monkeypatch):
    """Reading some of production and reporting on it as though it were all of
    production is the shape that loses rows quietly."""
    monkeypatch.setattr(ad, "_businesses", lambda _api: ["acme", "beta"])
    monkeypatch.setattr(ad, "_is_our_press", lambda _api: None)

    def flaky(_api, slug):
        if slug == "beta":
            raise urllib.error.URLError("connection reset")
        return [_row()], False

    monkeypatch.setattr(ad, "_live", flaky)
    assert _run(archive, "--check") == 2


def test_a_full_page_is_refused_rather_than_written(archive, monkeypatch):
    """A page at the cap means there may be more behind it, and an archiver that
    silently stops at the cap turns a record of 400 decisions into 50."""
    monkeypatch.setattr(ad, "_businesses", lambda _api: ["acme"])
    monkeypatch.setattr(ad, "_live", lambda _api, _slug: ([_row()] * ad.LIMIT, True))
    assert _run(archive, "--check") == 2


# --- writing, not just checking ---------------------------------------------


def test_without_check_the_missing_rows_are_appended(archive, monkeypatch):
    archive.write_text(json.dumps(_row()) + "\n")
    _wire(monkeypatch, businesses=["acme"], live=[_row(), _row(obligation_id="ob-2")])
    assert _run(archive) == 0
    rows = [json.loads(ln) for ln in archive.read_text().splitlines() if ln.strip()]
    assert [r["obligation_id"] for r in rows] == ["ob-1", "ob-2"], "appended, nothing rewritten"


def test_a_refusal_writes_nothing(archive, monkeypatch):
    """The whole point of exiting 2: an archive that looks complete and is not
    is worse than one that is obviously short."""
    archive.write_text(json.dumps(_row()) + "\n")
    before = archive.read_text()
    _wire(monkeypatch, raises=TimeoutError("timed out"))
    assert _run(archive) == 2
    assert archive.read_text() == before
