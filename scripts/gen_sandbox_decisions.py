#!/usr/bin/env python3
"""Generate the sandbox's decision fixtures BY RUNNING THE LADDER.

    uv run python scripts/gen_sandbox_decisions.py          # write the fixture
    uv run python scripts/gen_sandbox_decisions.py --check  # exit 1 if stale

WHY THIS EXISTS. The six sandbox rows were typed by hand, and one of them
described a decision the ladder cannot produce: `inv-0044` claimed *"140 USDC is
at or above the 100 per-payment limit, so the owner signs this one"* while
carrying `par_usdc: null`. With no benchmark and a bill of 140 against the 1
USDC unbenchmarked ceiling, check 5 returns first — the per-transaction rule at
check 8 is unreachable. The row was a plausible sentence about a decision that
never happened, rendered in the escalation queue, which cannot be shown at all
without a sandbox.

A demonstration the code cannot produce is the one kind of demo this project
cannot ship. So the fixture is no longer written; it is DERIVED. Each row below
states the inputs and the state it is meant to show, `decide()` produces the
row, and `--check` fails the build if the two drift apart.

The sandbox is still a demonstration — `businesses.json` says so and
`businesses.real()` keeps it out of every traction figure. What changes is that
it is now a demonstration of the real ladder rather than of a sentence.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from index_api.commitments import covering
from index_api.counterparty import CounterpartyVerdict
from index_api.operator import Obligation, decide
from index_api.par import Par

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "services" / "index_api" / "index_api" / "operator_decisions.sandbox.jsonl"

SELLER_A = "0x0721821F1B0a5b0f5B8e2a31cC2a4cF5cE2d0a11"
SELLER_B = "0x3d5364f5bD3a8C9eB0b7e4D7F2c6A1b9E0d8C7a2"
SELLER_C = "0xEC95f4477aB2d9C1e8F3b0A6d5C4e2B1a9D8c7F6"
CONTRACTOR = "0xD41c8F9b4e2A1d7C6b5A3e9F8d2C1b0A7e6D5c4B"
FLAGGED = "0x500C3A3498e7b2D1c0A9f8E7d6C5b4A3f2E1d0C9"

def _market(best_usdc: float = 0.5, best: str = SELLER_B) -> Par:
    """Three sellers of the same unit, with the cheapest at ``best_usdc``.

    THE CHEAPEST OFFER, NOT THE MEDIAN, is what check 6 reroutes against — so a
    bill sitting exactly at par is still rerouted if somebody independent is
    more than `MATERIAL_BP` below it. Writing these fixtures found that out: two
    rows intended as "paid at the going rate" came back as reroutes, because the
    market had a rival 204 bp cheaper and the agent correctly preferred it.

    A `pay` therefore needs a market in which this seller IS the cheapest, which
    is the honest shape of "there was nothing better to do".
    """
    return Par(
        resource="/compute/inference",
        available=True,
        denomination="unit",
        par_usdc=0.5,
        best_usdc=best_usdc,
        best_seller=best,
        sellers=3,
        basis="receipts",
    )


CLEAR = CounterpartyVerdict(address="", risk="clear", backend="denylist",
                            reason="not on a local list of 12")
UNKNOWN = CounterpartyVerdict(address="", risk="unknown", backend="denylist",
                              reason="no denylist is configured, so nothing was compared",
                              screened=False)
SANCTIONED = CounterpartyVerdict(address=FLAGGED, risk="flagged", backend="yente",
                                 matched=("eu_fsf", "us_ofac_sdn"),
                                 reason="2 match(es) in us_ofac_sdn")

AT = 1_790_900_000.0


def _ob(num: int, vendor: str, billed: float, **kw) -> Obligation:
    row = {
        "obligation_id": f"sandbox:inv-{num:04d}",
        "vendor": vendor,
        "billed_usdc": billed,
        "business": "sandbox",
        "category": "machine-services",
        "kind": "x402",
        "resource": "/compute/inference",
        "unit": "$/1k tokens",
        "period_start": AT - 7 * 86_400,
        "period_end": AT - 3_600,
    }
    row.update(kw)
    return Obligation(**row)


#: Each entry: the state it demonstrates, and the inputs that reach it.
CASES = [
    (
        "a bill at the going rate, paid on the agent's own authority",
        dict(ob=_ob(41, SELLER_A, 2.4, vendor_quantity=4.8),
             par=_market(best_usdc=0.5, best=SELLER_A), screen=CLEAR, metered_quantity=4.8,
             remaining_usdc=40.0, per_tx_limit_usdc=100.0),
    ),
    (
        "dearer than a rival who is actually offering: rerouted, with the saving named",
        dict(ob=_ob(42, SELLER_C, 3.1, vendor_quantity=5.2),
             par=_market(best_usdc=0.49), screen=CLEAR, metered_quantity=5.2,
             remaining_usdc=40.0, per_tx_limit_usdc=100.0),
    ),
    (
        "they billed for more than our own meter counted: escalated, recommend refuse",
        dict(ob=_ob(43, SELLER_B, 1.9, vendor_quantity=6.4),
             par=_market(), screen=CLEAR, metered_quantity=3.8,
             remaining_usdc=40.0, per_tx_limit_usdc=100.0),
    ),
    (
        # THE ROW THAT WAS WRONG. To reach check 8 the bill must first SURVIVE
        # check 5, so it needs a benchmark — which is the whole point: an
        # unpriceable 140 USDC bill never gets as far as the per-payment limit.
        "above the per-payment limit, so the owner signs it: escalated, recommend pay",
        dict(ob=_ob(44, CONTRACTOR, 140.0, vendor_quantity=280.0,
                    category="contractors", kind="milestone",
                    resource="/work/milestone-2", unit="$/hour"),
             par=Par(resource="/work/milestone-2", available=True, denomination="unit",
                     par_usdc=0.5, best_usdc=0.5, best_seller=SELLER_A, sellers=2,
                     basis="receipts"),
             screen=CLEAR, metered_quantity=280.0,
             remaining_usdc=400.0, per_tx_limit_usdc=100.0),
    ),
    (
        "a sanctioned counterparty: escalated, recommend refuse, before any pricing",
        dict(ob=_ob(45, FLAGGED, 0.8, vendor_quantity=1.6),
             par=_market(), screen=SANCTIONED, metered_quantity=1.6,
             remaining_usdc=40.0, per_tx_limit_usdc=100.0),
    ),
    (
        "paid, but nothing screened the counterparty — and the record says so",
        dict(ob=_ob(46, SELLER_A, 0.45, vendor_quantity=0.9),
             par=_market(best_usdc=0.5, best=SELLER_A), screen=UNKNOWN, metered_quantity=0.9,
             remaining_usdc=40.0, per_tx_limit_usdc=100.0),
    ),
    (
        # THE SYMBOLON REFUSING. The agreement covering this service allows 12
        # units; the vendor delivered and billed for 20. Inside no market's
        # opinion and outside the agreement, so it is held back with the
        # overage named — which is the half of a three-way match that saves
        # money rather than merely recording it.
        "more than the agreement allows: held back, with the overage priced",
        dict(ob=_ob(47, SELLER_A, 10.0, vendor_quantity=20.0),
             par=_market(best_usdc=0.5, best=SELLER_A), screen=CLEAR,
             metered_quantity=20.0,
             remaining_usdc=40.0, per_tx_limit_usdc=100.0),
    ),
]


def build() -> list[dict]:
    """Each case through `decide()`, with the agreement looked up for real.

    `covering()` rather than a hand-passed commitment, so the fixture exercises
    the register the live path reads and a row is covered only if the register
    really covers it. Two of these are, one is deliberately not — the vendor in
    the reroute case is a third party we never agreed anything with, which is
    why the market is the right basis there.
    """
    rows = []
    for i, (shows, kw) in enumerate(CASES):
        at = AT - (len(CASES) - i) * 600
        ob = kw["ob"]
        agreement = covering(ob.business, ob.vendor, ob.resource, at)
        d = decide(now=at, commitment=agreement, **kw)
        row = {k: v for k, v in d.as_record().items()}
        # `as_record` omits the payment's own results by design; a fixture is a
        # record of a decision, so the two are added back as a real log row has
        # them. Nothing here ever paid: the sandbox spends no money.
        row["paid_usdc"] = d.paid_usdc
        row["tx"] = d.tx
        rows.append((shows, row))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="report, do not write")
    a = ap.parse_args()

    rows = build()
    text = "".join(json.dumps(r, separators=(",", ":"), default=str) + "\n" for _s, r in rows)

    for shows, row in rows:
        agreed = row.get("commitment_verdict") or "-"
        print(f"  {row['obligation_id']:<22} {row['intent']:<9} "
              f"{'recommend ' + row['recommended_intent'] if row['recommended_intent'] else '':<18} "
              f"{agreed:<16} {shows}")

    if a.check:
        have = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if have == text:
            print(f"\n✓ {TARGET.name} is what the ladder produces")
            return 0
        print(f"\n✗ {TARGET.name} is stale — run `make sandbox-decisions`", file=sys.stderr)
        return 1

    TARGET.write_text(text, encoding="utf-8")
    print(f"\nwrote {TARGET.relative_to(ROOT)}: {len(rows)} decision(s), each one the ladder reached")
    return 0


if __name__ == "__main__":
    sys.exit(main())
