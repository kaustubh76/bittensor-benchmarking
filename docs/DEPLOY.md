# ACR — Public Cloud Deploy (Render + Vercel)

Make the ACR **seller API + webhooks** and the **Terminal dashboard** publicly
reachable, running live Circle Arc-testnet execution.

```
                 ┌────────────────────────┐        ┌──────────────────────┐
  buyers/agents →│  Render: acr-api       │←──────→│  Circle Gateway       │
  Circle webhooks│  FastAPI (Dockerfile)  │ verify │  (facilitator + DCW)  │
                 │  /x402 /webhooks /...  │ settle └──────────────────────┘
                 └───────────┬────────────┘                 ↑ reads
                             │ /terminal/data etc.           │ Arc testnet RPC
                 ┌───────────┴────────────┐        ┌─────────┴────────────┐
  browsers  →    │  Vercel: terminal      │        │  ACROracle / Registry │
                 │  Next.js (server proxy)│        │  (chain 5042)         │
                 └────────────────────────┘        └──────────────────────┘
```

## What is actually deployed

| Piece | Host | URL |
|---|---|---|
| Seller API (mainnet, 5042) | **Render** `acr-api-mainnet` (`srv-das1navlk1mc73dvsm8g`), from `render.yaml` | https://acr-api-mainnet.onrender.com — **LIVE** since 2026-09-27 (`gate: circle`, `signer: local`). `plan: starter` in `render.yaml`, NOT free, and the comment there says why: a sleeping press stranded testnet collateral twice. **Its image predates the operator** — 44 routes, no `/operator/*` and no `/par` as of 2026-10-07 (`make verify-drift`) |
| Seller API (testnet, 5042002) | **Render** `acr-api`, from `render.yaml` | https://acr-api-1fto.onrender.com — suspended 2026-09-15, **resumed 2026-10-01** and answering today (`chain_id: 5042002`). `plan: free`, so it sleeps: a cold start measured 25 s. This is the host that serves the operator |
| Seller API (probe, 5042) | **Render** `acr-probe-free` (`srv-db28nc97lnhs73e1tgfg`) | https://acr-probe-free.onrender.com — a mainnet-CHAIN press on the free plan. It had **never deployed successfully** until 2026-10-09: both attempts on 10-06 died on one guard violation, `ACR_HUMANID_APP_ID is unset`. **Now the most complete mainnet press there is**: 50 routes, and since 2026-10-11 an `ACR_SUBGRAPH_URL` + `ACR_GRAPH_API_KEY` on the **network gateway** (`via='gateway'`, `daily_cap=None`). Measured after that change: `/tca` answers (3 purchases, $0.00461), `/par` prices a bill (2500 bp over a 5-row market basket), `/operator/*` answer, `get_rate` reads a print 45 min old, and `npm run smoke` is **11 of 11 anonymously** — against **5 of 11 failing** on `acr-api-mainnet`. Two caveats, neither the subgraph: carded `query_tape` fails CLOSED because `/armor/info` reports `credentials_present: false` on this host, and `signer: none` so it posts nothing. **IT DOES NOT SLEEP, measured 2026-10-11.** Free-plan services spin down after ~15 idle minutes, but this one sets `ACR_SELF_URL`, so its own 60-second loop knocks on its public URL and Render counts that as traffic: after **17.5 minutes with zero external requests** `process_started_at` was unchanged to the second and `/health` answered in **1.34s**. A restart would have moved that field, which is why that is the thing to read rather than a latency |
| Terminal | **Vercel** | https://arc-compute-rate.vercel.app |

**THREE RENDER ACCOUNTS, AND NO KEY SEES THEM ALL.** This is the thing that
costs an hour if you meet it unprepared. `acr-api` belongs to one team,
`acr-probe-free` to another, and **`acr-api-mainnet` to a third that neither
API key in `.env` can see** — a `?name=acr-api-mainnet` query returns `[]` for
both. So a mainnet image swap is a dashboard action by its owner, not something
a script here can do. Measured 2026-10-09.

**THE TERMINAL STILL POINTS AT THE WEAKER OF THE TWO MAINNET PRESSES**, and one
environment variable is the whole of it. Measured on the live deployment
2026-10-11, `/api/health` reports `configured`, `published` and `active` all as
`https://acr-api-mainnet.onrender.com`, `fellBack: false`. That host's own
subgraph ledger reads **`queries=10 errors=10`, `via=studio-dev`, `cap=3000`** —
a 100% failure rate, which is the Studio development cap exhausted, not a slow
day — while `acr-probe-free` reads `via=gateway`, `cap=null`, `errors=0`. The
visible cost, measured on the live site: `/api/operator/businesses` and
`/api/operator/traction` answer **503 `upstream: "absent"`**, `/api/tape` is
`live: false`, and `/par` has no route to ask.

**AND AS OF 08:30Z IT IS AN OUTAGE, NOT A DEGRADATION.** `/terminal/data` is the
one route the front page needs, and it was measured twice on each host within
the same minute:

| host | `/terminal/data` |
|---|---|
| `acr-probe-free` | **HTTP 200 in 0.41s**, 18,395 bytes, all seven sections |
| `acr-api-mainnet` | **timed out at 60s, twice, 0 bytes** |

Its `/health` answers in 0.5s throughout, so health is not the signal here — the
press is up and cannot build a bundle, because every subgraph query inside that
build fails. The live consequence, three consecutive reads, `x-vercel-cache: MISS`
each time: `/api/terminal?chain=mainnet` returns **`live: false` in 9.7s**. The
whole site is on the archived bundle.

**UPDATE, same day, after the cushion was fixed and deployed (`e07f882`).** The
terminal now carries a real two-rung ladder for mainnet, and the measurement
that followed draws the boundary of what it can do, precisely:

| failure mode | what the press returns | ladder? |
|---|---|---|
| `/terminal/data` cannot build a bundle | **timeout / 5xx** | **advances** — the front page recovers |
| a subgraph read fails inside the press | **HTTP 200** `{"available": false, "reason": "the subgraph did not answer"}` (`graph_proxy.py:197`) | **stops, correctly** — a 200 is an answer |

So the ladder covers the outage from this morning and **cannot** cover
`/api/tape`, TCA, ratings or `/operator/*` on a press whose subgraph is dead:
those read `live: false` from a host that is answering. Falling back on a 200
would mean asking a second press to overrule the first's verdict, which is the
rule that also stops a 401 being retried against a different host. Measured
with the `meta` op posted to both presses in the same minute: `acr-api-mainnet`
`available: false`; `acr-probe-free` `available: true`, block 25,392,845.

And the front page being `live: true` right now is **not** the ladder — it is
`acr-api-mainnet` having restarted (uptime 1287s at 09:14Z), which lets a fresh
process assemble a bundle while its subgraph ledger still reads **76 queries,
76 errors**. A reboot buys a bundle, not a tape. **The env flip below is
therefore the only fix for every subgraph-backed surface, not merely the nicer
one.**

**Setting Vercel's `ACR_API` and `NEXT_PUBLIC_ACR_API` to
`https://acr-probe-free.onrender.com` and redeploying fixes all of it, and
fixes one more thing for free.** `lib/apiBase.ts` keeps `MAINNET_SELLER` as the
build's cushion, and `sellerCandidates` dedupes — so while `ACR_API` names the
same host as `MAINNET_SELLER` there is **one candidate and no cushion at all**,
which is precisely the pathology that file's own comment was written about. Flip
the variable and `configured != published`, so `acr-api-mainnet` becomes a live
second rung instead of a no-op. Do **not** also change the literal: two distinct
hosts is the point.

Three preconditions, all checked 2026-10-11 rather than assumed: `ACR_CORS_ORIGINS`
on the probe host already answers `access-control-allow-origin:
https://arc-compute-rate.vercel.app` (without which the wallet path and `/curve`
break and nothing else does); `ACR_ARC_CHAIN_ID=5042` matches `buildChainId=5042`,
so `chainMismatch` allows it; and it does not sleep, per the row above.

**AND NOBODY IS POSTING PRINTS RIGHT NOW.** Worth separating from the above,
because the repoint neither causes nor fixes it. `acr-api-mainnet` holds the
signer (`signer: local`, `0x1547…5379`) and reports **`posts: 0`** with
`note: "error"` on all three indices since its 07:17Z boot; the probe host has
no signer by design. Read straight off the gateway, the newest print was **1.61
hours old** against a 2-hour settle window and a 1.5-hour warn line, so the
oracle is drifting toward stale with no press able to write. The chain itself is
healthy — the subgraph is synced to the head, `hasIndexingErrors: false`.

The service is declared in [`render.yaml`](../render.yaml) at the repo root: a
`runtime: image` web service pinned to a dated `docker.io/kaushtubh02/acr-api:<tag>`
(never `:latest`: the dashboard's pin is what runs, and `deploy/redeploy-render.sh`
now sets it to the tag it pushes and reads it back — `scripts/render_pin.py`), health-
checked at `/health`, with the Arc RPC, the four contract addresses and the Circle
wallet ids set as plain env vars and the three Circle credentials
(`ACR_CIRCLE_API_KEY`, `ACR_CIRCLE_ENTITY_SECRET`, `ACR_CIRCLE_WEBHOOK_PUBLIC_KEY`)
marked `sync: false` so they are entered in the dashboard, never committed.

Three operational facts that have each cost a debugging session:

- **The free plan has no persistent disk.** Anything the API writes is gone on the
  next deploy; durable evidence has to live in the repo or on chain.
- **The free instance sleeps after ~15 idle minutes**, and the hourly oracle poster
  only runs while the process is alive. That is what
  [`.github/workflows/keepalive.yml`](../.github/workflows/keepalive.yml) is for —
  it pings `/health` every 10 minutes. A sleeping instance once took the hourly
  press with it, opening gaps of up to 216 minutes against a 120-minute settle
  window.
- **Editing an env var in the Render dashboard does not restart the service.** It
  needs an explicit redeploy (a `SKIP_BUILD` redeploy is enough). And when
  services/ changes, deploy **Render before Vercel** — the Terminal reads the API.

Deploy order for a normal change — **Render first, then Vercel**, because the
Terminal reads the API:

```bash
# 1. API: rebuild and push. --platform is MANDATORY from Apple silicon: an
#    arm64 push produces an image Render accepts and then cannot start, and
#    the symptom is a port-scan timeout rather than an arch error.
#    Tag dated AND latest: the dated tag is the rollback target and the only
#    thing a PINNED service can be pointed at; `latest` is what the testnet
#    service tracks.
TAG=$(date -u +%Y-%m-%d-%H%M)
docker build --platform linux/amd64 -t kaushtubh02/acr-api:$TAG -t kaushtubh02/acr-api:latest .
docker push kaushtubh02/acr-api:$TAG && docker push kaushtubh02/acr-api:latest

# 1b. Run it before you ship it. Thirty seconds, and it is the only check that
#     catches a bad image while it is still only yours:
docker run --rm -p 8097:8000 --platform linux/amd64 \
  -e ACR_ARC_CHAIN_ID=5042002 -e ACR_HUMANID_MODE=dev kaushtubh02/acr-api:$TAG
#     then: curl -s localhost:8097/openapi.json | jq '.paths | keys | length'

# 2. Confirm the new build is SERVED, with a witness that could not pass
#    against the old image. /health cannot do this — every image answers it.
#    Use a route the new image added, and the strict drift gate:
VERIFY_DRIFT_HOSTS=https://acr-api-mainnet.onrender.com VERIFY_DRIFT_STRICT=1 \
  uv run python scripts/verify_deploy_drift.py

# 3. Terminal: Vercel deploys are MANUAL for this project — trigger from the
#    Vercel dashboard (or `vercel --prod` from apps/terminal).
```

Both hosts deploy by hand on purpose. The failure mode this prevents is the
expensive one: re-debugging a bug that was already fixed but never shipped. Date
the running build before you trust it — count a string in the response that the
last change removed.

---

## Appendix — Google Cloud Run (an alternative, not the deployment in use)

> The runbook below was written when Cloud Run was the intended host. It is kept
> because it still works and is a reasonable path if you want scale-to-zero with a
> warm-instance option. **It is not what serves `acr-api-mainnet.onrender.com`** — the
> `acr-api-XXXX.run.app` URLs in this section are placeholders, not live endpoints.

### Cost
No paid **plan** — GCP is pay-as-you-go; just **enable billing** (new accounts get
$300 free credit / 90 days). Vercel Hobby is free. Arc is testnet (no real gas).
The default deploy is **scale-to-zero** → stays in Cloud Run's free tier. Only
`ALWAYS_ON=1` (one warm instance for the hourly poster) costs ~$5–15/mo.

---

## 1. Seller API → Google Cloud Run

Deploys from **local source** (Cloud Build builds the `Dockerfile`) — no GitHub push needed.

**Prereqs** (one-time):
```bash
gcloud auth login
gcloud config set project <YOUR_PROJECT>
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com
```

**Deploy (MINIMAL, $0, no secrets)** — live oracle reads + real x402 Circle
settlement + webhook recording, scale-to-zero:
```bash
./deploy/deploy-cloudrun.sh
```
This binds `0.0.0.0:$PORT` (Cloud Run injects 8080), `--allow-unauthenticated`
(public), `--max-instances=1` (in-memory counters assume a single instance), and
sets the live env (`ACR_X402_MODE=circle`, `FACILITATOR_URL`, `PAY_TO`, oracle +
registry addresses, `TAPE_SOURCE=arc`, `CORS=*`). It prints the public URL:
`https://acr-api-XXXX.run.app`.

> **The live deployment is not this one.** Production runs on Render
> (`deploy/deploy-render.sh`, image `docker.io/kaushtubh02/acr-api`) with
> `ACR_TAPE_SOURCE=sim` — the Cloud Run script's `TAPE_SOURCE=arc` above is not
> what serves `https://acr-api-mainnet.onrender.com`. Check `/health` for the truth.

**Options:**
- `ALWAYS_ON=1 ./deploy/deploy-cloudrun.sh` — one warm instance so the background
  loop (cache warm + optional posting) never dies (~$5–15/mo).
- `POST=1 SECRETS="ACR_POSTER_PRIVATE_KEY=acr-poster:latest" ALWAYS_ON=1 ./deploy/deploy-cloudrun.sh`
  — the cloud instance also **posts hourly prints**. Store the signer in Secret
  Manager first (never in the script/env-vars):
  ```bash
  echo -n "0x<poster-key>" | gcloud secrets create acr-poster --data-file=-
  # or the Circle-custody set: acr-circle-api-key / acr-circle-entity-secret / acr-circle-wallet-id
  ```
  For Circle custody use `SECRETS="ACR_CIRCLE_API_KEY=acr-circle-api-key:latest,ACR_CIRCLE_ENTITY_SECRET=acr-circle-entity-secret:latest,ACR_CIRCLE_WALLET_ID=acr-circle-wallet-id:latest"`.
  `REFRESH=3600` is the default in the script (hourly — do NOT post every 30s).

**Persistence (optional durability):** `data/*.jsonl` (webhook feed + settlement
ledger) is ephemeral per instance. To persist across restarts, mount a GCS bucket:
```bash
gcloud run services update acr-api --region <r> \
  --add-volume name=data,type=cloud-storage,bucket=<your-bucket> \
  --add-volume-mount volume=data,mount-path=/app/data
```
(GCS-FUSE appends are rewrites — fine at demo volume.)

**Verify:**
```bash
API=https://acr-api-XXXX.run.app
curl -s $API/health | python3 -m json.tool      # gate:circle, chain 5042, oracle_configured
curl -s $API/x402/info
curl -s $API/terminal/data | python3 -c "import sys,json;print('oracle',json.load(sys.stdin)['oracle'])"
```

---

## 2. Terminal → Vercel

The Terminal is a Next.js app with **server-side proxy routes** (not static) — it
needs a Node host. Root directory = `apps/terminal`.

```bash
cd apps/terminal
vercel deploy --prod \
  --build-env ACR_API=$API --build-env NEXT_PUBLIC_ACR_API=$API \
  --env ACR_API=$API --env NEXT_PUBLIC_ACR_API=$API
```
- `ACR_API` — server-side proxy target (all `/api/*` routes).
- `NEXT_PUBLIC_ACR_API` — the browser `/docs` link on `/developers` (build-time inlined).

Output: the public dashboard on the linked Vercel project (project name
`terminal` — deployed at `https://arc-compute-rate.vercel.app`). While the
API is unreachable (free-tier cold start) the terminal walks its connection
ladder honestly: instant shell + skeletons, "waking the press", direct
ACROracle reads via `/api/onchain`, and the bundled `lib/fallback.json`
archived edition as the floor.

### 2a. The custom domain — `arccomputerate.in` — NOT IN USE

**Set aside 2026-10-09. The canonical host is `arc-compute-rate.vercel.app`.**

This section is kept as the record of how to finish the move, not as a
description of the current state. What happened: the domain was added to the
Vercel project and both it and `www` report `verified: true` — verification only
proves you control the name — but **the DNS was never changed**. `dig +short A
arccomputerate.in` answers Hostinger's parking range, and the host returns a
"Parked Domain name on Hostinger DNS system" page at **HTTP 200**.

That 200 is the whole trap. Three documents, `metadataBase`, `robots.txt`, the
provider catalog Circle's crawler follows, and the standing liveness gate were
all moved onto the name, and nothing failed loudly: `tests/test_canonical_host.py`
compares those places to **each other** and stayed green, and `verify_live.py`
produced a red run that blamed a Next route. **Verify a host by fetching a page
and reading what comes back, never by its status code.**

To finish it later: add the two A records below, confirm with a fetch that the
content is the terminal and not a parking page, then move the canonical host in
the one place that defines it — `marketplace.PROVIDER_WEBSITE`, which
`test_canonical_host.py` reads as truth and which drags `metadataBase` and
`robots.ts` with it. The `www` 307 rule was deleted from
`apps/terminal/next.config.mjs` as dead config; re-add it with the DNS.

**DNS stays at Hostinger. Do not point the nameservers at Vercel.** The zone
carries live mail:

```
MX   5   mx1.hostinger.com
MX   10  mx2.hostinger.com
TXT  @   v=spf1 include:_spf.mail.hostinger.com ~all
```

Vercel offers a nameserver switch as its option (b). Taking it drops those
records and **mail to the domain stops without an error anywhere**. Vercel marks
the A-record route `[recommended]` regardless, and it wants an A record for the
apex *and* for `www` — not the apex-A / www-CNAME pairing older guides describe:

```
@     A  76.76.21.21
www   A  76.76.21.21
```

One trap when setting these: the zone shipped a malformed `www` record resolving
to `arccomputerate.in.arccomputerate.in.` — a value that already contained the
zone, entered in a field that appends it. Delete it rather than editing around it.

**Adding the domain needs the `vercel` CLI, not the API token.** A Vercel token
that can create deployments gets `403 forbidden — You don't have permission to
update the project` on `POST /v10/projects/{id}/domains`. The locally
authenticated CLI does it:

```bash
cd apps/terminal
vercel domains add arccomputerate.in terminal --non-interactive
vercel domains add www.arccomputerate.in terminal --non-interactive
vercel domains inspect arccomputerate.in --non-interactive   # the records it wants
```

**The press needs three env vars, and one of them breaks a feature silently.**
Set them on the Render service (single-key edits — the bulk env PUT replaces
every var) and redeploy, because env edits alone do not restart a service:

```
ACR_CORS_ORIGINS=https://arc-compute-rate.vercel.app
ACR_PROVIDER_WEBSITE=https://arc-compute-rate.vercel.app
ACR_PROVIDER_DOCS_URL=https://arc-compute-rate.vercel.app/developers
```

These are the values for the host that actually serves. The block named the
custom domain for one day, which would have sent Circle's crawler to a parked
page — and note the live mainnet service still carries the OLD list, which
**rejects** the custom domain and allows the Vercel origin, because a Render env
edit does not restart a service. Add the custom-domain origins back when its DNS
is finished, not before.

`ACR_CORS_ORIGINS` is the one to get right. Every read in the Terminal goes
through its own `/api/*` proxies, so a wrong value here breaks nothing a visitor
sees first — but `lib/apiBase.ts`'s `sellerBase()` exists so the **browser** pays
the press directly, and `lib/walletPayer.ts` reads `PAYMENT-REQUIRED` and
`PAYMENT-RESPONSE` off that cross-origin response. An origin missing from the
list takes out exactly two controls, pay-with-wallet on `/developers` and the
`/curve` shop floor, and leaves everything else working. There is no
`allow_origin_regex` anywhere, so the apex does **not** cover `www`: a
cross-origin fetch is refused before any redirect is followed.

`tests/test_canonical_host.py` checks all four places the hostname lives agree —
`metadataBase`, `robots.ts`, `PROVIDER_WEBSITE` and this CORS list.

### 2b. Real Circle settlement from the cloud dashboard (optional)

The Terminal is the buyer for the UI-triggered "LIVE buyer" and console "Settle
for real" actions (`app/api/buy`, `app/api/circle/balances`, Node runtime). To
enable them in the cloud, add ONE server-only secret in Vercel:

```bash
vercel env add ACR_BUYER_PRIVATE_KEY production   # a funded EOA with an OPEN Gateway deposit
# ACR_ARC_RPC_URL is optional (the Circle SDK defaults arcTestnet); ACR_API must
# point at a seller whose /health gate == "circle".
```

- Server-only (never `NEXT_PUBLIC_`); the browser only sees settlement results.
- The buyer signs EIP-3009 and settles via Circle Gateway — real USDC on Arc.
  Prereq is operator-only: `make buyer-key` → fund → `circle gateway deposit`
  (see [`agent-runbook.md`](agent-runbook.md) §4b). A hard $0.01 cap is enforced.
- Without the key the LIVE controls stay disabled; everything else still renders.
- Seller: keep it on `ACR_X402_MODE=circle`. For heavy live demos consider a
  paid instance (the 512MB free tier can cold-start 502 the heavy `/terminal/data`).

---

## 3. Circle webhook subscription

The receiver `POST /webhooks/circle` is public + ungated and ACKs 200 on the
confirmation ping. In the **Circle console** (Programmable / Developer-Controlled
Wallets → Webhooks) add a subscription:
```
https://acr-api-XXXX.run.app/webhooks/circle
```
Signature verification: pin `ACR_CIRCLE_WEBHOOK_PUBLIC_KEY` (offline) or set
`ACR_CIRCLE_API_KEY` (fetch by key-id). Without either, events are still recorded
(`verified=null`). Confirm deliveries:
```bash
curl -s $API/webhooks/recent | python3 -m json.tool
```

---

## 4. End-to-end check (live testnet, in the cloud)

```bash
# real x402 settlement against the cloud API (needs a funded buyer EOA — see docs/agent-runbook.md)
cd apps/agent && AGENT_PRIVATE_KEY=0x<funded> npm run start -- --live --api $API --count 5 --limit 0.01 --discover
curl -s $API/revenue                       # paid_queries climbs; scheme "exact", gateway-ref
curl -s $API/marketplace/receipts
```
Open the Vercel URL → every route renders live from the cloud API; settlement refs
show as `gateway-ref`, on-chain prints as `tx`, simulations as `sim`.
