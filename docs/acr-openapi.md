---
marp: true
paginate: true
---

# ACR — The Arc Compute Rate
## OpenAPI 3.1.0 · endpoint reference

## Where to send it

| host | chain | serves |
|---|---|---|
| `https://acr-api-1fto.onrender.com` | Arc testnet (5042002) | **start here** — all 50 routes, and a faucet, so nothing costs real money |
| `https://acr-probe-free.onrender.com` | Arc mainnet (5042) | all 50 routes, reading real mainnet state. Holds no signing key |
| `https://acr-api-mainnet.onrender.com` | Arc mainnet (5042) | 44 of 50: `/par` and `/operator/*` answer 404 until its image is rebuilt |

Live spec: `https://acr-api-1fto.onrender.com/openapi.json`
Catalog: `https://acr-api-1fto.onrender.com/marketplace/catalog`

### One call, runnable as written

`/par` prices a real invoice against published third-party list prices.
It needs no key, no card and no payment:

```bash
curl -s 'https://acr-api-1fto.onrender.com/par?unit=%24%2F1k%20tokens&billed_usdc=0.02&quantity=10'
```

All three query params are required (it answers 422 without them), and the
unit is one of `$/1k tokens`, `$/GPU-sec`, `$/MB`, percent-encoded.

Read `basket.status` before the verdict: **`ok`** and **`aging`** both price
against the market, the SCREAMING values (`STALE`, `ABSENT`, `NO_ROWS`) mean
it fell back to our own sellers, which `benchmarked_against` names. The basket
carries `age_s` beside `max_age_s`, so its age is readable without guessing
the window.

Index endpoints: **$0.0001 USDC per request**, x402 `exact` scheme,
Circle Gateway (GatewayWalletBatched), on the chain the host you called is
on: `eip155:5042` for Arc mainnet, `eip155:5042002` for testnet. `GET
/x402/info` on that host states its own, and is the field to trust.
Fleet sellers (`/compute/{label}`) price per unit; the catalog carries each one's terms.

Agent-to-agent calls may present an `AGENT-CARD` (see `GET /agent/challenge`);
carded tape reads pass Google Cloud Model Armor in both directions (`GET /armor/info`).

---

## Paid endpoints (x402-gated) · 6

**`GET /compute/{label}`** — Compute
**`GET /curve/{index_id}`** — Curve
**`GET /prints`** — Prints
**`GET /prints/{index_id}`** — Print One
**`GET /seller-scores/{index_id}`** — Seller Scores
**`GET /vol/{index_id}`** — Vol

---

## Free endpoints (no payment required) · 42

`GET /` — Root
`GET /agent/challenge` — Agent Challenge
`GET /agent/info` — Agent Info
`GET /agent/whoami` — Agent Whoami
`GET /armor/info` — Armor Info
`POST /demo/attack/start` — Demo Attack Start
`GET /demo/attack/status` — Demo Attack Status
`POST /demo/buyer/start` — Demo Buyer Start
`GET /demo/buyer/status` — Demo Buyer Status
`POST /desk/challenge` — Desk Challenge
`POST /desk/faucet` — Desk Faucet
`POST /desk/limits` — Desk Limits
`POST /desk/pass/claim` — Desk Pass Claim
`POST /desk/pass/status` — Desk Pass Status
`POST /desk/session` — Desk Session
`POST /desk/wallet` — Desk Wallet
`POST /desk/withdrawable` — Desk Withdrawable
`GET /fleet` — Fleet Listings
`GET /futures` — Futures Roster
`GET /futures/{index_id}` — Futures Desk
`GET /graph/operations` — Graph Operations
`POST /graph/query` — Graph Proxy Query
`GET /health` — Health
`GET /hedger` — Hedger State
`GET /humanid/info` — Humanid Info
`GET /marketplace/catalog` — Marketplace Catalog
`GET /marketplace/receipts` — Marketplace Receipts
`GET /onchain/{index_id}` — Onchain Print
`GET /operator/audit/{business}` — Operator Audit
`GET /operator/businesses` — Operator Businesses
`GET /operator/ledger/{business}` — Operator Ledger
`GET /operator/statement/{business}` — Operator Statement
`GET /operator/traction` — Operator Traction
`GET /par` — Price Check
`GET /rating/{seller}` — Rating
`GET /revenue` — Revenue
`GET /tca/human` — Tca Human
`GET /tca/{payer}` — Tca
`GET /terminal/data` — Terminal Data
`POST /webhooks/circle` — Circle Webhook
`GET /webhooks/recent` — Webhooks Recent
`GET /x402/info` — X402 Info

---

*Rendered by `scripts/gen_openapi_doc.py` from `app.openapi()` — 48 routes;*
*5 operator routes omitted on purpose. `make openapi-doc-check` fails when this is stale.*
