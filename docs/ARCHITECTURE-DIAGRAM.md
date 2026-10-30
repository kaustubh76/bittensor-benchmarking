# ACR — The Arc Compute Rate
### Architecture Blueprint · the companion document for `acr_architecture.excalidraw`

> Looking for the project itself — what ACR is, how to run it, what is live? That is the
> [root README](../README.md). This document explains the **architecture canvas**: every zone,
> every arrow, and the product reasoning the diagram assumes.

> **One-sentence core:** Payment exhaust on Arc is a noisy, batched, adversarial observation of a latent economic process — the true price of machine services — and ACR is the estimator that recovers it, published as a live on-chain reference rate that contracts can settle against.
>
> **The pitch line:** *"Machine commerce just got its SOFR — and it prints its own attack cost."*

**Live right now (Arc **mainnet**, chain 5042, since 2026-09-27):**
- **Terminal (dashboard):** https://arc-compute-rate.vercel.app
- **Seller API (x402-gated):** https://acr-probe-free.onrender.com on mainnet, https://acr-api-1fto.onrender.com on testnet. https://acr-api-mainnet.onrender.com is the same chain as the first but serves **44 of 50**: `/par` and all five `/operator/*` answer 404 until its image is rebuilt.
- **ACROracle:** [`0xAfe8…6D07`](https://explorer.arc.io/address/0xAfe8a941957EbD3C759fEA5365fe4B75f9626D07) · **ACROracleV2:** [`0xF77E…2f81`](https://explorer.arc.io/address/0xF77E763ef710096c0b9F02Ea16F76e68E0312f81) · **ACRFutures:** [`0xdb5D…6d3b`](https://explorer.arc.io/address/0xdb5D7dbB67aD8EC8517982E95c89906abFBC6d3b) · **AttestationRegistry:** [`0x13CF…5096`](https://explorer.arc.io/address/0x13CF727d1D25283d58Efe7647dCf30e9cE255096) · **ReceiptMirror:** [`0x1a53…5bE3`](https://explorer.arc.io/address/0x1a5390E59d4ce0a386Aa071365A932Dc34d35bE3) · **HumanIdMirror:** [`0x7134…40B5`](https://explorer.arc.io/address/0x7134622a01Add334eA306dDa5A5877b418a940B5) · **FeedAccessAttestor:** [`0xB950…A490`](https://explorer.arc.io/address/0xB9508caD4A5C367C3a62B6767C4837a607EaA490) — the full mainnet set is in [`README.md`](../README.md#live-right-now)
- **CI:** 4 jobs (python · contracts · agent · terminal) on every push — `.github/workflows/ci.yml`

---

## 0. What this file is

`acr_architecture.excalidraw` is the complete product blueprint for ACR. It is a single canvas (~3,480 × 2,700 units, 221 elements) containing:

- The full estimator pipeline (all four pillars) with the data flow numbered ①–⑩
- The on-chain contract layer and the cash-settled instrument layer
- The distribution/monetization loop (the index sold to machines, via machine payments)
- The live demo script, the judge-fit rationale, and the demo-day metrics slide
- The "Why Arc" rail — six *mathematical* (not deployment) dependencies on Arc
- The 7-week execution roadmap with cut-lines

**How to open:** go to [excalidraw.com](https://excalidraw.com) → `File → Open` → select the `.excalidraw` file. Everything is native Excalidraw shapes — fully editable, no plugins needed.

![The ACR architecture canvas, rendered](assets/acr_architecture.preview.svg)

*Rendered by `make diagram` from the same canvas, and held to it by `verify_claims.py`.*

**How to read it in 10 seconds:** start at the top-left (Market Exhaust), follow the numbered arrows left-to-right through the blue Estimator Core to the gold ACR Prints box, then right into the orange on-chain layer and green instrument layer. The bottom two rails are context (Why Arc + roadmap). The right column is what judges score.

---

## 1. Color code (the legend, explained)

| Color | Zone | Meaning |
|---|---|---|
| **Teal** `#0c8599` | Market exhaust / Circle-Arc primitives | Raw inputs and platform rails. Things we consume, not things we build. |
| **Blue** `#1971c2` | Estimator Core | **The product.** All estimation logic. If a judge asks "what did you build," everything blue is the answer. |
| **Gold** `#f08c00` | ACR Prints + Judge Fit | The two most important boxes on the canvas. Gold = "photograph this." |
| **Orange** `#e8590c` | On-chain contracts | Solidity/Foundry surface: `AttestationRegistry.sol`, `ACROracle.sol`, invariants. |
| **Green** `#2f9e44` | Instrument layer | The cash-settled ACR-Weekly future + the market maker. Pillar 4. |
| **Purple** `#9c36b5` | Distribution | x402-gated index API + terminal. The self-referential business model. |
| **Red** `#e03131` | Adversarial flow / red team | Attack traffic, the manipulation bound, and the live demo script. Red is both the threat model and the show. |
| **Gray dashed** | Zone containers | Grouping only — no semantic weight. |

**Line styles:** solid arrows = primary data/settlement flow. Dashed arrows = supporting/derived flows (attestation metadata, monetization loop, red-team reuse).

---

## 2. Zone-by-zone walkthrough

### Zone A — MARKET EXHAUST (top-left, gray container)
The four input streams, top to bottom:

1. **x402 Authorizations** (teal) — EIP-3009 signed payment payloads: price, size, buyer, seller, timestamp. The highest-frequency observable.
2. **Gateway Batch Settlements** (teal) — net positions settled *later* than economic time. The box states the diagram's most important sentence: the observed tape equals the **latent flow convolved with the batching schedule**. This is why naive VWAP measures the batch scheduler, not the market.
3. **Seller Attestations** (orange) — EIP-712 signed service metadata (model class, latency SLO, schema) flowing into `AttestationRegistry.sol`. Wired to Pillar 2: `PrintStore._merge_onchain_attestations` unions the registry into the estimator's feature set on every load, on-chain winning ties. On **this** deployment it feeds Pillar 2 nothing — the published tape is the simulator (`ACR_TAPE_SOURCE=sim`), whose seller ids share no address with the 4 records on-chain, so the union adds 4 rows the regression never matches to an event. `make seed-sellers` with `ACR_TAPE_SOURCE=arc` is the one command that closes it, and the page says so.
4. **Adversarial Flow** (red) — wash trades, spoof volume, sybil seller clusters. Deliberately drawn as a *first-class input*: the estimator is designed against contaminated data, not clean data.

### Zone B — ESTIMATOR CORE (center, blue container — the largest zone by design)
Title bar states the discipline: **"one estimand: the latent price of machine services."** Six stages + one derivation:

| Box | Pillar | Contents |
|---|---|---|
| **Tape Indexer** | — | Raw event ingestion, volume-time clock, clean tick timestamps (guaranteed by Malachite deterministic finality). |
| **Observation Model** | **Pillar 1** | The state-space formulation: observed tape = latent process ∘ batching operator + noise. Kalman-class filtering with irregular observations. The stage no other team will know exists. |
| **Cleaning Stack** | — | Wash-flow exclusion via funding-graph analysis, per-cluster volume caps, sybil detection (Louvain), self-dealing filters. |
| **Robust Estimator** | — | Volume-time trimmed weighted median (α-trim), documented breakdown point, confidence interval per print. |
| **Hedonic Adjustment** | **Pillar 2** | Notional-weighted regression of log-price on seller quality features (model class, latency) strips those effects → a **constant-quality** rate. Case-Shiller methodology, applied to compute. Features come from the registry *or* the tape's own attestations, whichever covers the seller (`PrintStore._merge_onchain_attestations`); on the published simulator tape they come from the tape, because the 4 on-chain records match no simulated seller. |
| **ACR PRINTS** (gold) | output | Hourly prints: **ACR-INF** ($/1k tokens), **ACR-GPU** ($/GPU-sec), **ACR-DATA** ($/MB) — each shipped with its CI **and its attack-cost-per-bp**. |
| **Manipulation Cost Bound** (red dashed) | **Pillar 3** | Lower bound on USDC required to move the print by 1bp = f(trim α, cluster caps, **deterministic USDC fees**). Only computable on Arc; on volatile-gas chains the bound is a random variable. |

Bottom of the zone carries two annotations: **"methodology paper = the product (Week 2, open source)"** and the full flow legend (see §3).

### Zone C — ON-CHAIN (right of core, orange container)
- **`AttestationRegistry.sol`** — seller EIP-712 metadata. The box names the flywheel: attest → better index placement → more buyer flow → sellers voluntarily feed the feature matrix. The moat is econometric, not marketing.
- **`ACROracle.sol`** — signed hourly prints posted on-chain: print + bound + CI together. This is the settlement-grade reference other contracts read.
- **Invariant notes** (dashed) — monotone timestamps, bound sanity, print-within-CI; full Foundry suite.

### Zone D — INSTRUMENT (bottom-right, green container — Pillar 4)
- **ACR-Weekly Future** — cash-settled against the oracle print. **No delivery, no bonds, no seller cooperation** — annotated as "capacity forwards, hardest organ amputated," recording that this instrument is the surviving form of the earlier Capacity Forwards idea with its weakest mechanism deleted.
- **Market Maker** — Avellaneda-Stoikov quoting on the future; Mission Control trades it live in the demo. This creates the first term structure in machine commerce.

### Zone E — DISTRIBUTION (mid-left, purple container)
- **x402-Gated Index API** — agents pay ~$0.0001/query via Nanopayments. The self-referential loop, stated in the box: *the index about machine commerce is bought by machines.* Every query is both revenue and a Nanopayments dogfood metric.
- **ACR Terminal** — live prints, curve, vol, seller reliability scores. Human-facing view.

### Zone H — THE JUDGE COLUMN (far right, three stacked panels)
1. **Live Demo — Attack the Index** (red): the five-step script. Baseline prints → unleash a 50k-tx wash-flow bot → naive VWAP swings wildly → ACR barely moves → a live counter burns the attacker's USDC. Closing line on the box: *"try to move my number — here's the bill."* The side-by-side chart is the money shot.
2. **Judge Fit** (gold): ICE administers LIBOR through ICE Benchmark Administration and is on Arc's testnet roster — benchmark construction pitched to the benchmark administrator. Apollo, BNY, Mastercard are rate-native institutions. SOFR itself was methodology-first, liquidity-second — the exact defense for building on thin testnet data.
3. **Demo-Day Metrics** (black frame): the final-slide numbers, and they are measured rather than aspirational — run `make verify-live` for the live set. At time of writing: hourly on-chain prints for 3 indices with attack-cost-per-bp on every one; **4** seller attestations on-chain; **three** live futures books (ACR-INF, ACR-GPU, ACR-DATA) whose maker is a Circle custody wallet, traded hourly by a keeper; **34** real Gateway x402 settlements from **2 distinct payers** (**27** from the CLI buyer agent, **7** from the autonomous hedger's backing EOA); 100% Foundry invariants passing. (The earlier version of this line carried literal `X`/`Y` placeholders and a "5+ sellers" target that was never met — a slide nobody had re-read.)

### Zone G — WHY ARC rail (bottom, teal)
Six boxes, each a **mathematical** dependency — the container title says it outright: *load-bearing for the MATH, not the deployment*:

| Primitive | Why the math needs it |
|---|---|
| Arc L1 (Malachite) | Deterministic sub-second finality → clean tick timestamps, no reorg ambiguity in the tape itself. |
| Circle Gateway | Single canonical rail → **complete observation** of the market; no unknowable selection bias. |
| Nanopayments (x402) | Sub-cent monetization → agents themselves are the paying customers of the index. |
| USDC numeraire | Prices are born in dollars → no gas-token deflator nested inside the index. |
| Deterministic USDC fees | Wash volume has a *known, fixed* cost → the manipulation bound is a **number**, not a distribution. |
| Agent Marketplace | ACR listed as a service; agents discover and pay for the rate natively. |

### The Circle Agent Stack in ACR (judge's map)

All five pillars of Circle's Agent Stack are implemented, each with a
credential-free offline mode and a live Arc-testnet path:

| Circle pillar | Where in ACR |
|---|---|
| **Agent Nanopayments** (Gateway x402) | Seller gate: `services/index_api/index_api/x402.py` → `POST /v1/x402/verify` + `/settle` on `gateway-api.circle.com`; scheme `exact`/GatewayWalletBatched on `eip155:5042`. |
| **Agent Wallets** | Buyer: `apps/agent/` pays via the official `@circle-fin/x402-batching` `GatewayClient` (wallet created/funded through the Circle CLI). Seller/oracle: Circle Developer-Controlled Wallets sign prints + attestations (`packages/acr_oracle_client/signer.py`) and deploy contracts (`scripts/deploy_circle.py`). |
| **Agent Marketplace** | `GET /marketplace/catalog` — Bazaar-shaped machine-readable listings with prices, input/output schemas, and on-chain attestation provenance (`AttestationRegistry` as the ERC-8004-style reputation anchor); `GET /marketplace/receipts` — the public settlement tape; rendered by the Terminal's shop floor, under `/curve`. |
| **Circle CLI** | `make circle-login / circle-wallet / circle-fund / circle-deposit / circle-balance` + `circle services search/inspect/pay` cross-checks — `docs/agent-runbook.md`. |
| **Circle Skills** | Consumed as the `circle-skills` Claude Code plugin (`make skills-install`): `use-agent-wallet`, `fund-agent-wallet`, `pay-via-agent-wallet`, `use-circle-cli` — and **published back**: `skills/acr-hedge/SKILL.md` teaches any agent the full loop (discover → pay for the print → read the venue → hedge), commands lifted from the running hedger. |

Demo economics: $0.0001/query (well under a $0.01/action ceiling);
`make agent-live` makes 60 discovered, receipt-verified x402 payments under a
$0.01 spend cap. Gateway settles payments in **batches**, so the honest
transaction story is *60 x402 payments + Gateway batch settlements + the
oracle's own EIP-712 `postPrint` transactions* — not "60 L1 transactions".

### Zone I — 7-WEEK ROADMAP (bottom strip)
Container title carries the cut-lines: *hedonic → class-buckets · future → paper-traded · **NEVER cut W4 adoption or the methodology paper**.*

| Week | Focus | Key deliverable |
|---|---|---|
| **W1 — TAPE** | Indexer live on Arc testnet; empirical batching study | OSS release: *"The Microstructure of Machine Payments on Arc"* |
| **W2 — ESTIMATOR v1** | State-space filter + trimmed VWM; first live prints | **Methodology paper published** |
| **W3 — ON-CHAIN** | Registry + Oracle contracts | Full invariant suite |
| **W4 — ADOPTION ★** | Sellers attest metadata; x402 index API live | Oracle consumed by partners (highlighted orange — never cut) |
| **W5 — RED TEAM** | Manipulation bound derived; attack own index with wash bots | Published attack-cost-per-bp |
| **W6 — INSTRUMENT** | Weekly cash-settled future; A-S MM quoting | Mission Control trading live |
| **W7 — SHIP** | Freeze Monday; paper polish | Attack demo rehearsed twice |

---

## 3. The numbered flow ①–⑩

The arrows carry the sequence; this is also the **live-demo narration order** — the diagram doubles as the demo script.

1. **Exhaust** — x402 authorizations stream into the indexer.
2. **Batched tape** — Gateway settlements arrive (the convolution; label calls it out).
3. **Deconvolve** — indexer → observation model; state-space filter recovers the latent process.
4. **Clean** — indexer → cleaning stack; wash/sybil/self-dealing flow excised via funding-graph analysis.
5. **Robust estimate** — observation model + cleaned flow → trimmed volume-weighted median.
6. **Hedonic** — quality features strip heterogeneity → constant-quality rate.
7. **Print + bound** — the gold box emits hourly prints, each with CI and attack-cost-per-bp.
8. **Oracle** — signed print posted to `ACROracle.sol`, settlement-grade.
9. **Settle** — the ACR-Weekly future cash-settles against the oracle print.
10. **Agents pay for the rate** — prints sold back to machines via the x402-gated API. The loop closes.

---

## 4. Arrow inventory (routing notes for editors)

| Arrow | Style | Meaning |
|---|---|---|
| Exhaust → Indexer (×2) | solid teal | Flows ① and ② |
| Adversarial → Indexer | solid red | "Contaminated flow" — the threat enters with the data |
| Attestations → Hedonic | dashed orange | Quality features (conceptually via the registry) |
| Attestations → Registry | dashed orange, multi-point | Routed *under* Zone A and across the corridor at x≈1930 to avoid crossing the Estimator Core |
| Indexer → Obs. Model → Estimator → Hedonic → Prints | solid blue | The pipeline spine (③–⑥) |
| Prints → Bound | solid red | ⑦ per-print bound |
| Prints → Oracle | solid orange | ⑧ signed print (+ bound + CI); routed through the x1900–1980 corridor |
| Oracle → Future | solid green | ⑨ cash-settlement reference |
| MM → Future | solid green | Quotes |
| Bound/Prints → Index API | solid purple | ⑩ prints sold via x402 |
| API → Nanopayments rail | dashed purple, multi-point | Monetization loop, routed through the x≈745 corridor |
| Arc L1 → Indexer | dashed teal, multi-point | "Canonical tape" — rises through the vertical corridor at x≈748 |
| Adversarial → Demo Theater | dashed red, full-canvas diagonal | **Intentional.** The longest arrow on the canvas: the same bots that threaten the index in production power the show on demo day. That arrow *is* the pitch structure. |

**Editing tips:** the canvas keeps two clean corridors for long arrows — vertical at x≈740–760 (between the left zones and the Estimator Core) and x≈1900–1980 (between the core and the on-chain zone). Route new long arrows through these. Zone containers are dashed and purely decorative — safe to resize. If you add boxes, run a quick eyeball on those corridors before export.

---

## 5. Product context the diagram assumes (for anyone opening this cold)

- **Event:** Arc/Circle 7-week hackathon, Agentic Economy track; top teams enter an 8-week accelerator. Judged on product-grade execution, real adoption, and a core that survives hostile Q&A — not feature count.
- **Why a benchmark:** every economy gets a spot market first and a reference rate second; nothing above spot (hedging, credit, term markets) can exist without the benchmark. Circle built the spot rails; ACR is the rate layer.
- **Why cash settlement matters:** it deletes the delivery-enforcement problem (bonds, slashing, redemption priority) that kills physically-settled designs — the same reason real commodity markets migrated to cash settlement.
- **Why Circle can't own it:** benchmark credibility requires neutrality from the settlement-rail operator — the LIBOR lesson. The administrator of the rate cannot be the operator of the rail. That's both the moat and the accelerator pitch.
- **Thin-testnet-data defense:** methodology is the product; SOFR was designed before it had a derivatives market. The estimator runs live on real testnet flow *and* on a calibrated simulation with published parameters.

### Kill-questions the canvas pre-answers
| Question | Where the answer lives on the canvas |
|---|---|
| "Isn't this a dashboard?" | Blue core (estimand + filter), orange oracle (settlement-grade), green future (tradable instrument). Dashboards don't have estimands. |
| "Testnet data is thin/fake." | W1–W2 roadmap boxes + Judge Fit panel (SOFR: methodology-first). |
| "What does it cost to manipulate you?" | The red Manipulation Bound box — the number prints next to the rate. |
| "Why Arc and not any chain?" | The entire bottom teal rail — six mathematical dependencies. |
| "Chicken-and-egg on adoption?" | Purple distribution loop (agents pay for the rate) + W4 adoption week + the MM box (we seed the market ourselves). |

---

## 6. File manifest

| File | Purpose |
|---|---|
| `acr_architecture.excalidraw` | The **comprehensive, implementation-accurate** canvas: 221 elements (67 rectangles, 119 text, 35 fully-bound arrows) covering the four-pillar estimator in detail, the role-based custody signer, EIP-712 verification, the x402 facilitator (concrete Circle wiring), robustness diagnostics, TapeSource, on-chain reads, **the on-chain `ACRFutures` venue + Public Desk (Circle user-controlled wallets)**, **the self-owning, self-rolling venue keeper**, **the autonomous hedger (an agent that reads the rate, then trades on it)**, **the `FeedAccessAttestor` (on-chain paid-feed access)**, **the agentic-economy demand side (live x402 buyer · Agent Marketplace · Circle webhooks · durable receipts)**, Circle SCP deploy, the verification surface, **and a "PLAIN ENGLISH" glossary panel**. Open at excalidraw.com. |
| `docs/GLOSSARY.md` | Plain-English definitions (with everyday analogies) of every technical term on the diagram, plus a jargon-free ①→⑩ walkthrough. Rendered live at [arc-compute-rate.vercel.app/companion](https://arc-compute-rate.vercel.app/companion) — and the Terminal masthead's one-click **plain** edition re-sets the whole site in this register. |
| `hackathon/canvases/acr_flows.excalidraw` | **"How each piece works, step by step"** — 180 elements, six end-to-end flows (hourly oracle press · x402 sale · venue keeper · autonomous hedger · attack demo · verification), one box per step with the real component name in every box. The companion to the architecture canvas: that one shows *what exists*, this one shows *what happens*. |
| `acr_architecture_v1_blueprint.excalidraw` | The original 143-element blueprint (kept for reference). |
| `docs/ARCHITECTURE-DIAGRAM.md` | This document. |
| `README.md` | The project README — what ACR is, quickstart, what is live. |

**Where the built system lives** (the canvas, implemented):

| Path | What it is |
|---|---|
| `packages/` | The estimator core: `acr_core` · `acr_estimator` · `acr_tape` · `acr_sim` · `acr_instrument` · `acr_oracle_client` |
| `contracts/` | `ACROracle.sol` + `AttestationRegistry.sol` + `ACRFutures.sol` + `FeedAccessAttestor.sol` (Foundry, 60 tests incl. invariants) — all deployed on Arc testnet |
| `services/index_api/` | The x402-gated seller API (FastAPI) — deployed three times: `acr-api-1fto` (testnet) and `acr-probe-free` (mainnet) serve all 50 routes, `acr-api-mainnet` serves 44 |
| `apps/terminal/` | The ACR Terminal (Next.js) — deployed at arc-compute-rate.vercel.app |
| `apps/agent/` | The machine buyer (Circle Gateway `x402-batching` client) |
| `.github/workflows/` | CI (4 jobs) + the keep-alive ping for the free-tier press |
| `docs/WALLETS.md` | Which Circle wallet product does which job — and the constraint that forces each choice |

**Suggested exports:** select the Estimator Core + On-chain + Instrument zones only → export PNG for the pitch deck's architecture slide. The Demo Theater box exports standalone as the demo-script slide. The Why-Arc rail exports as the "only on Arc" slide.

**Version note:** generated programmatically by `scripts/gen_architecture.py` (deterministic — re-running yields byte-identical output; every arrow is bound to its two boxes). Edit the declarative zone/card/wire spec there rather than hand-tweaking coordinates. The earlier thin blueprint is preserved as `acr_architecture_v1_blueprint.excalidraw`. To eyeball the layout without opening Excalidraw, `scripts/preview_excalidraw.py` renders the canvas to SVG + PNG using only the Python standard library.

---

*ACR — one estimand, four pillars, ten arrows, seven weeks. The rate machine commerce settles on.*