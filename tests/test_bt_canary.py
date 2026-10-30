"""`scripts/bt_canary.py` — the canary's own logic, exercised offline.

A canary nothing tests is indistinguishable from a canary that always passes,
and this one runs on a schedule where nobody is watching the green runs. So
every contract is checked BOTH ways here: against the committed fixture (which
must pass, or the floors are set above the real population and the canary is red
forever) and against a synthetic breach of each kind it claims to catch.

No network: `check` is pure and the payloads are fixtures or literals.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

import bt_canary  # noqa: E402
from bt_canary import CONTRACTS, Contract, check  # noqa: E402

FIXTURES = _ROOT / "tests" / "fixtures" / "bt"

# Committed fixture per contract, where one exists. The two raw Chutes payloads
# have none — `chutes_usage.json` is the JOINED output, not `/invocations/usage`
# — so they are covered by the synthetic cases below instead. Named explicitly
# rather than globbed, so a renamed fixture fails here instead of silently
# dropping a contract out of the test.
_BY_FIXTURE = {
    "lium /executors": "lium_executors.json",
    "chutes /pricing": "chutes_pricing.json",
    "chutes /miner/metagraph": "chutes_metagraph.json",
}

pytestmark = pytest.mark.skipif(
    not (FIXTURES / "lium_executors.json").exists(),
    reason="no Bittensor fixtures (run `make subnet-snapshot`)",
)


def _c(name: str) -> Contract:
    return next(c for c in CONTRACTS if c.name == name)


def test_every_contract_names_its_consumer_and_its_measurement():
    """The floor is only defensible if the reader can see what it was measured
    against and who breaks when it is crossed."""
    for c in CONTRACTS:
        assert c.consumer, f"{c.name} declares no consumer"
        assert c.measured, f"{c.name} declares no measured population"
        assert c.min_rows >= 1
        assert c.url.startswith("https://")


@pytest.mark.parametrize("name", sorted(_BY_FIXTURE))
def test_the_committed_fixture_passes_its_own_contract(name):
    """The floors must sit BELOW the real population. A canary that is red on a
    normal day gets muted, and a muted canary is worse than none."""
    payload = json.loads((FIXTURES / _BY_FIXTURE[name]).read_text())
    assert check(_c(name), payload) == []


def test_a_reshaped_200_is_caught_not_coerced():
    """The quiet one. An envelope around the array is a valid 200 with a valid
    sha256, and `lium_medians` reads zero rows from it without raising."""
    fails = check(_c("lium /executors"), {"data": []})
    assert len(fails) == 1
    assert "expected list, got dict" in fails[0]


def test_a_renamed_field_is_caught_at_the_field():
    """`price_per_gpu` -> `pricePerGpu` keeps the shape, the row count and the
    200. `anchors.lium_medians` would count every executor as `unpriced` and the
    basket would thin to its four curated rows."""
    rows = [
        {
            "id": f"e{i}",
            "machine_name": "NVIDIA H100 80GB HBM3",
            "pricePerGpu": 3.0,
            "tier": "secure",
        }
        for i in range(60)
    ]
    fails = check(_c("lium /executors"), rows)
    assert len(fails) == 1
    assert "price_per_gpu absent" in fails[0]
    assert "lium_medians" in fails[0]


def test_a_collapsed_population_is_caught():
    rows = [{"id": "e1", "machine_name": "m", "price_per_gpu": 3.0, "tier": "secure"}]
    fails = check(_c("lium /executors"), rows)
    assert any("below the floor of 40" in f for f in fails)
    assert any("152 executors" in f for f in fails)


def test_a_sparse_optional_key_is_not_a_breach():
    """An upstream that adds a key to one row of thousands has broken nothing,
    and a canary that says otherwise will be muted."""
    rows = [
        {"id": f"e{i}", "machine_name": "m", "price_per_gpu": 3.0, "tier": "secure"}
        for i in range(60)
    ]
    rows[0]["some_new_beta_flag"] = True
    assert check(_c("lium /executors"), rows) == []


def test_pricing_losing_tao_usd_is_caught():
    """It would not crash anything — it would silently stop the TAO/USD
    cross-check, which is exactly how that cross-check came to never run."""
    assert check(_c("chutes /pricing"), {"tao_usd": 248.9581}) == []
    for bad in ({"compute_unit_estimate": 1}, {"tao_usd": 0}, {"tao_usd": "248.95"}):
        fails = check(_c("chutes /pricing"), bad)
        assert any("cross_check_tao_usd" in f for f in fails), bad


def test_the_models_envelope_is_checked_as_an_envelope():
    """The one endpoint that is NOT an array. `bt_snapshot` reads
    `models.get("data")`, which yields [] for any reshape instead of raising."""
    assert check(_c("chutes /v1/models"), {"data": [{"id": "m", "price": {}}]}) == []
    for bad in ({"models": []}, {"data": []}, {"data": {}}):
        fails = check(_c("chutes /v1/models"), bad)
        assert any("not a non-empty array" in f for f in fails), bad


def test_the_metagraph_contract_matches_what_concentration_actually_reads():
    """Pinned against the real consumer rather than restated: a key the
    diagnostic reads must be a key the canary guards."""
    from acr_tape.chutes_metagraph import load_metagraph

    rows = load_metagraph(FIXTURES / "chutes_metagraph.json")
    assert rows, "the fixture is empty"
    for key in _c("chutes /miner/metagraph").required:
        assert key in rows[0], f"the canary guards {key!r}, which the fixture does not carry"


def test_the_join_contract_is_the_one_that_matters():
    """Both payloads can pass their own shape check and still share no
    (chute_id, date) key — which empties the ACR-INF tape. That is the break
    this whole audit started from, so it is checked end-to-end."""
    usage = [{"chute_id": "c1", "date": "2026-10-30", "usd_amount": 10.0}]
    stats = [
        {
            "chute_id": "c1",
            "date": "2026-10-30",
            "total_input_tokens": 1000,
            "total_output_tokens": 1000,
            "average_ttft": 1.1,
        }
    ]
    assert len(bt_canary.join_chutes(usage, stats)) == 1
    # Same rows, dates no longer agreeing: two healthy endpoints, no tape.
    assert bt_canary.join_chutes(usage, [{**stats[0], "date": "30-10-2026"}]) == []


def test_an_unreachable_endpoint_is_a_finding_not_a_crash(monkeypatch, capsys):
    """One dead upstream must still leave the other five measured — the same
    isolation `anchors --fetch` needed after a Lium outage half-wrote the set."""

    def boom(url, *a, **k):
        raise RuntimeError(f"{url} failed after 3 attempts: timed out")

    monkeypatch.setattr(bt_canary, "fetch", boom)
    monkeypatch.setattr(sys, "argv", ["bt_canary.py"])
    assert bt_canary.main() == 1
    out = capsys.readouterr().out
    # Every contract was attempted, not just the first.
    for c in CONTRACTS:
        assert c.name in out
    assert "does NOT break the product" in out


def test_a_fully_healthy_run_returns_zero(monkeypatch, capsys):
    """The green path, so a bug that makes the canary always red is caught here
    rather than by somebody muting it."""
    healthy = {
        "chutes /invocations/usage": [
            {"chute_id": f"c{i}", "date": "2026-10-30", "usd_amount": 10.0} for i in range(500)
        ],
        "chutes /invocations/stats/llm": [
            {
                "chute_id": f"c{i}",
                "date": "2026-10-30",
                "total_input_tokens": 1000,
                "total_output_tokens": 1000,
                "average_ttft": 1.1,
            }
            for i in range(5_000)
        ],
        "lium /executors": json.loads((FIXTURES / "lium_executors.json").read_text()),
        "chutes /pricing": json.loads((FIXTURES / "chutes_pricing.json").read_text()),
        "chutes /v1/models": {"data": [{"id": "m", "price": {}}]},
        "chutes /miner/metagraph": json.loads((FIXTURES / "chutes_metagraph.json").read_text()),
    }
    by_url = {c.url: healthy[c.name] for c in CONTRACTS}
    monkeypatch.setattr(bt_canary, "fetch", lambda url, *a, **k: by_url[url])
    monkeypatch.setattr(sys, "argv", ["bt_canary.py"])
    assert bt_canary.main() == 0
    assert "every Bittensor upstream still matches" in capsys.readouterr().out


def test_skip_heavy_does_not_silently_skip_the_join(monkeypatch, capsys):
    """`--skip-heavy` drops the 21 MB fetch, so the join cannot be attempted.
    That must be SAID, not treated as a pass."""
    monkeypatch.setattr(bt_canary, "fetch", lambda url, *a, **k: [])
    monkeypatch.setattr(sys, "argv", ["bt_canary.py", "--skip-heavy"])
    bt_canary.main()
    out = capsys.readouterr().out
    assert "skipped (--skip-heavy)" in out
    assert "the join could not be attempted" not in out
