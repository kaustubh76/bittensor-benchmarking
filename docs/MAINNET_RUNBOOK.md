# Arc mainnet — the ordered deploy

*Arc public mainnet (`eip155:5042`) opens 2026-09-16. This is the sequence that puts ACR on it,
written before the chain exists so that nothing on the day is a decision. Every step below was
rehearsed on `arc-testnet` (`eip155:5042002`), where the identical five scripts produced the
addresses in `render.yaml`. `make deploy-mainnet-dry` simulates all five against the mainnet RPC
and refuses to proceed if the RPC answers with any other chain id.*

**Deployment-ready, not deployed** — stated plainly because the Arc continuity bounty asks for
one or the other. Deployed on testnet since 2026-07; the mainnet transaction hash will be added
here and in `README.md` the day it lands.

## 0 · What is chain-specific, and what is not

| | testnet | mainnet |
|---|---|---|
| chain id | `5042002` | `5042` |
| RPC | `https://rpc.testnet.arc.io` | `https://rpc.mainnet.arc.io` is **public** (probed 2026-09-21, `eth_chainId` → `0x13b2`, no credentials); run the press on a keyed provider URL (Alchemy, QuickNode, dRPC, Blockdaemon all list Arc) and set `ACR_MAINNET_RPC_URL` to that |
| USDC | `0x3600…0000` (native) — **defaulted** in the scripts | **not defaulted**: `ACR_MAINNET_USDC` is required, so a testnet constant cannot ride onto mainnet by omission |
| explorer | `testnet.arcscan.app` | `https://explorer.arc.io` (public; the chain profile carries it) |
| gas | USDC | USDC — `Deploy.s.sol` 4.13M · `DeployOracleV2` 1.51M · `DeployReceiptMirror` 1.43M · `DeployHumanIdMirror` 1.00M · `DeployFutures` 2.23M ≈ **0.46 USDC** total at testnet prices |

Everything else — the estimator, the oracle's EIP-712 domain (binds `chainId`), the x402 gate,
the subgraph mappings, the Terminal — reads the chain id from configuration and changes nothing.

## 1 · Before the first transaction

```sh
export ACR_MAINNET_RPC_URL=https://...            # Arc's mainnet RPC
export ACR_MAINNET_USDC=0x...                     # Arc's mainnet USDC (read it from Arc's docs, do not guess)
export DEPLOYER_PRIVATE_KEY=0x...                 # a FRESH key, funded with >= 1 USDC for gas — single-use, see §3b
export ACR_EXPECTED_OWNER=0x...                   # who owns the contracts after §3b: the multisig, or the custody wallet until one exists
export ACR_HUMANID_SALT_COMMITMENT=0x...          # keccak of the salt — the SALT itself never leaves the operator's machine
make deploy-mainnet-dry                           # five simulations; exits 1 on a wrong chain id
```

The dry run is the gate. If any script fails to simulate, nothing is broadcast and the fix is made
here, not on chain.

## 2 · Deploy, in this order

```sh
export ACR_PRESS_SIGNER=0x...                     # the press custody wallet — the ONLY key that will sign prints
make deploy-mainnet
```

One script, one broadcast — `DeployMainnet.s.sol` — because the order carries dependencies
and the custody has to land in the same transaction as the contracts, not in a ceremony
someone can forget:

1. **ACROracle (v1)** and **AttestationRegistry**.
2. **ACROracleV2**, constructed with `MAX_MOVE_BPS` (2 000: a print moves at most 20% from
   the last; a stolen key walks, it does not jump).
3. **ACRFutures**, constructed against **v2** — its oracle pointer is immutable, which is why
   v2 exists before it. The venue's settlement views are primitives, so v1 or v2 works; v2 is
   the one with the bound.
4. **ReceiptMirror**, **HumanIdMirror** (with the salt commitment), **FeedAccessAttestor**.
5. `setSigner(press, true)` on every signing contract, then **`setSigner(deployer, false)` on
   every one** — each constructor grants the deployer a signer bit, and this is where it is
   taken back. After this broadcast the deploy key signs nothing.
6. If `ACR_EXPECTED_OWNER` is set: `transferOwnership` to it on all six owned contracts.
   The `acceptOwnership` half is §3 step 5 — the new owner's own act, deliberately.

Then `make backfill-oracle-v2` BEFORE any live post, so v2 carries the history the venue
will settle against.

The per-contract scripts (`Deploy.s.sol`, `DeployOracleV2.s.sol`, …) remain for testnet and
for adding one contract to an existing deployment; they are not the mainnet path.

## 3 · After the addresses exist

1. **Configuration.** `render.yaml` already carries the mainnet service, `acr-api-mainnet`,
   with every variable the guard requires — review that block, paste the `sync: false`
   values into the dashboard, fill the addresses from the deploy output. The service
   **refuses to boot** on 5042 until the gates are explicit and configured
   (`acr_core.mainnet_guard`); read its one raise, fix every line it lists, redeploy.

   The terminal (Vercel) is a separate build with its own variables:

   | variable | value | why |
   |---|---|---|
   | `NEXT_PUBLIC_ACR_API` | *(optional)* the seller's URL | **a production build already defaults to `https://acr-api-mainnet.onrender.com`** (`lib/apiBase.ts`), because `http://127.0.0.1:8000` is nothing on a server. Set it only to point somewhere else — a fork, a preview, a testnet deploy |
   | `NEXT_PUBLIC_ACR_CHAIN_ID` | *(optional)* `5042` | a belt-and-braces assertion: `lib/chain.test.ts` fails the build if the cold-start bundle is from another chain. Unset, the bundle's own chain is accepted — and the committed bundle is already 5042 |
   | `ACR_API` | *(optional)* same as above | server routes; falls back to the same default |
   | `ACR_ARC_RPC_URL` | a keyed provider URL for mainnet | server-side reads (balances, on-chain routes); never reaches the browser — a visitor's wallet is handed the payload's `public_rpc_url` (`https://rpc.mainnet.arc.io`) |
   | `ACR_TERMINAL_BUYER` | **unset** | the house buyer must not spend on a stranger's click; set to `1` only deliberately |
   | `ACR_BUYER_PRIVATE_KEY` | **unset** | same; there is no house buyer on mainnet unless you mean it |

   Then, BEFORE the Vercel deploy: `make snapshot` against the mainnet API, so the bundle a
   visitor sees while the press naps is mainnet data under a mainnet masthead. The build
   fails otherwise — that is the test doing its job.

2. **Subgraph.** `graph/subgraph.yaml`: network `arc`, each data source's `startBlock` = its
   deploy block; `make graph-deploy VERSION=v1.0.0-mainnet`. The Graph's registry lists `arc`
   as a Studio target (verified 2026-09-01, `hackathon/ethonline-2026/SPIKE-LOG.md`).
3. **Humans.** `make resolve-humans ARGS=--commit` for the current rotation window BEFORE any
   tape is generated, or the first week's settlements carry `human: false` forever.
4. **Signers.** Authorise the poster on v1 and v2, the mirror signer on ReceiptMirror, the
   resolver on HumanIdMirror — each script prints the `authorized signer` it set.
5. **Custody — before any user touches it.** `DeployMainnet.s.sol` already retired the
   deploy key as a signer and started the ownership hand-over. What remains is the half
   only the new owner can do — from `ACR_EXPECTED_OWNER`, on each of the six contracts:

   ```sh
   cast send $CONTRACT 'acceptOwnership()' --rpc-url $ACR_MAINNET_RPC_URL --private-key $OWNER_KEY
   ```

   After the last one, the deploy key owns nothing and signs nothing. Move it out of `.env`.
   If a Safe exists on Arc mainnet, `ACR_EXPECTED_OWNER` is the Safe; if not yet, it is the
   Circle custody owner wallet, and `docs/SECURITY.md` says so until that changes. The
   audit that made this a step: [`SECURITY-AUDIT.md`](SECURITY-AUDIT.md), C1.
6. **Prove it.** `make verify-mainnet` — the same preflight as testnet plus the custody
   checks, which are **hard failures on chain 5042**: no pending transfer, every owner equals
   `ACR_EXPECTED_OWNER`, the deploy key is neither owner nor signer, the press wallet signs.
   Then `make verify-live` against the redeployed API; its last section, *revenue*, checks
   the two ways a human pays (a 402 a wallet can sign, a Desk pass claim that refuses a
   stranger). Paste the first `postPrint` transaction hash into `README.md` and this file.
7. **Buy one thing yourself.** On the terminal, `/curve` → open the shop floor → any listing → *connect your
   wallet to buy* → *buy with your wallet*. A row on the tape, a receipt under
   `/api/marketplace/receipts`, and `/api/revenue` moving by the listing's price is the
   first mainnet revenue, and the proof the storefront works without the house buyer (which
   is off on 5042 unless `ACR_TERMINAL_BUYER=1`). No USDC on Arc yet? The same row bridges
   it from Base, Ethereum, Arbitrum, OP or Polygon (CCTP, inside the terminal) first.

## 4 · What does not move on day one

- **The futures venue opens with the demo maker only.** Real liquidity is a listing decision, not
  a deploy step.
- **The seller fleet stays on testnet** until real sellers exist; mainnet TCA begins with the
  first real x402 settlement to ReceiptMirror.
- **No funds are bridged by this runbook.** Gas is the only USDC it spends.

## 5 · Launch day, in order

> **Rows 1, 2a, 3 and 5 are DONE (2026-09-27).** Seven contracts are on chain, the image is
> rebuilt and pinned, `acr-api-mainnet` is live and answering, and the first three prints are
> posted. `data/mainnet-addresses.txt` (gitignored) holds every address. What remains, in the
> order it unblocks things:
>
> 1. **`acceptOwnership()` ×6 from `0xE804f54109b627205B31149965e534837D046dAd`** — only that key
>    can. Until then the *deploy key* still owns the contracts, so
>    **`data/mainnet-deploy-key.txt` must not be deleted yet**. It holds ~0.84 USDC that should be
>    swept back to the press afterwards.
> 2. ~~**A signer for the press on the host.**~~ **DONE** — `ACR_POSTER_PRIVATE_KEY` is set on the
>    service and it posted by itself (`0x09444bf8…f946d`). Disclosed in `docs/SECURITY.md`. The
>    press then stopped, because the free instance is OOM-killed faster than its hourly timer;
>    `ACR_WAKE_POST_AFTER_S=1200` is the interim fix and row 3a is the real one. Superseded note: the mainnet service
>    holds no key, by the same rule the testnet service follows (`ACR_CIRCLE_WALLET_ID`, never a
>    raw key in an env var). So the three prints were posted from a laptop and **the fixing will
>    not update on its own.** Either create the Circle **live** wallet set and set
>    `ACR_CIRCLE_WALLET_ID`, or accept a raw `ACR_POSTER_PRIVATE_KEY` on Render — the blast radius
>    of that key is now bounded by `ACROracleV2.MAX_MOVE_BPS`, which it was not when this rule was
>    written.
> 3. **Prove revenue with a payer that is not the treasury.** `ACR_X402_PAY_TO` is the press
>    wallet, and Circle's facilitator refuses `self_transfer`, so the press cannot buy from
>    itself. Fund any other wallet, deposit to Gateway, then
>    `PROBE_KEY=… SELLER=https://acr-api-mainnet.onrender.com make wallet-settle-probe`.
> 4. **Upgrade the Render plan from `free` to `starter`** (row 3a) and seed a futures series.


The community will do steps 1–5 of [`COMMUNITY-TEST.md`](COMMUNITY-TEST.md). Everything below
has to be true before the link goes out; each line names who can do it (the code cannot).

| # | do | who / what it needs | proves |
|---|---|---|---|
| 1 | `make deploy-mainnet` (§2) with a **fresh** funded deploy key, `ACR_PRESS_SIGNER`, `ACR_EXPECTED_OWNER` | operator: keys + ~1 USDC of gas on 5042 | seven contracts, deployer signs nothing |
| 2 | `acceptOwnership()` ×6 from the owner (§3 step 5); move the deploy key out of `.env` | the owner key | custody |
| 2a | **Rebuild and push the image.** `docker build --platform linux/amd64 -t kaushtubh02/acr-api:$(date +%F) -t kaushtubh02/acr-api:latest . && docker push` both tags | Docker Desktop running (ask the daemon: `docker version --format '{{.Server.Version}}'` — `docker info` exits 0 even when it cannot connect), `docker login` | **Render pulls a prebuilt image and there is no CI build**, so `:latest` means "whatever was last pushed". On 2026-09-26 that was a 09-13 build predating `mainnet_guard` itself — a mainnet service from it would have run the fail-open code the audit closed. Prove the new code is *inside* the image rather than trusting the build log: `docker run --rm --platform linux/amd64 --entrypoint sh <tag> -c 'uv run --no-sync python -c "from acr_core.mainnet_guard import violations; from acr_core.config import CHAIN_PROFILES; print(CHAIN_PROFILES[5042].public_rpc)"'`. `render.yaml` pins the dated tag |
| 3 | Render `acr-api-mainnet` **already exists**: `srv-das1navlk1mc73dvsm8g` in workspace `tea-d9dgp6n41pts73d3ueu0`, at `https://acr-api-mainnet.onrender.com`, image pinned, 26 env vars set, **suspended on purpose**. Three things remain, all secrets: `ACR_X402_PAY_TO` (treasury), `ACR_HUMANID_APP_ID`, a keyed `ACR_ARC_RPC_URL` — plus the addresses after step 1 and the Circle **LIVE** key + entity secret (test keys do not open `ARC` wallets). Patch them **one key at a time**: the bulk env PUT replaces every var. Env edits do not restart a service — redeploy with `SKIP_BUILD`. | Circle console (live env), Render dashboard | the guard lets it boot |
| 3a | **Upgrade the plan from `free` to `starter`. Measured as REQUIRED, not preferred.** On free (512 MiB) the service's `rss` climbs past 380 MiB within two minutes of boot and it is OOM-killed repeatedly: uptime observed at **1.9 minutes**, so the hourly poster timer never fires at all. `ACR_WAKE_POST_AFTER_S=1200` makes each restart top the record up, which stops the fixing freezing, but it treats the symptom — the press is still restarting every few minutes. Add a card, then `PATCH /v1/services/<id>` with `plan: "starter"`, and drop `ACR_WAKE_POST_AFTER_S` once it holds. | a card on the Render workspace | the press survives long enough to keep its own cadence |
| 4 | Gas Station policy for `ARC` in the Circle console | Circle console | Desk gas is sponsored; without it the SCA pays gas from its own USDC (the Desk detects and says so) |
| 5 | `make backfill-oracle-v2`, first `postPrint` from the press wallet | the press | a mainnet print exists |
| 6 | `make snapshot` against the mainnet API; Vercel: `NEXT_PUBLIC_ACR_API`, `ACR_API`, `ACR_ARC_RPC_URL` (keyed); **no** `ACR_TERMINAL_BUYER`; deploy | Vercel | the terminal's cold-start bundle is mainnet; the dateline chip says *Arc mainnet* |
| 6a | **`ACR_API` on Vercel still points at the testnet service — STILL OPEN, re-measured 2026-10-07** against the current production build: `/api/health` reports `configured: acr-api-1fto`, `refused: {1fto: "seller is on chain 5042002 but this build is for 5042"}`, `active: acr-api-mainnet`, `fellBack: true`. So the terminal is correct **only because 6b's guard refuses the configured host every time** — a permanent fallback, not a fixed setting, and the `chain_id: 5042` a casual probe returns is the ACTIVE host's, which is exactly how this looks fixed when it is not. Originally measured 2026-09-28: `POST /api/probe {"path":"/"}` on the live terminal returned `status: 503` with the body *"This service has been suspended by its owner"* — that is `acr-api-1fto.onrender.com`, and it beats the mainnet fallback because `lib/api.ts` reads `process.env.ACR_API?.trim() || sellerBase()`. Set it (and `NEXT_PUBLIC_ACR_API`) to `https://acr-api-mainnet.onrender.com`, **or simply delete both** — `lib/apiBase.ts` now defaults a production build to the mainnet seller. Then **redeploy**: Vercel bakes env into the function bundle, so an env edit alone changes nothing | Vercel, an owner — the API refuses this to a lesser token (*"Additional permissions are required to create production environment variables"*) | every terminal route stops falling back to the archived bundle |
| 6b | **The testnet seller was RESUMED, which made 6a worse.** 2026-10-01 00:56 UTC: `acr-api-1fto` came back online and answers 200 with `chain_id: 5042002`. The fallback rung honours a WORKING configured host — correct for a staging seller — so the terminal went straight back to it and served **live testnet data under a mainnet UI**, which is worse than the archive it replaced (the archive says ARCHIVED). Closed in 501bc74: `chainMismatch()` makes identity a second test beside health, probed once per host from `/health` against the chain the BUNDLE was built for, and `/api/health` reports every refusal by name. **A cushion written while the bad host was DOWN is untested against it coming back UP.** Verify: `/api/health` → `refused` names `acr-api-1fto` and `active` is `acr-api-mainnet`. NOTE main's keepalive now audits a *healthy testnet* deployment — live series, collateralized makers — and fails only on testnet prints being 16 days stale; it says nothing about mainnet either way | deleting `ACR_API` is still the real fix | the terminal cannot be pointed at another chain's seller by a stale env var |
| 6c | **The GitHub automation never left testnet — this is why prints went 48.9h stale.** `keepalive.yml` woke `acr-api-1fto.onrender.com` (suspended, 503) every scheduled run, nine consecutive failures, and audited with testnet RPC + oracle/futures/registry/custody. `scripts/verify_live.py` had the same host as its `API` **default**, never overridden. Fixed in **PR #28** — which must be MERGED: GitHub cron fires only from the default branch. Verify with `gh run list --workflow keepalive.yml --limit 5` (expect success) and `/onchain/ACR-INF` `posted_at` in minutes | an owner merges the PR | the press wakes itself and keeps posting |
| 6d | **The press wallet is the real runway limit, and 6c makes it burn FASTER.** Measured 2026-09-30 from chain receipts, not estimated: a posting cycle is 6 txs (3 indices × both oracle generations) at a mean **0.004387 USDC** each = **0.0263 USDC per cycle**, and cycles land every 25–55 min whenever something is waking the press. That is ~0.8–1.1 USDC/day against a balance of **0.596607** — about **13–17 hours of awake time**, not the 34.9 h that `PRESS_BURN_USDC_PER_DAY = 0.41` implies (that constant is deliberately low because it also sizes the faucet reserve; the hard floor is what protects the wallet). **Right now the press barely burns, because nothing wakes it** — which is the whole bug in 6c. So merging 6c without funding turns a stalled press into one that drains inside a day: **do both together.** The sweep: ~0.74 USDC as a plain native transfer from the deploy key `0xc2903b52…` (holds 0.789705, no remaining purpose) to the press `0x1547B0d3…`, taking it to ~1.34 and clearing the 1.0 floor | an operator — a real-world-transaction classifier declines it to an agent | the press keeps posting instead of stopping mid-day |
| 6e | **The press now brakes before it runs dry, and says why.** Added 2026-10-01. Its two jobs are not equally recoverable — a missed print is a gap the next print fills, a missed receipt mirror is evidence that never reaches the subgraph, so `/tca` can never see that settlement. They came out of one wallet with no priority, so the press would spend to zero and lose the irreplaceable one with the recoverable one. `OracleClient.post` now declines below `ACR_PRESS_PRINT_FLOOR_USDC` (**0.25**, sized from measured receipts: a cycle is 6 txs = 0.0263 USDC, a mirror 2 txs ≈ 0.0088, so 0.25 leaves ~22 mirrors) and the reason lands on the keeper's chore log via all THREE posting paths (timer, wake, overdue-recovery). **Mirroring is never gated on it.** Set the var to 0 to restore spending-to-empty. This does NOT replace 6d: the brake buys graceful degradation, not runway | — | a dry press degrades to mirroring-only and names the reason, instead of stopping silently the way it did for 48.9 hours |
| 7a | **`/tca` answered from whatever subgraph it was pointed at — including another chain's.** Closed 2026-10-01. `graph_proxy` checked only that `ACR_SUBGRAPH_URL` was SET, and `tca.py` does not go through that gate at all, so a stale URL meant testnet settlements reported as this deployment's TCA with nothing saying so. **Live instance:** `.env` still names `…/ethonline/v0.2.0`, measured at block **64,922,872** against an Arc mainnet head of **23,685,519** — 41.2M ahead, so every local run and script (including `verify_live.py`) was auditing testnet TCA. Production was never affected: Render sets the mainnet URL (23,685,505, fourteen behind head). `wrong_chain_reason()` refuses only when a subgraph is AHEAD of the chain — a certainty, since it cannot index blocks that do not exist — and BEHIND is reported, never refused, because a re-index from startBlock 23,013,298 is legitimately ~672k behind. `/tca` returns its existing `available: false` + reason; `/ops` shows the verdict via `transport_info.chain_check`. **Fix `.env`'s line to the mainnet URL** — until then local `/tca` states a refusal instead of lying | you, for `.env` | /tca cannot be made to report another network's settlements |
| 7b | **The UI's chain values are now GATED, not audited.** `apps/terminal/lib/mainnetOnly.test.ts` fails CI if any shipping `.ts`/`.tsx` under `app/`, `components/`, `lib/` — or `fallback.json` — carries `5042002`, `rpc.testnet`, `testnet.arcscan`, `gateway-api-testnet`, `acr-api-1fto`, `ARC-TESTNET`, `Arc_Testnet`, `Arc Testnet` or `arcTestnet`. Deliberate cases live in `ALLOWED` with a written reason, and an UNUSED entry also fails, so the ledger cannot become a list of excuses. The bundle is checked by content (chain_id, caip2, gateway_chain, circle_blockchain, explorer, Gateway wallet), because a presence test passes on a bundle full of another chain's data — which is what shipped on 09-28 | — | this class of drift cannot reach the UI again without a conscious, documented exception |
| 7c | **TWO numbers on the UI are not mainnet yet, and they are the same number twice.** `/revenue` **and** `/marketplace/receipts` — the settlement tape the shop floor renders under `/curve`, and the one `docs/COMMUNITY-TEST.md` step 5 tells a tester to look at — both report **112 paid queries / $0.333477** against a true 3 / $0.00461. Both read one facilitator ring rehydrated from the committed archive, so one filter covers both: **verified 2026-10-01 in the pushed `2026-09-30` image** against a two-row archive (one `eip155:5042`, one `eip155:5042002`) — both endpoints kept exactly the mainnet row (`paid_queries: 1`), and against the real 112-row archive both report 0. Row 7b's gate deliberately does not cover this: it reads this repo, and these numbers come from a running container | set the Render image tag | the last testnet-derived numbers on the product disappear, both of them |
| 7d | **The wiring audit, 2026-10-01 — what is connected but carries nothing.** Every hop checked and passing: 7/7 contracts have code; `/onchain/*` reads `0xAfe8a941…` for three indices; the x402 gate answers **402** on `/prints`, `/curve/*`, `/vol/*`, `/seller-scores/*`; `/agent/challenge` and `/tca/human` both refuse correctly; the subgraph is at head with `hasIndexingErrors: false`; all 7 terminal proxies report `live: true`; six pages render clean; and **all 41 routes `endpoints.ts` advertises are served — zero mismatches**. Wired but empty: **HumanID** (`agentbook: "fixture"`, `app_id: test_app_62`, and the subgraph's `humanClusters`/`sellerWindowHumans` are EMPTY — no `resolve_humans` has run for a mainnet window), the **venue** (`desks: {}`), and **Model Armor** (`backend: gcp, screened: 0`) | — | the difference between deployed and exercised is written down |
| 7e | **Cold start measured at 87 s**, against `fetchLiveMeta`'s 5 s default and `/api/probe`'s 12 s. Earlier cold starts were 20–23 s, so the free tier has got slower. Consequence, stated rather than discovered: a first visitor arriving cold ALWAYS gets the archived edition, and the honesty tiers in `lib/connection.ts` (`WAKE_WINDOW_S` 120) are what make that legible. Raising the proxy timeouts would trade a fast honest archive for a slow blank page, so this is recorded, not 'fixed' | the `starter` upgrade is the real cure | nobody mistakes the archived edition for a broken one |
| 6b | Note `NEXT_PUBLIC_ACR_CHAIN_ID` is **not read anywhere** in `apps/terminal` — it was in this list but sets nothing. Left out of 6 above rather than carried forward | — | one less variable that looks load-bearing and is not |
| 7 | ~~subgraph~~ **DONE 2026-09-28**: `acr` v1.0.0-mainnet on account `1762718`, indexing from 23013298 with `hasIndexingErrors: false`, and `ACR_SUBGRAPH_URL` set — `/tca/{payer}` answers `available: true`. Published to the gateway as `N69YD8crrap…`; switching to it needs `ACR_GRAPH_API_KEY` | The Graph Studio | TCA reads answer |
| 8 | **PolicyWallet on mainnet: `0x3a5F444B0e4Ddc7FA6dcddA07A71d0710eea4e04`**, deployed 2026-10-16 (tx `0x92f789e7c7b488cc3d6fd4cbf7dfdd74c39b2b3e552f8eaa3d1751dfed88bc33`, block 26243704) with the replay-protected bytecode from #66 (`consumedDecisionHashes` answers; `usdc()` = `0x3600…0000`, read off `ACRFutures`, not defaulted). Configured by the deploy key: agent `0x1547B0d3…5379` (the press EOA, which is what the operator's `taker` role falls back to while every mainnet Circle wallet id is empty); budget `machine-services` = `category_id("machine-services")` `0xb52ff521…`, cap 1.00 USDC per 24 h, a bill must be under 0.10 or it escalates. **The owner is still the deploy key until `acceptOwnership()` is sent from `0xE804f54109b627205B31149965e534837D046dAd`** (`transferOwnership` tx `0x07f97b9280f1344578b7828cb757dcf5328d27b5fc04ea3b9731d83d419b7e79`). It holds no USDC yet. Not wired yet: no `businesses.json` row with `chain: "mainnet"`, the mainnet press image predates `/operator/*`, and no `ACR_TREASURY_*`. Deployed without `--private-key` in argv, by a runner that read the key inside its own process. **2026-10-16, visible:** #78 added the business `acr-mainnet` (treasury `0xE804…`, this wallet), and `acr-probe-free` runs `acr-api:2026-10-16-1609`. Mainnet `/spend` opens on "ACR on Arc mainnet" with the on-chain budget (1.00/day, bills under 0.10, `configured: true`, 0.0 held), read without a key. **Still the owner's:** swap `acr-api-mainnet` (Render dashboard → Settings → Image URL `docker.io/kaushtubh02/acr-api:2026-10-16-1609` → Save → Manual Deploy; preflights clean, no decisions on it, its 112 receipts all archived); `ACR_OPERATOR_AUTORUN` stays `off` | the owner: `acceptOwnership()` from `0xE804…`, then fund it | an agent can spend within a budget on mainnet, and nothing over it without the owner |
| 8a | **The futures venue's house wallets.** `ACRFutures` `0xdb5D7dbB67aD8EC8517982E95c89906abFBC6d3b` is owned by the deploy key `0xc2903b52a3Ad365fD237B78389a2FDe99e886999`, which is also the maker of every book it lists: `openSeries` takes the maker as a parameter, and the owner hand-over to `0xE804…` does not move an existing book's maker. The house taker is `0x352F01788236c22c1438C5198bE53E9c389989eD` (`ACR_FUTURES_TAKER_KEY`); `trade` refuses `msg.sender == maker`, so it must be a second wallet. A fill between the two is real and first-party: the terminal tags it `ours` on the tape and leaves the maker out of every trader count (`apps/terminal/lib/futuresBook.ts`, `HOUSE_WALLETS`; its test fails if a listed wallet is missing from these docs). | operator | no fill of ours reads as a customer |
| 8 | `make resolve-humans ARGS=--commit` for the current window | the resolver key | `humans.n` is not silently 0 |
| 9 | `make verify-mainnet` then `make verify-live` (its *revenue* section checks the 402, the facilitator's chain list, the public RPC, the balances route) | anyone | all ✓ |
| 10 | ~~Buy one thing yourself~~ **DONE 2026-09-28** — three purchases from the deploy key (not the treasury: Circle refuses `self_transfer`, so the press cannot buy from itself). One hit `/compute/…`, the benchmarkable route, and `/tca/0xc2903b52…` now answers `purchases: 3, benchmarked: 1, vw_slippage_bp: -1139.8` from the subgraph. Still worth doing from a **browser wallet that bridged in**, which is the path a visitor actually walks | operator, a personal wallet, ~$2 on Base | the five community steps, end to end, before anyone else tries |
| 10a | `PROBE_KEY=0x… SELLER=<mainnet api> make wallet-settle-probe` | a funded Gateway balance | the same settle without a browser, as a one-command regression check. Proven on testnet 2026-09-26: 402 → wallet signature → `success` + Gateway id → data served → **100 atomic USDC debited on chain after ~9 min** (Gateway's batch window) |
| 10b | **Run `uv run python scripts/archive_receipts.py` within minutes of any real purchase**, before the free instance restarts | operator | the settlement survives. It did not on 2026-09-28: three real mainnet receipts were lost when the container restarted, and because the ref mirrored on chain is `keccak("acr.gateway::" + tx_ref)` they cannot be reconstructed from it. `/tca` still knows about all three — the chain is the durable record — but `/revenue` will never count them |
| 10c | ~~The committed `fallback.json` shows a testnet venue~~ **FIXED 2026-09-28.** The bundle was regenerated against mainnet and now declares what is genuinely absent instead of carrying another chain's data: `absent.futures` and `absent.hedger`, each naming chain 5042. Three separate leaks were closed to get there — the venue carry (now refused when a fill sits past the chain's head, which is what caught it: 56.1M vs a head of 23.18M), the hedger's receipts (filtered by payer but never by chain, so seven `eip155:5042002` rows reached a mainnet panel) and `_merge_live_receipts` (the local buyer's capture, merged unfiltered). Audit after regenerating: `eip155:5042002` must not appear anywhere in `fallback.json` | — | the cold-start page describes this chain only |
| 11 | ~~Paste the first `postPrint` hash and the first receipt into `README.md`~~ **DONE** (first print and first benchmark are in the README); send the link with `COMMUNITY-TEST.md` | operator | launch |

**The guard is your checklist, and it has already run.** Resumed once on 2026-09-26 against the
pinned image, the service refused to start and said why — in production, not in a test:

```
MainnetGuardError: refusing to start on Arc mainnet (chain 5042) — 3 violation(s):
  - ACR_X402_PAY_TO is not an address: paid prints would be paid to nobody
  - ACR_HUMANID_APP_ID is unset: a proof scoped to no app authorizes nothing
  - ACR_ARC_RPC_URL is not a mainnet https endpoint (the public one is https://rpc.mainnet.arc.io…)
```

Exactly three, all of them secrets a repo cannot hold. Everything else in the mainnet profile was
accepted, so that list is the remaining work — when it comes back empty, the service boots. This is
audit finding H1 (both gates fail OPEN in `auto`) verified on the deployed artifact.

A tester's failure lands as a *Mainnet test* issue (`.github/ISSUE_TEMPLATE/mainnet-test.md`).
Read the sentence they pasted first: every path in the terminal fails with one, so a raw code or a
blank is a bug in the terminal even when the cause is elsewhere.
