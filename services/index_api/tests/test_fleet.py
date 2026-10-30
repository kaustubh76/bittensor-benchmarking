"""Seller-fleet tests — the priced, multi-seller tape TCA is computed against.

The property under test is not "the endpoint answers". It is that two sellers of
the SAME model class charge DIFFERENT unit prices, because without that every
payer's slippage against arrival is identically zero and there is nothing for
transaction-cost analysis to measure or for a rating to rank.
"""

from __future__ import annotations

from collections import Counter

from fastapi.testclient import TestClient
from index_api.app import app
from index_api.fleet import (
    FLEET,
    fleet_summary,
    listing_for,
    listing_for_resource,
    listings_for_index,
)
from index_api.x402 import PaymentReceipt, get_facilitator, reset_facilitator

client = TestClient(app)


def _pay(amount: float) -> dict:
    return {"PAYMENT-SIGNATURE": f"x402 0xfleet-buyer:{amount}"}


# --- the property the whole fleet exists to provide -------------------------


def test_some_index_has_two_same_class_sellers_at_different_prices():
    """The like-for-like comparison a reroute suggestion must rest on.

    Across model classes a price gap is quality, which the hedonic stage adjusts
    away — it is not evidence anyone overcharged. Only a same-class pair makes
    "you could have paid less for the same thing" a true sentence.
    """
    for index_id in {x.index_id for x in FLEET.values()}:
        by_class = Counter(x.model_class for x in listings_for_index(index_id))
        for model_class, count in by_class.items():
            if count < 2:
                continue
            prices = {
                x.unit_price_usdc
                for x in listings_for_index(index_id)
                if x.model_class == model_class
            }
            assert len(prices) == count, (
                f"{index_id}/{model_class.value}: {count} sellers but "
                f"{len(prices)} distinct prices — no dispersion to measure"
            )
            return
    raise AssertionError("no index has two sellers of the same class to compare")


def test_prices_are_deterministic():
    """A tape whose prices move between runs cannot back a published benchmark."""
    first = {k: v.unit_price_usdc for k, v in FLEET.items()}
    import importlib

    import index_api.fleet as fleet_mod

    importlib.reload(fleet_mod)
    assert {k: v.unit_price_usdc for k, v in fleet_mod.FLEET.items()} == first


def test_every_listing_prices_a_real_index_in_its_own_unit():
    for listing in FLEET.values():
        assert listing.unit_price_usdc > 0
        assert listing.quantity > 0
        assert listing.unit
        # amount is what the 402 advertises: price × quantity, not a flat fee.
        assert abs(listing.amount_usdc - listing.unit_price_usdc * listing.quantity) < 1e-12


def test_sellers_are_distinct_addresses():
    addresses = [x.seller for x in FLEET.values()]
    assert len(set(addresses)) == len(addresses)


# --- resolution --------------------------------------------------------------


def test_listing_resolves_from_its_resource_path():
    label = next(iter(FLEET))
    assert listing_for_resource(f"/compute/{label}") is listing_for(label)
    assert listing_for_resource(f"https://acr.example/compute/{label}") is listing_for(label)


def test_unlisted_resources_resolve_to_nothing():
    """Malformed compute paths and the free reads resolve to nothing. The press's
    own PAID endpoints no longer belong in this list: they resolve to a platform
    listing (see the platform tests), because a settlement with no seller was a
    settlement the mirror refused, and the buyer agent's default discovery buys
    exactly those paths."""
    assert listing_for_resource("/compute/") is None
    assert listing_for_resource("") is None
    assert listing_for_resource("/compute/not-a-seller") is None
    assert listing_for_resource("/health") is None
    assert listing_for_resource("/fleet") is None


# --- the wire ----------------------------------------------------------------


def test_fleet_endpoint_publishes_the_spread():
    body = client.get("/fleet").json()
    assert len(body["listings"]) == len(FLEET)
    assert {r["label"] for r in body["listings"]} == set(FLEET)
    assert body["listings"] == fleet_summary()


def test_an_unknown_seller_is_404_before_it_is_402():
    """A Circle settlement is irrevocable. Charging for a seller that cannot
    answer would take money for nothing — and the payment would land on the
    PLATFORM wallet, because the fleet lookup finds nothing to override it."""
    assert client.get("/compute/not-a-seller").status_code == 404


def test_the_challenge_advertises_the_sellers_own_terms():
    a, b = _two_same_class_labels()
    ra = client.get(f"/compute/{a}").json()["accepts"][0]
    rb = client.get(f"/compute/{b}").json()["accepts"][0]
    assert ra["payTo"] == listing_for(a).seller
    assert rb["payTo"] == listing_for(b).seller
    assert ra["payTo"] != rb["payTo"]
    # The dispersion has to be visible on the wire, not just in our table.
    assert ra["amount"] != rb["amount"]


def test_the_flat_index_price_does_not_buy_a_fleet_call():
    """When the price was one global this check read the platform's flat fee,
    which would wave through a buyer paying a fiftieth of what is due."""
    label = next(iter(FLEET))
    r = client.get(f"/compute/{label}", headers=_pay(0.0001))
    assert r.status_code == 402
    assert "insufficient" in r.json()["detail"]


def test_a_paid_call_returns_the_terms_it_charged():
    label = next(iter(FLEET))
    listing = listing_for(label)
    r = client.get(f"/compute/{label}", headers=_pay(listing.amount_usdc * 2))
    assert r.status_code == 200
    body = r.json()
    assert body["seller"] == listing.seller
    assert body["unit_price_usdc"] == listing.unit_price_usdc
    assert body["quantity"] == listing.quantity


def test_a_fleet_receipt_carries_who_was_paid_and_what_was_bought():
    """Without these three fields a settlement has no unit price, and a tape of
    settlements with no unit price is what made TCA unmeasurable before."""
    reset_facilitator()
    label = next(iter(FLEET))
    listing = listing_for(label)
    client.get(f"/compute/{label}", headers=_pay(listing.amount_usdc * 2))
    receipt = get_facilitator().recent[-1]
    assert receipt.seller == listing.seller
    assert receipt.unit == listing.unit
    assert receipt.quantity == listing.quantity
    reset_facilitator()


def test_an_index_receipt_names_the_press_as_its_own_seller():
    """The old rule here was that a receipt naming a seller for a flat index
    endpoint "would put another wallet's name on ACR's own revenue". The new
    listing names the PRESS'S OWN payee — the wallet the money actually reaches —
    so the revenue is attributed to exactly the wallet that received it, and the
    receipt gains the two fields the mirror needs: a unit and a quantity.

    The alternative was the state measured on 2026-09-13: sixty real settlements
    from a verified human, refused as "no seller", absent from the tape."""
    from acr_core import get_settings
    from index_api.fleet import PLATFORM_INDEX_ID, listing_for_resource
    from index_api.x402 import PAY_TO

    reset_facilitator()
    client.get("/prints/ACR-INF", headers=_pay(0.0001))
    receipt = get_facilitator().recent[-1]
    assert receipt.seller == (get_settings().x402_pay_to or PAY_TO)
    assert receipt.unit == "$/query"
    assert receipt.quantity == 1.0
    assert receipt.unit_price == get_settings().x402_price_usdc
    # And it is mirrored under an index with no print ring — never a compute one.
    assert listing_for_resource(receipt.resource).index_id == PLATFORM_INDEX_ID
    reset_facilitator()


def test_a_receipt_without_a_quantity_has_no_unit_price():
    """Not zero. A zero would read as 'free' where the truth is 'unknown', and
    every legacy row in the committed archive is exactly this case."""
    legacy = PaymentReceipt(payer="0xa", amount_usdc=0.0001, tx_ref="ref")
    assert legacy.unit_price is None
    priced = PaymentReceipt(payer="0xa", amount_usdc=0.005, tx_ref="ref", quantity=0.01)
    assert priced.unit_price == 0.5


def _two_same_class_labels() -> tuple[str, str]:
    for index_id in {x.index_id for x in FLEET.values()}:
        by_class: dict = {}
        for x in listings_for_index(index_id):
            by_class.setdefault(x.model_class, []).append(x.label)
        for labels in by_class.values():
            if len(labels) >= 2:
                return labels[0], labels[1]
    raise AssertionError("fleet has no same-class pair")
