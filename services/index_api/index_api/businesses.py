"""The business registry — who the operator runs for.

A BUSINESS IS ITS TREASURY ADDRESS. That is not a shortcut, it is why ten
businesses are reachable at all: everything that reads per-business already keys
on a payer address. ``tca.payer_tca(payer)``, ``GET /tca/{payer}``, the
subgraph's ``Payer`` and ``PayerDay``, the Terminal's ``useTape(payer)``, and
``ReceiptMirror.lastSettledAt[payer]`` were all per-payer before this file
existed. So the registry adds the things an address cannot carry — a name,
consent, which PolicyWallet holds the money — and nothing else.

WHERE THIS LIVES, and why it is not in ``data/``. ``data/`` is untracked and
listed in ``.dockerignore``, so a registry there would be invisible to git and
absent from the image — present in development and empty in production, which is
the worst of the three states. The one committed data file that reaches the
running service is ``receipts_live.jsonl``, inside the package, so the registry
sits beside it.

ONBOARDING IS A COMMIT. There is no admin endpoint that writes here. For ten
businesses that is not a limitation: it makes every addition reviewable, dated
and reversible, and it means a misconfigured budget cannot be introduced by an
HTTP call. If this ever needs to be self-service, that is a different file with
different guarantees.

CONSENT GATES THE NAME, NOT THE NUMBERS. A business that has not agreed to be
named still counts in every total and still gets its ledger published — under a
stable pseudonym. Dropping it from the count would understate real usage;
publishing the name would be using somebody's identity without asking. Canteen
asks for named businesses *with consent*, so the honest report is a count that
includes everyone and names only those who said yes.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("index_api.businesses")

#: Beside ``receipts_live.jsonl``, for the reasons in the module docstring.
REGISTRY_PATH = Path(__file__).with_name("businesses.json")

#: How a business arrived. Reported separately because "our own company" and "a
#: studio we have never met" are different evidence, and collapsing them into one
#: number is the move that makes traction look better than it is.
TIERS = ("own", "network", "cohort", "oss")


#: A 20-byte hex address. The same shape the press validates everywhere else.
_ADDRESS_RE = re.compile(r"^0x[0-9a-f]{40}$")


@dataclass(frozen=True)
class Business:
    slug: str
    #: The treasury address. The identity — everything per-business keys on it.
    treasury: str
    #: The legal or trading name. Only ever published when ``consented``.
    name: str = ""
    #: The ``PolicyWallet`` holding this business's USDC. Empty means the
    #: operator can price and meter for them but cannot spend, which is a real
    #: and useful state: it is what an evaluation looks like.
    policy_wallet: str = ""
    consented: bool = False
    tier: str = "own"
    #: ``mainnet`` or ``testnet``. Kept per business because the two are
    #: reported separately and must never be added together.
    chain: str = "testnet"
    categories: tuple[str, ...] = field(default_factory=tuple)
    onboarded_at: float = 0.0
    #: A demonstration, not a customer.
    #:
    #: It renders everywhere a business renders, because the escalation queue
    #: and the budget block cannot be shown at all without one — and it is
    #: EXCLUDED from every traction figure, because showing the UI and claiming
    #: usage are different things and Canteen's FAQ draws the line exactly
    #: there: "what doesn't count is a synthetic dataset".
    sandbox: bool = False
    #: Addresses allowed to READ this business's detail, beside its treasury.
    #:
    #: Why this exists rather than "the treasury signs". A read card signed by
    #: the treasury means the key that SPENDS has to come online to look at a
    #: dashboard, which is the thing a real customer would refuse — and rightly.
    #: So a business can nominate a laptop key instead, and the treasury stays
    #: cold. Adding one is a commit, like every other change here, for the reason
    #: at the top of this file: an address that can read a vendor list should not
    #: be addable by an HTTP call.
    #:
    #: Deliberately NOT in ``as_public_dict``. Who may read a business is not a
    #: public fact about it.
    readers: tuple[str, ...] = field(default_factory=tuple)
    note: str = ""

    def may_read(self, address: str) -> bool:
        """Whether this address may read the business's detail.

        Lower-cased on both sides, never compared raw — the same discipline
        ``key`` applies to the treasury, and for the reason ``agentcard._bytes32``
        records about the neighbouring bytes32 fields: a field whose malformed
        form still verifies is a field that must be normalised at the edge rather
        than trusted.
        """
        got = (address or "").strip().lower()
        if not got:
            return False
        return got == self.treasury.strip().lower() or got in self.readers

    @property
    def public_name(self) -> str:
        """What may be shown. A pseudonym derived from the slug when there is no
        consent, so the same business reads the same way every time without ever
        naming them."""
        if self.consented and self.name:
            return self.name
        return f"business {self.slug}"

    @property
    def key(self) -> str:
        return self.treasury.lower()

    def as_public_dict(self) -> dict:
        """The shape a public surface may serve. ``name`` is absent, not blank,
        when there is no consent: a key present and empty invites a UI to render
        an unnamed row as though the name were simply missing."""
        d = {
            "slug": self.slug,
            "label": self.public_name,
            "treasury": self.treasury,
            "tier": self.tier,
            "chain": self.chain,
            "consented": self.consented,
            "categories": list(self.categories),
            "onboarded_at": self.onboarded_at,
            "spends": bool(self.policy_wallet),
            "sandbox": self.sandbox,
        }
        if self.consented and self.name:
            d["name"] = self.name
        return d


def _readers(slug: str, raw) -> tuple[str, ...]:
    """The reader allowlist, normalised and shape-checked.

    Dropped with a warning rather than taken on trust: a malformed entry here
    would be an address that silently never matches, which reads as "my card is
    wrong" to whoever is holding a perfectly good card. Lower-cased on the way in
    so every comparison downstream is already normalised.
    """
    out: list[str] = []
    for item in raw or ():
        addr = str(item or "").strip().lower()
        if _ADDRESS_RE.match(addr):
            out.append(addr)
        else:
            log.warning("businesses: %s has a reader that is not an address: %r", slug, item)
    # De-duplicated, because the same address listed twice is one grant.
    return tuple(dict.fromkeys(out))


def _one(row: dict) -> Business | None:
    slug = str(row.get("slug") or "").strip()
    treasury = str(row.get("treasury") or "").strip()
    if not slug or not treasury:
        log.warning("businesses: a row without a slug or a treasury is not a business")
        return None
    tier = str(row.get("tier") or "own")
    if tier not in TIERS:
        log.warning("businesses: %s has an unknown tier %r", slug, tier)
        tier = "own"
    chain = str(row.get("chain") or "testnet")
    if chain not in ("mainnet", "testnet"):
        log.warning("businesses: %s has an unknown chain %r", slug, chain)
        chain = "testnet"
    return Business(
        slug=slug,
        treasury=treasury,
        name=str(row.get("name") or ""),
        policy_wallet=str(row.get("policy_wallet") or ""),
        consented=bool(row.get("consented")),
        tier=tier,
        chain=chain,
        categories=tuple(str(c) for c in (row.get("categories") or ())),
        onboarded_at=float(row.get("onboarded_at") or 0.0),
        sandbox=bool(row.get("sandbox")),
        readers=_readers(slug, row.get("readers")),
        note=str(row.get("note") or ""),
    )


def load(path: str | Path | None = None) -> tuple[Business, ...]:
    """Every registered business, or an empty tuple.

    A missing or unreadable registry is NOT an error: a host with no businesses
    onboarded is a working state, and it is the state every host starts in. A
    malformed row is dropped with a warning rather than taking the rest of the
    registry down with it.
    """
    target = Path(path) if path else REGISTRY_PATH
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return ()
    except Exception as exc:
        log.warning("businesses: registry unreadable (%s)", exc)
        return ()

    rows = raw.get("businesses") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        log.warning("businesses: registry is not a list of businesses")
        return ()

    out: list[Business] = []
    seen: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        b = _one(row)
        if b is None:
            continue
        # Two businesses on one treasury would merge their ledgers and their
        # budgets, so the duplicate is dropped rather than quietly last-wins.
        if b.key in seen:
            log.warning(
                "businesses: %s shares a treasury with %s; dropping %s",
                b.slug, seen[b.key], b.slug,
            )
            continue
        seen[b.key] = b.slug
        out.append(b)
    return tuple(out)


def by_treasury(treasury: str, registry: tuple[Business, ...] | None = None) -> Business | None:
    if not treasury:
        return None
    reg = registry if registry is not None else load()
    key = treasury.strip().lower()
    for b in reg:
        if b.key == key:
            return b
    return None


def by_slug(slug: str, registry: tuple[Business, ...] | None = None) -> Business | None:
    if not slug:
        return None
    reg = registry if registry is not None else load()
    want = slug.strip()
    for b in reg:
        if b.slug == want:
            return b
    return None


def resolve(ident: str, registry: tuple[Business, ...] | None = None) -> Business | None:
    """A business by slug OR by treasury address, because a URL will carry
    either and a reader should not have to know which."""
    reg = registry if registry is not None else load()
    return by_slug(ident, reg) or by_treasury(ident, reg)


def real(registry: tuple[Business, ...] | None = None) -> tuple[Business, ...]:
    """The businesses that count: everything except the sandbox.

    Named and used rather than filtered inline, so there is one definition of
    "counts as usage" and every traction figure goes through it.
    """
    reg = registry if registry is not None else load()
    return tuple(b for b in reg if not b.sandbox)


def counts(registry: tuple[Business, ...] | None = None) -> dict:
    """The traction numbers the submission form asks for, derived rather than
    maintained. Mainnet and testnet are reported separately and never summed.

    Sandbox businesses are excluded here, not at the call sites, so no figure
    can accidentally include one.
    """
    reg = real(registry)
    per_tier = {t: 0 for t in TIERS}
    for b in reg:
        per_tier[b.tier] = per_tier.get(b.tier, 0) + 1
    return {
        "businesses": len(reg),
        "consented": sum(1 for b in reg if b.consented),
        "mainnet": sum(1 for b in reg if b.chain == "mainnet"),
        "testnet": sum(1 for b in reg if b.chain == "testnet"),
        "spending": sum(1 for b in reg if b.policy_wallet),
        "by_tier": per_tier,
    }
