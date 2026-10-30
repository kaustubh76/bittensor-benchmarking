"""Agent-marketplace surfaces — machine-readable listings + the settlement ledger.

``/marketplace/catalog`` lists every x402-gated endpoint in the shape agents
already parse in the wild (the x402 "Bazaar" item: ``{resource, type,
x402Version, accepts, metadata}``), so a buyer agent can discover, price, and
pay ACR services without human configuration. The on-chain
``AttestationRegistry`` doubles as the listing's reputation anchor (the
ERC-8004 idea — identity + attested quality metadata — surfaced here as
``metadata.provider.attestation``): a cautious buyer can require attested
sellers before paying.

``/marketplace/receipts`` is the public settlement ledger: the facilitator's
ring of recent :class:`~index_api.x402.PaymentReceipt` rows, newest first.
Receipts carry a monotone ``seq`` ordinal, deliberately not a wall-clock time
(prints and the Terminal narrate in Fixing numbers, never dates).

Offline-invariant: with no ``ACR_REGISTRY_ADDRESS`` the registry seam is a
``NullRegistry`` and ``provider.attestation`` is ``null`` — the catalog itself
never needs chain, Circle, or credentials.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import threading
import time

from acr_core import ALL_INDEX_IDS, get_settings, spec_for

from .x402 import Facilitator, build_payment_requirements, price_usdc

log = logging.getLogger("index_api.marketplace")

#: Cache the on-chain attestation summary: reading it is ~1 + 2·N rate-limited
#: RPC calls (sellerCount + sellerAt/getAttestation per seller), too slow to do
#: on every /marketplace/catalog request. The registry only changes when someone
#: attests, so a TTL above the refresh interval (warmed by the background loop)
#: keeps the request path from ever blocking. Mirrors onchain.READ_ALL_TTL_S.
ATTESTATION_TTL_S = 90.0

#: The x402 version the wire actually speaks. Circle's Discovery API serves
#: `x402Version: 2` on every one of its 958 listings; this catalog advertised 1,
#: which is the sort of mismatch a crawler resolves by skipping you.
X402_CATALOG_VERSION = 2

#: Discovery metadata. Overridable because a fork should describe itself, not us.
#:
#: THE ADDRESS A CRAWLER FOLLOWS. These two reach Circle's Discovery API through
#: `/marketplace/catalog`, so they are not decoration: they are how an agent
#: that found us gets to a page it can read.
#:
#: MOVED BACK OFF THE CUSTOM DOMAIN 2026-10-09, because it never served. The
#: name was pointed at the project in Vercel's dashboard — both it and `www`
#: report `verified: true` — but its DNS was never changed: `dig +short A`
#: answers Hostinger's parking range and the host returns a "Parked Domain name
#: on Hostinger DNS system" page at **HTTP 200**. A 200 is what made it look
#: fine. Every value below was sending Circle's crawler to that page.
#:
#: This constant is also the one the four-place host gate reads as its
#: definition of canonical (`tests/test_canonical_host.py`), so moving it moves
#: `metadataBase` and `robots.txt` with it. Verify a host by reading a page it
#: returns, never by its status code.
PROVIDER_WEBSITE = os.environ.get(
    "ACR_PROVIDER_WEBSITE", "https://arc-compute-rate.vercel.app"
)
PROVIDER_DOCS_URL = os.environ.get(
    "ACR_PROVIDER_DOCS_URL", "https://arc-compute-rate.vercel.app/developers"
)
#: One of Circle's six discovery categories.
PROVIDER_CATEGORY = os.environ.get("ACR_PROVIDER_CATEGORY", "FINANCIAL_ANALYSIS")
PROVIDER_TAGS = ("x402", "arc", "usdc", "compute", "index", "oracle", "futures")

#: ISO-8601 `lastUpdated`, which every Discovery API item carries and a crawler
#: uses to decide whether to re-read a listing. Stamped ONCE at import rather
#: than per request: it means "when this catalog was last rebuilt", and a value
#: that advanced on every call would tell a crawler the listing changed
#: constantly when nothing had — busywork for them and a lie from us. A deploy
#: restarts the process, which is exactly when the catalog can actually change.
_BUILT_AT = _dt.datetime.now(_dt.UTC).isoformat(timespec="milliseconds").replace(
    "+00:00", "Z"
)
_att_lock = threading.Lock()
_att_cache: dict | None = None
_att_at = 0.0

_INDEX_ID_PARAM = {
    "type": "object",
    "properties": {
        "index_id": {
            "type": "string",
            "enum": list(ALL_INDEX_IDS),
            "description": "index id (path parameter)",
        }
    },
    "required": ["index_id"],
}

_PRINT_FIELDS = {
    "index_id": {"type": "string"},
    "ts": {"type": "number", "description": "sim-seconds; Fixing Nº = ts/3600"},
    "value": {"type": "number"},
    "ci_lo": {"type": "number"},
    "ci_hi": {"type": "number"},
    "unit": {"type": "string"},
    "naive_vwap": {"type": "number"},
    "cleaned_pct": {"type": "number"},
    "cost_to_move_1pct": {"type": "number"},
}

#: The five flat-priced endpoint families (``app.GATED_ENDPOINTS`` minus the fleet's
#: per-seller ``/compute/{label}``, which the catalog carries as listings), each with
#: enough machine-readable metadata for an agent to decide *before* paying.
ENDPOINT_FAMILIES: list[dict] = [
    {
        "family": "prints",
        "template": "/prints",
        "description": (
            "All latest ACR fixings for every index: constant-quality rate, "
            "bootstrap CI, manipulation cost, robustness."
        ),
        "input": {"type": "object", "properties": {}},
        "output": {
            "type": "object",
            "properties": {
                "prints": {
                    "type": "object",
                    "additionalProperties": {"type": "object", "properties": _PRINT_FIELDS},
                }
            },
        },
    },
    {
        "family": "print",
        "template": "/prints/{index_id}",
        "description": "One fixing with full diagnostics (CI, attack cost, robustness).",
        "input": _INDEX_ID_PARAM,
        "output": {"type": "object", "properties": _PRINT_FIELDS},
    },
    {
        "family": "curve",
        "template": "/curve/{index_id}",
        "description": "Term structure: Avellaneda–Stoikov mids at 1/2/4/8-week tenors.",
        "input": _INDEX_ID_PARAM,
        "output": {
            "type": "object",
            "properties": {
                "index_id": {"type": "string"},
                "curve": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "tenor_weeks": {"type": "integer"},
                            "expiry_ts": {"type": "number"},
                            "mid": {"type": "number"},
                            "bid": {"type": "number"},
                            "ask": {"type": "number"},
                            "spread_bp": {"type": "number"},
                        },
                    },
                },
            },
        },
    },
    {
        "family": "vol",
        "template": "/vol/{index_id}",
        "description": "Annualized realized volatility from the print history.",
        "input": _INDEX_ID_PARAM,
        "output": {
            "type": "object",
            "properties": {
                "index_id": {"type": "string"},
                "annualized_vol": {"type": "number"},
            },
        },
    },
    {
        "family": "seller-scores",
        "template": "/seller-scores/{index_id}",
        "description": "Seller reliability: attestation and clean-volume share per seller.",
        "input": _INDEX_ID_PARAM,
        "output": {
            "type": "object",
            "properties": {
                "index_id": {"type": "string"},
                "sellers": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "seller": {"type": "string"},
                            "score": {"type": "number"},
                            "clean_share": {"type": "number"},
                            "attested": {"type": "boolean"},
                            "volume_usdc": {"type": "number"},
                        },
                    },
                },
            },
        },
    },
]


def catalog_resources() -> list[tuple[str, dict]]:
    """Expand the endpoint families into concrete ``(path, family)`` listings —
    one per index for the parametrized families (13 resources total)."""
    out: list[tuple[str, dict]] = []
    for fam in ENDPOINT_FAMILIES:
        if "{index_id}" in fam["template"]:
            for iid in ALL_INDEX_IDS:
                out.append((fam["template"].replace("{index_id}", iid), fam))
        else:
            out.append((fam["template"], fam))
    return out


# --- registry seam (mirrors onchain.get_reader): lazy, offline-tolerant ---


class NullRegistry:
    """The no-chain stand-in: never connected, no attestations."""

    def connected(self) -> bool:
        return False

    def all_attestations(self) -> list:
        return []


_registry = None


def get_registry():
    """The attestation-registry client, built lazily from env.

    Only constructs a real ``RegistryClient`` (and thus touches web3) when
    ``ACR_REGISTRY_ADDRESS`` is set — otherwise the catalog stays chain-free.
    """
    global _registry
    if _registry is None:
        s = get_settings()
        if s.registry_address:
            from acr_oracle_client import RegistryClient

            _registry = RegistryClient()
        else:
            _registry = NullRegistry()
    return _registry


def set_registry(registry) -> None:
    """Install a registry (tests inject a fake with canned attestations)."""
    global _registry
    _registry = registry


def reset_registry() -> None:
    global _registry, _att_cache, _att_at
    _registry = None
    with _att_lock:
        _att_cache, _att_at = None, 0.0


def _read_attestation_summary(registry, settings) -> dict | None:
    """Read + summarize on-chain seller attestations (no cache — the raw read).

    ``None`` when no registry is connected — an honest "unattested listing"
    rather than a fabricated reputation.
    """
    try:
        if not registry.connected():
            return None
        attestations = registry.all_attestations()
    except Exception:  # pragma: no cover - live chain hiccup → honest null
        log.warning("attestation read failed", exc_info=True)
        return None
    return {
        "registry": settings.registry_address,
        "standard": "EIP-712 seller attestations (ERC-8004-style reputation anchor)",
        "sellers_attested": len(attestations),
        # The rows behind the count. ``all_attestations()`` already paid for
        # sellerCount + sellerAt/getAttestation per seller and this kept one
        # integer — the same miss the hedger's receipt ledger had. A count is an
        # assertion; the addresses are the only thing on this listing a buyer can
        # check without us. The page that printed this count directly above 60
        # simulated rows that were NOT these sellers has since been removed, but
        # the reason survives it: a count is still unverifiable and the addresses
        # still are not. Zero extra RPC: same read,
        # already TTL-cached and warmed off-request by the background loop.
        #
        # Registry order (sellerAt(0..n-1)), deliberately not re-sorted: that is
        # the order they filed, which `services` below is free to discard and a
        # row list is not. snake_case to match the block it lives in.
        "sellers": [
            {
                "seller": a.seller,
                "service": a.service.value,
                "model_class": a.model_class.value,
                "latency_slo_ms": a.latency_slo_ms,
                "schema_id": a.schema_id,
            }
            for a in attestations
        ],
        "services": sorted({a.service.value for a in attestations}),
        "latency_slo_ms": {
            "min": min((a.latency_slo_ms for a in attestations), default=None),
            "max": max((a.latency_slo_ms for a in attestations), default=None),
        },
    }


def cached_attestation_summary(settings, *, force: bool = False) -> dict | None:
    """The default (singleton-registry) path, TTL-cached so the catalog endpoint
    doesn't block on a burst of rate-limited RPC reads. ``force`` refreshes it
    (the background loop's off-request warm). Only ever touches the singleton
    registry — an injected registry reads fresh via ``_read_attestation_summary``.
    """
    global _att_cache, _att_at
    if not force:
        with _att_lock:
            if _att_at and time.monotonic() - _att_at < ATTESTATION_TTL_S:
                return _att_cache
    summary = _read_attestation_summary(get_registry(), settings)
    with _att_lock:
        _att_cache, _att_at = summary, time.monotonic()
    return summary


def warm_attestation_summary() -> None:
    """Refresh the attestation-summary cache off the request path (background loop)."""
    cached_attestation_summary(get_settings(), force=True)


def build_catalog(base_url: str, settings=None, registry=None) -> dict:
    """The Bazaar-shaped catalog of every paid ACR resource."""
    s = settings or get_settings()
    # An injected registry (tests / overrides) reads fresh; the default singleton
    # path is TTL-cached (a live-RPC optimization warmed by the background loop).
    attestation = (
        _read_attestation_summary(registry, s)
        if registry is not None
        else cached_attestation_summary(s)
    )
    base = (s.x402_resource_base or base_url).rstrip("/")
    # Shaped to match what Circle's Discovery API actually serves — measured
    # against `GET https://api.circle.com/v2/x402/discovery/resources`, not
    # guessed. `category` and `tags` are the fields that endpoint FILTERS on, so
    # a catalog without them is one no agent can narrow down to; FINANCIAL_
    # ANALYSIS carries 447 of its 958 listings and is the right bucket for a
    # price index. The `attestation` anchor stays because it is the one thing in
    # our listing no other listing has: an on-chain reputation signal a cautious
    # buyer can require before paying.
    provider = {
        "name": "ACR — The Arc Compute Rate",
        "tagline": "the constant-quality price of machine services, sold to machines",
        "description": (
            "A manipulation-resistant reference rate for machine services on Arc: "
            "inference ($/1k tokens), GPU compute ($/GPU-sec) and data egress "
            "($/MB). Every print ships a confidence interval and the USDC an "
            "attacker must burn to move it one basis point, and settles on-chain "
            "against a cash-settled futures venue."
        ),
        "website": PROVIDER_WEBSITE,
        "docsUrl": PROVIDER_DOCS_URL,
        "category": PROVIDER_CATEGORY,
        "tags": list(PROVIDER_TAGS),
        "attestation": attestation,
    }
    items = []
    for path, fam in catalog_resources():
        resource = base + path
        items.append(
            {
                "resource": resource,
                "type": "http",
                "x402Version": X402_CATALOG_VERSION,
                "lastUpdated": _BUILT_AT,
                "accepts": [build_payment_requirements(resource, s)],
                "metadata": {
                    "family": fam["family"],
                    "description": fam["description"],
                    "input": fam["input"],
                    "output": fam["output"],
                    "provider": provider,
                    "units": {iid: spec_for(iid).unit for iid in ALL_INDEX_IDS},
                },
            }
        )
    items.extend(_fleet_items(base, provider, s))
    return {"x402Version": X402_CATALOG_VERSION, "provider": provider, "items": items}


#: What one fleet call returns — thin on purpose (the point is the settlement,
#: not the payload), but declared so an agent knows before paying.
_COMPUTE_OUTPUT = {
    "type": "object",
    "properties": {
        "seller": {"type": "string"},
        "label": {"type": "string"},
        "index_id": {"type": "string"},
        "unit": {"type": "string"},
        "unit_price_usdc": {"type": "number"},
        "quantity": {"type": "number"},
        "amount_usdc": {"type": "number"},
        "model_class": {"type": "string"},
        "settlement": {
            "type": "object",
            "properties": {"tx_ref": {"type": "string"}, "payer": {"type": "string"}},
        },
    },
}


def _fleet_items(base: str, provider: dict, s) -> list[dict]:
    """The seller fleet, as discoverable listings.

    Without this the fleet is unreachable: a buyer agent discovers what to buy
    from THIS catalog, and `/compute/{label}` appeared in no listing, no doc and
    no client. The endpoints were live and tested, and nothing could ever route
    a payment to one — so every settlement in the system kept going to the one
    platform wallet at the one flat price, which is exactly the degenerate tape
    the fleet was built to end. Downstream that is not a cosmetic gap: with no
    per-seller settlement there is no unit-price dispersion, so `slippageBp` is
    identically zero, `SellerDay`/`realVolume` stay empty and a seller rating
    has nothing to rank.

    The terms are built by the SAME `build_payment_requirements` the gate uses,
    with the same per-listing payee and amount, so what the catalog advertises
    and what the 402 charges cannot drift apart.
    """
    from .fleet import FLEET

    out = []
    for listing in FLEET.values():
        resource = base + listing.resource
        out.append(
            {
                "resource": resource,
                "type": "http",
                "x402Version": X402_CATALOG_VERSION,
                "lastUpdated": _BUILT_AT,
                "accepts": [
                    build_payment_requirements(
                        resource, s, pay_to=listing.seller, price=listing.amount_usdc
                    )
                ],
                "metadata": {
                    "family": "compute",
                    "description": (
                        f"Metered {listing.service.value.lower()} from {listing.label} "
                        f"({listing.model_class.value}), priced per {listing.unit} and "
                        f"settled to the seller's own wallet."
                    ),
                    "input": {"type": "object", "properties": {}},
                    "output": _COMPUTE_OUTPUT,
                    "provider": provider,
                    # The comparison an agent needs BEFORE paying: two sellers of
                    # the same model_class pricing the same index are the
                    # like-for-like pair, and the cheaper one is a real choice.
                    "seller": listing.seller,
                    "label": listing.label,
                    "index_id": listing.index_id,
                    "service": listing.service.value,
                    "model_class": listing.model_class.value,
                    "unit": listing.unit,
                    "unit_price_usdc": listing.unit_price_usdc,
                    "quantity": listing.quantity,
                    "amount_usdc": listing.amount_usdc,
                },
            }
        )
    return out


def build_sim_receipts(n: int = 24, settings=None) -> dict:
    """A deterministic simulated settlement ledger for the offline bundle.

    Exactly the :func:`build_receipts` shape, but seeded: payers are realistic
    40-hex-char addresses derived from a seeded sha256, tx_refs count ``sim-1``
    .. ``sim-N``. Honest by construction — ``scheme: "sim"`` plus the ``sim-``
    tx_ref prefix are precisely how the Terminal labels simulated rows.
    """
    import hashlib

    s = settings or get_settings()
    price = s.x402_price_usdc
    rows = [
        {
            "seq": i,
            "payer": "0x" + hashlib.sha256(f"acr-sim-payer-{i}".encode()).hexdigest()[:40],
            "amount_usdc": price,
            "tx_ref": f"sim-{i}",
            "network": s.caip2(),
            "scheme": "sim",
        }
        for i in range(1, n + 1)
    ]
    rows.reverse()  # newest first, like the live ledger
    return {
        "gate": "dev",
        "paid_queries": n,
        "revenue_usdc": round(n * price, 6),
        "price_usdc": price,
        "receipts": rows,
    }


def build_receipts(fac: Facilitator) -> dict:
    """The public settlement ledger — the facilitator's recent-receipt ring,
    newest first, with a monotone ``seq`` ordinal.

    Carries ``settled_at`` when the receipt has one. The ledger now rehydrates
    real Gateway settlements from a committed archive, so some rows are
    genuinely old — and a ledger that shows *what* was paid while hiding *when*
    invites a reader to assume it was recent. The row is true either way; the
    timestamp is what makes it unambiguous. (Omitted when zero, so a receipt
    from before the field existed does not claim to have settled at the epoch.)

    Carries ``resource`` on the same terms. ``PaymentReceipt`` has stamped the
    bought path since the field was added, but this builder dropped it, so the
    catalog could list thirteen resources and the tape could prove thirty-four
    settlements with nothing joining the two — the marketplace had listings and
    sales and no way to say which listing sold. Emitted only when non-empty:
    most archived rows predate the stamp, and an empty string rendered as a
    resource would attribute every one of them to the same nameless listing.
    """
    receipts = list(fac.recent)
    # Clamped: a paid query landing between the two reads above can skew the
    # count by one for a single poll — never let an ordinal go below 1.
    first_seq = max(1, fac.paid_queries - len(receipts) + 1)
    rows = [
        {
            "seq": first_seq + i,
            "payer": r.payer,
            "amount_usdc": r.amount_usdc,
            "tx_ref": r.tx_ref,
            "network": r.network,
            "scheme": r.scheme,
            **({"settled_at": r.settled_at} if r.settled_at else {}),
            **({"resource": r.resource} if r.resource else {}),
            # Who was paid and what was bought. Omitted while every settlement
            # went to one platform wallet at one flat price; carried now that a
            # seller fleet exists, because without them a receipt has no unit
            # price — and `scripts/mirror_receipts.py`, which reads THIS payload,
            # could not mirror a single row.
            **({"seller": r.seller} if r.seller else {}),
            **({"unit": r.unit} if r.unit else {}),
            **({"quantity": r.quantity} if r.quantity else {}),
            # The tier the buyer's card earned. Carried so the ticker can mark a
            # human-attributed settlement as one; omitted on legacy rows, where
            # absence means "recorded before the gate existed", not "anonymous".
            **({"tier": r.tier} if r.tier else {}),
        }
        for i, r in enumerate(receipts)
    ]
    rows.reverse()
    from .x402 import CircleFacilitator

    return {
        "gate": "circle" if isinstance(fac, CircleFacilitator) else "dev",
        "paid_queries": fac.paid_queries,
        "revenue_usdc": fac.revenue_usdc,
        "price_usdc": price_usdc(),
        "receipts": rows,
    }
