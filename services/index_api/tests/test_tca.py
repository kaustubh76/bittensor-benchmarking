"""TCA + rating tests — the arithmetic, and the honesty of what it will not claim."""

from __future__ import annotations

import pytest
from index_api import tca as tca_mod
from index_api.tca import MIN_RATED_N, WEIGHTS, _grade, _p50_bp, _reroute, _vw_bp

SELLER = "0x" + "22" * 20
OTHER = "0x" + "33" * 20
PAYER = "0x" + "11" * 20


# --- arithmetic --------------------------------------------------------------


def test_volume_weighted_slippage_divides_the_tenth_bp_figure():
    # 100 USDC-1e6 of volume carrying 1000 tenth-bp == 100 bp.
    assert _vw_bp(100 * 1000, 100) == pytest.approx(100.0)


def test_no_benchmarked_volume_is_none_not_zero():
    """Zero bp means 'paid exactly the benchmark'. None means 'nothing to
    compare'. A surface that renders the second as the first is lying."""
    assert _vw_bp(0, 0) is None


def test_p50_interpolates_inside_a_bucket():
    # 10 settlements all in b3 ([50, 100)); the median sits mid-bucket.
    assert _p50_bp([0, 0, 0, 10, 0, 0, 0]) == pytest.approx(75.0)


def test_p50_splits_across_buckets():
    # 10 in b2 ([0,50)) and 10 in b3 ([50,100)) → the median lands on the seam.
    assert _p50_bp([0, 0, 10, 10, 0, 0, 0]) == pytest.approx(50.0)


def test_p50_reports_an_open_tail_at_its_edge_rather_than_inventing_an_interior():
    assert _p50_bp([10, 0, 0, 0, 0, 0, 0]) == -100.0
    assert _p50_bp([0, 0, 0, 0, 0, 0, 10]) == 500.0


def test_p50_of_an_empty_histogram_is_none():
    assert _p50_bp([0] * 7) is None


# --- grading -----------------------------------------------------------------


def test_grade_boundaries():
    assert _grade(0.95) == "A"
    assert _grade(0.85) == "A"
    assert _grade(0.84) == "B"
    assert _grade(0.70) == "B"
    assert _grade(0.50) == "C"
    assert _grade(0.49) == "D"
    assert _grade(None) == "Unrated"


def test_the_published_weights_sum_to_one_hundred():
    assert sum(WEIGHTS.values()) == 100


# --- the wire ----------------------------------------------------------------


def _fake_graph(monkeypatch, payload: dict):
    monkeypatch.setattr(tca_mod, "_cfg", lambda: ("https://example.invalid", ""))
    monkeypatch.setattr(tca_mod, "graph_query", lambda *a, **k: payload)


def _seller_day(**over) -> dict:
    row = {
        "day": 20660, "volume": "1000000", "bmVolume": "1000000",
        "wSlipTenthBp": "1000000000",  # 1000 tenth-bp × 1e6 volume == 100 bp
        "humanVolume": "0", "synthVolume": "1000000", "realVolume": "0",
        "n": 25, "nAll": 25, "nStale": 0,
        "b0": 0, "b1": 0, "b2": 0, "b3": 0, "b4": 25, "b5": 0, "b6": 0,
    }
    row.update(over)
    return row


def test_a_rating_carries_n_and_the_synthetic_share(monkeypatch):
    """Every card, always — a grade without them invites being read as a claim
    about a market rather than about our own testnet flow."""
    _fake_graph(monkeypatch, {"sellerDays": [_seller_day()], "seller": {"id": SELLER}})
    r = tca_mod.seller_rating(SELLER)
    assert r["available"] is True
    assert r["n"] == 25
    assert r["synthetic_share"] == 1.0
    assert r["components"]["fairness"]["vw_slippage_bp"] == pytest.approx(100.0)


def test_a_thin_tape_is_unrated_with_the_reason(monkeypatch):
    _fake_graph(monkeypatch, {"sellerDays": [_seller_day(n=5, nAll=5)], "seller": {"id": SELLER}})
    r = tca_mod.seller_rating(SELLER)
    assert r["grade"] == "Unrated"
    assert f"n=5 < {MIN_RATED_N}" in r["unrated_reason"]


def test_an_all_unbenchmarked_seller_is_unrated_for_KIND_not_quantity(monkeypatch):
    """The press selling $/query: twelve fills, none of them benchmarkable, because
    a flat query fee has no arrival price. "Thin tape (n=0 < 20)" would be true
    arithmetic and the wrong diagnosis — it sends a reader looking for missing
    fills that are all right there. The reason has to name the kind of problem."""
    _fake_graph(monkeypatch, {"sellerDays": [_seller_day(n=0, nAll=12)], "seller": {"id": SELLER}})
    r = tca_mod.seller_rating(SELLER)
    assert r["grade"] == "Unrated"
    assert r["n"] == 0 and r["n_all"] == 12
    assert "unbenchmarked" in r["unrated_reason"]
    assert "12 fill(s)" in r["unrated_reason"]
    # The phrase, not the substring: "no THINg to grade against" contains "thin".
    assert "thin tape" not in r["unrated_reason"]


def test_a_genuinely_thin_tape_still_says_thin(monkeypatch):
    # The other branch must survive: a few benchmarked fills is a quantity problem.
    _fake_graph(monkeypatch, {"sellerDays": [_seller_day(n=3, nAll=3)], "seller": {"id": SELLER}})
    r = tca_mod.seller_rating(SELLER)
    assert "thin tape" in r["unrated_reason"]
    assert "unbenchmarked" not in r["unrated_reason"]


def test_unsupported_components_are_excluded_from_the_weight_not_scored_zero(monkeypatch):
    """A seller must not be marked down for a signal ACR has not started
    collecting. The card says what share of the methodology the grade rests on."""
    _fake_graph(monkeypatch, {"sellerDays": [_seller_day()], "seller": {"id": SELLER}})
    r = tca_mod.seller_rating(SELLER)
    assert r["components"]["cleanliness"]["available"] is False
    assert r["components"]["human_depth"]["available"] is False
    # Only fairness is supported here, so the grade rests on 40 of 100.
    assert r["weight_covered_pct"] == WEIGHTS["fairness"]
    # …and it is a real grade, not a zero dragged down by the missing 60.
    assert r["grade"] in {"A", "B", "C", "D"}


def _seller_window(**over) -> dict:
    win = {"window": 2951, "distinctPayers": 2, "distinctHumans": 1,
           "volume": "1000000", "humanVolume": "500000"}
    win.update(over)
    return win


def test_human_depth_counts_a_fleet_once(monkeypatch):
    """Two wallets, one verified human. The component's whole reason to exist is
    that those are different numbers — a seller that has met one person is not
    one that has met two."""
    _fake_graph(monkeypatch, {
        "sellerDays": [_seller_day(humanVolume="500000")],
        "seller": {"id": SELLER},
        "sellerWindow": _seller_window(),
    })
    r = tca_mod.seller_rating(SELLER)
    depth = r["components"]["human_depth"]
    assert depth["available"] is True
    assert depth["distinct_humans"] == 1
    assert depth["distinct_payers"] == 2
    assert depth["score"] == pytest.approx(0.5)
    assert depth["human_volume_share"] == pytest.approx(0.5)
    # The grade now rests on fairness AND human depth, and says so.
    assert r["weight_covered_pct"] == WEIGHTS["fairness"] + WEIGHTS["human_depth"]


def test_human_depth_is_refused_rather_than_blended_over_a_longer_window(monkeypatch):
    """A cluster id is minted per rotation window, so distinct humans cannot be
    summed across windows. Serving a 30d fairness beside a 7d human depth would
    be one grade quietly built out of two different spans."""
    _fake_graph(monkeypatch, {
        "sellerDays": [_seller_day()],
        "seller": {"id": SELLER},
        "sellerWindow": _seller_window(),
    })
    r = tca_mod.seller_rating(SELLER, days=30)
    depth = r["components"]["human_depth"]
    assert depth["available"] is False
    assert "rotation window" in depth["reason"]
    assert r["weight_covered_pct"] == WEIGHTS["fairness"]


def test_a_window_with_no_resolutions_is_unavailable_not_zero(monkeypatch):
    """Nobody resolved is not the same fact as nobody human, and a seller must
    not be marked down for the difference."""
    _fake_graph(monkeypatch, {
        "sellerDays": [_seller_day()],
        "seller": {"id": SELLER},
        "sellerWindow": _seller_window(distinctHumans=0, humanVolume="0"),
    })
    depth = tca_mod.seller_rating(SELLER)["components"]["human_depth"]
    assert depth["available"] is False
    assert "this window" in depth["reason"]


def test_a_seller_paying_the_benchmark_exactly_grades_top(monkeypatch):
    _fake_graph(
        monkeypatch,
        {"sellerDays": [_seller_day(wSlipTenthBp="0", b4=0, b2=25)], "seller": {"id": SELLER}},
    )
    assert tca_mod.seller_rating(SELLER)["grade"] == "A"


def test_an_unreachable_subgraph_says_so(monkeypatch):
    _fake_graph(monkeypatch, {})
    r = tca_mod.seller_rating(SELLER)
    assert r["available"] is False and "did not answer" in r["reason"]


def test_payer_tca_breaks_down_by_seller_and_ranks_worst_first(monkeypatch):
    _fake_graph(monkeypatch, {
        "payerDays": [{
            "day": 20660, "spent": "2000000", "bmSpent": "2000000",
            "wSlipTenthBp": "2000000000", "overpay": "20000", "n": 2, "nAll": 2,
        }],
        "settlements": [
            {"seller": {"id": SELLER}, "amount": "1000000",
             "slippageTenthBp": "1880", "synthetic": True, "human": True, "index": "ACR-INF"},
            {"seller": {"id": OTHER}, "amount": "1000000",
             "slippageTenthBp": "-120", "synthetic": True, "human": False, "index": "ACR-INF"},
        ],
    })
    r = tca_mod.payer_tca(PAYER)
    assert r["purchases"] == 2 and r["benchmarked"] == 2
    assert r["spent_usdc"] == pytest.approx(2.0)
    assert r["by_seller"][0]["seller"] == SELLER  # worst first
    assert r["by_seller"][0]["vw_slippage_bp"] == pytest.approx(188.0)
    assert r["by_seller"][-1]["vw_slippage_bp"] == pytest.approx(-12.0)
    # The human share rides beside the synthetic share, per seller, same shape.
    # It is what lets the tape page mark a seller's row as human-backed.
    assert r["by_seller"][0]["human_share"] == 1.0
    assert r["by_seller"][-1]["human_share"] == 0.0


def test_the_reroute_names_both_sides_and_calls_itself_a_suggestion(monkeypatch):
    _fake_graph(monkeypatch, {
        "payerDays": [{
            "day": 20660, "spent": "2000000", "bmSpent": "2000000",
            "wSlipTenthBp": "2000000000", "overpay": "20000", "n": 2, "nAll": 2,
        }],
        "settlements": [
            {"seller": {"id": SELLER}, "amount": "1000000",
             "slippageTenthBp": "1880", "synthetic": True, "index": "ACR-INF"},
            {"seller": {"id": OTHER}, "amount": "1000000",
             "slippageTenthBp": "-120", "synthetic": True, "index": "ACR-INF"},
        ],
    })
    rr = tca_mod.payer_tca(PAYER)["reroute"]
    assert rr["from"] == SELLER and rr["to"] == OTHER
    assert rr["saving_bp"] == pytest.approx(200.0)
    assert rr["note"] == "a suggestion, not a promise"


def test_no_reroute_when_there_is_nothing_to_reroute_to():
    assert _reroute([{"seller": SELLER, "vw_slippage_bp": 10.0, "n": 4, "volume_usdc": 1.0}]) is None


def test_no_reroute_when_the_cheapest_seller_is_the_one_already_used():
    same = [
        {"seller": SELLER, "vw_slippage_bp": 10.0, "n": 4, "volume_usdc": 1.0},
        {"seller": OTHER, "vw_slippage_bp": 10.0, "n": 4, "volume_usdc": 1.0},
    ]
    assert _reroute(same) is None


# --- the queries themselves --------------------------------------------------


def _operations(*modules):
    """Every GraphQL operation a module defines, however it stores them.

    Two shapes in this codebase: `tca.py` uses module-level string constants and
    `graph_proxy.py` keeps a dict of named operations. Both are walked, because a
    guard that only understood one of them would pass while checking half the
    queries — and "it enumerated nothing" is the failure this test exists to stop
    one level down.
    """
    for mod in modules:
        for name, value in vars(mod).items():
            if isinstance(value, str) and "query " in value:
                yield mod.__name__, name, value
            elif isinstance(value, dict):
                for key, text in value.items():
                    if isinstance(text, str) and "query " in text:
                        yield mod.__name__, f"{name}[{key!r}]", text


def test_every_query_declares_the_variables_it_uses():
    """An undeclared `$var` is not a syntax error — graph-node resolves it to
    null and rejects the operation on the ARGUMENT, so the failure surfaces as
    "Invalid value provided for argument `id`: Null" and this service degrades it
    to "the subgraph did not answer".

    That is indistinguishable from an outage, which is how `_SELLER_DAYS` shipped
    using `$windowId` without declaring it: every seller's rating came back
    unavailable, `GradeChip` rendered a muted ellipsis, and nothing anywhere said
    the query was malformed. The comment above that query had even predicted the
    failure mode; predicting it did not prevent it.

    Static on purpose — no network, no fixture, no live schema. It catches the
    whole class at import time rather than this one instance, and it runs in CI
    where a real graph-node does not.
    """
    import re

    from index_api import graph_proxy

    seen = 0
    for mod_name, const, text in _operations(tca_mod, graph_proxy):
        sig = re.search(r"query\s+\w*\s*\(([^)]*)\)", text)
        declared = set(re.findall(r"\$(\w+)", sig.group(1))) if sig else set()
        used = set(re.findall(r"\$(\w+)", text)) - declared
        assert not used, (
            f"{mod_name}.{const} uses undeclared variable(s) {sorted(used)}. "
            "graph-node will resolve them to null and reject the operation; the "
            "caller will report it as an outage."
        )
        seen += 1
    # A guard that enumerates nothing passes vacuously, which is the failure
    # shape this whole test exists to catch one level down.
    assert seen >= 10, f"only found {seen} operations — the scan stopped matching"


def test_the_breakdown_and_the_headline_cover_the_same_window(monkeypatch):
    """On the busiest live payer, `by_seller` summed to 7.08 of the week's volume:
    the daily rollups were filtered to seven days and the settlement rows were not,
    so the reroute priced its saving on fills the headline never counted. Both
    halves now carry the same cutoff — a day index for one, unix seconds for the
    other — and the query itself must say so."""
    captured: dict = {}

    def _spy(url, query, variables, key):
        captured["query"] = query
        captured["variables"] = variables
        return {"payerDays": [], "settlements": []}

    monkeypatch.setattr(tca_mod, "_cfg", lambda: ("https://example.invalid", ""))
    monkeypatch.setattr(tca_mod, "graph_query", _spy)
    tca_mod.payer_tca(PAYER, days=7)
    assert "settledAt_gte: $sinceTs" in captured["query"]
    v = captured["variables"]
    assert int(v["sinceTs"]) == v["since"] * 86400, "one cutoff, two spellings"
    assert v["since"] == tca_mod._day_now() - 7
    # The human card takes the same two variables, by the same rule.
    assert "settledAt_gte: $sinceTs" in tca_mod._HUMAN_DAYS


def test_no_query_asks_the_graph_for_more_than_its_hard_ceiling():
    """The Graph refuses `first` above 1000 with a GraphQL error, which the client
    reports as "the subgraph did not answer". `_HUMAN_DAYS` asked for 2000, so the
    one endpoint a verified human proof gates answered every proof with an outage.
    Pinned for every query string in the module."""
    import re

    for name, text in vars(tca_mod).items():
        if isinstance(text, str) and "query " in text and "{" in text:
            for n in re.findall(r"first:\s*(\d+)", text):
                assert int(n) <= 1000, f"{name} asks first: {n}"


# --- a payer nobody has seen is not a payer who spent nothing ----------------


def _rollup(**kw) -> dict:
    """One `payerDays` row, with the sums the card folds."""
    row = {"spent": "0", "bmSpent": "0", "wSlipTenthBp": "0", "overpay": "0", "n": "0", "nAll": "0"}
    row.update({k: str(v) for k, v in kw.items()})
    return row


def test_an_unseen_payer_is_marked_unseen_and_told_why():
    """THE CONFIDENT ZERO, pinned.

    A wallet with no mirrored settlements used to answer `available: true,
    purchases: 0, spent_usdc: 0.0, overpaid_usdc: 0.0` and nothing else — which
    reads as a clean bill of health and means "I have never heard of you".
    Measured against production: an arbitrary address answered exactly that, and
    so did two real payers taken from `/marketplace/receipts`, because this
    surface reads mirrored on-chain settlements and those are a different payer
    set. A stranger pointing this at their own agent gets the most reassuring
    possible answer about money ACR never saw.
    """
    card = tca_mod._card({"payerDays": [], "settlements": []}, 7)
    assert card["available"] is True, "the subgraph answered; this is not an outage"
    assert card["seen"] is False
    assert card["purchases"] == 0
    assert "no record of this payer" in card["note"]
    # The note has to name the way OUT, not just the absence.
    assert "/par" in card["note"]
    # AND IT MUST NOT OVERCLAIM. `nAll` is summed over day rows filtered by
    # `day_gte`, so this is "not in this window", never "never". A payer who
    # bought 90 days ago and asked for 7 lands here too, and telling them ACR
    # has no record of them at all would be the same confident falsehood one
    # level down. The window and the way to widen it both have to be in the
    # sentence.
    assert "last 7 days" in card["note"]
    assert "Widening `days`" in card["note"]


def test_a_payer_on_the_record_is_seen_and_carries_no_note():
    """The other half, so `seen` cannot quietly become a constant. A note on a
    payer with real settlements would be noise on the answer that works."""
    card = tca_mod._card(
        {"payerDays": [_rollup(spent=4410, bmSpent=4410, wSlipTenthBp=-11398 * 441, n=1, nAll=3)],
         "settlements": []},
        7,
    )
    assert card["seen"] is True
    assert card["purchases"] == 3
    assert "note" not in card


def test_unseen_is_decided_by_purchases_not_by_spend():
    """A settlement of zero USDC is still a settlement — ACR saw it. Keying
    `seen` on `spent_usdc` would call that payer a stranger, which is the same
    conflation this field exists to end."""
    card = tca_mod._card({"payerDays": [_rollup(nAll=1)], "settlements": []}, 7)
    assert card["spent_usdc"] == 0.0
    assert card["seen"] is True, "nAll is the evidence, not the money"
    assert "note" not in card
