"""Marketplace tests — Bazaar-shaped catalog + settlement ledger (offline, hermetic)."""

from __future__ import annotations

from acr_core import ALL_INDEX_IDS, ModelClass, SellerAttestation, Service, reset_settings
from fastapi.testclient import TestClient
from index_api.fleet import FLEET
from index_api.marketplace import (
    NullRegistry,
    build_catalog,
    build_receipts,
    catalog_resources,
    get_registry,
    reset_registry,
    set_registry,
)
from index_api.x402 import DevFacilitator, reset_facilitator, set_facilitator


class FakeRegistry:
    def __init__(self, attestations):
        self._attestations = attestations

    def connected(self) -> bool:
        return True

    def all_attestations(self):
        return self._attestations


def _fake_attestations():
    return [
        SellerAttestation(
            seller="0xSellerA", service=Service.INFERENCE, model_class=ModelClass.FRONTIER,
            latency_slo_ms=250.0, schema_id="acr-v1", ts=0.0,
        ),
        SellerAttestation(
            seller="0xSellerB", service=Service.GPU, model_class=ModelClass.MID,
            latency_slo_ms=500.0, schema_id="acr-v1", ts=0.0,
        ),
    ]


def test_catalog_expands_all_resources():
    resources = catalog_resources()
    paths = [p for p, _ in resources]
    # 1 unparametrized (/prints) + 4 families × 3 indices.
    assert len(paths) == 1 + 4 * len(ALL_INDEX_IDS) == 13
    assert "/prints" in paths
    assert "/curve/ACR-INF" in paths and "/seller-scores/ACR-DATA" in paths
    assert not any("{index_id}" in p for p in paths)  # no template residue


def test_catalog_items_are_bazaar_shaped():
    reset_settings()
    cat = build_catalog("http://test:8000/", registry=NullRegistry())
    # 2, not 1: Circle's Discovery API serves x402Version 2 on every one of its
    # listings, and a crawler resolves a version mismatch by skipping you.
    assert cat["x402Version"] == 2
    # 13 index resources + one listing per fleet seller.
    assert len(cat["items"]) == 13 + len(FLEET)
    item = next(i for i in cat["items"] if i["resource"].endswith("/prints"))
    assert item["type"] == "http"
    acc = item["accepts"][0]
    # Requirements are the same objects the 402 challenge emits (v1+v2 keys).
    assert acc["scheme"] == "exact"
    assert acc["network"] == "eip155:5042002"
    assert acc["maxAmountRequired"] == acc["amount"] == "100"
    assert acc["resource"] == "http://test:8000/prints"
    assert acc["payTo"].startswith("0x")
    meta = item["metadata"]
    assert meta["description"] and meta["input"]["type"] == "object"
    assert "properties" in meta["output"]
    assert meta["provider"]["attestation"] is None  # offline → honest null
    # The fields Circle's Discovery API FILTERS on. Without them a listing is
    # present but unfindable — an agent narrowing by category or price never
    # sees it. FINANCIAL_ANALYSIS carries 447 of their 958 listings.
    prov = meta["provider"]
    assert prov["category"] == "FINANCIAL_ANALYSIS"
    assert prov["website"].startswith("http") and prov["docsUrl"].startswith("http")
    assert "x402" in prov["tags"] and "arc" in prov["tags"]
    assert prov["description"]


def test_catalog_honors_resource_base(monkeypatch):
    monkeypatch.setenv("ACR_X402_RESOURCE_BASE", "https://acr.example")
    reset_settings()
    try:
        cat = build_catalog("http://ignored:1234/", registry=NullRegistry())
        assert all(i["resource"].startswith("https://acr.example/") for i in cat["items"])
    finally:
        monkeypatch.undo()
        reset_settings()


def test_catalog_attestation_block_from_registry():
    reset_settings()
    cat = build_catalog("http://test/", registry=FakeRegistry(_fake_attestations()))
    att = cat["items"][0]["metadata"]["provider"]["attestation"]
    assert att["sellers_attested"] == 2
    assert att["services"] == ["gpu", "inference"]
    assert att["latency_slo_ms"] == {"min": 250.0, "max": 500.0}
    # The rows behind the count. The summary used to keep only the scalar, so
    # the removed /sellers page printed a "4" over sixty simulated rows that were
    # not those four, with nothing to show instead. Pinned field by field: this is
    # the wire a discovery crawler parses. Nothing in the Terminal renders these
    # rows today — ShopFloor reads `sellers_attested` alone — so the crawler is
    # now the whole audience, which is reason enough to keep the shape exact.
    assert att["sellers"] == [
        {
            "seller": "0xSellerA",
            "service": "inference",
            "model_class": "frontier",
            "latency_slo_ms": 250.0,
            "schema_id": "acr-v1",
        },
        {
            "seller": "0xSellerB",
            "service": "gpu",
            "model_class": "mid",
            "latency_slo_ms": 500.0,
            "schema_id": "acr-v1",
        },
    ]
    # Enum VALUES, not reprs — `ModelClass.FRONTIER` on the wire is unparseable.
    assert all(isinstance(r["service"], str) for r in att["sellers"])
    # Registry order (sellerAt(0..n-1)), not re-sorted: `services` above is free
    # to sort and discard who-filed-when; a row list is not.
    assert [r["seller"] for r in att["sellers"]] == ["0xSellerA", "0xSellerB"]
    # The count IS the rows' length, so the card and its table cannot disagree.
    assert len(att["sellers"]) == att["sellers_attested"]
    # One object, two readers: a crawler reading an item and one reading the
    # root must not get different reputation blocks.
    assert cat["provider"]["attestation"] == att


def test_registry_seam_defaults_to_null():
    reset_settings()
    reset_registry()
    try:
        assert isinstance(get_registry(), NullRegistry)  # no ACR_REGISTRY_ADDRESS → chain-free
    finally:
        reset_registry()


def test_registry_seam_injectable():
    fake = FakeRegistry([])
    set_registry(fake)
    try:
        assert get_registry() is fake
    finally:
        reset_registry()


def test_receipts_ledger_shape_and_order():
    fac = DevFacilitator()
    import asyncio

    from fastapi import Response

    for i in range(3):
        asyncio.run(fac.process(None, f"x402 0xagent-{i}:0.0001", Response()))
    ledger = build_receipts(fac)
    assert ledger["gate"] == "dev"
    assert ledger["paid_queries"] == 3
    assert ledger["price_usdc"] == 0.0001
    seqs = [r["seq"] for r in ledger["receipts"]]
    assert seqs == [3, 2, 1]  # newest first, monotone ordinals
    assert ledger["receipts"][0]["payer"] == "0xagent-2"
    # The dev tx_ref ordinal matches the ledger seq (dev-3 is Nº 3).
    assert ledger["receipts"][0]["tx_ref"] == "dev-3"
    assert all("timestamp" not in r and "date" not in r for r in ledger["receipts"])

    # --- the join between the catalog and the tape ---
    # PaymentReceipt has stamped the bought path for a while, but this builder
    # dropped it, so the shop floor (then /exchange) could list thirteen
    # resources and prove
    # thirty-four settlements with nothing connecting the two. The rule is
    # "emit it when it exists, omit it when it does not": most archived rows
    # predate the stamp, and an empty string would attribute all of them to one
    # nameless listing. Asserted here rather than as a new test() because
    # pytest's count is stated in six docs plus the architecture diagram.
    from index_api.x402 import PaymentReceipt

    fac.recent.append(
        PaymentReceipt(
            payer="0xbuyer",
            amount_usdc=0.0001,
            tx_ref="dev-4",
            network="eip155:5042002",
            scheme="exact",
            resource="/curve/ACR-GPU",
        )
    )
    rows = build_receipts(fac)["receipts"]
    stamped = next(r for r in rows if r["tx_ref"] == "dev-4")
    assert stamped["resource"] == "/curve/ACR-GPU"
    # ...and the three rows above it carry no resource at all, not "".
    assert all("resource" not in r for r in rows if r["tx_ref"] != "dev-4")

    # The catalog cannot advertise a gate that does not exist, nor miss one
    # that does: ENDPOINT_FAMILIES and app.GATED_ENDPOINTS are two lists
    # maintained by hand. The index families are the flat-priced five; the one
    # paid route outside them is the fleet's metered endpoint, which the catalog
    # carries as per-seller LISTINGS (each with its own terms) rather than as a
    # family. Named here exactly, so a seventh paid route cannot hide behind it.
    from index_api.app import GATED_ENDPOINTS
    from index_api.marketplace import ENDPOINT_FAMILIES

    families = {f["template"] for f in ENDPOINT_FAMILIES}
    assert families <= set(GATED_ENDPOINTS)
    assert set(GATED_ENDPOINTS) - families == {"/compute/{label}"}


def test_receipts_empty_state():
    ledger = build_receipts(DevFacilitator())
    assert ledger["paid_queries"] == 0 and ledger["receipts"] == []


def test_marketplace_endpoints_via_http():
    from index_api.app import app

    reset_settings()
    reset_facilitator()
    reset_registry()
    try:
        client = TestClient(app)
        cat = client.get("/marketplace/catalog")
        assert cat.status_code == 200
        assert len(cat.json()["items"]) == 13 + len(FLEET)
        # Discovery is free; the data itself still costs (402 without payment).
        assert client.get("/prints").status_code == 402

        set_facilitator(DevFacilitator())
        r = client.get("/vol/ACR-INF", headers={"X-Payment": "x402 0xledger-agent:0.0001"})
        assert r.status_code == 200
        led = client.get("/marketplace/receipts").json()
        assert led["paid_queries"] == 1
        assert led["receipts"][0]["payer"] == "0xledger-agent"
        # The root service card advertises the marketplace.
        assert client.get("/").json()["marketplace"]["catalog"] == "/marketplace/catalog"
    finally:
        reset_settings()
        reset_facilitator()
        reset_registry()


def test_catalog_lists_every_fleet_seller_at_its_own_terms():
    """The fleet has to be DISCOVERABLE, not merely live.

    A buyer agent picks what to pay for out of this catalog. While
    ``/compute/{label}`` appeared in no listing, nothing could ever route a
    payment to a fleet seller, so every settlement kept going to the single
    platform wallet at the single flat price — and a tape with no unit-price
    dispersion makes `slippageBp` identically zero and leaves every seller-side
    entity in the subgraph empty.
    """
    reset_settings()
    cat = build_catalog("http://test:8000/", registry=NullRegistry())
    compute = {
        i["metadata"]["label"]: i
        for i in cat["items"]
        if i["metadata"].get("family") == "compute"
    }
    assert set(compute) == set(FLEET), "every fleet listing must be discoverable"

    for label, listing in FLEET.items():
        item = compute[label]
        acc = item["accepts"][0]
        assert item["resource"] == f"http://test:8000{listing.resource}"
        # The advertised terms ARE the gate's terms — same builder, same values,
        # so a buyer that reconciles its own receipt against this cannot be
        # surprised at the 402.
        assert acc["payTo"] == listing.seller
        assert acc["maxAmountRequired"] == acc["amount"]
        assert int(acc["amount"]) == round(listing.amount_usdc * 1_000_000)
        assert acc["scheme"] == "exact"
        meta = item["metadata"]
        assert meta["seller"] == listing.seller
        assert meta["model_class"] == listing.model_class.value
        assert meta["unit_price_usdc"] == listing.unit_price_usdc

    # Each seller is paid at its OWN address: one shared payee would rebuild the
    # single-wallet tape the fleet exists to replace.
    payees = {i["accepts"][0]["payTo"] for i in compute.values()}
    assert len(payees) == len(FLEET)

    # And the like-for-like pair TCA is built on survives discovery: two sellers
    # of the same class on the same index, at different prices.
    mids = [
        i["metadata"]
        for i in compute.values()
        if i["metadata"]["model_class"] == "mid" and i["metadata"]["index_id"] == "ACR-INF"
    ]
    assert len(mids) == 2
    assert mids[0]["unit_price_usdc"] != mids[1]["unit_price_usdc"]
