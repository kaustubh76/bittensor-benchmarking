"""Lium (Bittensor subnet 51) as an ACR-GPU anchor row set.

`anchors/_basket/ACR-GPU.json` claimed until 2026-10-29 that no GPU cloud
published a clean unauthenticated price JSON, so its rows were curated. Lium
falsifies that, and these tests pin the three judgements the switch rests on —
each of which is a place the aggregate could be quietly wrong.

**Grouping, not raw rows.** 94 secure executors give the basket a 44x spread, and
`anchors.aggregate`'s own docstring says a basket spanning 13x has no single
price. One row per accelerator class keeps it comparable.

**On-demand only.** The basket has always said spot and marketplace bids are a
different product. Lium labels them `tier`, so the rule is now enforceable rather
than aspirational.

**A comparable accelerator tier.** The curated rows are H100/A100 datacenter
parts. Including Lium's consumer RTX cards would take the gap to 49.5x — and that
number is disclosed in the basket note, because excluding rows is exactly the move
a reader should be able to check.

Runs off the committed Lium fixture: no network.
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

from anchors import (  # noqa: E402
    LIUM_URL,
    _check_thin,
    latest_anchor,
    lium_medians,
    load_basket,
    thin_classes,
)

FIXTURE = _ROOT / "tests" / "fixtures" / "bt" / "lium_executors.json"

pytestmark = pytest.mark.skipif(
    not FIXTURE.exists(),
    reason="no Lium fixture (run `uv run python scripts/bt_snapshot.py --capture-fixture`)",
)


@pytest.fixture(scope="module")
def executors() -> list[dict]:
    return json.loads(FIXTURE.read_text())


@pytest.fixture(scope="module")
def rule() -> dict:
    return load_basket("ACR-GPU")["rule"]


def test_the_basket_declares_the_tier_and_the_accelerators(rule):
    """Declared in the basket, not hard-coded in the fetcher, so a reader can see
    what was included without reading the code that included it."""
    assert rule["lium_tier"] == "secure"
    assert set(rule["lium_accelerators"]) == {"H100", "H200", "B200", "B300"}


def test_spot_executors_are_excluded(executors, rule):
    _, counts = lium_medians(executors, rule)
    assert counts["wrong_tier"] > 0, "the fixture has no spot rows to exclude"
    spot = sum(1 for e in executors if e.get("tier") != "secure")
    assert counts["wrong_tier"] == spot


def test_consumer_cards_are_excluded(executors, rule):
    _, counts = lium_medians(executors, rule)
    assert counts["other_accelerator"] > 0
    groups, _ = lium_medians(executors, rule)
    for name in groups:
        assert any(k in name for k in rule["lium_accelerators"]), name
        assert "RTX" not in name or "PRO" in name


def test_one_row_per_accelerator_class_not_per_executor(executors, rule):
    """The design decision: 94 raw rows is an aggregate over two markets, and a
    94-quote /par payload whose best_seller is an executor UUID."""
    groups, counts = lium_medians(executors, rule)
    assert 0 < len(groups) < 20
    assert sum(g["n"] for g in groups.values()) > len(groups)
    assert counts["total"] == len(executors)


def test_each_class_price_is_the_median_of_its_executors(executors, rule):
    groups, _ = lium_medians(executors, rule)
    name, got = next(iter(groups.items()))
    expected = statistics.median(
        float(e["price_per_gpu"])
        for e in executors
        if e.get("machine_name") == name and e.get("tier") == "secure" and e.get("price_per_gpu")
    )
    assert got["usd_per_gpu_hour"] == pytest.approx(expected)
    assert got["min"] <= got["usd_per_gpu_hour"] <= got["max"]


def test_the_grouped_spread_stays_under_the_no_single_price_threshold(executors, rule):
    """`aggregate`'s docstring: a basket whose rows span 13x has no single price."""
    groups, _ = lium_medians(executors, rule)
    px = sorted(g["usd_per_gpu_hour"] for g in groups.values())
    assert px[-1] / px[0] < 13.0


def test_the_unrestricted_population_really_would_be_too_wide(executors):
    """The counterfactual the basket note cites, asserted rather than asserted-at.

    If this ever stopped being true the exclusion would no longer be justified,
    and the note would be stale in a way nothing else would catch.
    """
    secure = [
        float(e["price_per_gpu"])
        for e in executors
        if e.get("tier") == "secure" and e.get("price_per_gpu")
    ]
    assert len(secure) > 20
    assert max(secure) / min(secure) > 13.0


def test_an_unpriced_or_unknown_class_is_counted_not_crashed_on(rule):
    groups, counts = lium_medians(
        [
            {"machine_name": "NVIDIA H100 80GB HBM3", "tier": "secure", "price_per_gpu": None},
            {"machine_name": "NVIDIA H200", "tier": "secure", "price_per_gpu": 0},
            {"machine_name": "NVIDIA B200", "tier": "secure", "price_per_gpu": 6.0},
        ],
        rule,
    )
    assert counts["unpriced"] == 2
    assert set(groups) == {"NVIDIA B200"}


def test_an_empty_response_is_no_groups_not_an_exception(rule):
    groups, counts = lium_medians([], rule)
    assert groups == {}
    assert counts["total"] == 0


def test_the_source_url_is_the_one_that_works():
    """`api.lium.io` and `celiumcompute.ai` do not resolve; only lium.io/api does."""
    assert LIUM_URL == "https://lium.io/api/executors"


# ── the per-class observation floor ─────────────────────────────────────────
#
# `aggregate.n` read 10 of 10 while `NVIDIA H100 80GB HBM3` went from 6 secure
# executors to 1 inside 24h. Coverage cannot see this: the row priced. These
# pin the level below it — how many sellers stood behind each row.


def test_the_basket_declares_the_floor_and_its_ceiling(rule):
    """Both in the basket beside `lium_tier`, for the same reason: a selection
    rule a reader can check without reading the fetcher."""
    assert rule["lium_min_executors"] == 2
    assert rule["lium_max_thin"] == 2
    assert rule["lium_max_thin"] >= 0


def test_a_single_executor_class_is_measured_as_thin(executors, rule):
    """The fixture already contains two n=1 classes. That is the point — thin is
    the normal state of SN51 inventory, not an outage."""
    groups, _ = lium_medians(executors, rule)
    floor = rule["lium_min_executors"]
    thin = {name for name, g in groups.items() if g["n"] < floor}
    assert thin == {"NVIDIA B300 SXM6 AC", "NVIDIA H200 NVL"}
    # And the thin median IS that one executor's ask, which is the whole worry.
    assert groups["NVIDIA H200 NVL"]["n"] == 1
    assert (
        groups["NVIDIA H200 NVL"]["min"]
        == groups["NVIDIA H200 NVL"]["max"]
        == groups["NVIDIA H200 NVL"]["usd_per_gpu_hour"]
    )


def test_the_committed_anchor_is_inside_its_own_thin_ceiling():
    """The ratchet has to be green on the file in the repo, or it is a test that
    is red forever and teaches its reader to ignore it."""
    doc = latest_anchor("ACR-GPU")
    assert doc is not None
    names = thin_classes("ACR-GPU", doc)
    assert names == ["lium/h200-nvl"]
    assert _check_thin("ACR-GPU", doc) == []


def test_thinning_past_the_ceiling_fails_the_check():
    doc = latest_anchor("ACR-GPU")
    doc["provenance"]["thin_classes"] = [
        "lium/h200-nvl",
        "lium/h100-80gb-hbm3",
        "lium/b300-sxm6-ac",
    ]
    fails = _check_thin("ACR-GPU", doc)
    assert len(fails) == 1
    # Named, not just counted: a count says the basket thinned, the ids say
    # which market did.
    assert "lium/h100-80gb-hbm3" in fails[0]
    assert "lium_max_thin" in fails[0]


def test_an_anchor_written_before_the_floor_existed_is_recomputed_not_waved_through():
    """`provenance.thin_classes` is new. Every Lium row has carried
    `raw.n_executors` since the first fetch, so an older file is re-read from
    the rows rather than treated as zero thin classes."""
    doc = latest_anchor("ACR-GPU")
    doc["provenance"].pop("thin_classes", None)
    assert thin_classes("ACR-GPU", doc) == ["lium/h200-nvl"]


def test_a_basket_with_no_declared_floor_is_not_checked():
    """ACR-INF and ACR-DATA reach no Lium, so the floor must not invent a
    verdict about them."""
    for index_id in ("ACR-INF", "ACR-DATA"):
        doc = latest_anchor(index_id)
        assert thin_classes(index_id, doc) is None
        assert _check_thin(index_id, doc) == []
