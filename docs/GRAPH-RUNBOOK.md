# Graph integration — the operator runbook

Five steps, in this order. Everything is built and tested; each of these needs
live credentials, and two of them are **not recoverable if done out of order**.

Read the two warnings first — they are the only ways this sequence can cost
something real.

> **Never repoint `ACR_ORACLE_ADDRESS`.** `ACRFutures.oracle` is `immutable`,
> and `settle()` needs a print posted within `MAX_SETTLE_AGE = 7200` of expiry. On the
> venue deployed before the escape hatch, a feed that stays quiet strands every expired
> series until v1 prints again; the current source adds `settleStale` after a 7-day
> grace (docs/SECURITY.md). v2 is deployed *alongside*
> v1 and the poster writes to both. v1 is never allowed to go stale to serve v2.

> **Backfill v2 before its first live post.** `postPrint` enforces *strictly*
> monotone economic timestamps, so once v2 takes one live print no earlier one
> can ever be inserted. A v2 that starts empty stays empty for its whole
> history, the subgraph's arrival ring has nothing in it, and every settlement
> comes back `benchmarked: false`.

---

## 1 · Attest the seller fleet

The fleet is what makes transaction-cost analysis mean anything: before it,
every settlement paid one wallet one flat price, so slippage-versus-benchmark
was identically zero for everyone. Two of its sellers are new.

```bash
make attest-once     # files all six DEMO_SELLERS on the live registry
make snapshot        # regenerates apps/terminal/lib/fallback.json from chain
```

**Why it is first:** `apps/terminal/lib/chain.test.ts` asserts that every label
in `demo_sellers.py` derives to an address actually filed on Arc — it reads the
attested set out of `fallback.json`. Until this runs, the terminal CI job is red
on that one test. The terminal count in the docs is already correct for
the post-attestation state; do not lower it.


## 2 · Deploy the ReceiptMirror

Circle Gateway settles x402 off-chain and returns a batch UUID, not a
transaction. There is no settlement event on Arc for a subgraph to index, so
without this contract the tape does not exist.

```bash
export DEPLOYER_PRIVATE_KEY=0x…
export ACR_MIRROR_SIGNER=0x…      # the wallet that signs oracle prints
make deploy-mirror-dry            # simulate first
make deploy-mirror
```

Then record it in **two** places:

- `.env` / Render → `ACR_RECEIPT_MIRROR_ADDRESS=0x…`
- `graph/subgraph.yaml` → the `ReceiptMirror` data source's `address` **and**
  its `startBlock` (the deploy's block number).


## 3 · Deploy oracle v2

```bash
export ACR_ORACLE_V2_SIGNER=0x…   # the same print signer
make deploy-oracle-v2-dry
make deploy-oracle-v2
```

Set `ACR_ORACLE_V2_ADDRESS`. **Leave `ACR_ORACLE_ADDRESS` on v1.** Add the v2
data source's `address` and `startBlock` to `graph/subgraph.yaml` and redeploy
the subgraph.


## 4 · Backfill v2 — before any live post

```bash
make backfill-oracle-v2 ARGS=--dry-run   # read the plan first
make backfill-oracle-v2
```

It re-signs v1's last 48 hours under the v2 typehash and posts them ascending,
stamped with a distinguished policy hash (`sha256("acr.oracle.v2.backfill")`) so
anyone re-deriving the tape can tell these were re-signed during the migration
rather than produced by the estimator at the time. It refuses to post anything
that would put v2 *ahead* of v1, because the poster seeds its cursor from the
maximum across both and a v2 that runs ahead would revert every subsequent post
forever.

Only after this should the poster be allowed to take a live v2 print — which it
does automatically, on the next cycle, once `ACR_ORACLE_V2_ADDRESS` is set.


## 5 · Deploy the subgraph

```bash
# The slug must ALREADY EXIST in Studio — the CLI cannot create one, and an
# unknown slug answers with a bare "Subgraph not found" that is identical to
# what a bad deploy key returns. Create it at https://thegraph.com/studio.
npx graph auth <deploy-key>
make graph-deploy SUBGRAPH=<slug> NETWORK=arc VERSION=v1.0.0-mainnet   # default slug: ethonline

# The deploy key is ACCOUNT-scoped, and "Subgraph not found" is what Studio says
# when the key's account does not hold that slug -- it reads exactly like a typo
# in the name. Tell them apart by deploying to a slug you KNOW exists: if
# `ethonline` is also "not found", the key is from the wrong account, not the
# slug from the wrong string. (Measured 2026-09-28: a key that could see neither
# cost an hour of hunting for a misspelling that did not exist.)
```

Do **not** run `graph init` from Studio's onboarding panel: it scaffolds a fresh
boilerplate subgraph, and this one already exists in `graph/`. Only the slug and
the deploy key from that panel matter here.

Deployed 2026-09-05 as `ethonline` v0.1.0 (`Qmb8Dw6cBZjzkCx4PRc7BC8defLxgLZJDBLoho2oocsjZf`);
**current is v0.2.0** (`QmdsGieTC7B4KTyd2pEhV1wmFLCeF1yCYwLLgBXL2a9Ci6`), which added the
`HumanIdMirror` data source, `HumanCluster`, `SellerWindow` and `Settlement.human`.
Production reads v0.2.0 (`render.yaml`). Query endpoint:

```
https://api.studio.thegraph.com/query/1758707/ethonline/v0.2.0
```

**`acr` v0.1.0 — the MAINNET deployment, 2026-10-11.** Deployed to the slug `acr`
(`QmZ5tUKEv9SY5CiUUDPyHyWFpm9LfZJefRbMy9JUPbPJzs`), built for `NETWORK=arc`:

```
https://api.studio.thegraph.com/query/1763111/acr/v0.1.0
```

Verified rather than assumed, because a subgraph can deploy and then index nothing
while reporting no error — which, as the `graph-deploy` guards say, reads exactly
like a quiet market. At deploy time `_meta.block.number` was **25,368,493** against
an Arc mainnet head of **25,368,497** — four blocks behind, so synced — with
`hasIndexingErrors: false`, and it answers with real rows: settlements from
`0xc2903b52…` and an `ACR-INF` print of `0.497734`, which is the value the mainnet
press was serving at that moment.

**Note the account id.** The old endpoints are `/query/1758707/…` and this one is
`/query/1763111/…`, a different Studio account — which is why it has its own
development allowance rather than inheriting the exhausted one. It is also the
other half of the deploy-key trap above: a key is ACCOUNT-scoped, so a key for
1763111 cannot see `ethonline` on 1758707 and will answer "Subgraph not found"
for it, which reads exactly like a misspelling.

**What this deploy does and does not fix.** It gives the mainnet press a working
endpoint with a fresh cap — set `ACR_SUBGRAPH_URL` on that Render service to the
URL above. It does **not** put anything on the gateway: that is step 6 below, needs
a publish transaction on Arbitrum One and a gateway API key, and a *deploy* key is
not a gateway key.

That is the Studio *development* endpoint and needs no gateway API key. **It is capped at
3,000 queries a day and Studio's dashboard does not count it** — which is why "the portal shows
zero queries" while the press has made thousands. The press keeps its own count on
`GET /graph/operations` → `transport` (queries, cache hits, host, `via`, pace per day) and
`/ops` warns when the pace nears the cap. A `ACR_GRAPH_API_KEY` only matters once the subgraph
is published to the network — step 6.

## 6 · Publish, and switch to the gateway (the path Studio counts)

> **DONE for mainnet, 2026-10-11.** `acr` is published and the gateway serves it.
> Verified through the press's own `graph_query`, not just with curl:
>
> **Two URLs work, and Studio hands you the second one**, so both are recorded.
> Each was verified through `graph_query` with the Bearer key, and they returned
> the identical block and rows:
>
> ```
> # stable — survives version bumps, no Render edit on a later publish
> ACR_SUBGRAPH_URL  https://gateway.thegraph.com/api/subgraphs/id/48b7j1avjuKapURQmgF3fY25xC6vK4aR4PHU4JTYfY6N
>
> # pinned — what Studio's v0.1.0 panel shows; one exact build
> ACR_SUBGRAPH_URL  https://gateway.thegraph.com/api/deployments/id/QmZ5tUKEv9SY5CiUUDPyHyWFpm9LfZJefRbMy9JUPbPJzs
>
> ACR_GRAPH_API_KEY <the gateway key, sent as Authorization: Bearer>
> ```
>
> Prefer the **stable** one here. Not because pinning is wrong — it is the safer
> default in most systems — but because of where this particular service lives:
> `acr-api-mainnet` is in a Render account that has repeatedly been the thing
> blocking a fix, and a pinned id guarantees an edit to it on every future
> publish. The stable id removes that coupling. Pin deliberately when you want a
> specific build and are willing to pay that edit.
>
> `block 25,370,517 · hasIndexingErrors false`, real rows (a benchmarked `0.00441`
> settlement, an unbenchmarked `0.0001`, an `ACR-GPU` print at `0.010832`), and
> `transport_info` reporting **`via='gateway'`, `daily_cap=None`, `errors=0`** —
> which is the whole point: the 3,000/day warning is not silenced, it stops
> applying, because `daily_cap` is only set for `studio-dev`.
>
> **Use the SUBGRAPH id on that path, not the deployment id.** Measured:
>
> | path | id | result |
> |---|---|---|
> | `/api/subgraphs/id/` | subgraph | **OK** |
> | `/api/deployments/id/` | deployment | **OK** — pins one build |
> | `/api/subgraphs/id/` | deployment | **rejected:** *"subgraph not found: invalid subgraph ID"* |
>
> The refusal names the *id*, so a wrong **path** reads exactly like a wrong
> **value** — the same trap shape as the account-scoped deploy key above. And an
> unauthenticated gateway request answers `auth error: missing authorization
> header` for a **bogus** deployment id too, so that error proves the route is
> reachable and nothing about whether your subgraph is published.
>
> **PROVEN ON A LIVE PRESS, 2026-10-11.** The switch is not theoretical: both vars
> went onto `acr-probe-free` (the one mainnet-chain service an `.env` key can
> reach) via the single-key endpoint plus a redeploy, and it went live in ~90 s.
> Before and after, same host, `npm run smoke`:
>
> | | before | after |
> |---|---|---|
> | transport | `via='unset'` | `via='gateway'`, `daily_cap=None`, `errors=0` |
> | `/tca` | refused | 3 purchases, `$0.00461` |
> | tools answering | tape tools refused | **11 of 11 anonymously** |
>
> The two residual failures under a CARD are not the subgraph and are worth not
> misreading: `query_tape` fails **closed** because `/armor/info` on that host
> says `credentials_present: false`, and `can_i_pay` correctly reports no mainnet
> USDC for a wallet that is funded on testnet. Both are honest answers.
>
> So the recipe for `acr-api-mainnet` is now evidence rather than a plan: a
> current image (it serves **44** routes against this host's 50) plus these two
> variables.
>
> Still on Studio's capped dev endpoint: the **testnet** press
> (`ethonline/v0.2.0`, last measured ~9,144/day against the 3,000 cap). Publishing
> a testnet subgraph needs its own slug, which the CLI cannot create.


Arc is listed on The Graph's supported-networks page as *Subgraphs · Hosted (No issuance)*: it
can be published to the network and served by the upgrade indexer, without indexing rewards.
Four clicks in Studio, two values on Render:

1. **Studio → `ethonline` → Publish** (v0.2.0). A wallet transaction on **Arbitrum One**; signal
   is optional — leave it at 0 for a testnet subgraph. Copy the **Deployment ID** (`Qm…`) or the
   **Subgraph ID** the publish dialog shows.
2. **Studio → API Keys → Create**. Name it `acr-press`. Optionally restrict it to this
   subgraph. This key's **usage page** is where the queries will appear.
3. **Render** (single-key PUTs, never the bulk env replace):
   `ACR_SUBGRAPH_URL=https://gateway.thegraph.com/api/subgraphs/id/<DEPLOYMENT ID>` and
   `ACR_GRAPH_API_KEY=<the key>` (sent as `Authorization: Bearer`, `graph_client.py`).
4. `SKIP_BUILD=1 ./deploy/redeploy-render.sh`. Then `curl $API/graph/operations | jq .transport`
   → `"via": "gateway"`; `/ops` → *subgraph queries … via gateway*; `make verify-live` stops
   warning about the development URL; Studio's API-key usage counts from that moment on.

The free plan is 100,000 queries a month. The press's 20 s query cache (`CACHE_TTL_S` in
`graph_client.py`) keeps a dozen open tapes from spending that: repeats within the window are
served from memory and reported as `cache_hits`, never hidden.

`make graph-deploy` refuses while **any** data source still holds a placeholder
address *or* `startBlock: 0`. That is why it comes last: a subgraph pointed at
`0x0` indexes nothing, reports no error, and serves an empty tape that reads
exactly like a quiet market — and a `startBlock` of 0 makes the indexer scan the
whole chain instead of starting at the deploy.

Then set `ACR_SUBGRAPH_URL` (and `ACR_GRAPH_API_KEY`, server-side only), and
record in `hackathon/ethonline-2026/SPIKE-LOG.md`: `_meta.block` against RPC head, whether
`hasIndexingErrors` is false, and how long the backfill from block 53066540
actually took. If it is slow, raise `startBlock` and say so there.


---

## Checking it worked

```bash
make verify-live                  # v1 freshness is a hard fail; v2 is a warn
make recompute                    # recomputed value within the on-chain CI
make recompute ARGS=--rederive-cleaning
curl -s $ACR_API/tca/<payer>      # carries n and the synthetic share
curl -s $ACR_API/rating/<seller>  # plus weight_covered_pct
curl -s -X POST $ACR_API/graph/query -H 'content-type: application/json' \
  -d '{"operation":"meta"}'       # _meta.block vs head, hasIndexingErrors
curl -s $ACR_API/graph/operations  # every operation the proxy will run
```

**Arguments go in `variables`, not at the top level.** Four operations need one
(`prints`/`economicPrints` take `index`, `sellerDays` takes `seller`,
`payerDays` takes `payer`); the rest only take the optional `first`:

```bash
curl -s -X POST $ACR_API/graph/query -H 'content-type: application/json' \
  -d '{"operation":"prints","variables":{"index":"ACR-INF","first":5}}'
```

Omit a required one and the proxy names it (`needs variable(s) index`) rather
than reporting the subgraph as unreachable — the two are very different
problems and used to produce the same message.

The mirror keeper runs on its own timer inside the service (`ACR_KEEPER_MIRROR_S`,
120s default) and reports on `/health` under `keeper.mirror`. For a first run or
a backlog, `make mirror-receipts` is the same code path by hand.

## Recurring chore — re-resolve humans every rotation window

**A human resolution goes stale on a seven-day clock, and it fails SILENTLY.**

`HumanIdMirror` mints a cluster id per rotation window (`RATING_WINDOW = 7 days`), so a wallet
resolved in window *N* has no cluster in window *N+1*. Nothing errors when the window rolls: the
subgraph simply finds no clusters for the current window, `/api/humanid` reports `"n": 0`, the
dateline's humans chip disappears, and every surface goes quiet as though nobody had ever been
verified. There is no message anywhere saying "these resolutions belong to last week".

Measured 2026-09-12: the roll from 2957 to 2958 did exactly this to production, and it was only
noticed because a deploy check happened to read `humans.n`.

```bash
# Which window are we in, and when does it roll?
uv run python -c "import time;W=604800;c=int(time.time()//W);print(c, time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime((c+1)*W)))"

# Re-resolve. ALWAYS dry-run first; it signs and checks against the contract's own digest.
ACR_HUMANID_MIRROR_ADDRESS=0x… uv run python scripts/resolve_humans.py --dry-run
ACR_HUMANID_MIRROR_ADDRESS=0x… uv run python scripts/resolve_humans.py --commit
```

**Run it BEFORE the tape moves on.** `Settlement.human` is stamped at finalize and the entity is
immutable, so a payer that settles while unresolved is counted non-human forever; the subgraph
flags it (`Payer.resolvedLate`) but cannot repair it.

Confirm with `clusterOf(wallet, <window>)` on chain, not with the absence of an error — the whole
failure mode here is that absence looks like success.

## What stays unfinished, deliberately

- **`humanAdjustedBound` is absent, not zero.** The contract accepts `0` as the
  "not computed" sentinel and the tape stores it as null. It stays that way
  until the World module lands: publishing the human bound equal to the wallet
  bound would claim identities are as cheap to buy as wallets, which is false.
- **Cleanliness and human-depth are excluded from seller ratings**, not scored
  zero — every rating carries `weight_covered_pct` so a reader knows what share
  of the published methodology the grade actually rests on.
- **The Terminal still reads v1.** Appending fields keeps the first seven words
  of the struct byte-identical, so it decodes v2 correctly and simply cannot see
  the new fields. Updating it is mainnet-week work.
