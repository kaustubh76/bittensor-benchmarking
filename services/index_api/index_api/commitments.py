"""What we agreed to buy, before the bill arrives — Prior Art #03, the symbolon.

A symbolon was an object broken in two, with each party keeping half; fitting
the halves back together proved the agreement was real. Accounts-payable clerks
still perform the same match by hand: purchase order, goods received, invoice.

THIS REPO HAD TWO OF THE THREE HALVES. The vendor's bill arrives as
``billed_usdc`` and ``vendor_quantity``; our own meter counts what we actually
received (``meter_quantity``, Prior Art #06). What was missing was the FIRST
half — a prior agreement to match them against. The ``PolicyWallet``'s budget
looks like one and is not: it says "you may spend at most X on infra", which is
a LIMIT. A commitment says "at most X with this payee, for this service, at this
price, inside this window". The second is strictly more specific, and it is the
one a clerk reconciles against.

WHY IT ALSO UNBLOCKS THE LADDER, which is the part worth understanding. Check 5
escalates any bill the agent cannot price above ``UNBENCHMARKED_MAX_USDC`` — one
USDC — and a benchmark needs ``MIN_SELLERS`` independent sellers of the same
unit. A contractor's hourly rate and a SaaS seat price can never have two, so
every bill of real size was unpayable by construction, whatever its kind.

A market needs competitors. **A commitment needs none, because it is what we
agreed.** So "is this bill right?" now has three answers instead of two: a
market, an agreement, or the unbenchmarked ceiling — and the ceiling becomes the
last resort rather than the only one.

SIGNED OFF CHAIN, COMMITTED ON CHAIN. The commitment's hash rides inside the
decision record that ``PolicyWallet`` already hashes into the paying
transaction, so the chain commits to "this payment was made against that
agreement" with no new Solidity and no redeploy. The contract stays as it is,
and it is right to: it says *"two numbers per category, because a third would be
a number somebody could get wrong"*, and *"REFUSE, NEVER CLAMP"*. A commitment
adds specificity ABOVE the cap without asking the cap to express it.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass
from pathlib import Path

log = logging.getLogger("acr.commitments")

#: The committed register, inside the package, and the runtime one beside it.
#: Same two-tier shape as the decision log and for the same reason: ``data/`` is
#: untracked AND in ``.dockerignore``, so a register written only there is
#: present in development and absent in production.
ARCHIVE_PATH = Path(__file__).with_name("operator_commitments.jsonl")
LOG_PATH = os.environ.get("ACR_COMMITMENTS_PATH", "data/operator_commitments.jsonl")

#: How far over the agreed total a bill may run before it stops matching.
#: Separate from the meter's tolerance: that one forgives a counting difference,
#: this one forgives rounding on a price we agreed in writing. Tighter, because
#: there is less to forgive.
PRICE_TOLERANCE = float(os.environ.get("ACR_COMMITMENT_TOLERANCE", "0.005"))

#: The verdicts, and every one of them is a sentence a reviewer can act on.
WITHIN = "within"
OVER_TOTAL = "over_total"
OVER_UNIT_PRICE = "over_unit_price"
OVER_QUANTITY = "over_quantity"
OUTSIDE_WINDOW = "outside_window"
NONE_FOUND = "no_commitment"


@dataclass(frozen=True)
class Commitment:
    """One agreement: this payee, this service, at this price, up to this much.

    ``max_quantity`` and ``unit_price_usdc`` are kept apart rather than reduced
    to a total, because the two failures they catch are different: a vendor who
    delivers the agreed amount at a higher rate, and one who delivers more than
    was asked for at the agreed rate. A single total would let either hide
    inside the other.
    """

    commitment_id: str
    business: str
    payee: str
    resource: str
    unit: str = ""
    unit_price_usdc: float | None = None
    max_quantity: float | None = None
    max_total_usdc: float | None = None
    #: The window the agreement covers, in unix seconds. Zero means unbounded,
    #: which is a real state — an open retainer — and not the same as expired.
    starts_at: float = 0.0
    ends_at: float = 0.0
    note: str = ""

    @property
    def total_usdc(self) -> float | None:
        """The most this agreement can be billed for, however it was expressed."""
        if self.max_total_usdc is not None:
            return float(self.max_total_usdc)
        if self.unit_price_usdc is not None and self.max_quantity is not None:
            return float(self.unit_price_usdc) * float(self.max_quantity)
        return None

    def as_record(self) -> dict:
        return asdict(self)

    def hash(self) -> str:
        """A stable hex digest of the whole agreement.

        Canonicalised exactly the way ``decision_hash`` canonicalises a decision
        — sorted keys, no whitespace — so that the two hashes are produced by
        one rule and a reader who has verified one knows how to verify the
        other. Hex rather than bytes because it travels inside a JSON record.
        """
        from acr_oracle_client.mirror import to_bytes32

        canonical = json.dumps(
            self.as_record(), sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, default=str,
        )
        return "0x" + to_bytes32(f"acr.commitment::{canonical}").hex()


def _read_one(target: Path) -> list[dict]:
    """One JSONL file's rows, tolerantly — a missing file is an empty list.

    The same shape as ``statement._read_one``, including skipping a malformed
    line rather than treating it as the end of the file: an interrupted write
    leaves half a line, and reading that as EOF would silently drop every
    agreement behind it.
    """
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except Exception as exc:
        log.warning("commitments: %s unreadable (%s)", target.name, exc)
        return []
    rows: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _one(row: dict) -> Commitment | None:
    """One row as a `Commitment`, or None if it cannot be trusted.

    A commitment with no payee or no resource cannot be matched to anything, and
    a commitment with no ceiling of any kind authorises everything — which is
    the opposite of what an agreement is for. All three are dropped with a
    warning rather than loaded, because a register that silently contains a
    blank cheque is worse than an empty one.
    """
    payee = str(row.get("payee") or "").strip()
    resource = str(row.get("resource") or "").strip()
    cid = str(row.get("commitment_id") or "").strip()
    if not (payee and resource and cid):
        log.warning("commitments: skipping a row with no id, payee or resource")
        return None
    try:
        c = Commitment(
            commitment_id=cid,
            business=str(row.get("business") or ""),
            payee=payee,
            resource=resource,
            unit=str(row.get("unit") or ""),
            unit_price_usdc=_num(row.get("unit_price_usdc")),
            max_quantity=_num(row.get("max_quantity")),
            max_total_usdc=_num(row.get("max_total_usdc")),
            starts_at=float(row.get("starts_at") or 0.0),
            ends_at=float(row.get("ends_at") or 0.0),
            note=str(row.get("note") or ""),
        )
    except (TypeError, ValueError) as exc:
        log.warning("commitments: skipping %s (%s)", cid, exc)
        return None
    if c.total_usdc is None:
        log.warning("commitments: %s has no ceiling, so it authorises everything", cid)
        return None
    return c


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) else None


def load(path: str | Path | None = None) -> tuple[Commitment, ...]:
    """Every agreement on the register: the committed one, then the runtime one.

    An explicit ``path`` reads only that file, which is what a test wants. Later
    rows win on a repeated id, so a renegotiation appended at runtime overrides
    the one that shipped in the image — the same precedence the decision log's
    two tiers already use.
    """
    rows = _read_one(Path(path)) if path is not None else (
        _read_one(ARCHIVE_PATH) + _read_one(Path(LOG_PATH))
    )
    by_id: dict[str, Commitment] = {}
    for row in rows:
        c = _one(row)
        if c is not None:
            by_id[c.commitment_id] = c
    return tuple(by_id.values())


def covering(
    business: str,
    payee: str,
    resource: str,
    at: float,
    register: tuple[Commitment, ...] | None = None,
) -> Commitment | None:
    """The agreement that covers this payee and service at this moment.

    Matched on ``(business, payee, resource)``, case-insensitively on the
    address, and on the window containing ``at``. The resource is compared
    through ``resource_path`` for the reason the omission check gives: the
    archive records ``/compute/x`` and a catalog records
    ``https://host/compute/x``, and matching them as raw strings finds nothing.

    The NARROWEST live window wins, so a specific agreement for one month
    overrides an open retainer rather than being hidden by it.
    """
    from .operator import resource_path

    want_payee, want_res = payee.lower(), resource_path(resource)
    hits = [
        c for c in (register if register is not None else load())
        if c.business == business
        and c.payee.lower() == want_payee
        and resource_path(c.resource) == want_res
        and (not c.starts_at or at >= c.starts_at)
        and (not c.ends_at or at <= c.ends_at)
    ]
    if not hits:
        return None

    def span(c: Commitment) -> float:
        return (c.ends_at - c.starts_at) if (c.starts_at and c.ends_at) else float("inf")

    return min(hits, key=span)


def assess(c: Commitment | None, billed_usdc: float, quantity: float | None, at: float) -> dict:
    """Does this bill fit the agreement it was made under?

    Returns a verdict a reviewer can read, never a bare boolean. Each failure is
    named separately because each calls for a different conversation: a price
    above what was agreed is a renegotiation, a quantity above it is an
    over-delivery, and a bill outside the window is an agreement that has run
    out.

    ``over_usdc`` is what the bill exceeds the agreement by — the figure a
    mismatch holds back, and the only one here that is money rather than a
    comparison.
    """
    if c is None:
        return {"matched": False, "verdict": NONE_FOUND, "commitment_id": "",
                "commitment_hash": "", "over_usdc": None}

    out = {
        "matched": False,
        "verdict": WITHIN,
        "commitment_id": c.commitment_id,
        "commitment_hash": c.hash(),
        "over_usdc": None,
        "agreed_total_usdc": c.total_usdc,
        "agreed_unit_price_usdc": c.unit_price_usdc,
    }

    # The window first: an expired agreement is not a cheaper one, it is none.
    if (c.starts_at and at < c.starts_at) or (c.ends_at and at > c.ends_at):
        out["verdict"] = OUTSIDE_WINDOW
        return out

    total = c.total_usdc
    if total is not None and billed_usdc > total * (1.0 + PRICE_TOLERANCE):
        out["verdict"] = OVER_TOTAL
        out["over_usdc"] = round(billed_usdc - total, 6)
        return out

    if c.max_quantity is not None and quantity is not None and quantity > c.max_quantity:
        out["verdict"] = OVER_QUANTITY
        # Priced at what was agreed, so the figure is the over-delivery's worth
        # rather than the vendor's own rate for it.
        if c.unit_price_usdc is not None:
            out["over_usdc"] = round((quantity - c.max_quantity) * c.unit_price_usdc, 6)
        return out

    # The unit price last, because it is the one that needs a quantity to mean
    # anything — and a bill inside both the total and the quantity can still be
    # dearer per unit than agreed if it delivered less than it charged for.
    if c.unit_price_usdc is not None and quantity:
        unit_price = billed_usdc / quantity
        if unit_price > c.unit_price_usdc * (1.0 + PRICE_TOLERANCE):
            out["verdict"] = OVER_UNIT_PRICE
            out["over_usdc"] = round((unit_price - c.unit_price_usdc) * quantity, 6)
            return out

    out["matched"] = True
    return out
