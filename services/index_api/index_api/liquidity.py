"""Cash against what is coming — the first thing RFB 4 asks the agent to decide.

The brief's problem statement is "watches cash against upcoming obligations",
and its list of what the AI decides opens with "whether there is enough
liquidity to cover what is due, and what is due next". Until this module there
was no such notion anywhere: the operator checked each bill against a BUDGET,
which is a different question.

    budget      `cap - spent`, from the contract's own counters. PERMISSION.
                It reads healthy on a wallet holding nothing, and the payment
                then reverts on chain.
    liquidity   what the wallet HOLDS, against what is DATED and coming.
                MONEY.

Both are needed and neither substitutes. A bill can be inside budget and
unaffordable, or affordable and over budget, and those call for different
things: fund the wallet, or raise the cap.

WHAT THIS IS NOT. It is not a forecast. `docs/TAMEION.md` says plainly that the
cash-flow forecast was researched and not built — "the part most treasury bots
guess at" — and that remains true: there is no model here, no burn rate, no
runway in days, and no yield figure. This is a *current* picture over a window:
what is held now, and what carries a due date inside the horizon now. Calling
it a forecast would be the overstatement this file exists to avoid.

PURE, AND INJECTED. No I/O: the balance arrives from
`PolicyClient.balance_usdc()` and the obligations from whichever feeder built
them, exactly as `par`, `screen` and `commitment` arrive at `decide()` — "so
`decide` stays pure and a test can put a known agreement in front of it".
"""

from __future__ import annotations

import os
from dataclasses import dataclass

#: How far ahead "what is due next" looks, in seconds. Thirty days, because
#: that is the window a net-30 invoice lives in and the one a person running
#: accounts payable already thinks in.
#:
#: DELIBERATELY NOT `operator.PAY_WINDOW_S`. That one is "pay no earlier than
#: three days before due" — a per-bill threshold about when to release cash.
#: This is how far forward we look when asking whether the cash is there at
#: all. Two different decisions wearing the same unit, and sharing a constant
#: would couple them so that tuning one silently moved the other.
HORIZON_S = float(os.environ.get("ACR_OPERATOR_LIQUIDITY_HORIZON_S", str(30 * 86_400)))


@dataclass(frozen=True)
class Liquidity:
    """What is held, and what is coming, at one moment.

    `held_usdc` is `None` when it could not be read — never 0.0. An unfunded
    wallet and an unreachable node are different facts and the surface says
    which; collapsing them would make "no money" and "no answer" render alike.
    """

    held_usdc: float | None
    due_usdc: float
    due_count: int
    soonest_at: float | None
    #: Obligations with NO due date, counted apart and never added to
    #: `due_usdc`. "We do not know when this is due" is not "it is due later",
    #: and folding them in would understate what is coming while looking more
    #: precise. Reported so the figure can say what it does not cover — the
    #: same reason `searched` is printed beside `found` in the ledger audit.
    undated: int
    horizon_s: float

    @property
    def measured(self) -> bool:
        """Whether the cash side is known at all."""
        return self.held_usdc is not None

    @property
    def covers_due(self) -> bool | None:
        """Whether what is held covers what is dated. `None` if not measured."""
        if self.held_usdc is None:
            return None
        return self.held_usdc >= self.due_usdc

    def as_dict(self) -> dict:
        """The shape the statement renders. `held_usdc` stays `None` rather
        than becoming 0.0, and `covers_due` is `None` for the same reason."""
        return {
            "held_usdc": self.held_usdc,
            "due_usdc": round(self.due_usdc, 6),
            "due_count": self.due_count,
            "soonest_at": self.soonest_at,
            "undated": self.undated,
            "horizon_days": round(self.horizon_s / 86_400, 2),
            "covers_due": self.covers_due,
        }


def assess(
    held_usdc: float | None,
    obligations,
    now: float,
    horizon_s: float = HORIZON_S,
) -> Liquidity:
    """One liquidity picture from a balance and the obligations in hand.

    Counts an obligation toward `due_usdc` only when it carries a numeric
    `due_at` inside the horizon. Three exclusions, each deliberate:

      * no due date          -> counted in `undated`, not in `due_usdc`
      * due beyond the horizon -> not yet this window's problem
      * already past due     -> counted IN, because an overdue bill is the most
                                due thing there is. A horizon that only looks
                                forward would quietly drop the bills that
                                matter most.
    """
    def field(row, name):
        """An `Obligation`, an `ObligationDecision`, or a logged row.

        The ladder hands this dataclasses; the statement hands it dicts read
        back out of the decision log. Both carry `due_at` and `billed_usdc`
        under those exact names, so accepting either is honest rather than
        lenient — and converting one to the other just to read two fields
        would be a second shape to keep in step.
        """
        if isinstance(row, dict):
            return row.get(name)
        return getattr(row, name, None)

    due = 0.0
    count = 0
    undated = 0
    soonest: float | None = None
    for ob in obligations or ():
        at = field(ob, "due_at")
        amount = field(ob, "billed_usdc") or 0.0
        if not isinstance(at, (int, float)):
            undated += 1
            continue
        if at - now > horizon_s:
            continue
        due += float(amount)
        count += 1
        if soonest is None or at < soonest:
            soonest = float(at)
    return Liquidity(
        held_usdc=held_usdc,
        due_usdc=round(due, 6),
        due_count=count,
        soonest_at=soonest,
        undated=undated,
        horizon_s=horizon_s,
    )


def short_by(liq: Liquidity | None, billed_usdc: float) -> float:
    """How much paying this bill would leave us short of what is dated.

    Zero when there is nothing to answer with — an unmeasured balance is not
    evidence of a shortfall, and refusing on a read that failed would be the
    "fail closed on no information" mistake the counterparty check already
    argues against.

    The bill itself is counted on both sides when it is one of the dated ones,
    which is correct: paying it removes it from both the cash and the queue. A
    shortfall therefore means the OTHER dated bills cannot be met afterwards.
    """
    if liq is None or liq.held_usdc is None:
        return 0.0
    after = liq.held_usdc - max(0.0, float(billed_usdc or 0.0))
    others = max(0.0, liq.due_usdc - max(0.0, float(billed_usdc or 0.0)))
    return round(max(0.0, others - after), 6)
