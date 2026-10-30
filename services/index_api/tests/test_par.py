"""Observed-quote PAR — the arithmetic, and the claims it refuses to make.

The headline test is ``test_a_market_priced_invoice_is_not_measured_against_the
_index``: it demonstrates, in numbers, the mistake this module exists to prevent.
Everything else guards a refusal — one offer is not a market, the incumbent is
not an alternative to itself, and a price nobody will beat is dear rather than
recoverable.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest
from index_api.par import (
    AGE_WARN_FRACTION,
    ANCHOR_MAX_AGE_S,
    FLEET_SELLERS,
    MATERIAL_BP,
    Par,
    Quote,
    assess,
    is_first_party,
    market_basket,
    par_from_quotes,
    price_from_accepts,
    quotes_from_catalog,
    quotes_from_receipts,
)

#: The repo root, for the one test that reads a sibling file by path rather
#: than importing it — see `test_the_warn_fraction_is_imported_by_the_gate`.
#: Same idiom as `mcp/src/printAge.test.ts`, which pins `ANCHOR_MAX_AGE_S` by
#: reading `par.py`.
ROOT = Path(__file__).resolve().parents[3]

RES = "https://acr.example/compute/inference"
A = "0x" + "aa" * 20
B = "0x" + "bb" * 20
C = "0x" + "cc" * 20


def _q(seller: str, price: float, source: str = "catalog", **kw) -> Quote:
    return Quote(seller=seller, price_usdc=price, source=source, resource=RES, **kw)


# --- the mistake this module prevents --------------------------------------

def test_a_market_priced_invoice_is_not_measured_against_the_index():
    """anchors/GAP.md puts ACR-INF's reference level at 0.5 $/1k tokens while the
    market sits near 0.00043. Measuring a real invoice against the index reads as
    a ~9,990 bp discount on a bill that is in fact slightly over the going rate.
    Against observed quotes for the same service the answer is small and right."""
    index_reference = 0.5          # what the index prints
    billed = 0.00050              # what a real vendor charged
    market = [0.00043, 0.00045, 0.00047]  # what others charge

    naive_bp = ((billed - index_reference) / index_reference) * 10_000
    assert naive_bp < -9_000, "this is the nonsense number we must never publish"

    par = par_from_quotes(RES, [_q(A, market[0]), _q(B, market[1]), _q(C, market[2])])
    out = assess(billed, par)

    assert out["benchmarked"] is True
    assert out["par_usdc"] == pytest.approx(0.00045)
    assert 0 < out["over_par_bp"] < 2_000, "a real, small, defensible number"
    assert out["verdict"] == "over_par"
    assert out["saving_usdc"] == pytest.approx(0.00007)


# --- one offer is not a market ---------------------------------------------

def test_no_quotes_is_a_stated_reason_not_a_price():
    p = par_from_quotes(RES, [])
    assert p.available is False and p.reason == "NO_QUOTES"
    assert p.par_usdc is None, "an absent benchmark must not render as free"


def test_a_single_seller_is_refused_rather_than_compared_to_itself():
    p = par_from_quotes(RES, [_q(A, 1.0)])
    assert p.available is False and p.reason == "ONE_SELLER"
    assert p.sellers == 1


def test_the_incumbent_is_not_an_alternative_to_itself():
    """Excluding the biller both keeps the median off the price under
    examination and stops 'reroute to the cheapest' naming the incumbent."""
    p = par_from_quotes(RES, [_q(A, 1.0)], exclude_seller=A)
    assert p.available is False and p.reason == "NO_INDEPENDENT_SELLER"

    p2 = par_from_quotes(RES, [_q(A, 0.10), _q(B, 1.00), _q(C, 1.20)], exclude_seller=A)
    assert p2.available is True
    assert p2.best_seller == B, "the cheap incumbent is gone from the alternatives"
    assert p2.par_usdc == pytest.approx(1.00), "lower median of [1.00, 1.20]"


def test_a_seller_listing_twice_counts_once_at_their_cheapest():
    """Otherwise one party moves the benchmark without underselling anybody."""
    p = par_from_quotes(RES, [_q(A, 5.0), _q(A, 9.0), _q(A, 7.0), _q(B, 1.0)])
    assert p.sellers == 2
    assert p.best_usdc == pytest.approx(1.0)
    assert sorted(q.price_usdc for q in p.quotes) == [1.0, 5.0]


def test_the_seller_match_ignores_address_case():
    p = par_from_quotes(RES, [_q(A.upper(), 2.0), _q(A.lower(), 3.0), _q(B, 1.0)])
    assert p.sellers == 2, "0xAA… and 0xaa… are one seller"


def test_a_zero_or_negative_price_is_not_a_quote():
    p = par_from_quotes(RES, [_q(A, 0.0), _q(B, -1.0), _q(C, 2.0)])
    assert p.available is False and p.sellers == 1


# --- what a saving is allowed to mean --------------------------------------

def test_a_saving_is_measured_against_the_best_offer_not_the_median():
    p = par_from_quotes(RES, [_q(A, 1.00), _q(B, 2.00), _q(C, 3.00)])
    out = assess(5.00, p)
    assert out["par_usdc"] == pytest.approx(2.00)
    assert out["over_par_usdc"] == pytest.approx(3.00), "vs the going rate"
    assert out["saving_usdc"] == pytest.approx(4.00), "vs what was actually available"


def test_a_dear_price_nobody_will_beat_yields_no_saving():
    """Above the median with nothing cheaper on offer is dear, not recoverable.
    Calling that a saving would be inventing one."""
    p = par_from_quotes(RES, [_q(A, 10.0), _q(B, 12.0), _q(C, 14.0)])
    out = assess(12.0, p)
    assert out["par_usdc"] == pytest.approx(12.0)
    assert out["over_par_usdc"] == pytest.approx(0.0), "exactly the going rate"
    assert out["saving_usdc"] == pytest.approx(2.0), "and 10.0 was on offer"

    # The cheapest price in the market is dear in absolute terms and still
    # recovers nothing, because nobody is beating it.
    out_best = assess(10.0, p)
    assert out_best["saving_usdc"] == pytest.approx(0.0)
    assert out_best["verdict"] == "under_par"


def test_a_price_inside_the_material_band_is_at_par():
    p = par_from_quotes(RES, [_q(A, 1.0), _q(B, 1.0), _q(C, 1.0)])
    out = assess(1.0 * (1 + (MATERIAL_BP / 2) / 10_000), p)
    assert out["verdict"] == "at_par"


def test_a_cheap_price_reads_as_under_par():
    p = par_from_quotes(RES, [_q(A, 1.0), _q(B, 1.0), _q(C, 1.0)])
    out = assess(0.5, p)
    assert out["verdict"] == "under_par"
    assert out["saving_usdc"] == pytest.approx(0.0), "nothing to recover"


def test_an_unbenchmarked_assessment_passes_the_reason_through():
    out = assess(1.0, Par(resource=RES, available=False, reason="ONE_SELLER"))
    assert out["benchmarked"] is False
    assert out["reason"] == "ONE_SELLER"
    assert out["verdict"] == "unbenchmarked"
    assert "saving_usdc" not in out, "no number may be read off an absent benchmark"


# --- reading prices out of the rails ---------------------------------------

def test_both_x402_vintages_are_read():
    assert price_from_accepts([{"amount": "1000"}]) == pytest.approx(0.001)
    assert price_from_accepts([{"maxAmountRequired": "2500"}]) == pytest.approx(0.0025)
    assert price_from_accepts([{"amount": 1000}]) == pytest.approx(0.001)


def test_the_cheapest_option_is_quoted_not_the_first():
    """A seller offering two schemes at two prices is quoting the cheaper one;
    taking accepts[0] would make the benchmark depend on list order."""
    accepts = [{"amount": "9000"}, {"amount": "1000"}]
    assert price_from_accepts(accepts) == pytest.approx(0.001)
    assert price_from_accepts(list(reversed(accepts))) == pytest.approx(0.001)


def test_an_unpriced_listing_is_none_rather_than_zero():
    assert price_from_accepts([]) is None
    assert price_from_accepts([{"scheme": "exact"}]) is None
    assert price_from_accepts([{"amount": "not-a-number"}]) is None


def test_the_catalog_yields_one_quote_per_listing_keyed_on_who_gets_paid():
    catalog = {
        "items": [
            {"resource": RES, "accepts": [{"amount": "1000", "payTo": A}]},
            {"resource": RES, "accepts": [{"amount": "2000", "payTo": B}]},
            {"resource": "other", "accepts": [{"amount": "5", "payTo": C}]},
            {"resource": RES, "accepts": [{"payTo": C}]},  # unpriced
        ]
    }
    qs = quotes_from_catalog(catalog, RES)
    assert {q.seller for q in qs} == {A, B}
    assert all(q.source == "catalog" for q in qs)


def test_fills_are_read_from_the_same_ledger_the_tape_publishes():
    receipts = [
        {"resource": RES, "amount_usdc": 0.5, "seller": A, "settled_at": 1.0},
        {"resource": RES, "amount_usdc": 0.0, "seller": B},          # unpriced
        {"resource": "other", "amount_usdc": 9.0, "seller": C},      # wrong resource
        {"resource": RES, "amount_usdc": 0.7, "seller": B, "quantity": 2.0, "unit": "tok"},
    ]
    qs = quotes_from_receipts(receipts, RES)
    assert [q.seller for q in qs] == [A, B]
    assert all(q.source == "fill" for q in qs)


def test_a_basis_names_every_kind_of_evidence_behind_it():
    p = par_from_quotes(RES, [_q(A, 1.0, "catalog"), _q(B, 2.0, "fill")])
    assert p.basis == "catalog + fill"


# --- units -----------------------------------------------------------------

def test_a_missing_quantity_makes_the_unit_price_none_never_zero():
    """PaymentReceipt.unit_price makes the same choice: a service with no
    recorded quantity must not render as free."""
    assert _q(A, 1.0).unit_price is None
    assert _q(A, 1.0, quantity=0.0).unit_price is None
    assert _q(A, 1.0, quantity=2.0).unit_price == pytest.approx(0.5)


def test_the_even_sample_median_is_the_lower_of_the_two_middle_prices():
    """Pinned because it is chosen, not accidental: the house weighted_median is
    a 50% quantile. It biases par down and over_par up, which is why the verdict
    keys off saving (vs best) rather than off over_par."""
    p = par_from_quotes(RES, [_q(A, 1.00), _q(B, 1.20)])
    assert p.par_usdc == pytest.approx(1.00)

    # The bias cannot reach the number that becomes a claim.
    out = assess(1.00, p)
    assert out["over_par_usdc"] == pytest.approx(0.0)
    assert out["saving_usdc"] == pytest.approx(0.0)
    assert out["verdict"] == "at_par", "not over_par, despite the downward median"


# --- the market basket: real prices, and what they are allowed to do -------

UNIT = "$/1k tokens"


def _basket_dir(tmp_path, rows, fetched="2026-10-06T00:00:00+00:00", missing=()):
    """An anchors/ tree with one dated basket, shaped like the real thing."""
    d = tmp_path / "ACR-INF"
    d.mkdir(parents=True)
    (d / "2026-10-06.json").write_text(json.dumps({
        "index_id": "ACR-INF", "unit": UNIT, "fetched_at": fetched,
        "rows": rows, "missing": list(missing),
    }))
    # `_basket/` holds the selection RULES, not observations. It must be skipped.
    (tmp_path / "_basket").mkdir()
    (tmp_path / "_basket" / "ACR-INF.json").write_text('{"unit": "$/1k tokens"}')
    return tmp_path


def _row(model: str, price: float) -> dict:
    return {"id": model, "label": model, "price_usd_per_unit": price,
            "quality": {"model_class": "mid"},
            "source": {"kind": "http", "url": "https://example.test/models"}}


def test_the_shipped_basket_is_real_prices_and_none_of_them_are_ours():
    """Against `anchors/` as committed, not a fixture.

    This is the claim RFB 3's "what the market is actually paying" rests on, so
    it is checked against the file a reviewer would open. Every row is a third
    party by construction — a market basket names a model, and a model is not
    an address this deployment holds a key for.
    """
    b = market_basket(UNIT)
    assert b.status == "" and b.quotes, f"no usable basket: {b.status}"
    assert b.index_id == "ACR-INF"
    assert all(q.source == "market" for q in b.quotes)
    assert not any(q.first_party for q in b.quotes)
    # And the rows it HASN'T got are reported, because a basket that shrinks
    # silently re-medians a different population — GAP.md's own words.
    assert b.requested >= b.rows > 0


def test_a_basket_reports_what_it_is_missing(tmp_path):
    root = _basket_dir(tmp_path, [_row("a/one", 0.0004), _row("b/two", 0.0006)],
                       missing=["c/three", "d/four"])
    b = market_basket(UNIT, anchor_dir=root)
    assert (b.rows, b.requested) == (2, 4)


def test_a_stale_basket_is_a_named_refusal_and_never_a_price(tmp_path):
    """`--fetch` is manual by design, so an old basket is the expected failure.
    A price nobody refreshed is still a number, which is what makes it
    dangerous: it would price a live invoice against a market that has moved."""
    root = _basket_dir(tmp_path, [_row("a/one", 0.0004)],
                       fetched="2026-01-01T00:00:00+00:00")
    b = market_basket(UNIT, anchor_dir=root, now=1_790_000_000.0)
    assert b.status == "STALE"
    assert b.quotes == (), "a stale basket must not hand back a price"
    assert b.usable is False
    # …and the same file, read inside the window, is fine.
    fresh = market_basket(UNIT, anchor_dir=root, now=1_767_225_600.0 + ANCHOR_MAX_AGE_S / 2)
    assert fresh.status == "" and len(fresh.quotes) == 1


def test_an_aging_basket_is_said_out_loud_and_still_prices(tmp_path):
    """The six days between the warning and the cliff, which the product spent
    answering "ok".

    `AGE_WARN_FRACTION` has always decided that past 0.8 of the window a basket
    is worth mentioning, and until 2026-10-12 it lived in `scripts/anchors.py`,
    so the only thing that ever consulted it was `make ci` — against the
    anchors in the REPO, while the press reads the ones in its image.

    THE SECOND HALF OF THIS TEST IS THE IMPORTANT HALF. `usable` is
    `bool(quotes) and not status`, and `app.py` reroutes to the fleet on
    `not usable` — so putting this warning in `status` would take a healthy
    basket off the market and price a stranger's bill on our own scale, which
    is the ±196,546 bp reading the warning exists to prevent. The band must
    label and never refuse.
    """
    root = _basket_dir(tmp_path, [_row("a/one", 0.0004), _row("b/two", 0.0006)])
    at = datetime.fromisoformat("2026-10-06T00:00:00+00:00").timestamp()
    warn = AGE_WARN_FRACTION * ANCHOR_MAX_AGE_S

    just_inside = market_basket(UNIT, anchor_dir=root, now=at + warn - 3600)
    assert just_inside.wire_status == "ok"
    assert just_inside.aging is False

    aging = market_basket(UNIT, anchor_dir=root, now=at + warn + 3600)
    # THE HALF THAT MUST NOT MOVE, ASSERTED FIRST, so that an implementation
    # which routes this through `status` fails on the message that explains the
    # damage rather than on a casing mismatch three lines down. Checked when
    # the ordering was reversed: it failed on `wire_status` instead, which is a
    # true report of a wrong thing.
    assert aging.status == "", "the band is a label, never a refusal"
    assert aging.usable is True, "an aging basket must NOT reroute to the fleet"
    assert len(aging.quotes) == 2, "it still prices, with the same rows"
    # …and only then, that it actually says so.
    assert aging.wire_status == "aging", "past the warn mark it must say so"
    assert aging.aging is True

    # And the cliff still refuses, so the new band did not soften it.
    past = market_basket(UNIT, anchor_dir=root, now=at + ANCHOR_MAX_AGE_S + 3600)
    assert past.wire_status == "STALE" and past.usable is False


def test_an_age_it_cannot_know_is_none_and_never_zero(tmp_path):
    """Zero would read as "fetched this instant" — the freshest possible answer
    for the least evidence. The same refusal `held_usdc` makes on a failed
    balance read, and `chainMismatch` on an unparseable chain id."""
    root = _basket_dir(tmp_path, [_row("a/one", 0.0004)], fetched="not a date")
    b = market_basket(UNIT, anchor_dir=root)
    assert b.age_s is None
    assert b.aging is False, "unknowable is not aging"
    assert b.wire_status == "ok", "and it is not a refusal either"
    # A unit with no basket at all reports the same way.
    assert market_basket("$/furlong", anchor_dir=root).age_s is None


def test_the_warn_fraction_is_imported_by_the_gate_not_copied(tmp_path):
    """`scripts/anchors.py` declared its own `AGE_WARN_FRACTION` until
    2026-10-12. Two copies of the number that decides when to warn is how the
    CI gate and the live surface came to disagree for six days at a stretch,
    and `anchors.py` makes exactly this argument about `ANCHOR_MAX_AGE_S` four
    lines above the import."""
    src = (ROOT / "scripts" / "anchors.py").read_text(encoding="utf-8")
    assert "AGE_WARN_FRACTION" in src, "the gate must still consult it"
    assert "AGE_WARN_FRACTION = " not in src, (
        "scripts/anchors.py declares its own AGE_WARN_FRACTION again — import "
        "it from index_api.par, where the cliff it divides lives"
    )
    assert 0.0 < AGE_WARN_FRACTION < 1.0, (
        "the warn mark must fall BEFORE the cliff; at or past it the basket has "
        "already stopped returning quotes and the warning is an obituary"
    )


def test_a_unit_with_no_basket_says_absent_rather_than_empty(tmp_path):
    root = _basket_dir(tmp_path, [_row("a/one", 0.0004)])
    b = market_basket("$/furlong", anchor_dir=root)
    assert b.status == "ABSENT" and b.quotes == ()


def test_the_selection_rules_directory_is_not_read_as_observations(tmp_path):
    """`anchors/_basket/` holds the aggregation rule per index and carries a
    `unit`, so a reader that globbed every directory would match it and return
    a basket with no rows."""
    root = _basket_dir(tmp_path, [_row("a/one", 0.0004)])
    assert market_basket(UNIT, anchor_dir=root).index_id == "ACR-INF"


# --- whose prices are these -----------------------------------------------

def test_first_party_is_resolved_against_the_fleet_this_deployment_runs():
    assert FLEET_SELLERS, "the fleet registry is empty, so the disclosure is vacuous"
    ours = sorted(FLEET_SELLERS)[0]
    assert is_first_party(ours) is True
    assert is_first_party(ours.upper()) is True, "an address is not case-sensitive"
    assert is_first_party("0x" + "9a" * 20) is False
    assert is_first_party("openai/gpt-4o-mini") is False
    assert is_first_party("") is False


def test_a_par_says_how_many_of_its_sellers_are_ours():
    """The standard `graph/schema.graphql` already states: a benchmark that
    counts its own seller silently is claiming security it has not got."""
    ours = sorted(FLEET_SELLERS)[0]
    par = par_from_quotes(RES, [
        _q(ours, 1.0, first_party=True),
        _q(B, 1.2),
        _q(C, 1.4),
    ])
    assert par.sellers == 3
    assert par.first_party_sellers == 1
    assert par.as_dict()["first_party_sellers"] == 1


def test_the_best_quote_carries_where_it_came_from():
    """A caller cannot tell a payable seller from a price reference by looking
    at the string, so the source travels with the figure."""
    par = par_from_quotes(RES, [_q("openai/gpt-4o-mini", 0.0002, source="market"),
                                _q(B, 0.0009, source="market")])
    assert par.best_seller == "openai/gpt-4o-mini"
    assert par.best_source == "market"
    assert assess(0.01, par)["best_source"] == "market"
