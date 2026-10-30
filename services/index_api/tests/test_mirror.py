"""Mirror-policy tests — which settlements may become public benchmark rows.

Everything this layer lets through becomes a row in a tape that cannot be edited
afterwards, so the interesting cases are all the ones it must refuse.
"""

from __future__ import annotations

from index_api.mirror import BATCH, MAX_MIRROR_LAG_S, mirror_once, mirrorable
from index_api.x402 import PaymentReceipt

NOW = 1_785_000_000.0
SELLER = "0x" + "22" * 20
PAYER = "0x" + "11" * 20


def _receipt(**over) -> PaymentReceipt:
    base = dict(
        payer=PAYER,
        amount_usdc=0.005229,
        tx_ref="ref-1",
        network="eip155:5042002",
        scheme="exact",
        settled_at=NOW - 30,
        resource="/compute/acr-seller-inf-mid-a",
        seller=SELLER,
        unit="$/1k tokens",
        quantity=0.01,
    )
    base.update(over)
    return PaymentReceipt(**base)


class _FakeClient:
    """A MirrorClient stand-in that records calls instead of spending gas."""

    def __init__(self, already: dict | None = None, fail_on: str = ""):
        self.opened: list[str] = []
        self.finalized: list[str] = []
        self._already = already or {}
        self._fail_on = fail_on

    def configured(self) -> bool:
        return True

    def state_for(self, tx_ref: str):
        return self._already.get(tx_ref)

    def open_settlement(self, tx_ref, **kw):
        if self._fail_on == "open":
            raise RuntimeError("chain said no")
        self.opened.append(tx_ref)
        # Remember it, because the chain does. A fake that forgets what it
        # opened re-opens the same settlement every tick and can therefore never
        # model a backlog — which is precisely why the drain bug below was
        # invisible to this suite until the operator hit it on real receipts.
        self._already[tx_ref] = {"opened": True, "finalized": False}
        return "0xopen"

    def finalize_settlement(self, tx_ref, **kw):
        if self._fail_on == "finalize":
            raise RuntimeError("chain said no")
        self.finalized.append(tx_ref)
        self._already[tx_ref] = {"opened": True, "finalized": True}
        return "0xfinalize"


# --- the gates ---------------------------------------------------------------


def test_a_well_formed_fleet_receipt_is_mirrorable():
    assert mirrorable(_receipt(), NOW) == (True, "")


def test_a_dev_receipt_never_reaches_the_chain():
    """Invented revenue on a public counter is bad; invented settlements in a
    published benchmark are worse."""
    ok, why = mirrorable(_receipt(scheme="dev"), NOW)
    assert not ok and "real settlement" in why


def test_a_receipt_with_no_seller_is_still_refused():
    """No seller means no unit price, and that is still a refusal.

    What changed is upstream: the press's own paid endpoints now resolve to a
    platform listing in `_record()`, so a platform receipt ARRIVES here with a
    seller. This gate therefore covers only a genuinely malformed receipt — one
    that reached the mirror without going through `_record()` at all."""
    ok, why = mirrorable(_receipt(seller=""), NOW)
    assert not ok and "unit price" in why


def test_a_receipt_without_a_quantity_is_refused():
    ok, why = mirrorable(_receipt(quantity=0.0), NOW)
    assert not ok and "quantity" in why


def test_a_settlement_dated_into_the_future_is_refused():
    ok, why = mirrorable(_receipt(settled_at=NOW + 60), NOW)
    assert not ok and "future" in why


def test_a_settlement_past_the_mirror_window_is_refused_not_forced():
    """The contract bounds the ordinary path at an hour. Pushing an older one
    through the owner-only late path is an operator's decision, not a keeper's."""
    ok, why = mirrorable(_receipt(settled_at=NOW - MAX_MIRROR_LAG_S - 1), NOW)
    assert not ok and "late path" in why


def test_the_window_boundary_is_inclusive():
    assert mirrorable(_receipt(settled_at=NOW - MAX_MIRROR_LAG_S), NOW)[0]


# --- the sweep ---------------------------------------------------------------


def test_an_eligible_receipt_is_opened_then_finalized():
    c = _FakeClient()
    verdict = mirror_once([_receipt()], client=c, now=NOW)
    assert c.opened == ["ref-1"]
    assert c.finalized == ["ref-1"]
    assert "opened 1" in verdict and "finalized 1" in verdict


def test_an_already_open_settlement_is_only_finalized():
    """Re-running must not re-open. The contract would revert, but spending gas
    to be told what a read could have said is the keeper wasting the press's
    money every tick."""
    c = _FakeClient(already={"ref-1": {"opened": True, "finalized": False}})
    mirror_once([_receipt()], client=c, now=NOW)
    assert c.opened == []
    assert c.finalized == ["ref-1"]


def test_a_fully_mirrored_settlement_is_left_alone():
    c = _FakeClient(already={"ref-1": {"opened": True, "finalized": True}})
    assert mirror_once([_receipt()], client=c, now=NOW) is None
    assert c.opened == [] and c.finalized == []


def test_one_failure_does_not_stop_the_others():
    """A chore that throws costs the press a beat."""
    c = _FakeClient(fail_on="open")
    verdict = mirror_once([_receipt(tx_ref="a"), _receipt(tx_ref="b")], client=c, now=NOW)
    assert "FAILED 2" in verdict
    assert c.finalized == []


def test_the_sweep_is_bounded_per_tick():
    c = _FakeClient()
    many = [_receipt(tx_ref=f"ref-{i}") for i in range(BATCH + 5)]
    mirror_once(many, client=c, now=NOW)
    assert len(c.opened) == BATCH


def test_a_backlog_larger_than_one_tick_actually_drains():
    """BATCH bounds the WORK, not the candidate list.

    It used to slice `eligible[:BATCH]`, so a backlog larger than BATCH could
    never drain: the first BATCH stay eligible for the whole mirror window, are
    re-examined every tick as already-finalized no-ops, and the next one is
    never reached. The keeper never noticed because live receipts arrive a few
    at a time and age out — but `make mirror-receipts`, whose whole documented
    purpose includes "a backlog", stalled at exactly BATCH forever.
    """
    c = _FakeClient()
    many = [_receipt(tx_ref=f"ref-{i}") for i in range(BATCH * 2 + 3)]

    mirror_once(many, client=c, now=NOW)
    assert len(c.opened) == BATCH, "first tick does one batch"

    mirror_once(many, client=c, now=NOW)
    assert len(c.opened) == BATCH * 2, "second tick must reach past the first batch"

    mirror_once(many, client=c, now=NOW)
    assert len(c.opened) == len(many), "the backlog drains"

    # And once drained it is quiet, rather than re-reporting work it did not do.
    assert mirror_once(many, client=c, now=NOW) is None


def test_a_backlog_past_the_window_is_reported_not_silently_dropped():
    """A hole in a public tape that nobody is told about reads as an absence of
    trading rather than an absence of mirroring."""
    c = _FakeClient()
    stale = _receipt(tx_ref="old", settled_at=NOW - 2 * MAX_MIRROR_LAG_S)
    verdict = mirror_once([_receipt(), stale], client=c, now=NOW)
    assert "operator late-path needed" in verdict


def test_an_unconfigured_client_stands_down_quietly():
    class _Off(_FakeClient):
        def configured(self):
            return False

    assert mirror_once([_receipt()], client=_Off(), now=NOW) is None


def test_a_receipt_for_an_unknown_resource_is_skipped():
    c = _FakeClient()
    # `/compute/<label>` for a label that is not in FLEET: it passed `mirrorable`
    # (a seller and a quantity were stamped on it somehow) but nothing resolves
    # it, so there is no index to mirror it under. Used to use /prints here —
    # that path now resolves to the platform listing, and is asserted positively
    # below, so this needs a resource that is unlisted on purpose.
    verdict = mirror_once([_receipt(resource="/compute/not-a-seller")], client=c, now=NOW)
    assert c.opened == []
    assert verdict is not None and "no listing" in verdict


# --- platform receipts: the press as its own seller --------------------------
# The buyer agent's DEFAULT discovery buys /prints, /curve and /vol. Until these
# existed, every one of those settlements was refused above and vanished from the
# tape — sixty real x402 settlements from a verified human did exactly that on
# 2026-09-13. The fix is one resolver; these pin what it must and must not do.


class _CapturingClient(_FakeClient):
    """Records the kwargs too, so the index id and service can be asserted."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.open_kw: dict = {}
        self.finalize_kw: dict = {}

    def open_settlement(self, tx_ref, **kw):
        self.open_kw = kw
        return super().open_settlement(tx_ref, **kw)

    def finalize_settlement(self, tx_ref, **kw):
        self.finalize_kw = kw
        return super().finalize_settlement(tx_ref, **kw)


def _platform_receipt(**over) -> PaymentReceipt:
    """A /vol receipt AS `_record()` now enriches it — the shape refused this morning."""
    from index_api.fleet import listing_for_resource

    resource = over.pop("resource", "/vol/ACR-INF")
    listing = listing_for_resource(resource)
    assert listing is not None, "the platform resolver must recognise its own paid path"
    return _receipt(
        resource=resource,
        seller=listing.seller,
        unit=listing.unit,
        quantity=listing.quantity,
        amount_usdc=listing.amount_usdc,
        **over,
    )


def test_a_platform_receipt_is_now_mirrorable():
    """The exact receipt the mirror refused this morning, after enrichment."""
    ok, why = mirrorable(_platform_receipt(), NOW)
    assert ok, why


def test_a_platform_receipt_carries_the_flat_price_as_its_unit_price():
    """One query per call, so the unit price IS the price the 402 advertised —
    the one figure a judge can check without trusting us."""
    from acr_core import get_settings

    r = _platform_receipt()
    assert r.quantity == 1.0
    assert r.unit == "$/query"
    assert r.unit_price == get_settings().x402_price_usdc


def test_a_platform_receipt_mirrors_under_the_query_index_not_a_compute_one():
    """THE LOAD-BEARING ASSERTION. The subgraph benchmarks a settlement against
    the print ring of its indexId. A $0.0001 query fee measured against a $0.49
    ACR-INF print would read as -99.98% slippage — garbage in the seller's grade
    and the payer's TCA, wearing the shape of a measurement. ACR-QUERY has no
    ring, so it lands `benchmarked = false` with a stated reason instead."""
    from acr_core import Service
    from index_api.fleet import PLATFORM_INDEX_ID

    c = _CapturingClient()
    mirror_once([_platform_receipt()], client=c, now=NOW)
    assert c.opened == ["ref-1"] and c.finalized == ["ref-1"]
    assert c.open_kw["index_id"] == PLATFORM_INDEX_ID
    assert c.open_kw["index_id"] not in ("ACR-INF", "ACR-GPU", "ACR-DATA")
    assert c.finalize_kw["service"] == Service.DATA
    assert c.finalize_kw["quantity"] == 1.0


def test_every_platform_family_resolves_and_every_free_read_does_not():
    from index_api.fleet import PLATFORM_INDEX_ID, listing_for_resource

    for path in ("/prints", "/prints/ACR-INF", "/curve/ACR-GPU", "/vol/ACR-DATA",
                 "/seller-scores/ACR-INF?days=7", "https://acr-api-1fto.onrender.com/vol/ACR-INF"):
        lst = listing_for_resource(path)
        assert lst is not None and lst.index_id == PLATFORM_INDEX_ID, path
    for path in ("/health", "/fleet", "/humanid/info", "/terminal/data", "/graph/operations", ""):
        assert listing_for_resource(path) is None, path


def test_a_compute_path_still_resolves_to_the_fleet_unchanged():
    from index_api.fleet import FLEET, listing_for_resource

    lst = listing_for_resource("/compute/acr-seller-inf-mid-a")
    assert lst is FLEET["acr-seller-inf-mid-a"]
    assert lst.index_id == "ACR-INF"


def test_platform_listings_never_enter_the_fleet_summary_or_comparable_sets():
    """/fleet and the catalog iterate FLEET. The press sells benchmark queries,
    not inference, and must not appear as a compute seller anyone is compared
    against — or a reroute suggestion could be built on it."""
    from index_api.fleet import PLATFORM_INDEX_ID, fleet_summary, listings_for_index

    assert all(row["index_id"] != PLATFORM_INDEX_ID for row in fleet_summary())
    assert listings_for_index(PLATFORM_INDEX_ID) == []
