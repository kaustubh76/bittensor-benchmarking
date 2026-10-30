"""ACR-TAO — the ACR rate in TAO, and what a subnet's alpha buys of its own work.

`docs/BITTENSOR.md` promised this number and nothing computed it, which is the
exact failure the repo's claim gates exist to catch. This module closes it.

### It is a change of numéraire, not a fourth index

ACR-TAO is deliberately **not** an entry in `acr_core.INDEX_REGISTRY`, for one
practical reason and one principled one.

Practically, `ALL_INDEX_IDS` is iterated by `scripts/gen_golden.py`,
`scripts/eval.py`, `scripts/rate.py`, `scripts/gen_evalset.py` and
`scripts/anchors.py`. A fourth id would demand its own anchor basket, its own
eval gate, its own golden entry and its own rate components — a large blast
radius for what is arithmetic on an existing print.

Principally, it would be wrong. ACR-INF prices one unit of inference; ACR-TAO
prices the same unit in a different currency. The *good* has not changed, only
the money. A change of unit is a conversion, and conversions do not get their own
index — otherwise every reference rate would need one entry per tradeable
currency.

### The caveat, which is the point and not a disclaimer

**A TAO-denominated rate moves when TAO moves, even when the price of compute has
not changed at all.** Over a week where inference is flat in dollars and TAO
halves, ACR-TAO doubles — and nothing about the compute market happened.

That is not a defect to apologise for. It is precisely why a reference rate is
denominated in a stable unit in the first place, and why ACR's own estimand
(`docs/methodology.md` §1) is a *dollar* rate. So:

* `tao_per_unit` is the right number for a **TAO-native payer** deciding what to
  spend out of a TAO balance.
* It is the **wrong number for reading the compute market**, because a move in it
  is two signals added together and you cannot tell which moved.

`test_denomination.py` asserts that conflation directly rather than describing it:
it holds the dollar rate fixed, moves TAO, and shows the TAO rate moving. A test
that demonstrates a limitation is worth more than a sentence claiming one.

### What is genuinely new: `alpha_per_unit`

`alpha_per_unit` is how much of a subnet's alpha token buys one unit of the work
that subnet actually delivers. No existing Bittensor metric is this. An alpha
price today is a claim on future emission — a number about the token — and it has
never been quotable against delivered compute. This is the work-denominated
yardstick the Bittensor submission argues for, and it is computable only because
the pool reserves and the realized price of the work are both observable.

Read it as a ratio and not a valuation: it says nothing about whether the token
is cheap, only what it currently buys.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from acr_core import ACRPrint

#: Divergence between two independently-sourced TAO/USD rates above which the
#: pair is worth a human look. Measured 2026-10-29: Taostats $248.57 against
#: Chutes `/pricing` $248.96 — 0.16% apart, two unrelated sources. A percent is
#: loose enough not to cry wolf on ordinary quote latency and tight enough that
#: a stale or wrong feed shows up.
TAO_USD_DIVERGENCE_WARN = 0.01


@dataclass(frozen=True)
class TaoDenominated:
    """One ACR print, restated in TAO and in a subnet's alpha."""

    index_id: str
    #: The published rate, in its own unit (e.g. $/1k tokens). Unchanged.
    usd_per_unit: float
    tao_usd: float
    #: Which feed `tao_usd` came from. Recorded rather than averaged — see
    #: `cross_check_tao_usd`.
    tao_usd_source: str
    #: The same rate, in TAO. Moves with TAO; see the module docstring.
    tao_per_unit: float
    #: TAO per alpha for the subnet, from its pool reserves. None when unknown.
    alpha_price_tao: float | None = None
    #: Alpha tokens per unit of work. The work-denominated yardstick.
    alpha_per_unit: float | None = None
    netuid: int | None = None
    as_of: str = ""

    @property
    def units_per_tao(self) -> float:
        """How much work one TAO buys — the reciprocal, which reads better aloud."""
        return 1.0 / self.tao_per_unit if self.tao_per_unit > 0 else float("inf")

    def summary(self) -> str:
        line = (
            f"{self.index_id}: {self.usd_per_unit:.8f} USD = "
            f"{self.tao_per_unit:.10f} TAO per unit "
            f"(TAO/USD {self.tao_usd:,.4f} via {self.tao_usd_source})"
        )
        if self.alpha_per_unit is not None:
            line += f"; {self.alpha_per_unit:.6f} alpha per unit on netuid {self.netuid}"
        return line


def denominate(
    print_obj: ACRPrint,
    tao_usd: float,
    pool=None,
    *,
    tao_usd_source: str = "taostats",
    as_of: str | None = None,
) -> TaoDenominated | None:
    """Restate an ACR print in TAO, and in alpha where the pool is known.

    ``pool`` is an `acr_tape.taostats_client.SubnetPool`. Its
    ``alpha_price_tao`` is reused rather than recomputed from the reserves, so
    there is one definition of the pool price in the codebase and not two.

    ``None`` on a non-positive TAO/USD rate: dividing by it would produce an
    infinity that reads like a number, and a missing FX rate means the conversion
    cannot be done rather than that the work is free.
    """
    if tao_usd <= 0 or print_obj.value <= 0:
        return None

    tao_per_unit = print_obj.value / tao_usd
    alpha_price = getattr(pool, "alpha_price_tao", None) if pool is not None else None
    alpha_per_unit = (
        tao_per_unit / alpha_price if alpha_price is not None and alpha_price > 0 else None
    )
    return TaoDenominated(
        index_id=print_obj.index_id,
        usd_per_unit=print_obj.value,
        tao_usd=tao_usd,
        tao_usd_source=tao_usd_source,
        tao_per_unit=tao_per_unit,
        alpha_price_tao=alpha_price,
        alpha_per_unit=alpha_per_unit,
        netuid=getattr(pool, "netuid", None) if pool is not None else None,
        as_of=as_of or datetime.now(UTC).isoformat(timespec="seconds"),
    )


def cross_check_tao_usd(a: float, b: float) -> tuple[float, bool]:
    """(relative divergence, whether it is within tolerance) for two TAO/USD feeds.

    Two independent sources are available — Taostats `/v1/price` and Chutes
    `/pricing.tao_usd` — and they are **not averaged**. An average of a good feed
    and a stale one is a number with no provenance that looks healthier than
    either input, and it hides the single thing worth noticing: that they
    disagree. So one is used, which one is recorded on the result, and the other
    is a check.
    """
    if a <= 0 or b <= 0:
        return float("inf"), False
    divergence = abs(a - b) / ((a + b) / 2.0)
    return divergence, divergence <= TAO_USD_DIVERGENCE_WARN
