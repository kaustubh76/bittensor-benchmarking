#!/usr/bin/env python3
"""Generate the pivot diagram (excalidraw): the index demoted to a feature.

    uv run python scripts/gen_pivot_diagram.py
    uv run python scripts/gen_pivot_diagram.py --check   # build, validate, write nothing

ONE ARGUMENT, DRAWN. The index is what this project leads with, and it is the
weakest thing it owns: `anchors/GAP.md` records its own reference levels sitting
20x to 1159x away from real market prices, the published tape is `sim`, and every
payer so far is ours. The spend operator needs none of that to be useful. So in
this picture the benchmark is a small box wired into ONE gate of eight, and the
money path is the subject.

Reuses `gen_architecture.py`'s drawing vocabulary rather than re-deriving the
excalidraw wire format: same palette, same `card`/`zone`/`wire` helpers, same
bound-arrow validation. That module keeps its elements in module-level lists, so
this one clears them first and then builds its own scene — importing it is safe
because its `build()` only runs under `__main__`.

HONESTY IS A DRAWING RULE HERE. Solid stroke is working today; dashed is built
but not deployed or not wired; red is a gap stated in words. A diagram that
flatters the system is the one a reviewer catches, so `PolicyWallet` undeployed,
`moved_usdc` 0 and the unbuilt Discovery source are all on the page.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import gen_architecture as ga  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "docs/assets/acr_pivot.excalidraw"

TEAL, BLUE, GOLD, GREEN, PURPLE, RED, GRAY, INK = (
    ga.TEAL, ga.BLUE, ga.GOLD, ga.GREEN, ga.PURPLE, ga.RED, ga.GRAY, ga.INK
)

# The eight refusals, in the order `decide()` fires them. The order is the
# product: a duplicate is caught before a price is ever fetched, and the budget
# is consulted last because it is the only one that needs a chain read.
GATES = [
    ("g1", "1 · duplicate", "paid once already → refuse, permanently"),
    ("g2", "2 · payable", "no payee, or a burn address → refuse"),
    ("g3", "3 · meter", "billed above what we counted → escalate"),
    ("g4", "4 · counterparty", "sanctions hit → escalate, never pay"),
    ("g5", "5 · benchmarked", "fewer than 2 sellers → unbenchmarked, not wrong"),
    ("g6", "6 · price", "materially above the cheapest offer → reroute"),
    ("g7", "7 · timing", "due now, or worth holding for the discount"),
    ("g8", "8 · budget & liquidity", "over the limit → the owner's call, not ours"),
]


def build() -> None:
    ga._elements.clear()
    ga._by_id.clear()
    ga._boxes.clear()

    ga.text(60, 0, "ACR — the pivot: the index becomes a feature, the money path becomes the product",
            22, INK, w=1500, bold=True)

    # ---- Zone 0: what the product leads with today, and why it is being demoted
    ga.zone(60, 48, 1920, 160, "TODAY — the index is the product", GRAY)
    ga.card("t_tape", 90, 100, 300, 70, "simulated tape", ["calibrated sim, not demand"], GRAY)
    ga.card("t_index", 440, 100, 300, 70, "index", ["ACR-INF · GPU · DATA"], GRAY)
    ga.card("t_print", 790, 100, 300, 70, "hourly print", ["on Arc mainnet, chain 5042"], GRAY)
    ga.card("t_venue", 1140, 100, 300, 70, "futures venue", ["cash-settles vs the print"], GRAY)
    for a, b in (("t_tape", "t_index"), ("t_index", "t_print"), ("t_print", "t_venue")):
        ga.wire(a, b, GRAY)
    ga.text(1490, 104,
            "reference levels 20x–1159x off market\n"
            "(anchors/GAP.md, deliberately frozen)\n"
            "tape = sim · every payer is ours · moved 0",
            13, RED, w=470)

    # ---- Zone 1: what comes in
    ga.zone(60, 250, 430, 450, "WHAT COMES IN", TEAL)
    ga.card("usdc", 90, 302, 370, 76, "a business's USDC",
            ["held in a contract, not by us"], TEAL)
    ga.card("pw", 90, 400, 370, 150, "PolicyWallet",
            ["per-category caps · per-tx limit",
             "refuses rather than clamps",
             "enforced in the contract,",
             "not in the prompt"], TEAL)
    ga.card("obl", 90, 572, 370, 106, "obligations",
            ["what the paywall actually settled, or",
             "--entitlements: an invoice + an",
             "independent count. \"a usage export,",
             "and no keys.\""], TEAL)
    ga.wire("usdc", "pw", TEAL)

    # ---- Zone 2: the decision, as a ladder
    ga.zone(540, 250, 900, 900, "THE DECISION — eight refusals, in firing order", BLUE)
    y = 320
    for gid, title, line in GATES:
        ga.card(gid, 580, y, 380, 56, title, [line], BLUE, title_size=14, body_size=12)
        y += 64
    # strict=False, not True: pairing a list with its own tail is deliberately
    # one short, which is what makes this walk consecutive gates.
    for (a, _, _), (b, _, _) in zip(GATES, GATES[1:], strict=False):
        ga.wire(a, b, BLUE, side_a="B", side_b="T")

    ga.card("n_meter", 580, 865, 820, 80, "why an independent meter at all",
            ["A seller who supplies both the goods and the measuring cup will",
             "eventually supply a smaller cup. Live: billed 6.4, we metered 3.8."], GOLD,
            title_size=14, body_size=12)
    ga.card("n_screen", 580, 955, 820, 80, "a screen that cannot answer is never \"clear\"",
            ["Three states, and the middle one is the point: `unknown` is not",
             "`clear`, because a timeout must not read as a clean bill of health."], GOLD,
            title_size=14, body_size=12)
    ga.card("n_liq", 580, 1045, 820, 80, "budget is permission · liquidity is money",
            ["`cap - spent` reads healthy on a wallet holding nothing, and the",
             "payment then reverts on chain. Both are needed; neither substitutes."], GOLD,
            title_size=14, body_size=12)

    # ---- The benchmark: small, and wired into exactly one gate
    ga.zone(60, 740, 430, 410, "THE BENCHMARK — one gate of eight", GOLD)
    ga.card("par", 90, 792, 370, 150, "PAR — observed quotes",
            ["prices somebody really offered,",
             "at the same unit",
             "MIN_SELLERS = 2 · one offer is",
             "not a market",
             "basis points only, never dollars"], GOLD)
    ga.card("quotes", 90, 962, 370, 165, "three quote populations",
            ["the fleet's own prices (first-party,",
             "never medianed with the market)",
             "anchors/ baskets (a URL per row)",
             "x402 Discovery — 2,818 listings,",
             "7 sellers on Arc  [not built]"], GOLD)
    ga.wire("quotes", "par", GOLD, side_a="T", side_b="B")
    ga.wire("par", "g6", GOLD, side_a="R", side_b="L", lane=515)
    ga.wire("obl", "g1", TEAL, side_a="R", side_b="L", lane=515)
    ga.wire("pw", "g8", TEAL, side_a="R", side_b="L", lane=530)

    # ---- Zone 3: what comes out. Each outcome sits at the y of the gate that
    # produces it, so the four wires are straight lines that cross nothing and
    # the mapping gate→intent is readable without following a route.
    ga.zone(1490, 250, 490, 900, "WHAT COMES OUT", GREEN)
    ga.card("refuse", 1520, 320, 240, 56, "refuse", ["and say why"], RED, title_size=14, body_size=12)
    ga.card("escalate", 1520, 512, 240, 56, "escalate", ["not its call"], GOLD, title_size=14, body_size=12)
    ga.card("reroute", 1520, 640, 240, 56, "reroute", ["a cheaper seller"], GREEN, title_size=14, body_size=12)
    ga.card("pay", 1520, 768, 240, 56, "pay", ["clears all eight"], GREEN, title_size=14, body_size=12)

    ga.card("owner", 1520, 856, 430, 104, "the owner's own wallet",
            ["spendAsOwner, gated on msg.sender == owner",
             "not a signature the agent relays,",
             "and not a flag in a database"], PURPLE, body_size=12)
    ga.card("ledger", 1520, 982, 430, 166, "the ledger",
            ["double-entry beancount, every transaction",
             "summing to zero — each annotated with the",
             "rule that fired, what we metered, the par,",
             "the screen, and the hash the chain holds",
             "a reroute moved no money, so it is a note",
             "and never a transaction"], PURPLE, body_size=12)

    ga.wire("g1", "refuse", RED, side_a="R", side_b="L")
    ga.wire("g4", "escalate", GOLD, side_a="R", side_b="L")
    ga.wire("g6", "reroute", GREEN, side_a="R", side_b="L")
    ga.wire("g8", "pay", GREEN, side_a="R", side_b="L")
    ga.wire("escalate", "owner", PURPLE, side_a="R", side_b="T", lane=1860)
    ga.wire("owner", "ledger", PURPLE, side_a="B", side_b="T")

    # ---- What the brief actually measures, given its own width
    ga.card("metrics", 60, 1190, 1920, 112, "WHAT THE BRIEF MEASURES — and where it stands",
            ["businesses operated: 1 real, 1 labelled demonstration     ·     "
             "USDC received and paid out: never netted, never summed across chains     ·     paid out = 0",
             "obligations settled on time without a human touching them: 0     ·     "
             "decisions made vs escalated, and how often the human agreed: no approval has ever run"],
            GREEN)

    # ---- The honest footer
    ga.text(60, 1326,
            "LIVE TODAY  ·  the eight refusals, the independent meter, the screen, PAR from the fleet and "
            "from anchors/, the ledger, the replayable record.", 13, INK, w=1900)
    ga.text(60, 1350,
            "NOT YET  ·  PolicyWallet is deployed nowhere, so moved_usdc is 0 and no obligation has been "
            "settled without a human. Discovery is unbuilt. The loop runs on Arc testnet.", 13, RED, w=1900)
    ga.text(60, 1386,
            "The test of whether this is a pivot and not a rebrand: after it, the published index values can "
            "be deleted and the product loses nothing.", 15, INK, w=1900, bold=True)


def main() -> None:
    build()
    summary = ga.validate()
    doc = {
        "type": "excalidraw",
        "version": 2,
        "source": "acr-pivot-generator",
        "elements": ga._elements,
        "appState": {"gridSize": None, "viewBackgroundColor": "#ffffff"},
        "files": {},
    }
    if "--check" in sys.argv:
        print("dry-run:", summary)
        return
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(doc, indent=1, ensure_ascii=False))
    print(f"wrote {OUT.relative_to(Path.cwd()) if OUT.is_relative_to(Path.cwd()) else OUT}: {summary}")


if __name__ == "__main__":
    main()
