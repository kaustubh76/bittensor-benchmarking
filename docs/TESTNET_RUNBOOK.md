# Testnet runbook — deploy, verify, and go live on Arc

The exact ordered operator sequence that takes ACR from "green offline test
suite" to "real contracts on Arc testnet, real signed prints, real x402
settlements on the tape". Every step is marked:

- **[OPERATOR]** — a human does it (keys, faucet clicks, `.env` edits, OTP).
- **[AUTOMATED]** — a make target / script does it; you only run the command.

> **Already live:** `ACROracle` `0x4f00e3BDd224F4c4b4958D54cD774E84B9092609` ·
> `AttestationRegistry` `0x23ae3E1A306824F0CBA0b6561cB7E5502f63dFb7` on chain
> `5042002`. Following §2–3 deploys a **new, divergent** contract pair — skip
> to §4 to run against the live ones instead. Cloud state:
> [`docs/DEPLOY.md`](DEPLOY.md); judge-facing status:
> [`hackathon/arc-circle-2026/SUBMISSION.md`](../hackathon/arc-circle-2026/SUBMISSION.md) (archived).

Chain facts this runbook is built on:

| fact | value |
|---|---|
| chain id | `5042002` (CAIP-2 `eip155:5042002`) |
| RPC | `https://rpc.testnet.arc.io` |
| explorer | `https://testnet.arcscan.app` |
| gas token | **USDC** — a native system contract at `0x3600000000000000000000000000000000000000` |
| faucet | `https://faucet.circle.com` → network **Arc Testnet** |
| x402 facilitator (testnet) | `https://gateway-api-testnet.circle.com` |

Related: [`docs/agent-runbook.md`](agent-runbook.md) (the buyer-agent detail
this runbook's step 5 condenses), [`IMPLEMENTATION_STATUS.md`](../hackathon/arc-circle-2026/IMPLEMENTATION_STATUS.md) (archived),
[`docs/SHIP-CHECKLIST.md`](../hackathon/arc-circle-2026/SHIP-CHECKLIST.md) (the submission-morning ritual and
the deadline's open items).

---

## 1. Fund the poster wallet

The oracle poster signs prints EIP-712 (domain "ACR Oracle" v1) with
`ACR_POSTER_PRIVATE_KEY` and relays them itself — so its address needs USDC,
which on Arc **is** the gas.

1. **[OPERATOR]** Derive the poster address from the key already in `.env`:

   ```sh
   uv run python -c "from acr_core import get_settings; from eth_account import Account; print(Account.from_key(get_settings().poster_private_key).address)"
   ```

2. **[OPERATOR]** Open `https://faucet.circle.com`, pick **Arc Testnet**, paste
   the address, request USDC.

3. **CHECKPOINT** — `https://testnet.arcscan.app/address/<poster address>`
   shows a non-zero USDC balance.

## 2. Deploy the contracts (Foundry)

Use the **same key** as the deployer: `ACROracle`'s constructor auto-authorizes
the deployer as a signer, so poster == deployer means no extra `setSigner` step.

1. **[OPERATOR]** Export the key (env only — never paste it into a file):

   ```sh
   export DEPLOYER_PRIVATE_KEY=0x<the ACR_POSTER_PRIVATE_KEY value>
   ```

2. **[AUTOMATED]** Simulate first — no broadcast, catches RPC/balance/compile
   problems for free:

   ```sh
   make deploy-testnet-dry
   ```

3. **[AUTOMATED]** Deploy for real and note the two addresses from the forge
   log (`ACROracle: 0x…`, `AttestationRegistry: 0x…`):

   ```sh
   make deploy-testnet
   ```

   If forge's EIP-1559 fee estimation fails on Arc, retry the same command
   with `--legacy` appended to the forge invocation:

   ```sh
   cd contracts && forge script script/Deploy.s.sol \
     --rpc-url https://rpc.testnet.arc.io \
     --private-key $DEPLOYER_PRIVATE_KEY --broadcast --legacy
   ```

4. **CHECKPOINT** — both `https://testnet.arcscan.app/address/<address>` pages
   show **contract code** (not an empty EOA).

## 2b. Deploy the futures venue (ACRFutures) against the existing oracle

The cash-settled futures venue (pillar 4) settles against the **already-deployed**
`ACROracle` — so it is deployed on its own (`DeployFutures.s.sol`), NOT via
`Deploy.s.sol` (which would redeploy the oracle/registry). It reads
`ACR_ORACLE_ADDRESS` from the env, uses Arc's native USDC (`0x3600…0000`) for
collateral, and 20% initial margin by default (`ACR_FUTURES_MARGIN_BPS`).

```sh
ACR_ORACLE_ADDRESS=0x<the live ACROracle> \
DEPLOYER_PRIVATE_KEY=0x<funded deployer> \
make deploy-futures            # add --legacy to the forge call if EIP-1559 estimation fails
```

Then **seed a live series** (opens a series, maker + taker post USDC collateral —
verifying Arc's USDC `transferFrom` — and put on a trade so the desk shows a real
book). Small multiplier keeps notional within testnet balances:

```sh
ACR_FUTURES_ADDRESS=0x<from deploy-futures> \
MAKER_PRIVATE_KEY=0x<deployer / maker> \
TAKER_PRIVATE_KEY=0x<a second funded testnet key> \
uv run python scripts/futures_seed.py
```

**Live deployment (2026-07-31):** `ACRFutures` = `0x29d97c629a8278f7ec4218ab0bd8baa9182642fe`
(arcscan `/address/0x29d97c629a8278f7ec4218ab0bd8baa9182642fe`), settling against
oracle `0x4f00e3BDd224F4c4b4958D54cD774E84B9092609`; series 0 (ACR-INF, multiplier
10) seeded with a live long/short book. Arc's native USDC supports standard
`approve`/`transferFrom` — collateral works.

**Propagation:** set `ACR_FUTURES_ADDRESS` on the Render seller (env-var →
`/terminal/data` gains a `futures` block + `chain.futures_address`) and rebuild
the image so the new `FuturesReader`/`/futures` code ships. The Vercel Terminal
needs **no** futures env — it reads the address purely from `/terminal/data`.

## 3. Point `.env` at the deploy

**[OPERATOR]** Edit `.env` and set exactly these lines — the value on the
**SAME line** as the `=`, with **NO inline comments** (dotenv reads a trailing
`# comment` as part of the value; the repo's `.env` has previously shipped a
known-broken `ACR_X402_PAY_TO` whose "value" was literal comment text — this
line replaces it):

```
ACR_ORACLE_ADDRESS=0x<ACROracle address from step 2>
ACR_REGISTRY_ADDRESS=0x<AttestationRegistry address from step 2>
ACR_FUTURES_ADDRESS=0x<ACRFutures address from step 2b — optional; enables the desk>
ACR_X402_PAY_TO=0x<seller wallet that receives USDC — the deployer EOA is fine>
ACR_X402_FACILITATOR_URL=https://gateway-api-testnet.circle.com
ACR_EXPLORER_BASE=https://testnet.arcscan.app
```

Only if the poster key is **not** the deployer key (skip otherwise): authorize
it as an oracle signer —

```sh
cast send <ACR_ORACLE_ADDRESS> "setSigner(address,bool)" <poster address> true \
  --rpc-url https://rpc.testnet.arc.io --private-key $DEPLOYER_PRIVATE_KEY
```

Then `make attest-once` (and `make seed-sellers` if on the arc tape) — the
registry starts empty.

## 4. Verify, serve, post the first prints

1. **[AUTOMATED]** Read-only preflight — chain id, bytecode at both addresses,
   poster `isSigner()`, per-index `latestPrint` ("no print yet" is expected
   before the first post), arcscan links:

   ```sh
   make verify-testnet
   ```

   Exits non-zero on any ✗ — including a still-unconfigured `.env`
   ("no oracle configured — run the deploy first").

2. **[AUTOMATED]** Serve the API, then **[OPERATOR]** check the health card:

   ```sh
   make api            # leave running
   curl -s localhost:8000/health
   ```

   Expect `"gate": "circle"` (the real x402 gate engaged) and
   `"oracle_configured": true` (and `"signer": "local"`). The production
   deployment signs via the Circle Developer-Controlled custody wallet instead
   (`ACR_CIRCLE_API_KEY` / `ACR_CIRCLE_ENTITY_SECRET` / `ACR_CIRCLE_WALLET_ID`
   — `/health` then shows `"signer": "circle"`).

3. **[AUTOMATED]** One estimator cycle → signed `postPrint` per index:

   ```sh
   make post-once
   ```

4. **CHECKPOINT** — the script prints one
   `https://testnet.arcscan.app/tx/0x…` per index and the tx pages show
   successful `postPrint` calls; the Terminal (`make terminal` →
   `/index/ACR-INF`) shows the **settled on-chain** mark with the tx link.

## 5. The buyer loop — live x402 nanopayments

The buyer agent needs its own funded EOA plus a Circle Gateway deposit
(detail: [`docs/agent-runbook.md`](agent-runbook.md)). The Terminal can also
be the buyer itself: its **LIVE buyer** (`ACR_BUYER_PRIVATE_KEY`, hard $0.01
cap) originates real Gateway settlements from the shop floor under `/curve` — see agent-runbook
§4b.

1. **[OPERATOR]** `make circle-login EMAIL=you@example.com` — email-OTP,
   testnet session (CLI v0.0.6 auto-provisions agent wallets on login).
2. **[OPERATOR]** `make buyer-key` then `make circle-wallet` — the x402 `exact`
   scheme is EIP-3009, signed with a **raw** key, but v0.0.6 `circle wallet
   create` only makes custodied agent wallets, so `buyer-key` mints a testnet
   EOA and `circle-wallet` **imports** it (`circle wallet import buyer
   --private-key`). Then `export AGENT_PRIVATE_KEY=0x…` (the same key — env
   only, never an argv).
3. **[OPERATOR]** Fund the buyer address at `https://faucet.circle.com`
   (Arc Testnet), or `make circle-fund ADDR=0x<buyer>`.
4. **[OPERATOR]** `make circle-deposit ADDR=0x<buyer>` — deposits USDC into
   Gateway (minimum **0.5**, chain `ARC-TESTNET`); the Gateway balance is what
   x402 spends.
5. **[AUTOMATED]** `make interop` (with `make api` still running) — 12
   field-level checks that our 402 parses exactly as `GatewayClient.pay()`
   parses it. Expect 12/12 PASS.
6. **[AUTOMATED]** `make agent-live` — 60 discovery-driven paid queries,
   $0.01 cap.
7. **CHECKPOINT** — the Terminal's shop-floor tape (under `/curve`) fills with settlements;
   receipts carry scheme `exact` and network **exactly** `eip155:5042002`.

## 5b. The Public Desk — a reader trades with their own wallet

The desk lets a visitor open a Circle **user-controlled** wallet (an SCA on
Arc, PIN-secured in Circle's hosted UI) and take a real position against the
maker. The key is derived and held client-side, so — unlike every other flow in
this runbook — **no server-side script can stand in for the ceremony**. The
only honest verification drives a browser.

1. **[AUTOMATED]** `make desk-preflight` — read-only gates: the series has
   life left, the oracle mark is fresh, the margin math leaves a tradable
   size, the maker can absorb it both ways, and the custody wallet (the faucet
   source) is funded. Exit 0 means clear to run.
2. **[OPERATOR]** `make api` and `ACR_API=http://127.0.0.1:8000 make terminal`.
   Hit `/api/futures` once first — a cold roster read can exceed the proxy's
   upstream timeout, and the desk renders read-only until it answers.
3. **[OPERATOR, once]** `npm i playwright && npx playwright install chromium`
   anywhere (Node ≥ 20). Playwright is deliberately not a repo dependency.
4. **[AUTOMATED]** `PLAYWRIGHT_DIR=<that>/node_modules make desk-e2e` — drives
   PIN setup → faucet → `approve` → `postCollateral` → `trade`, then writes
   `data/desk_e2e_last.json`. Set `DESK_PROFILE=<dir>` to reuse one browser
   profile (and therefore one wallet) across runs; the faucet is one drip per
   address, so a fresh profile per attempt burns the cap.
5. **[AUTOMATED]** `make desk-evidence USER_ID=<the id the run printed>` —
   confirms the run from two directions: Circle's own transaction ledger
   (states, hashes, fees) and the venue's `CollateralPosted` /
   `CollateralWithdrawn` / `Traded` logs plus a live `positionOf`.
6. **[AUTOMATED]** The exit is part of the flow, not an afterthought: the desk
   shows a withdraw button per series a wallet holds collateral in — including
   expired and settled ones, because collateral outlives the market it was
   posted to and a roll leaves a returning reader holding a stake in the old
   series. `DESK_MODE=withdraw DESK_USER_ID=<id> make desk-e2e` drives only
   that step, which is how a wallet whose browser profile is long gone gets its
   money back (the PIN is still required, so this grants nothing new).

### Keeping the venue alive

> **A workflow file on a feature branch is not a scheduled job.** GitHub runs
> `cron` **only** from the default branch. These workflows sat on
> `feat/onchain-futures` for days and never fired once — the book was alive
> only while somebody ran the loop locally. If you add or move a scheduled
> workflow, confirm it by finding its **runs**
> (`gh run list --workflow <file>`), never by reading its cron line.
>
> Free-tier `cron` is also **late**, and it **drops ticks**. The first real
> heartbeat ran the `21:00` slot at `21:51`, and the observed gaps between
> ticks were 59m, 63m, 150m and 209m. Tens of minutes of delay is normal;
> don't read a missing run in the first half hour as a broken schedule — and
> don't size anything (like the trade tape's window) on the nominal cadence.

A series expires. Two idempotent commands own that, and
the in-process keeper runs them; `.github/workflows/futures-lifecycle.yml` is the dispatch-only fallback (it used to run hourly at `:17`, offset
from the heartbeat so they never race on the maker's nonce):

- `make futures-roll` — opens a successor when the current series is running
  out. No-ops on a healthy venue, guards the maker's gas budget before any
  write, and **exits non-zero unless the collateral actually landed**. Do not
  use `scripts/futures_seed.py` for this: it exits 0 after failing that step,
  leaving an open series no one can trade against.
- `make futures-settle` — cash-settles anything expired, which is what flattens
  positions and frees each trader's whole cleared balance for withdrawal. It
  refuses (rather than reverting) when the oracle print is outside the
  contract's 2 h freshness window; that window reopens on the next print, so
  the fix is to wait, not to force it.

### Getting the project's own collateral back out

Collateral is **per series**, so a roll leaves the previous stake behind on a
series where nothing trades and nothing reclaims it. Readers have the desk's
withdraw button for this; the keys that run the book (the heartbeat taker, the
maker) live only as repository secrets, so they get a workflow:

```sh
make futures-withdraw                       # dry run: what is free, everywhere
WITHDRAW_DRY_RUN=0 WITHDRAW_SERIES=0 make futures-withdraw
```

or `futures-recover.yml` → **Run workflow** (dispatch-only, `dry_run` defaults
to true). **Run the dry pass first and read it.** Without `WITHDRAW_SERIES` the
job takes the free margin out of the *live* series too — during the 2026-08-02
recovery that would have pulled 1.904305 USDC of working collateral out from
under the heartbeat.

The amount is computed by the same `free_collateral_units` the desk quotes to
readers, so a run doubles as a check on the product. Each withdrawal must show
four independent witnesses agreeing — the contract's record, the venue's USDC,
the recipient's USDC, and the `CollateralWithdrawn` event — because a mined
transaction is not evidence that money moved:

```
  series 0 (ACR-INF, settled): holds 6.096010 USDC, free 6.096010
    tx 0x09264fc62a69be7394f8a962dce84a5f393115bc7c4dca64e38ed5718bce1875
    contract balance  6096010 → 0 units
    venue USDC        11.051299 → 4.955289
    wallet USDC       8.629074 → 14.723416  (gas is paid from this)
    CollateralWithdrawn event  6096010 units
    ✓ all witnesses agree
```

### The first live run — 2026-08-01, series 0 (ACR-INF)

Wallet `0x95DE70736E21e70DF921Fb3ab91dD56750965b59` (Circle user
`acr-desk-1gkdjcmj`), driven entirely through the browser under a PIN:

| Step | Transaction |
|---|---|
| faucet drip (0.5 USDC, from custody) | [`0xb6ef0981…`](https://testnet.arcscan.app/tx/0xb6ef098134690045ebf4012c8d2e28573d1c45d499e859efbf22efabb7d41572) |
| `approve` USDC → the venue | [`0xb4ed94b9…`](https://testnet.arcscan.app/tx/0xb4ed94b9c5c6fdef97fdce407e05b1b56405b66b20a229cccab3f49b9d0998c3) |
| `postCollateral` 0.50 USDC | [`0x272687b1…`](https://testnet.arcscan.app/tx/0x272687b15c31110cb7e64b4dfb540c49fac15bb7095f89ba9bf263c8101a1916) |
| `trade` **+0.46 @ 0.48450** | [`0x0888fb1b…`](https://testnet.arcscan.app/tx/0x0888fb1bdf73313ecc9b9374c66c6cd70c9fb5813ae03f163450eecf26237876) |

`positionOf(0, wallet)` reads back `0.46 contracts @ 0.48450`.

Three facts this run settled, each previously assumed:

- **Gas Station sponsors these SCAs.** The authority is the ERC-4337
  `UserOperationEvent`'s `paymaster` field — here
  `0x7cea357b5ac0639f89f9e378a1f03aa5005c0a25`, i.e. sponsored. Do **not** read
  Circle's `networkFee` as a user debit: it is non-zero on every one of these
  operations and the wallet still paid nothing. The balance agrees — 0.500000
  in, 0.5 posted as collateral, 0.000000 left.
- **Every desk action is a smart-account user operation**, routed through the
  EntryPoint at `0x5FF137D4b0FDCD49DcA30c7CF57E578a026d2789` rather than a
  direct call.
- **$0.50 buys well under one contract** on a 10× index (~0.9955 USDC of
  initial margin per contract at a 0.4977 mark) — which is why the desk quotes
  a server-computed size (here 0.46) instead of a hardcoded 1. A "BUY 1" button
  would have reverted `taker margin` *after* the reader entered their PIN.

## 6. Bake the real artifacts into the offline bundle

**[AUTOMATED]** With the oracle configured in `.env`:

```sh
make snapshot
```

Regenerates `apps/terminal/lib/fallback.json` through the same code path that
serves `/terminal/data`, so the offline Terminal edition now ships real
on-chain provenance instead of the chain-agnostic placeholder.

**Re-run this after every roll or settle.** The bundle is the tier served when
everything else is down — the one a judge on a cold free-tier box is likeliest
to hit — so a stale one misrepresents the venue to exactly the wrong audience.
It sat for a day describing series 0 as `settled: false` with `open_interest:
3.0` after that series had settled. Check the output names the *live* series:

```sh
python3 -c "import json,time; d=json.load(open('apps/terminal/lib/fallback.json'));\
 [print(k, v['series_id'], v['settled'], round((v['expiry_ts']-time.time())/3600,1),'h')\
  for k,v in (d.get('futures') or {}).items()]"
```

### Arc's `eth_getLogs` limits — three separate walls

The trade tape reads one event over a block range, and Arc constrains that in
three ways that look alike and are not:

1. **A string `toBlock`.** `"latest"` over a wide range returns 413; the *same*
   range with a numeric `toBlock` is accepted. Both readers pass numbers.
2. **A hard range cap of ~15000 blocks.** Measured by binary search: 14843
   answers, 15000 returns 413, regardless of how few logs match. This is a range
   limit, not a response-size limit, so it cannot be dodged by asking for a
   sparser filter. **The tape's reach can only be extended by paging** — asking
   for a wider window just fails every time and silently falls back.
3. **429 throttling.** Looks like the others and is nothing like them: a
   narrower range is no cure, because the request was never too big. Code that
   narrows on a 429 walks its cursor a few hundred blocks per attempt and
   destroys its own reach while paying full retry backoff — see
   `_is_range_error`, which exists to keep these apart.

---

## Risks & fallbacks

- **Gateway settle refs may be UUIDs, not tx hashes.** Gateway batches
  nanopayments; each payment gets a settlement *reference* and the on-chain
  batch tx lands separately. Don't paste a UUID into arcscan `/tx/` — the
  honest transaction story is "N x402 payments + Gateway batch settlements +
  the oracle's own `postPrint` txs".
- **EIP-1559 estimation can fail on Arc** — retry the forge script with
  `--legacy` (step 2). The Makefile target's comment carries the same note.
- **If live fails mid-demo, nothing bricks:** the UI degrades to the sim tier
  automatically — offline-tolerance is a repo invariant (dev gate, simulator
  tape, offline poster markers). Fix the credential/RPC issue and re-run the
  step; nothing needs resetting.
- **`verify_deploy.py`'s "last post" link is best-effort** — it scans the most
  recent ~50k blocks of `PricePosted` logs and skips quietly if the public RPC
  declines the range. The per-index `make post-once` output is the
  authoritative tx list.
- **Faucet rate limits**: the Circle faucet caps requests per address per day;
  fund both the poster and the buyer early.

### The ceremony on a SMALL book, proven on production — 2026-08-05, series 4 (ACR-GPU)

Wallet `0x86904D879e12e79187a8671A202ADd6b3bDc7bA5` (Circle user
`acr-desk-bbcb1779…`), driven against the **live production site** — the first
clean run since the milestone predicates began asserting chain state instead
of page copy:

| Step | Result |
|---|---|
| PIN ceremony | SCA created through Circle's hosted UI |
| faucet | 0.5 USDC from custody |
| `approve` + `postCollateral` | 0.05-sized GPU stakes on series 4 (per-book reader sizing) |
| `trade` | **long 2.00 @ 0.01083** — [`0x40e0c510…`](https://testnet.arcscan.app/tx/0x40e0c5103daec78b05103dfbeca6e8ba56e8a49b9f8f7d9222bba7d6e62f5da9) |
| `withdrawCollateral` | free 0.0538 → 0.0027 — [`0xcd8ef4f1…`](https://testnet.arcscan.app/tx/0xcd8ef4f1808e0078df3c5ec3213b1ebeb1a5cdc1de22a873a17cd1e27078bbaa) |
| evidence | `make desk-evidence` → **CONFIRMED ON-CHAIN**: `positionOf(4)` = 2.0 @ 0.010831, Circle ledger 8 COMPLETE ops, Gas Station paymaster `0x7cea357b…` paid |

**The bug this run found — judge-facing, invisible on the big book:** the
resume path only asked the venue about posted collateral when the wallet was
*empty*, which is only true on ACR-INF (the whole 0.50 converts). On ACR-GPU a
reader posts 0.05 and keeps 0.45 — so a returning GPU reader was re-offered
"post collateral", and a second click posts a second stake. Fixed (the venue
check now runs whenever localStorage doesn't say collateralized) and deployed.
The harness itself had three lies of its own fixed the same afternoon —
mangled PIN entry burning real attempts, a hardcoded ACR-INF collateral read,
and a poll that turned its own rate-limiting into "0 collateral"; see
`scripts/desk_e2e.mjs`'s comments.

### The full ceremony, proven on production — 2026-08-02

Wallet `0xdd2112121d004c780d714d59736db13b663b31e0` (Circle user
`acr-desk-96edcb21…`), driven end to end through the browser under a PIN:

| Step | Result |
|---|---|
| PIN ceremony | SCA created — Create PIN → Re-enter → Recovery method → questions → Summary → typing **"I agree"** |
| faucet | 0.5 USDC from custody |
| `approve` + `postCollateral` | wallet 0.5 → 0.0, venue holds 0.5 on series 1 |
| `trade` | **long 0.45 @ 0.49533** on chain |
| `withdrawCollateral` | free 0.0515 → 0.0026; wallet 0.051489 |

The withdrawal takes only the **free** margin, not the whole stake: 0.45
contracts pin 0.448511 USDC as initial margin, exactly as the contract
requires. That is the desk working correctly, not a partial failure.

**Two bugs this run found, both invisible to endpoint checks:**

1. `refreshWallet` decided the phase from **spendable** balance, so a reader
   who posted their whole stake (0.00 left — the ordinary happy path) was
   demoted to "unfunded" on the next poll and shown no trade button, while
   `/desk/limits` was offering `max_buy: 0.45`. Fixed in
   `apps/terminal/lib/deskPhase.ts`, now a pure tested function.
2. The withdraw button showed the **pre-trade** free amount for a moment after
   a fill — offering "WITHDRAW 0.50 USDC" when 0.05 was free. The server
   re-reads and withdraws the right amount, so no money was at risk, but the
   button was promising what the venue would refuse.

Both survived earlier runs because the first live trade happened in the same
page session as the deposit. **Re-run `make desk-e2e` after any change to the
desk's phase machine** — endpoint checks cannot see either of these.

## The weekly human resolution — and why it is stuck

`HumanIdMirror` mints a cluster id **per 7-day window**, so a wallet resolved in
window *N* has no cluster in *N+1* and nothing errors when it rolls. The chore is
`make resolve-humans ARGS=--dry-run`, then `ARGS=--commit`. Current window is
**2962** (windows are absolute — `floor(unix / 604800)` — and roll every Thursday
00:00 UTC).

**As of 2026-10-10 neither signer can run it, and the two failures look nothing
alike.**

1. **The Circle poster wallet is refused.** The dry run dies in
   `DeveloperAccountApi->get_public_key` with `{"code":3,"message":"Forbidden"}`,
   before it reads anything on chain. `build_role_signer("poster")` prefers the
   role's own Circle wallet whenever `ACR_CIRCLE_WALLET_ID` is set and
   deliberately will **not** fall back to the ambient `ACR_POSTER_PRIVATE_KEY`
   (`signer.py:290-298` records why: that ambient key is how every venue script
   came to sign as a raw EOA on a host with full Circle credentials).
2. **The local key is not authorized.** Blanking `ACR_CIRCLE_WALLET_ID` for one
   invocation does reach `ACR_POSTER_PRIVATE_KEY` — address
   `0x1547B0d3C334F264D28B087566B512AB5CD85379`, which is the **mainnet** press
   wallet — and the script stops with *"is not in the mirror's signer set. Every
   `record()` would revert 'bad signer'."*

Either one of these closes it:

```bash
# (a) restore the Circle credential, then:
make resolve-humans ARGS=--dry-run && make resolve-humans ARGS=--commit

# (b) or the mirror's owner (0x33189c643774ED2713EbFf5A6923e5fa42b96eE8)
#     authorizes the local poster once:
cast send 0x7f41faA38F35F1FABfc76Df5B1618fC8d0c0d8e5 'setSigner(address,bool)' \
  0x1547B0d3C334F264D28B087566B512AB5CD85379 true \
  --rpc-url $ACR_ARC_RPC_URL --private-key <owner key>
```

**Check the mirror is on the chain you think before believing "not
authorized".** `.env` is a mix — `ACR_ARC_CHAIN_ID=5042002` with a testnet RPC,
but a mainnet poster key — and a wrong mirror address produces the *same*
refusal for an entirely different reason. `eth_getCode` settles it:
`0x7f41faA38F…` holds **3064 bytes on testnet and 0 on mainnet**, so the
configured address is right for the chain in play.

**What it costs while it is stuck.** `/api/humanid` serves `n: 0` and the
dateline's humans chip vanishes — and in `acr-mcp`, `wallet_tca("me")` returns
`wallet_count: 0` *with the proof accepted*: `/humanid/info` `verified_proofs`
climbs, the human's cluster comes back (`0x252e31d6…`, window 2962), and no
wallets are mapped to it. An accepted credential over an empty answer is the
most confusing shape available, so `npm run smoke` reports it as "proof
accepted; no wallets resolved in window N — run resolve_humans" rather than as a
pass. Run the chore **before** the tape moves on: `Settlement.human` is stamped
at finalize and the entity is immutable, so a payer that settles while
unresolved counts as non-human for ever.
