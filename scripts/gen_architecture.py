#!/usr/bin/env python
"""Generate the comprehensive ACR architecture diagram (excalidraw).

The blueprint grew past its original 143-element sketch — this regenerates a
detailed canvas depicting the *implemented* system: the four-pillar estimator in
full, the signer abstraction, EIP-712 on-chain verification, the x402 facilitator
(Dev/Circle), robustness diagnostics, TapeSource (Sim/Arc), on-chain reads, the
Arc native-USDC economics, and the verification surface.

Deterministic (no RNG, fixed timestamps) so re-running yields byte-identical
output. Every connector arrow is *bound* to its two boxes (start/end binding +
boundElements back-refs), the lesson from the earlier wiring pass.

    uv run python scripts/gen_architecture.py [--check]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "acr_architecture.excalidraw"
UPDATED = 1731542400000

# --- palette (the README color code) ---
TEAL, BLUE, GOLD, ORANGE, GREEN, PURPLE, RED, GRAY, INK = (
    "#0c8599", "#1971c2", "#f08c00", "#e8590c", "#2f9e44",
    "#9c36b5", "#e03131", "#495057", "#1e1e1e",
)

_elements: list[dict] = []
_by_id: dict[str, dict] = {}
_boxes: dict[str, tuple[float, float, float, float]] = {}
_ctr = [0]


def _nid(prefix: str) -> str:
    _ctr[0] += 1
    return f"{prefix}-{_ctr[0]:04d}"


def _base(el: dict) -> dict:
    _ctr[0] += 1
    el.setdefault("angle", 0)
    el.setdefault("strokeWidth", 2)
    el.setdefault("strokeStyle", "solid")
    el.setdefault("roughness", 0)
    el.setdefault("opacity", 100)
    el.setdefault("groupIds", [])
    el.setdefault("frameId", None)
    el.setdefault("roundness", None)
    el.setdefault("boundElements", None)
    el.setdefault("link", None)
    el.setdefault("locked", False)
    el.setdefault("isDeleted", False)
    el.setdefault("fillStyle", "solid")
    el.setdefault("backgroundColor", "transparent")
    el["seed"] = 100000 + _ctr[0] * 7
    el["version"] = 1
    el["versionNonce"] = 200000 + _ctr[0] * 13
    el["updated"] = UPDATED
    _elements.append(el)
    _by_id[el["id"]] = el
    return el


def rect(rid, x, y, w, h, stroke, *, dashed=False, bg="transparent", rounded=True, sw=2):
    _base({
        "id": rid, "type": "rectangle", "x": x, "y": y, "width": w, "height": h,
        "strokeColor": stroke, "backgroundColor": bg,
        "strokeStyle": "dashed" if dashed else "solid", "strokeWidth": sw,
        "roundness": {"type": 3} if rounded else None,
    })
    _boxes[rid] = (x, y, w, h)
    return rid


def text(x, y, s, size, color, *, w=None, align="left", bold=False):
    n = s.count("\n") + 1
    height = round(n * size * 1.25, 1)
    width = w if w is not None else max(10, int(max(len(line) for line in s.split("\n")) * size * 0.55))
    _base({
        "id": _nid("t"), "type": "text", "x": x, "y": y, "width": width, "height": height,
        "strokeColor": color, "fontSize": size, "fontFamily": 2 if not bold else 3,
        "textAlign": align, "verticalAlign": "top", "containerId": None,
        "lineHeight": 1.25, "text": s, "originalText": s, "autoResize": False,
    })


def zone(x, y, w, h, title, color):
    rid = rect(_nid("zone"), x, y, w, h, color, dashed=True, rounded=True, sw=2)
    text(x + 18, y + 12, title, 15, color, w=w - 36, bold=True)
    return rid


def card(cid, x, y, w, h, title, lines, color, *, title_size=15, body_size=13):
    rect(cid, x, y, w, h, color, rounded=True, sw=2)
    text(x + 13, y + 10, title, title_size, color, w=w - 24, bold=True)
    if lines:
        text(x + 13, y + 12 + title_size * 1.5, "\n".join(lines), body_size, INK, w=w - 24)
    return cid


def _center(b):
    x, y, w, h = b
    return x + w / 2, y + h / 2


def _edge(b, tx, ty):
    x, y, w, h = b
    cx, cy = x + w / 2, y + h / 2
    dx, dy = tx - cx, ty - cy
    if dx == 0 and dy == 0:
        return cx, cy
    sx = (w / 2) / abs(dx) if dx else 1e9
    sy = (h / 2) / abs(dy) if dy else 1e9
    s = min(sx, sy)
    return cx + dx * s, cy + dy * s


# Edge-midpoint of a box on a given side (L/R/T/B). Attaching here with a
# perpendicular stub makes the excalidraw `focus:0` binding resolve to exactly
# this point — which is why straight orthogonal arrows meet boxes square-on
# while diagonals looked detached.
_SIDE_PT = {
    "L": lambda x, y, w, h: (x, y + h / 2),
    "R": lambda x, y, w, h: (x + w, y + h / 2),
    "T": lambda x, y, w, h: (x + w / 2, y),
    "B": lambda x, y, w, h: (x + w / 2, y + h),
}


def _side(box, s):
    return _SIDE_PT[s](*box)


def _orthogonalize(pts):
    """Insert right-angle elbows so every segment is axis-aligned, then dedupe."""
    out = [pts[0]]
    for x, y in pts[1:]:
        px, py = out[-1]
        if abs(x - px) > 0.5 and abs(y - py) > 0.5:
            out.append((x, py))  # horizontal-first elbow
        out.append((x, y))
    dd = [out[0]]
    for p in out[1:]:
        if abs(p[0] - dd[-1][0]) > 0.5 or abs(p[1] - dd[-1][1]) > 0.5:
            dd.append(p)
    return dd


def wire(a, b, color, *, dashed=False, side_a=None, side_b=None, lane=None,
         first="auto", via=None, sw=2):
    """Orthogonal (Manhattan) connector: leaves/enters box-edge midpoints on the
    facing sides, routed as axis-aligned legs. `side_a`/`side_b` force the exit
    /entry side; `lane` sets the mid-corridor coordinate; `via` gives explicit
    corridor points (auto-elbowed). Every arrow stays bound both ends."""
    ba, bb = _boxes[a], _boxes[b]
    ax, ay, aw, ah = ba
    bx, by, bw, bh = bb
    dx = (bx + bw / 2) - (ax + aw / 2)
    dy = (by + bh / 2) - (ay + ah / 2)
    if side_a is None:
        horiz = first == "H" or (first == "auto" and abs(dx) >= abs(dy))
        side_a = ("R" if dx >= 0 else "L") if horiz else ("B" if dy >= 0 else "T")
    if side_b is None:
        side_b = ("L" if dx >= 0 else "R") if side_a in ("L", "R") else ("T" if dy >= 0 else "B")

    sp = _side(ba, side_a)
    ep = _side(bb, side_b)
    sx, sy = sp
    ex, ey = ep
    if via is not None:
        chain = [sp, *via, ep]
    else:
        eh, nh = side_a in ("L", "R"), side_b in ("L", "R")
        if eh and nh:
            lx = lane if lane is not None else (sx + ex) / 2
            chain = [sp, (lx, sy), (lx, ey), ep]
        elif not eh and not nh:
            ly = lane if lane is not None else (sy + ey) / 2
            chain = [sp, (sx, ly), (ex, ly), ep]
        elif eh and not nh:
            chain = [sp, (ex, sy), ep]
        else:
            chain = [sp, (sx, ey), ep]

    chain = _orthogonalize(chain)
    ox, oy = chain[0]
    pts = [[round(px - ox, 2), round(py - oy, 2)] for px, py in chain]
    xs = [p[0] for p in chain]
    ys = [p[1] for p in chain]
    aid = _nid("arr")
    _base({
        "id": aid, "type": "arrow", "x": round(ox, 2), "y": round(oy, 2),
        "width": round(max(xs) - min(xs), 2), "height": round(max(ys) - min(ys), 2),
        "strokeColor": color, "strokeStyle": "dashed" if dashed else "solid",
        "strokeWidth": sw, "points": pts, "lastCommittedPoint": None,
        "startArrowhead": None, "endArrowhead": "arrow",
        "startBinding": {"elementId": a, "focus": 0.0, "gap": 4.0},
        "endBinding": {"elementId": b, "focus": 0.0, "gap": 4.0},
    })
    for box_id in (a, b):
        el = _by_id[box_id]
        el["boundElements"] = (el.get("boundElements") or []) + [{"id": aid, "type": "arrow"}]
    return aid


def build() -> None:
    # ---------- masthead + legend ----------
    text(80, 40, "ACR — THE ARC COMPUTE RATE", 32, INK, w=1100, bold=True)
    text(80, 92, "A manipulation-resistant benchmark family for machine commerce · "
                 "one estimand · four pillars · settlement-grade on-chain rate", 15, GRAY, w=1600)
    text(80, 116, "◆ LIVE on Arc MAINNET (chain 5042) · Terminal on Vercel · Seller API on Render · "
                  "hourly Circle-signed posts · agents read AND trade the rate (autonomous hedger)", 13, GREEN, w=1700, bold=True)

    zone(2860, 40, 620, 250, "LEGEND", GRAY)
    legend = [
        (TEAL, "market exhaust / inputs"), (BLUE, "estimator core (the product)"),
        (GOLD, "ACR prints / positioning"), (ORANGE, "on-chain (Foundry)"),
        (GREEN, "instrument (cash-settled)"), (PURPLE, "distribution (x402)"),
        (RED, "adversarial / red team"), (GRAY, "zones / verification"),
    ]
    for i, (c, lbl) in enumerate(legend):
        yy = 84 + i * 24
        rect(_nid("sw"), 2884, yy, 18, 18, c, bg=c, rounded=False, sw=1)
        text(2912, yy - 1, lbl, 13, INK, w=560)

    # ---------- A · MARKET EXHAUST & INGESTION ----------
    zone(60, 300, 620, 790, "A · MARKET EXHAUST & INGESTION", TEAL)
    card("a_x402", 80, 352, 560, 112, "x402 AUTHORIZATIONS",
         ["EIP-3009 signed payloads", "price · size · buyer · seller · ts",
          "highest-frequency observable"], TEAL)
    card("a_gateway", 80, 474, 560, 120, "GATEWAY BATCH SETTLEMENTS",
         ["net positions, settled later", "observed = latent ∘ batch-op + noise",
          "y = H·x + v   (the convolution)"], TEAL)
    card("a_attest", 80, 604, 560, 112, "SELLER ATTESTATIONS",
         ["EIP-712: model class · latency SLO · schema", "→ AttestationRegistry.sol",
          "feeds Pillar-2 features"], ORANGE)
    card("a_adv", 80, 726, 560, 120, "ADVERSARIAL FLOW",
         ["self-deals · reciprocal funding ring", "pure-sybil cluster (all 3 shapes)",
          "a first-class contaminated input"], RED)
    card("a_tape", 80, 856, 560, 120, "TapeSource (ABC)",
         ["SimSource — calibrated simulator", "ArcSource — Arc 5042 live decode",
          "attested-market: amount = price · non-attested dropped"], TEAL, body_size=12)

    # ---------- B · ESTIMATOR CORE ----------
    zone(720, 300, 1480, 900, "B · ESTIMATOR CORE — one estimand: the latent price of machine services", BLUE)
    card("b_indexer", 745, 352, 350, 120, "TAPE INDEXER",
         ["raw event ingestion", "volume-time bars (equal $)",
          "clean ticks — Malachite finality"], BLUE)
    card("b_obs", 1120, 352, 430, 175, "OBSERVATION MODEL · Pillar 1",
         ["observed = latent ∘ batch-H + noise", "state-space; H = box avg, width k",
          "Kalman filter + RTS smoother", "deconvolves batching; var → CI",
          "observation_model.py"], BLUE)
    card("b_clean", 745, 500, 360, 215, "CLEANING STACK",
         ["4 funding-graph defenses:", "· self-deal  (buyer = seller)",
          "· wash-cycle  (reciprocal A↔B)", "· Louvain sybil   I/(I+B)",
          "· 2-sided cluster caps", "~100% injected wash zeroed"], BLUE)
    card("b_robust", 1180, 560, 370, 180, "ROBUST ESTIMATOR",
         ["α-trim volume-weighted median", "breakdown point ½",
          "weighted-bootstrap CI / print", "robust.py"], BLUE)
    card("b_hedonic", 745, 750, 400, 180, "HEDONIC ADJUSTMENT · Pillar 2",
         ["WLS:  log p ~ quality features", "reprice → MID / 250ms reference",
          "→ constant-quality rate", "Case-Shiller for compute", "hedonic.py"], BLUE)
    card("b_robdiag", 1180, 762, 370, 96, "ROBUSTNESS DIAGNOSTICS",
         ["single-cluster flip  c/(0.5(1−2α))", "leave-one-community-out shift (bp)",
          "robustness.py"], BLUE)
    card("b_prints", 1180, 888, 370, 150, "ACR PRINTS (hourly)",
         ["ACR-INF  $/1k tokens", "ACR-GPU  $/GPU-sec", "ACR-DATA  $/MB",
          "+ CI  + attack-cost / bp"], GOLD)
    card("b_bound", 745, 962, 400, 200, "MANIPULATION COST BOUND · Pillar 3",
         ["least surviving wash N* to move 1bp", "threat: cleaning-evading sybil",
          "cost = 2N*·fee_bp + (N*/trade + ids)·fee_flat", "a NUMBER on Arc (deterministic fee)",
          "bound.py + redteam/optimal_attack.py"], RED)

    # ---------- C · ON-CHAIN ----------
    zone(2240, 300, 560, 880, "C · ON-CHAIN (settlement-grade)", ORANGE)
    card("c_signer", 2260, 352, 510, 116, "SIGNER (custody-only in prod)",
         ["build_role_signer(maker·taker·poster·owner) → own Circle wallet",
          "CircleWalletSigner (Dev-Controlled) · ignores ambient .env key",
          "LocalKeySigner only for dev/anvil · docs/WALLETS.md"], ORANGE, body_size=11)
    card("c_oracle", 2260, 480, 510, 205, "ACROracle.sol",
         ["EIP-712 verifies the signer", "any relayer submits postPrint",
          "invariants: value∈CI · bound>0", "monotone ts · MAX_TS_SKEW",
          "isStale · pause · 2-step owner"], ORANGE)
    card("c_registry", 2260, 697, 510, 116, "AttestationRegistry.sol",
         ["EIP-712 seller metadata · nonce + deadline",
          "attestWithSig — seller signs, relayer pays",
          "→ one relayer registers many sellers"], ORANGE)
    card("c_foundry", 2260, 825, 510, 92, "FOUNDRY",
         ["80 tests · oracle · registry · futures · attestor · receipt mirror",
          "invariants: netOI=0 · collateral-backed · fail_on_revert"], ORANGE, body_size=12)
    card("c_deploy", 2260, 929, 510, 132, "CIRCLE DEPLOY",
         ["Smart Contract Platform + Gas Station", "oracle + registry + ACRFutures → setSigner",
          "deploy_circle.py · Deploy / DeployFutures / DeployAttestor.s.sol"], ORANGE, body_size=12)
    card("c_attestor", 2260, 1073, 510, 96, "FeedAccessAttestor.sol",
         ["EIP-712 FeedAccess: payer·beneficiary·paidUntil·amountUsdc·nonce",
          "seller signs (poster Circle wallet) · anyone relays redeem()",
          "hasFeedAccess(addr) → on-chain fact · MAX_ACCESS_WINDOW 90d",
          "0xe671…FD47 · attest_feed_access.py"], ORANGE, body_size=11)

    # ---------- D · INSTRUMENT ----------
    zone(2240, 1210, 560, 350, "D · INSTRUMENT & PUBLIC DESK (Pillar 4 · on-chain)", GREEN)
    card("d_future", 2260, 1256, 510, 96, "ACRFutures.sol  (on-chain venue)",
         ["weekly cash-settled vs oracle · stale-guard (7200s)",
          "maker mirrors every taker → net OI = 0 · 20% margin",
          "USDC collateral · Traded tape · socialized-loss settle"], GREEN, body_size=12)
    card("d_mm", 2260, 1358, 510, 96, "MARKET MAKER + VENUE KEEPER",
         ["A–S: r = s − q·γ·σ²·(T−t) · inventory skews the curve",
          "keeper OWNS the venue (maker Circle wallet 0x9D44…)",
          "roll_if_needed opens next series onlyOwner · shape read from chain"], GREEN, body_size=11)
    card("d_desk", 2260, 1460, 510, 96, "PUBLIC DESK",
         ["readers trade ACRFutures via Circle user-controlled wallet",
          "PIN ceremony · SCA · Gas-Station gas · no server key",
          "desk.py · /desk/* · per-identity rate-limited"], GREEN, body_size=12)

    # ---------- E · DISTRIBUTION ----------
    zone(720, 1250, 1480, 360, "E · DISTRIBUTION (self-referential · x402-monetized)", PURPLE)
    card("e_api", 745, 1302, 430, 190, "INDEX API (FastAPI)",
         ["x402-gated: /prints /curve /vol /futures", "/desk/* /marketplace /webhooks /hedger /revenue",
          "lifespan: refresh + poster + keeper + book warm", "/onchain + futures readers (90s TTL)"], PURPLE, body_size=12)
    card("e_facil", 1195, 1302, 430, 190, "x402 FACILITATOR",
         ["Dev (mock) | Circle (Nanopayments)", "gateway-api.circle.com /v1/x402",
          "verify+settle · exact · x402Version 2 · fail-closed",
          "durable receipts_live.jsonl (in image) · /revenue rounded 6dp"], PURPLE, body_size=11)
    card("e_term", 1660, 1302, 510, 190, "ACR TERMINAL (Next.js · 'Arc Dawn')",
         ["editorial 'The Fixing' · /curve /companion /developers", "/spend /traction · operator statement · beancount + audit",
          "PublicDesk · FuturesDesk · FuturesTape · QuoteCorridor",
          "ChainFactsStrip · OracleProvenance · FinalityBadge", "SWR live polling · apps/terminal"], PURPLE)

    # ---------- K · AGENTIC ECONOMY (demand side) ----------
    zone(720, 1630, 1480, 410, "K · AGENTIC ECONOMY (demand side · the loop closes)", PURPLE)
    card("k_market", 745, 1682, 430, 168, "AGENT MARKETPLACE",
         ["/marketplace/catalog — Bazaar-shaped listings", "/marketplace/receipts — paid-query ledger",
          "the index listed as a payable service", "marketplace.py"], PURPLE)
    card("k_agent", 1195, 1682, 430, 168, "AUTONOMOUS x402 BUYER (live)",
         ["apps/agent — TS · viem · GatewayPayer / Circle Gateway", "--live real settlements · USDC spend cap",
          "discovers catalog → pays per query → receipts.ts", "interop.ts (12-check) · x402-buy.yml"], PURPLE, body_size=12)
    card("k_webhooks", 1660, 1682, 510, 168, "CIRCLE WEBHOOKS",
         ["/webhooks/circle — settlement events", "P-256 (ECDSA) signature verified",
          "→ SettlementTape · in-app floor buyer /demo/buyer/*", "webhooks.py · buyer_demo.py"], PURPLE)
    card("k_hedger", 745, 1870, 700, 150, "AUTONOMOUS HEDGER — reads the rate, then trades on it",
         ["Circle agent wallet (no exportable key) · scripts/hedger.py · GET /hedger",
          "1 pay x402 for ACR-INF print → 2 read own on-chain futures position",
          "3 gap = mandate TARGET − position → 4 trade(qty) the difference",
          "refuses on position-cap / book-room breach — never silently clamps"], PURPLE, body_size=12)

    # ---------- G · RED TEAM / LIVE DEMO ----------
    zone(60, 1150, 620, 300, "G · RED TEAM / LIVE DEMO", RED)
    card("g_demo", 80, 1202, 560, 150, "ATTACK THE INDEX  (5-step)",
         ["1  baseline: ACR printing hourly", "2  unleash wash-flow bot",
          "3  naive VWAP swings wildly", "4  ACR barely moves",
          "5  attack-cost counter burns USDC"], RED)
    card("g_opt", 80, 1362, 560, 78, "optimal_attack.py",
         ["runs the priced attack through real clean()", "→ the bound is attainable, not loose"], RED)

    # ---------- H · VERIFICATION ----------
    zone(2240, 1590, 560, 300, "H · VERIFICATION", GRAY)
    card("h_tests", 2260, 1642, 510, 180, "TESTS · 1759 py + 220 forge + 431 terminal + 35 agent",
         ["6 CI jobs: python · contracts · agent · terminal · subgraph · mcp", "anvil-gated on-chain integration · eval gate",
          "workflows: heartbeat · lifecycle · recover · keepalive · x402-buy",
          "glossary gate · ruff · make deck · hermetic conftest (Circle mocked)"], GRAY, body_size=11)

    # ---------- Positioning + live metrics (far right) ----------
    card("j_judge", 2860, 320, 620, 240, "WHY A BENCHMARK (positioning)",
         ["ICE administers LIBOR via IBA — on Arc roster", "Apollo · BNY · Mastercard: benchmark-native",
          "SOFR was methodology-first, liquidity-second", "the rate's administrator ≠ the rail's operator",
          "(the LIBOR neutrality lesson = the moat)"], GOLD)
    card("j_metrics", 2860, 590, 620, 470, "LIVE ON ARC · MEASURED",
         ["Terminal (Vercel) · Seller API (Render) · chain 5042",
          "ACROracle            0x4f00…2609",
          "AttestationRegistry  0x23ae…dFb7",
          "ACRFutures           0x29d9…42fe · self-rolling (series 3)",
          "FeedAccessAttestor   0xe671…FD47",
          "wallets by role: poster 0x8366… · owner+maker 0x9D44…",
          "taker 0xc972… · readers user-controlled · hedger agent wallet",
          "retired deploy EOA 0x3318…: holds no authority",
          "real x402 settled via Circle Gateway · durable receipts",
          "attack-cost-per-bp on EVERY print · 50–560× vs naive-VWAP",
          "100% Foundry invariants passing",
          "1759 py · 220 forge · 431 terminal · 35 agent — green"], INK, body_size=12)

    # ---------- PLAIN ENGLISH glossary panel ----------
    zone(2860, 1090, 620, 800, "PLAIN ENGLISH  ·  read the jargon", GOLD)
    glossary = [
        "latent price — the real price you can't see; ACR estimates it",
        "benchmark / rate — the official number contracts settle on (SOFR)",
        "VWAP — a plain volume-weighted average; fake volume fools it",
        "hedonic — strip quality gaps so you compare like-for-like",
        "constant-quality — price with quality removed; moves w/ the market",
        "observation model — 'what we see = true price, blurred + noisy'",
        "convolution / batching — settlements smear trades together in time",
        "deconvolution — un-smear it to recover the true price",
        "Kalman filter — recover a clean signal from noisy, delayed data",
        "RTS smoother — a backward pass that sharpens earlier estimates",
        "WLS — regression that weights bigger trades more heavily",
        "α-trim median (VWM) — drop the extreme few %, keep the middle",
        "bootstrap CI — resample to get an honest 'give-or-take' range",
        "sybil — many fake identities run by one attacker",
        "wash trade / self-deal — fake trades to pump volume or price",
        "Louvain — finds tight clusters in a who-paid-whom graph",
        "manipulation bound — least USDC (N*) to move the rate 1 bp",
        "basis point (bp) — one hundredth of a percent (0.01%)",
        "oracle — on-chain contract that publishes the rate to read",
        "postPrint / relayer — publish the rate; anyone may submit it",
        "role signer — each job (maker/taker/poster) signs from its own Circle wallet",
        "EIP-712 verify — contract checks WHO signed, not who sent it",
        "MAX_TS_SKEW / isStale — reject far-future or too-old prints",
        "nonce + deadline — one-time counter + expiry ⇒ replay-proof",
        "2-step ownership — new admin must accept ⇒ no wrong-address handoff",
        "Malachite finality — Arc confirms in <1s and never reverses (no reorgs)",
        "EIP-3009 — pay by signing a message (no separate approval tx)",
        "x402 / Nanopayments — pay-per-call in sub-cent USDC (HTTP 402)",
        "facilitator /verify /settle — check the payment, then move the USDC",
        "fail-closed — if payment can't verify, deny (the safe default)",
        "Arc / USDC-gas — Circle's chain; fees are paid in USDC itself",
        "TapeSource — one data-in interface: SimSource | ArcSource",
        "/onchain reader — API serves the rate read straight from chain",
        "settlement-grade — trustworthy enough for contracts to settle on",
        "cash-settled future — bet on the rate; pays the diff, no delivery",
        "Avellaneda–Stoikov — a recipe for a market maker's buy/sell quotes",
        "ACRFutures — an on-chain weekly future, cash-settled vs the oracle",
        "initial margin — post ~20% collateral to hold a futures position",
        "maker mirror-side — one maker takes the opposite of every fill (net 0)",
        "socialized-loss — if a loser can't pay, winners are haircut pro-rata",
        "Public Desk — trade the future with a Circle user-controlled wallet",
        "user-controlled wallet — the user's own PIN/passkey keys; server has none",
        "Agent Marketplace — catalog of payable services + receipts ledger",
        "meta-attestation (attestWithSig) — seller signs, a relayer pays gas",
        "Circle wallet / SCP — custodial signer + on-chain deploy platform",
        "webhook — Circle pings the API when a settlement lands",
        "SWR — the Terminal auto-refreshes its data every few seconds",
        "autonomous hedger — an agent that buys the rate, then trades futures on it",
        "agent wallet — a Circle wallet an agent signs with; no exportable key",
        "self-rolling venue — the futures book opens its own next series (it owns itself)",
        "venue keeper — the loop that trades + rolls the book, reading its shape on-chain",
        "FeedAccessAttestor — on-chain proof a wallet paid for the feed (hasFeedAccess)",
        "durable receipts — the settlement ledger ships in the image; survives restarts",
    ]
    text(2884, 1132, "\n".join(glossary), 11, INK, w=588)
    text(2884, 1862, "full glossary (every term) → docs/GLOSSARY.md", 10.5, GRAY, w=560)

    # ---------- F · WHY ARC ----------
    zone(60, 2060, 3420, 250,
         "F · WHY ARC — load-bearing for the MATH, not the deployment    ·    chain 5042 · USDC = native gas token", TEAL)
    why = [
        ("ARC L1 (Malachite)", ["deterministic sub-second finality · no reorgs",
                                 "chain 5042 · rpc.mainnet.arc.io"]),
        ("CIRCLE GATEWAY", ["single canonical rail", "= complete observation, no selection bias"]),
        ("NANOPAYMENTS (x402)", ["sub-cent index monetization", "agents are the paying customers"]),
        ("USDC NUMERAIRE", ["native system contract 0x3600… = gas token",
                            "no deflator · no Paymaster (gasless intrinsic)"]),
        ("DETERMINISTIC USDC FEES", ["wash volume has a known, fixed cost", "→ the manipulation bound is a NUMBER"]),
        ("AGENT MARKETPLACE", ["ACR listed as a payable service", "agents discover + pay natively (Zone K)"]),
    ]
    for i, (t, lines) in enumerate(why):
        card(f"f{i}", 80 + i * 566, 2135, 520, 150, t, lines, TEAL)

    # ---------- T · MACHINE TCA ----------
    # The transaction-cost layer, in one band, so a reader can
    # see the continuity work as a layer on the frozen product rather than hunt
    # for it. Left to right is the data's own order: settlement → mirror → index
    # → measure → decide → pay — and the loop re-enters Zone K at the buyer.
    zone(60, 2720, 3420, 330,
         "T · MACHINE TCA · "
         "a person, not a wallet, is the unit    ·    Arc + The Graph + World", BLUE)
    card("t_mirrors", 80, 2795, 540, 215, "ReceiptMirror.sol · HumanIdMirror.sol · ACROracleV2.sol",
         ["0xA9CD…DB65  every x402 settlement, mirrored on Arc",
          "0x7f41…d8e5  which wallets are ONE person, per 7-day window",
          "  cluster = keccak(nullifier, salt, window) — never a World ID",
          "0xFCa0…FFEA  v2 print: policyHash + humanAdjustedBound",
          "monotone per-payer guard · salt committed on chain"], ORANGE, body_size=12)
    card("t_graph", 650, 2795, 540, 215, "acr-tape SUBGRAPH (The Graph · Studio ethonline v0.2.0)",
         ["7 data sources on arc (mainnet) · ~3 s behind head",
          "Settlement.slippageBp benchmarked IN THE MAPPING",
          "  vs the arrival print, at the settling block",
          "Settlement.human · SellerDay / PayerDay · SellerWindow",
          "graph/ · 58 matchstick tests · no Substreams on Arc (stated)"], TEAL, body_size=12)
    card("t_tca", 1220, 2795, 540, 215, "MACHINE TCA · RATINGS · REROUTE",
         ["/tca/{payer}  vw slippage · overpaid · by_seller · human_share",
          "/rating/{seller}  fairness · human depth · weight_covered_pct",
          "/tca/human  one card across EVERY wallet a person owns",
          "reroute: worst → best seller, saving in bp on past fills",
          "make recompute  re-derive the index from the indexed tape"], BLUE, body_size=12)
    card("t_gate", 1790, 2795, 540, 215, "AGENT GATE + MODEL ARMOR",
         ["AGENT-CARD: EIP-712, no verifyingContract, audience + 15-min TTL",
          "three tiers: anonymous (host) · carded (key) · human (cluster)",
          "human claim confirmed on HumanIdMirror or 401, never a downgrade",
          "Google Cloud Model Armor on carded /graph/query, both directions",
          "the tier rides on the RECEIPT and the settlement ticker"], PURPLE, body_size=12)
    card("t_world", 2360, 2795, 540, 215, "WORLD · HUMANS, NOT WALLETS",
         ["HUMAN-PROOF: CAIP-122 signature → AgentBook → cluster",
          "make prove-human  runnable by anyone, nothing secret",
          "humanAdjustedBound: sybils are free, people are not",
          "Sandbox identities flagged onto chain + every count",
          "rotation chore on /ops · resolutions expire weekly"], GOLD, body_size=12)
    card("t_reroute", 2930, 2795, 530, 215, "THE LOOP CLOSES · apps/agent --reroute · MCP",
         ["buyer reads ITS OWN /tca before paying",
          "drops the worst seller, pays the suggested one first",
          "Circle Gateway nanopayment → ReceiptMirror → subgraph → /tca",
          "mcp/: 11 tools (check_spend · spend_report · wallet_tca('me'))",
          "skills/acr-analyst: Ask the Tape, in English"], PURPLE, body_size=12)

    # ---------- 7-week roadmap ----------
    zone(60, 3080, 3420, 340,
         "7-WEEK EXECUTION — cut lines: hedonic → class-buckets · future → paper-traded · NEVER cut W4 adoption or the paper", GRAY)
    weeks = [
        ("W1 — TAPE", ["indexer live on Arc mainnet", "empirical batching study", "OSS: microstructure paper"]),
        ("W2 — ESTIMATOR v1", ["state-space filter", "trimmed VWM + first prints", "METHODOLOGY PAPER out"]),
        ("W3 — ON-CHAIN", ["AttestationRegistry +", "ACROracle contracts", "full invariant suite"]),
        ("W4 — ADOPTION ★", ["sellers attest metadata", "x402 index API live", "oracle consumed by partners"]),
        ("W5 — RED TEAM", ["manipulation bound derived", "attack own index (wash bots)", "publish attack-cost/bp"]),
        ("W6 — INSTRUMENT", ["weekly cash-settled future", "A-S MM quoting live", "Mission Control trading"]),
        ("W7 — SHIP", ["freeze Monday", "paper polish + rehearse", "attack demo twice"]),
    ]
    for i, (t, lines) in enumerate(weeks):
        card(f"w{i}", 80 + i * 486, 3150, 450, 240, t, lines, GRAY if i != 3 else ORANGE)

    # ---------- WIRING (all bound · orthogonal routing) ----------
    # Zone A → indexer: fan up the A|B corridor (x≈690–706) into the indexer's left.
    wire("a_x402", "b_indexer", TEAL, side_a="R", side_b="L", lane=706)      # ①
    wire("a_gateway", "b_indexer", TEAL, side_a="R", side_b="L", lane=700)   # ②
    wire("a_adv", "b_indexer", RED, side_a="R", side_b="L", lane=694)        # contaminated
    wire("a_tape", "b_indexer", TEAL, dashed=True, side_a="R", side_b="L", lane=688)
    # Estimator internals (orthogonal L/Z within zone-B whitespace).
    wire("b_indexer", "b_obs", BLUE, side_a="R", side_b="L")                 # ③
    wire("b_indexer", "b_clean", BLUE, side_a="B", side_b="T")               # ④
    wire("b_obs", "b_robust", BLUE, side_a="B", side_b="T")                  # ⑤
    wire("b_clean", "b_robust", BLUE, side_a="R", side_b="L")                # ⑤
    wire("b_robust", "b_hedonic", BLUE, side_a="L", side_b="R", lane=1162)   # ⑥
    wire("a_attest", "b_hedonic", ORANGE, dashed=True, side_a="R", side_b="L", lane=708)
    wire("b_robust", "b_robdiag", BLUE, dashed=True, side_a="B", side_b="T")
    wire("b_hedonic", "b_prints", BLUE, side_a="R", side_b="L", lane=1162)
    wire("b_prints", "b_bound", RED, side_a="L", side_b="R", lane=1162)      # ⑦
    # Attestations → registry: routed over the top of zone B (y≈285), clear of all cards.
    wire("a_attest", "c_registry", ORANGE, dashed=True, side_a="R", side_b="L",
         via=[(700, 660), (700, 285), (2215, 285), (2215, 755)])
    wire("c_signer", "c_oracle", ORANGE, side_a="B", side_b="T")            # signer signs
    wire("b_prints", "c_oracle", ORANGE, side_a="R", side_b="L", lane=1905)  # ⑧ post
    # Oracle → futures venue: down the B|C lane (x≈2220), clear of the C stack.
    wire("c_oracle", "d_future", GREEN, side_a="L", side_b="L", lane=2220)   # ⑨
    wire("d_mm", "d_future", GREEN, side_a="T", side_b="B")                  # maker seeds book
    wire("d_desk", "d_future", GREEN, side_a="L", side_b="L", lane=2210)     # reader trades (PIN)
    # Long reads → API: down the B|C lane, across the B/E band (y≈1230), into e_api's top.
    wire("d_future", "e_api", GREEN, dashed=True, side_a="L", side_b="T",
         via=[(2230, 1304), (2230, 1228), (975, 1228)])   # desk/inventory → curve skew
    wire("c_oracle", "e_api", ORANGE, dashed=True, side_a="L", side_b="T",
         via=[(2225, 582), (2225, 1236), (960, 1236)])    # /onchain read
    wire("b_bound", "e_api", PURPLE, side_a="B", side_b="T")                 # ⑩
    wire("e_facil", "e_api", PURPLE, side_a="L", side_b="R")                 # x402 verify/settle
    wire("e_api", "e_term", PURPLE, side_a="T", side_b="T", lane=1272)       # over the E band (clears facil)
    # $-loop → Nanopayments: down the left margin (x≈700), across the F band (y≈2115).
    wire("e_api", "f2", PURPLE, dashed=True, side_a="L", side_b="T",
         via=[(700, 1397), (700, 2115), (1472, 2115)])
    # Arc L1 canonical-tape riser: up the f0|f1 gap (x≈623), then the A|B corridor (x≈700).
    wire("f0", "b_indexer", TEAL, dashed=True, side_a="R", side_b="L",
         via=[(623, 2210), (623, 2040), (700, 2040), (700, 412)])
    wire("a_adv", "g_demo", RED, dashed=True, side_a="R", side_b="R", lane=704)  # clears a_tape
    # Zone K — the demand side / the loop closes.
    wire("k_agent", "e_facil", PURPLE, side_a="T", side_b="B")              # buyer pays via x402
    wire("k_agent", "k_market", PURPLE, dashed=True, side_a="L", side_b="R")  # discovers catalog
    wire("e_api", "k_market", PURPLE, dashed=True, side_a="B", side_b="T")    # lists the index
    wire("e_facil", "k_webhooks", PURPLE, dashed=True, side_a="B", side_b="T", lane=1587)
    # Autonomous hedger: reads the print (x402), then trades the gap on-chain — the loop, closed.
    wire("e_api", "k_hedger", PURPLE, dashed=True, side_a="L", side_b="L",
         via=[(690, 1397), (690, 1945)])                                    # serves the ACR-INF print
    wire("k_hedger", "e_facil", PURPLE, side_a="R", side_b="R",
         via=[(1642, 1945), (1642, 1397)])                                  # pays x402 (the $ loop)
    wire("k_hedger", "d_future", GREEN, side_a="R", side_b="L", lane=2230)  # trades the gap on-chain
    # Poster Circle wallet also signs the on-chain feed-access attestation.
    wire("c_signer", "c_attestor", ORANGE, side_a="R", side_b="R", lane=2820)
    # Zone T, left to right: settlement → mirror → index → measure → decide → pay.
    wire("t_mirrors", "t_graph", TEAL, side_a="R", side_b="L")
    wire("t_graph", "t_tca", BLUE, side_a="R", side_b="L")
    wire("t_tca", "t_gate", PURPLE, side_a="R", side_b="L")
    wire("t_gate", "t_world", GOLD, side_a="R", side_b="L")
    wire("t_world", "t_reroute", PURPLE, side_a="R", side_b="L")


def validate() -> dict:
    arrows = [e for e in _elements if e["type"] == "arrow"]
    rects = [e for e in _elements if e["type"] == "rectangle"]
    texts = [e for e in _elements if e["type"] == "text"]
    ids = {e["id"] for e in _elements}
    for a in arrows:
        for side in ("startBinding", "endBinding"):
            eid = a[side]["elementId"]
            assert eid in ids, f"{a['id']} {side} → missing {eid}"
            refs = _by_id[eid].get("boundElements") or []
            assert any(r["id"] == a["id"] for r in refs), f"{a['id']} not back-referenced by {eid}"
    return {"rectangles": len(rects), "text": len(texts), "arrows": len(arrows), "total": len(_elements)}


def main() -> None:
    build()
    summary = validate()
    doc = {
        "type": "excalidraw", "version": 2, "source": "acr-architecture-generator",
        "elements": _elements,
        "appState": {"gridSize": None, "viewBackgroundColor": "#ffffff"},
        "files": {},
    }
    if "--check" in sys.argv:
        print("dry-run:", summary)
        return
    OUT.write_text(json.dumps(doc, indent=1, ensure_ascii=False))
    print(f"wrote {OUT.name}: {summary}")
    print(f"  arrows: {summary['arrows']} (all bound)")


if __name__ == "__main__":
    main()
