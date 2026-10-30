"""The seller fleet — the priced, multi-seller tape TCA is computed against.

Until this existed, every x402 settlement in the system went to ONE platform
wallet at ONE flat price (``ACR_X402_PRICE_USDC``). ``ReceiptSource`` says so in
its own docstring, and draws the right conclusion: fed to the estimator such a
tape yields zero surviving observations. It also means every payer's slippage
against arrival is identically zero, so transaction-cost analysis has nothing to
measure and a seller rating has nothing to rank.

A fleet fixes that at the source. Each listing is a real x402-gated endpoint
with its OWN payee address and its OWN unit price, so a settlement carries a
unit price that differs between sellers — which is the entire input to TCA.

Two properties this file exists to keep true:

* **Like-for-like.** The comparison that matters is between sellers of the same
  ``model_class``. A frontier seller charging more than an open one is not
  overcharging, it is selling something else, and the hedonic stage exists to
  adjust that away. The same-class pair in ``DEMO_SELLERS`` is what a reroute
  suggestion is allowed to be built on.
* **Deterministic.** Premia are a pure function of the seller's label, so the
  same fleet prices the same way on every machine and in every test. Nothing
  here samples a random number: a tape whose prices move when you re-run it
  cannot be the basis of a published benchmark.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from acr_core import INDEX_REGISTRY, ModelClass, Service, index_for_service
from acr_oracle_client.demo_sellers import DEMO_SELLERS, DemoSeller

#: Quality premium over the index reference level, by model class. Signed, and
#: economically ordered: frontier capacity is scarcer than open-weights.
_CLASS_PREMIUM: dict[ModelClass, float] = {
    ModelClass.FRONTIER: 0.18,
    ModelClass.MID: 0.02,
    ModelClass.SMALL: -0.05,
    ModelClass.OPEN: -0.09,
}

#: How far a single seller may sit from its class's premium, in either
#: direction. This is the dispersion TCA actually measures: two sellers of the
#: same class, one of them dearer, and a payer who could have chosen. Kept small
#: (±4%, i.e. ±400bp) so the numbers stay in a range a real market would show
#: rather than a range chosen to make the demo look dramatic.
_IDIOSYNCRATIC = 0.04


@dataclass(frozen=True)
class Listing:
    """One x402-gated compute endpoint, priced per unit."""

    label: str
    seller: str
    index_id: str
    service: Service
    model_class: ModelClass
    #: The index's own unit — "$/1k tokens", "$/GPU-sec", "$/MB".
    unit: str
    #: USDC per unit. What TCA compares against the arrival print.
    unit_price_usdc: float
    #: Units delivered per paid call. The index's published per-settlement
    #: quantity convention, so an amount can be divided back into a unit price.
    quantity: float

    @property
    def resource(self) -> str:
        return f"/compute/{self.label}"

    @property
    def amount_usdc(self) -> float:
        """What one call costs — the figure the 402 advertises."""
        return self.unit_price_usdc * self.quantity


def _premium(seller: DemoSeller) -> float:
    """A stable per-seller premium: class base ± an idiosyncratic spread.

    Derived from the label's digest rather than drawn at random, so the fleet is
    byte-identical on every machine. The spread is what makes two same-class
    sellers comparable-but-different, which is the whole point of the fleet.
    """
    digest = hashlib.sha256(f"acr-fleet::{seller.label}".encode()).digest()
    # digest[0]/255 -> [0, 1]; centred and scaled to ±_IDIOSYNCRATIC.
    offset = (digest[0] / 255.0 - 0.5) * 2.0 * _IDIOSYNCRATIC
    return _CLASS_PREMIUM[seller.model_class] + offset


def _listing(seller: DemoSeller) -> Listing:
    spec = index_for_service(seller.service)
    return Listing(
        label=seller.label,
        seller=seller.address,
        index_id=spec.id,
        service=seller.service,
        model_class=seller.model_class,
        unit=spec.unit,
        unit_price_usdc=spec.reference_level * (1.0 + _premium(seller)),
        quantity=spec.arc_unit_qty,
    )


#: Every listing, keyed by label. Built once at import: the fleet is a constant.
FLEET: dict[str, Listing] = {s.label: _listing(s) for s in DEMO_SELLERS}


def listing_for(label: str) -> Listing | None:
    return FLEET.get(label)


#: The index id a platform receipt is mirrored under. NOT a compute index, on
#: purpose: the subgraph benchmarks every settlement against the print ring of
#: its indexId and computes slippage from the gap. A $0.0001 query fee measured
#: against a $0.49 ACR-INF print would read as -99.98% slippage — garbage in the
#: seller's grade and in the payer's TCA, wearing the shape of a measurement.
#: An id with no print ring lands as `benchmarked = false, NO_PRINT_YET`, which
#: is the truth: real volume, a real unit price, nothing to benchmark it against.
#: ReceiptMirror.sol puts no `require` on indexId and decodeIndexId is a generic
#: NUL-terminated decode, so any string under 32 bytes round-trips.
PLATFORM_INDEX_ID = "ACR-QUERY"

#: The press's own paid endpoints, by path family. These are what the buyer
#: agent's DEFAULT discovery buys — and until platform receipts carried a seller
#: and a unit price, every one of those settlements was refused by the mirror
#: ("no seller — a platform-flat receipt has no unit price") and vanished from
#: the tape. Sixty real settlements from a verified human did exactly that on
#: 2026-09-13 before this existed.
_PLATFORM_FAMILIES = ("prints", "curve", "vol", "seller-scores")


def platform_listing_for(resource: str, settings=None) -> Listing | None:
    """The press as its own seller, for its own x402-gated data endpoints.

    Kept OUT of ``FLEET`` deliberately. ``fleet_summary()``, ``listings_for_index()``,
    ``/fleet`` and the catalog iterate ``FLEET``, and the press must not appear as
    a compute seller in a comparable set — it sells benchmark queries, not
    inference. What this gives a platform receipt is exactly what ``_record()``
    and ``mirror_once()`` need and nothing more: a payee, a unit, a quantity, and
    an index id that keeps it honestly unbenchmarked.
    """
    if not resource:
        return None
    path = resource.split("?")[0]
    # Tolerate a full URL as well as a path — receipts store the path, the
    # catalog advertises the URL, and both have reached this function before.
    if "://" in path:
        path = "/" + path.split("://", 1)[1].split("/", 1)[1] if "/" in path.split("://", 1)[1] else "/"
    parts = [seg for seg in path.split("/") if seg]
    if not parts or parts[0] not in _PLATFORM_FAMILIES:
        return None
    family = parts[0]

    # Lazy on both counts: settings, so tests can swap the pay-to and price
    # without rebuilding a module constant; and PAY_TO from x402.py, which
    # imports THIS module lazily for the same reason — a top-level import in
    # either direction is a cycle.
    from acr_core import get_settings

    from .x402 import PAY_TO

    # The CALLER's settings when it has some — `_fleet_terms(resource, settings)`
    # builds the 402 from the settings the gate was constructed with, and a
    # payee read from the global singleton instead would advertise one address
    # in the challenge and settle to another whenever the two differ.
    s = settings or get_settings()
    return Listing(
        label=f"acr-press-{family}",
        seller=(s.x402_pay_to or PAY_TO),
        index_id=PLATFORM_INDEX_ID,
        # The press sells data. The on-chain service enum is inference/gpu/data
        # and adding a fourth value touches a deployed contract; DATA is the
        # honest fit among the three that exist.
        service=Service.DATA,
        # Required by the dataclass, surfaced nowhere: platform listings never
        # enter FLEET, so no comparable set or catalog row reads this.
        model_class=ModelClass.OPEN,
        unit="$/query",
        unit_price_usdc=float(s.x402_price_usdc),
        quantity=1.0,
    )


def listing_for_resource(resource: str, settings=None) -> Listing | None:
    """Resolve a paid path back to what was sold and by whom.

    ``/compute/<label>`` resolves to its fleet listing, exactly as before. The
    press's own paid endpoints resolve to a platform listing (see
    ``platform_listing_for``). Every other path — the free reads — returns None,
    and a receipt for one of those is not a settlement.
    """
    if not resource:
        return None
    prefix = "/compute/"
    idx = resource.find(prefix)
    if idx >= 0:
        label = resource[idx + len(prefix) :].split("/")[0].split("?")[0]
        return FLEET.get(label)
    return platform_listing_for(resource, settings)


def listings_for_index(index_id: str) -> list[Listing]:
    """Every listing pricing the same index — the comparable set."""
    return [x for x in FLEET.values() if x.index_id == index_id]


def fleet_summary() -> list[dict]:
    """The fleet as JSON, for the catalog and for a human checking the spread."""
    out: list[dict] = []
    for x in sorted(FLEET.values(), key=lambda v: (v.index_id, v.label)):
        ref = INDEX_REGISTRY[x.index_id].reference_level
        out.append(
            {
                "label": x.label,
                "seller": x.seller,
                "index_id": x.index_id,
                "unit": x.unit,
                "model_class": x.model_class.value,
                "unit_price_usdc": round(x.unit_price_usdc, 8),
                "quantity": x.quantity,
                "amount_usdc": round(x.amount_usdc, 8),
                # Where this seller sits against its index's reference level, in
                # basis points. Not a benchmark — the on-chain print is — but it
                # makes the fleet's own spread readable without one.
                "vs_reference_bp": round(1e4 * (x.unit_price_usdc - ref) / ref, 1),
            }
        )
    return out
