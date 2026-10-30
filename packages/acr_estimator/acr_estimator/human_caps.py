"""Pillar 3, denominated in people — what it costs to move the print in HUMANS.

The published bound prices an attack in wallets, and a wallet costs a funding
transfer. That is the honest number when identity is free, and it is why the
figure is large in dollars and unimpressive in effort. Once some payers are
resolved to verified humans, a second question becomes answerable: how many
distinct PEOPLE would an attacker need standing behind the flow?

Almost none of this is new arithmetic. `bound.manipulation_bound` already prices
a cleaning-evading adversary against a per-cluster volume cap and already returns
how many capped clusters the injection needs. This supplies one thing — a
different cap for flow that is human-backed — and reads the cluster count back
out as a count of humans.

WHY A CLUSTER IS A HUMAN HERE. Under the wallet cap a "cluster" is a funding
community an attacker fabricates, so the identity count multiplies by
`SYBIL_MAX_SIZE + 1`: they must stand up a whole community to look organic. A
verified human needs no such disguise — they are already, verifiably, one person.
So the human count is `sybil_clusters_required` directly, with no evade-size
multiplier. Carrying the multiplier over would inflate the headline by ~40x,
which is the kind of error that flatters and therefore does not get questioned.

WHAT THIS NUMBER IS NOT. It is a LOWER bound, and it says so everywhere it
surfaces. `C_human` — the cost of controlling one verified identity an attacker
does not own — has no citable market rate, so `anchors/_basket/C-HUMAN.json`
carries a deliberately low floor rather than an estimate, and records that as a
finding. Understating it can only make the benchmark look weaker than it is,
which is the safe direction for a number nobody can yet source. See that file
before quoting anything derived from here.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .bound import ManipulationBound, manipulation_bound

#: Share of window volume one VERIFIED human may contribute before being capped.
#: Looser than the unverified cap on purpose: the cap exists to bound what one
#: actor can do, and a verified human is a costlier actor to be. Rewarding proof
#: of humanity with more headroom is the whole economic point.
CAP_HUMAN = 0.08
#: …and the tighter cap that unresolved flow keeps.
CAP_UNVERIFIED = 0.03

_BASKET = Path(__file__).resolve().parents[3] / "anchors" / "_basket" / "C-HUMAN.json"


@dataclass
class HumanBound:
    """The manipulation bound, priced in people rather than wallets."""

    #: Distinct verified humans an attacker must control to move the print 1bp.
    humans_required: int
    #: USDC, at the floor in the basket. A LOWER bound, never an estimate.
    cost_usdc: float
    #: What one human was priced at, and where that came from.
    cost_per_human: float
    basket_status: str
    #: True when the basket carries no cited rows, i.e. the figure rests on a
    #: floor. Surfaced so no caller can present this as measured by accident.
    is_lower_bound: bool
    #: The caps this was computed under — part of the policy hash.
    cap_human: float = CAP_HUMAN
    cap_unverified: float = CAP_UNVERIFIED


def load_cost_per_human(path: Path | None = None) -> tuple[float, str, bool]:
    """`(usd_per_human, status, is_lower_bound)` from the anchor basket.

    Read from `anchors/`, never a constant here. `reference_level` is the reason:
    it called itself nominal, nobody re-derived it, and it was 20-1250x off. A
    number that scales an entire published bound belongs where it can be audited
    and where `--check` goes red if somebody edits it without re-anchoring.
    """
    p = path or _BASKET
    if not p.exists():
        raise FileNotFoundError(
            f"{p} is missing — the human bound cannot be priced from a constant"
        )
    basket = json.loads(p.read_text())
    rule = basket.get("rule") or {}
    status = str(basket.get("status") or "unknown")
    if rule.get("aggregate") == "floor":
        return float(rule["floor_usd"]), status, True
    rows = [float(r["usd_per_human"]) for r in basket.get("rows") or []]
    if not rows:
        raise ValueError(f"{p} declares no floor and carries no rows")
    return float(np.median(rows)), status, False


def humans_required(
    prices: np.ndarray,
    weights: np.ndarray,
    alpha: float,
    fee_bps: float,
    fee_flat: float,
    raw_total: float,
    cap_human: float = CAP_HUMAN,
    trade_notional: float = 50.0,
) -> tuple[int, ManipulationBound]:
    """How many distinct verified humans a 1bp move needs, under the human cap.

    Same machinery as the wallet bound with a different cap — deliberately, so
    the two numbers are comparable and a divergence between them means something
    about the caps rather than about two implementations drifting.
    """
    mb = manipulation_bound(
        prices, weights, alpha, fee_bps, fee_flat,
        trade_notional=trade_notional,
        cluster_cap=cap_human,
        raw_total=raw_total,
        # One verified human is one capped actor. No funding community to
        # fabricate, so no evade-size multiplier — see the module docstring.
        sybil_min_evade_size=1,
    )
    return mb.sybil_clusters_required, mb


def human_bound(
    prices: np.ndarray,
    weights: np.ndarray,
    alpha: float,
    fee_bps: float,
    fee_flat: float,
    raw_total: float,
    wallet_cost_per_bp: float,
    cap_human: float = CAP_HUMAN,
    trade_notional: float = 50.0,
    basket: Path | None = None,
) -> HumanBound | None:
    """The human-denominated bound, or None when it cannot be computed.

    None rather than zero, and None rather than a guess: `ACROracleV2` treats 0
    as "not computed", and publishing a human bound equal to the wallet bound
    would claim identities are as cheap to buy as wallets, which is false.
    """
    people, mb = humans_required(
        prices, weights, alpha, fee_bps, fee_flat, raw_total,
        cap_human=cap_human, trade_notional=trade_notional,
    )
    if people <= 0 or not math.isfinite(mb.notional_per_bp):
        return None

    per_human, status, is_floor = load_cost_per_human(basket)
    cost = people * per_human + mb.cost_per_bp

    # The contract enforces `humanAdjustedBound == 0 || >= attackCostPerBp`, and
    # it is enforcing something true: buying people cannot be cheaper than buying
    # wallets. If the arithmetic ever says otherwise the floor is too low, not
    # the invariant wrong — so clamp UP to the wallet bound and let the count
    # carry the claim, rather than posting a number the chain would reject.
    cost = max(cost, float(wallet_cost_per_bp))

    return HumanBound(
        humans_required=people,
        cost_usdc=cost,
        cost_per_human=per_human,
        basket_status=status,
        is_lower_bound=is_floor,
        cap_human=cap_human,
    )
