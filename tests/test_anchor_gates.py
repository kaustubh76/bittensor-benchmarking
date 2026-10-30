"""The two anchor gates that were added after the other three stayed green.

`scripts/anchors.py --check` asserted that every index has an anchor, that its
`reference_level` still matches `indices.py`, and that the declared gap
recomputes from the file's own rows. All three were true, and all three stayed
true, while ACR-INF lost five of its ten basket rows — every Anthropic, Google
and Mistral model in it. Measured 2026-10-07: 5 priced of 10 requested, down
from 6, with no signal in any gate, log or page.

They stayed true because each is a property of the rows that REMAIN. A median
over five rows recomputes exactly as well as a median over ten.

WHY THAT IS NOT COSMETIC. `par.market_basket` returns a `Basket` whose `usable`
property requires both quotes and an empty status, and `app.py`'s `/par` route
reads it like this:

    is_first_party(payee) or not basket.usable  ->  price against the FLEET

So a basket that empties or ages out does not produce a missing answer. It
produces a confident wrong one: a stranger's invoice benchmarked against our own
sellers, on a scale `anchors/GAP.md` puts 10x to 1250x off market, from the one
endpoint whose entire claim is that it compares you to somebody else.

THESE TESTS EXIST BECAUSE A GATE NOBODY HAS SEEN FAIL HAS NOT BEEN TESTED. The
coverage ratchet and the age limit are both one comparison each, and a
mis-typed operator or a threshold read from the wrong field would leave them
permanently green — which is indistinguishable from the state they were added
to end. So each one is driven to failure here, on a copy, and the committed
baskets are never touched.
"""

from __future__ import annotations

import copy
import json
import pathlib
import sys
from datetime import UTC, datetime, timedelta

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

from anchors import (  # noqa: E402
    AGE_WARN_FRACTION,
    ANCHOR_MAX_AGE_S,
    MIN_SELLERS,
    _check_age,
    _check_coverage,
    check_anchors,
    latest_anchor,
    load_basket,
)

#: The http basket, and the only one that can thin on its own: its prices come
#: from a live catalogue, while the curated ones carry their figures inline.
THINNABLE = "ACR-INF"


def _iso(age_days: float) -> str:
    return (datetime.now(UTC) - timedelta(days=age_days)).isoformat()


def test_the_committed_anchors_pass_today() -> None:
    """The baseline. Without it, every assertion below could be passing because
    the gate rejects everything."""
    assert check_anchors() == []


def test_coverage_is_actually_being_measured() -> None:
    """ACR-INF is the regression that motivated this, so the number it reports
    is asserted rather than assumed — a gate reading the wrong field would be
    green here too."""
    doc = latest_anchor(THINNABLE)
    agg = doc["aggregate"]
    assert agg["n"] < agg["n_requested"], (
        "ACR-INF is expected to be short of its requested rows; if the fetch has "
        "been repaired, raise `rule.min_priced` to the new count so the ratchet "
        "holds the improvement"
    )
    assert _check_coverage(THINNABLE, doc) == [], "the committed state must pass"


def test_a_thinner_basket_fails() -> None:
    """One row removed from the ACR-INF anchor, which is exactly the shape of
    the regression nothing caught."""
    doc = copy.deepcopy(latest_anchor(THINNABLE))
    floor = load_basket(THINNABLE)["rule"]["min_priced"]
    doc["aggregate"]["n"] = floor - 1
    fails = _check_coverage(THINNABLE, doc)
    assert fails, f"{floor - 1} priced rows passed a floor of {floor}"
    assert "min_priced" in fails[0], "the message must name the knob to turn"


def test_a_basket_below_MIN_SELLERS_fails_for_its_own_reason() -> None:
    """Two failures, not one, and the second is the one that matters: below
    `par.MIN_SELLERS` the problem is not that coverage dropped, it is that there
    is no market to compare against at all."""
    doc = copy.deepcopy(latest_anchor(THINNABLE))
    doc["aggregate"]["n"] = MIN_SELLERS - 1
    fails = _check_coverage(THINNABLE, doc)
    assert any("MIN_SELLERS" in f for f in fails), (
        f"a basket of {MIN_SELLERS - 1} must fail on not being a market, "
        f"independently of any declared floor: {fails}"
    )


def test_an_anchor_with_no_row_count_fails_rather_than_passing_silently() -> None:
    """`aggregate.n` absent is the failure mode that would otherwise read as
    coverage of zero rows against a floor it never compares — or, worse, as
    nothing at all."""
    doc = copy.deepcopy(latest_anchor(THINNABLE))
    doc["aggregate"].pop("n")
    assert _check_coverage(THINNABLE, doc), "an anchor that declares no count must fail"


@pytest.mark.parametrize("age_days", [0.0, 1.0, 23.0])
def test_a_fresh_anchor_passes_the_age_gate(age_days: float) -> None:
    doc = {"fetched_at": _iso(age_days)}
    assert _check_age("ACR-GPU", doc) == [], f"{age_days}d should be inside the window"


def test_an_anchor_past_the_fraction_fails_BEFORE_par_stops_trusting_it() -> None:
    """The whole point of the fraction. At the cliff itself `/par` has already
    been rerouting, so the gate has to fire while there is still time to act."""
    cliff_days = ANCHOR_MAX_AGE_S / 86_400
    warn_days = AGE_WARN_FRACTION * cliff_days
    assert warn_days < cliff_days, "the gate must fire before the behaviour changes"

    fails = _check_age("ACR-GPU", {"fetched_at": _iso(warn_days + 0.5)})
    assert fails, f"an anchor {warn_days + 0.5:.1f}d old passed a {warn_days:.1f}d limit"

    # And it is still inside the window `par` itself enforces, which is the
    # margin this gate exists to buy.
    assert warn_days + 0.5 < cliff_days


def test_an_unparseable_date_fails_rather_than_reading_as_brand_new() -> None:
    """A clock that says "0 days old" when it has stopped is the `time.monotonic`
    mistake in another costume: the honest value for "unknown" is not the value a
    fresh anchor has."""
    assert _check_age("ACR-GPU", {"fetched_at": "not a date"})
    assert _check_age("ACR-GPU", {})


def test_every_basket_declares_a_floor() -> None:
    """An undeclared floor makes the ratchet hold nothing, and that state is
    silent — the exact property this whole file is about. Checked over the
    baskets on disk rather than over a hard-coded list, so a new index cannot
    arrive without one."""
    for path in sorted((pathlib.Path(__file__).resolve().parents[1]
                        / "anchors" / "_basket").glob("*.json")):
        rule = (json.loads(path.read_text()).get("rule") or {})
        if path.stem == "C-HUMAN":
            continue  # not an index; `check_c_human` validates it separately
        assert isinstance(rule.get("min_priced"), int), (
            f"{path.name} declares no `rule.min_priced`, so its coverage can fall "
            f"to zero without failing anything"
        )
