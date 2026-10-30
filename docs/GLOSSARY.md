# ACR — Plain-English Glossary

> The diagram (`acr_architecture.excalidraw`) and the code are dense with jargon.
> This file explains every term in one line, with an everyday analogy. If you
> read only two things, read **"The whole thing in one paragraph"** and the
> **plain-words ①→⑩ walkthrough** at the bottom.
>
> **Live in the product:** the Terminal renders this glossary at
> [arc-compute-rate.vercel.app/companion](https://arc-compute-rate.vercel.app/companion)
> ("The Reader's Companion"), and the masthead's one-click **plain** edition
> re-sets every page of the paper in this register — same numbers, plain words
> (`apps/terminal/lib/plainGlossary.ts` is the distilled, coverage-tested subset).

---

## The whole thing in one paragraph

Machines are starting to buy services from each other (AI inference, GPU time,
data) and pay in digital dollars (USDC) on a blockchain called **Arc**. Those
payments are messy: they arrive in delayed batches, and cheaters flood in fake
trades to make a price look higher or lower than it really is. **ACR** watches
all those payments and computes the *honest going rate* for each service —
carefully, so that fakes barely move it — then publishes that rate on-chain so
other contracts can trust it and settle against it (the way loans settle against
an interest-rate benchmark like SOFR). It also prints, next to every rate, the
literal dollar cost an attacker would have to burn to nudge it — *"here's the
bill."* Same idea as an official financial benchmark, but for machine commerce,
and it proves its own tamper-resistance.

---

## Benchmark & economics

- **Latent price / estimand** — the *true* price you can't see directly, only
  estimate from noisy data. *Like guessing a room's real temperature from a few
  cheap, laggy thermometers.*
- **Benchmark / reference rate** — one agreed, official number that lots of
  contracts settle against. *Like the "official" exchange rate a bank quotes.*
- **SOFR / LIBOR** — real-world interest-rate benchmarks banks settle loans on;
  ACR is the same idea for machine services. *The "prime rate," but for compute.*
- **VWAP (volume-weighted average price)** — a plain average that weights each
  trade by its size. Easy to fool: print huge fake volume and it follows you.
  *Like averaging house prices — one staged $10M sale drags the average.*
- **Hedonic adjustment / constant-quality (Case-Shiller)** — adjust prices so
  you compare like-for-like quality. *A cheap studio and a luxury penthouse both
  "sold" — strip the quality difference to get the true market move.*
- **Basis point (bp)** — one hundredth of one percent (0.01%). *A penny on a
  hundred dollars.*
- **Numeraire** — the unit prices are measured in (here, USDC / dollars). *The
  "ruler" you measure value with.*
- **Term structure / the curve** — prices for the same thing at different future
  dates. *Airfares for the same route next week vs next month.*

## Estimator & statistics (the four pillars)

- **Positioning** — where ACR sits in the market and why: a rate's administrator must not be
  the rail's operator (the LIBOR lesson), and a benchmark is published methodology-first,
  liquidity-second (the SOFR path). It is the reasoning on the canvas's *why a benchmark* card,
  not a feature. *Knowing which shelf the product goes on before building the product.*
- **Observation model / state-space** — a math model that says *"what we see =
  the true signal, distorted and delayed, plus noise."* *A muffled phone call:
  model the muffling so you can recover the words.*
- **Convolution / batching operator (H)** — the smearing/mixing of the signal
  over time because settlements arrive batched, not instantly. *Trades get
  blurred together like a long-exposure photo.*
- **Deconvolution** — undoing that smear to recover the sharp, true signal.
  *Un-blurring the photo.*
- **Kalman filter** — the standard algorithm that recovers a clean, current
  estimate of a hidden signal from noisy, delayed measurements. *Your phone's
  GPS blending laggy fixes into one smooth position.* (Pillar 1)
- **RTS smoother (Rauch–Tung–Striebel)** — a second backward pass over the Kalman
  filter that also uses *later* data to sharpen *earlier* estimates. *Re-reading
  a sentence's start once you've seen how it ends.*
- **batch operator (H)** — the mathematical object that describes how settlements
  smear trades together; the model treats it as *known* so it can be inverted.
  *The exact recipe of the blur, so you can un-blur it.*
- **WLS (weighted least squares)** — a regression that lets bigger trades count
  more than tiny ones. *Averaging exam scores but weighting the final heavier
  than a quiz.*
- **VWM (volume-weighted median)** — shorthand for the trimmed weighted median ACR
  uses; the middle value weighted by trade size. *The "typical" price, by dollars
  not by count.*
- **constant-quality rate** — the price after quality differences are stripped
  out, so it only moves when the *market* moves. *Same-model, same-mileage car
  price, tracked over time.*
- **leave-one-community-out (LOCO)** — a robustness check: drop each detected
  cluster in turn and see how far the rate moves. *Re-tallying the vote with each
  precinct removed to see who swings it.*
- **N\*** — in the attack-cost formula, the minimum wash volume (in USDC) that
  would actually move the rate by the target amount. *The smallest bribe that
  changes the outcome.*
- **fee_bp / fee_flat** — the two parts of Arc's fixed USDC transfer fee (a per-
  dollar rate + a flat per-transfer charge) that make the attack cost a number.
  *A card fee of "2% + 30¢."*
- **Volume-time bars** — sample the tape by equal dollars traded, not by equal
  clock time, so busy and quiet periods count evenly. *Weigh flour by grams, not
  by "one scoop."*
- **α-trim (trimmed) weighted median** — throw away the most extreme few percent
  on each side, then take the middle value weighted by size. Very hard to drag.
  *Olympic scoring: drop the highest and lowest judges, keep the middle.*
- **Breakdown point (½)** — how much of the data can be pure garbage before an
  estimator can be pushed anywhere. The median tolerates up to 50%. *You'd need
  to rig half the votes to flip the winner.*
- **(Weighted) bootstrap confidence interval (CI)** — resample the data many
  times to get an honest "the rate is X, give or take Y" range. *Re-polling a
  crowd repeatedly to see how much the answer wobbles.*
- **Funding graph** — the who-paid-whom network of addresses. *A map of arrows:
  who sent money to whom.*
- **Sybil / sybil cluster** — many fake identities secretly controlled by one
  attacker. *One troll with a hundred sock-puppet accounts.*
- **Wash trade** — a fake trade with no real economics, done to inflate volume or
  price. *Selling your car to yourself repeatedly to fake a "hot market."* The
  reference attacker emits three flavors the cleaning stack is built to catch:
  - **self-deal** — buyer and seller are the *same* address.
  - **wash-cycle / reciprocal funding** — money loops A→B and B→A so net flow is
    ~zero but volume looks real. *Two people passing the same $20 back and forth.*
  - **pure-sybil (ring)** — a tight cluster of fresh fake identities trading only
    with each other. *A room full of sock-puppets clapping for one another.*
- **Louvain community detection** — an algorithm that finds tightly-knit clusters
  in a graph — used to spot sybil rings. *Spotting friend-groups on a social
  network by who talks mostly to whom.*
- **Cluster caps** — no single cluster's volume may count for more than a fixed
  share of the window. *No one voter gets to cast 40% of the ballots.*
- **Robustness diagnostics (flip fraction, LOCO)** — per-rate checks: "can one
  capped cluster even flip this?" and "how much does dropping any one community
  move it?" *A stress test printed on the label.*
- **Manipulation cost bound (attack-cost per bp)** — the least USDC an attacker
  must burn to move the rate by one basis point, computed against a
  cleaning-evading attacker. Prints next to the rate. *"Moving this number 0.01%
  costs you $X" — the bill, on the tag.* (Pillar 3)
- **OU process (Ornstein–Uhlenbeck)** — a "wandering-but-pulled-back-to-normal"
  random process the simulator uses for the true price. *A dog on an elastic
  leash: it drifts but keeps getting tugged back.*

## Crypto / Circle / Arc

- **Arc** — Circle's blockchain built for payments, where **USDC itself is the
  gas** (fee) token. *A toll road where you pay tolls in the same dollars you're
  already carrying.*
- **chain id 5042 / CAIP-2 (`eip155:5042`)** — the network's numeric address;
  CAIP-2 is a standard way to name a chain. ACR runs on Arc **mainnet**, 5042;
  Arc **testnet** is 5042002, where this was proven first. *A phone country code,
  but for blockchains.*
- **testnet** — a practice copy of a blockchain using fake-value tokens, for
  building safely before going live. *A flight simulator before the real plane.*
- **USDC** — Circle's regulated dollar stablecoin (1 USDC ≈ $1). On Arc it's a
  *native system contract* at `0x3600000000000000000000000000000000000000` and is
  itself the gas token. *A digital dollar bill that also pays its own postage.*
- **Malachite** — Arc's consensus engine (the software that lets all the
  computers agree on order). *The referee crew that decides what officially
  happened.*
- **finality / Malachite finality / no reorgs** — once Arc confirms a
  transaction it is **permanent within a second and never reversed** ("reorg" =
  the chain rewriting recent history, which can't happen here). This is why the
  tape's timestamps are trustworthy enough to do the math on. *The whistle blows,
  the goal counts, and no replay can take it back.*
- **Circle Gateway** — Circle's rail that pools and settles USDC across chains.
  *The clearing house all the payments flow through.*
- **Agent Marketplace** — Circle's directory where AI agents discover and pay for
  services (ACR lists itself there). *An app store for machine-to-machine
  services.*
- **SLO (service-level objective)** — a promised performance level, e.g. "99% of
  responses under 250 ms" — one of the quality features ACR adjusts for. *The
  "delivered in 30 min or it's free" promise.*
- **x402** — the HTTP "402 Payment Required" standard: pay per API call. *A
  turnstile that takes a coin before it lets you through.*
- **Nanopayments** — sub-cent payments (fractions of a cent per call). *Dropping
  a tenth of a penny in the meter each time.*
- **EIP-3009 ("transfer with authorization")** — pay by signing a message, no
  separate approval transaction. *Signing a check instead of moving cash twice.*
- **EIP-712** — the standard for signing structured, human-readable data so a
  contract can verify who signed. *A notarized form with a verifiable signature.*
- **Oracle** — an on-chain contract that publishes off-chain facts (here, the
  rate) for other contracts to read. *The stadium scoreboard everyone trusts.*
- **Attestation / AttestationRegistry** — a signed statement of a seller's
  metadata (model class, latency), stored on-chain to feed the quality
  adjustment. *A verified badge on a seller's profile.*
- **x402 Facilitator (Dev / Circle)** — the service that verifies and settles an
  x402 payment (`/verify` + `/settle`). "Dev" is a local mock; "Circle" is the
  real Nanopayments one. *The payment terminal that approves the card.*
- **Signer (Local / Circle wallet) / custody** — who holds the key and signs the
  rate. Local = a raw dev key; Circle wallet = a managed (custodial) key. *Your
  own house key vs a key the bank holds in a vault for you.*
- **Foundry / invariant tests / `fail_on_revert`** — the Solidity toolkit and its
  tests; *invariants* are rules that must ALWAYS hold no matter what. *"The scores
  can never go negative" — checked against every possible play.*
- **Gas / gasless** — the fee to run a transaction. On Arc it's paid in USDC, so
  users never need a separate coin. *Paying the postage in the same currency as
  the goods.*

## On-chain & payments mechanics

- **`.sol`** — a Solidity source file, i.e. a smart contract's code (`ACROracle.sol`,
  `AttestationRegistry.sol`). *The recipe card for an on-chain vending machine.*
- **`postPrint`** — the oracle function that publishes a new rate on-chain. *Pinning
  today's number to the public board.*
- **relayer** — whoever *submits* the signed rate transaction; because the contract
  checks the *signature*, the submitter needn't be the signer. *A courier can drop
  off a sealed, signed envelope — the seal is what's trusted, not the courier.*
- **signature verification (verifies the signer)** — the contract recovers who
  signed and checks it's authorized, instead of trusting who sent it. *The bank
  checks the signature on the check, not who walked it in.*
- **MAX_TS_SKEW** — a guardrail: a rate whose timestamp is too far in the *future*
  is rejected, so a fat-fingered date can't jam the feed. *Refusing a check
  post-dated to the year 3000.*
- **isStale / staleness / latestPrintWithAge** — a way for readers to ask "how old
  is this rate?" and refuse to settle on a stale one. *Checking the milk's
  best-by date before you drink it.*
- **monotone timestamps** — each new rate must be strictly newer than the last.
  *Page numbers that only ever go up.*
- **pause / setPaused** — an emergency stop that halts new posts. *The big red
  "stop the line" button.*
- **2-step ownership (transfer)** — handing over admin control needs the new owner
  to *accept*, so you can't send it to a wrong/dead address. *A certified letter
  that only counts once the recipient signs for it.*
- **nonce** — a per-seller counter included in a signed message so an old signature
  can't be reused. *A one-time code that expires the moment it's used.*
- **deadline** — an expiry timestamp on a signed message. *A coupon with a "use by"
  date.*
- **replay / replay-proof** — "replay" = re-submitting an old signed message to
  cheat; the nonce + deadline make that impossible. *A movie ticket that can't be
  scanned twice.*

## Services & interfaces (the code that runs it)

- **TapeSource / SimSource / ArcSource** — one common "data-in" interface with two
  implementations: `SimSource` (the simulator) and `ArcSource` (real Arc testnet).
  The estimator doesn't care which. *One faucet handle; the water can come from
  the tank or the mains.*
- **offline-tolerant** — if no chain/credentials are configured, the code quietly
  falls back to the simulator instead of crashing. *A GPS that still shows the map
  when it loses signal.*
- **FastAPI** — the Python web framework serving the index API. *The waiter that
  takes requests and brings back data.*
- **lifespan** — a startup/shutdown hook; here it launches the background refresh +
  poster loop when the API boots. *Flipping the "open" sign and starting the
  coffee machine when the shop opens.*
- **poster loop** — a background job that re-estimates and posts a fresh rate on a
  schedule (hourly). *The clock tower that chimes every hour on its own.*
- **x402-gated** — an endpoint that returns "402 Payment Required" until you pay.
  *A paywall on an article.*
- **`/verify` + `/settle`** — the facilitator's two steps: check the payment is
  valid, then actually move the USDC. *Authorize the card, then charge it.*
- **`/onchain` (reader)** — an API endpoint that returns the rate *read straight
  from the blockchain* (not the freshly computed one). *Reading the number off the
  official public board, not your own notes.*
- **DevFacilitator / CircleFacilitator** — the mock (local, for testing) vs the
  real (Circle Nanopayments) payment verifier. *A toy cash register vs the real
  card terminal.*
- **LocalKeySigner / CircleWalletSigner / `build_signer`** — sign with a raw local
  key (dev) or a Circle-managed custodial wallet (prod); `build_signer` picks the
  right one from config. *Sign with your own pen, or have the bank's vault sign
  for you.*
- **PAYMENT-REQUIRED / PAYMENT-SIGNATURE / PAYMENT-RESPONSE** — the three x402 HTTP
  headers: the server's price challenge, the client's signed payment, and the
  server's receipt. *"That'll be $2" → you tap the card → here's your receipt.*
- **fail-closed** — if a payment can't be verified, deny access (the *safe*
  default). *A door that locks itself when the power's out, not swings open.*
- **SSR (server-side rendering)** — the web page is built on the server and sent
  ready-to-read, so it loads fast and works without heavy client JavaScript. *A
  meal delivered plated, not as raw ingredients to cook yourself.*
- **settlement-grade** — trustworthy and well-guarded enough that other contracts
  can safely settle money against it. *"Bank-grade," but for a published number.*
- **manipulation-resistant** — built so fakes and attackers can barely move it.
  *A scale you can't fool by leaning on it.*
- **benchmark-native (institution)** — a firm whose core business is rates/indices
  (ICE, Apollo, BNY, Mastercard) — the natural customers/judges for ACR. *People
  who run scoreboards for a living.*
- **ICE / IBA (ICE Benchmark Administration)** — the company that officially
  administers LIBOR; it's on Arc's testnet roster, i.e. a realistic ACR adopter.
  *The org that keeps the "official interest rate" — a perfect customer.*
- **ABC (abstract base class)** — a code "interface" that lists methods every
  implementation must provide (e.g. `TapeSource`). *A job description every hire
  must fulfil.*
- **reprice** — recompute each trade's price at a reference quality (what the
  hedonic step does). *Re-quoting every sale as if it were the standard model.*
- **red-team** — deliberately attacking your own system to prove it holds up.
  *Hiring burglars to test your own locks.*
- **paper-traded** — simulated trading with no real money at risk (an acceptable
  cut for the future). *Playing poker with matchsticks.*
- **deflator** — a hidden factor that quietly shrinks a measured value — e.g. a
  volatile gas token baked into a price; ACR dodges it by pricing purely in USDC.
  *Measuring height with a ruler that keeps shrinking.*
- **conftest / hermetic tests** — `conftest.py` wires up the test suite; *hermetic*
  means tests run sealed off from any local config, so they give the same result
  everywhere. *A cleanroom: same inputs, same output, every time.*
- **CI (continuous integration)** — automation that runs the tests/gates on every
  change. (Note: "CI" also means *confidence interval* elsewhere — context tells
  which.) *A robot that re-checks your homework each time you edit it.*

## Agentic economy & Circle live-wiring (the demand side)

- **agentic economy** — a market where the buyers and sellers are software agents,
  not people. *A bazaar where the shoppers are robots.*
- **buyer agent** — a program that autonomously finds services and pays for them
  (`apps/agent`, TypeScript, using **viem** — a TS Ethereum library). *A robot
  shopper with its own wallet.*
- **DevPayer / GatewayPayer** — the agent's two payment modes: a local mock vs the
  real Circle path. *A pretend till vs a real card machine.*
- **GatewayClient / `@circle-fin/x402-batching`** — Circle's client library the
  agent uses to make batched x402 payments. *The pay-app on the robot's phone.*
- **GatewayWallet / GatewayWalletBatched** — the Circle smart contract that holds
  pooled USDC and that x402 payments are signed *against* (EIP-712 domain name
  `GatewayWalletBatched`; testnet address `0x0077777d7EBA4688BDeF3E311b846F25870A19B9`).
  *The shared prepaid account the turnstile debits.*
- **facilitator endpoint** — the real Circle host `gateway-api.circle.com` on
  mainnet (`gateway-api-testnet.circle.com` on testnet) with `POST /v1/x402/verify`
  and `/v1/x402/settle`; scheme `exact`. *The card network's authorize-then-charge
  API.*
- **scheme `exact`** — the x402 payment scheme that pays an exact amount via
  EIP-3009. *Paying the precise sticker price, no haggling.*
- **Agent Marketplace endpoints** — `/marketplace/catalog` (the **catalog** of
  payable services) and `/marketplace/receipts` (a **ledger** of paid queries).
  *A menu, and the till roll of everything sold.*
- **Bazaar / Bazaar-shaped** — the format of Circle's Agent Bazaar (its
  marketplace for agent-payable services); our catalog is shaped to match it so
  agents can discover ACR the standard way. *Listing on the same shelf format the
  big store uses.*
- **x402Version 2 (`resource` + `accepts`)** — the version and JSON shape of the
  402 challenge body that buyer SDKs parse: what resource you're buying and which
  payment methods it `accepts`. *The vending machine's little screen saying what
  it sells and which cards it takes.*
- **payable service / listing** — an endpoint an agent can pay to call. *A
  vending-machine slot with a price on it.*
- **floor buyer** — an in-app demo agent (`/demo/buyer/*`) that keeps buying to
  show the loop live. *A house shill who keeps feeding the machine.*
- **webhook (`/webhooks/circle`)** — an HTTP callback Circle sends the API the
  moment a settlement lands. *The bank texting you the instant a payment clears.*
- **Circle Smart Contract Platform (SCP) / Gas Station** — Circle's API to deploy
  and manage contracts, with gas sponsored so deploys are gasless
  (`deploy_circle.py`, with `setSigner` to authorize the poster, or
  `--import-by-address` to register an already-deployed one). *A concierge that
  installs your vending machine and pays the install fee.*
- **Developer-Controlled Wallet / `circle-developer-controlled-wallets` (SDK)** —
  a wallet whose keys Circle custodies for your app; an **SDK** is just a code
  library. *A company card the bank holds and swipes on your say-so.*
- **`attestWithSig` (meta-attestation)** — a seller EIP-712-signs their attestation
  (with a nonce + deadline) and a **relayer** submits it and pays the gas, so one
  funded account can register many sellers. *Mailing in a signed form someone else
  files for you.*
- **interop / conformance check** — the agent's self-test (`interop.ts`) that our
  402 challenge parses exactly the way Circle's client expects. *A dry-run to make
  sure our plug fits their socket.*
- **Paymaster / ERC-4337** — an "account-abstraction" way to have someone else
  sponsor gas; ACR **doesn't need it** because on Arc, USDC *is* the gas. *A
  gift-card for tolls you don't need when tolls are already free.*
- **The Fixing / Arc Dawn** — the Terminal's editorial framing (the hourly rate
  "fixing") and its visual theme (a pre-dawn navy→gold sunrise). *The newspaper's
  masthead and its front-page look.*
- **ChainFactsStrip / OracleProvenance / FinalityBadge / SettlementTape /
  WebhookActivity** — Terminal panels that show, respectively: the chain facts,
  the `postPrint` transaction + block (**provenance**), the print's age/staleness,
  a live feed of settlements, and recent webhook events. *The dashboard lights
  that prove the number is real, fresh, and paid-for.*
- **SWR** — a React library that automatically re-fetches data every few seconds
  so the Terminal stays live. *An auto-refreshing scoreboard.*

## On-chain futures & the Public Desk (Pillar 4, live)

- **ACRFutures.sol** — the on-chain venue: a weekly **cash-settled** future per index
  that resolves against the oracle. *A betting window at the stadium that pays out
  against the official scoreboard.*
- **series** — one tradeable contract line (an index + expiry + multiplier + a
  designated maker). *One specific "Team A to win, by Friday" market.*
- **multiplier** — USDC paid per 1.0 of index value per contract (**10** on this deployment; the contract's own comment says "e.g. 1000", which the terminal once quoted as fact and overstated a position 200×). *How
  many dollars each "point" is worth.*
- **designated maker / mirror side** — one appointed counterparty takes the exact
  opposite of every taker's fill, so the book's net position is always zero
  (**open interest = 0**). *The house automatically takes the other side of your
  bet.*
- **open interest (OI)** — the total size of open positions; here it nets to zero
  because the maker mirrors everyone. *How much money is riding on the table.*
- **initial margin (`MARGIN_BPS` — 2000 bp / 20% on the current deployment; it is an immutable constructor argument, so read it from the venue rather than assuming it)** — collateral you must post to hold a
  position; trades/withdrawals revert below it. *The deposit you leave to hold
  the bet.* (Initial-margin only — no intraday liquidation, a documented testnet
  simplification.)
- **mark-to-oracle** — positions are valued at the live oracle price. *Your bet's
  worth, updated to the current score.*
- **collateral / `post_collateral` / withdrawable** — the USDC you lock to trade,
  and what you're allowed to take back out. *Chips you buy in with, and what you
  can cash out.*
- **`Traded` event / trade tape / fill** — every executed trade emits a `Traded`
  log; the Terminal streams these as the live tape. *The ticker of trades
  scrolling by.*
- **socialized-loss settlement** — at expiry (at the first print the chain saw after
  it, which must have landed within `MAX_SETTLE_AGE` = 7200s of expiry; after
  `SETTLE_GRACE` = 7 days, `settleStale` uses the best print that exists so nothing
  stays locked), losers are floored to zero and any
  shortfall haircuts the winners pro-rata, so the contract never pays out more
  USDC than it holds (**solvency**, no minting). *If a loser can't cover, the pot
  is shared out fairly rather than promising money that isn't there.*
- **`FuturesClient` / `FuturesReader`** — the Python client that reads/writes the
  venue, and the API-side cached reader that serves the desk, maker inventory, and
  trade tape to the Terminal. *The cashier who reads the board and takes orders.*
- **maker inventory** — the maker's current net position; read back on-chain and
  used to **skew the term-structure curve** (a long book pulls the curve down).
  *Which way the house is leaning, which tilts its quotes.*
- **`futures_loop.py` / `futures_seed.py`** — the maker bot that seeds and keeps
  the on-chain book moving. *The market-stall owner who keeps restocking so
  there's always something to trade.*
- **Public Desk** — the feature that lets an ordinary reader take a **real**
  ACRFutures position from the Terminal. *A "place your bet" button for the public.*
- **user-controlled wallet / SCA** — a smart-contract account whose keys the *user*
  holds (PIN/passkey), not the server; contrast the developer-controlled (custody)
  wallet and the Circle agent wallet. **Four wallet types**: user-controlled (the
  desk reader) · developer-controlled (the press, the treasury, and every
  unattended venue job) · Circle agent wallet (the autonomous hedger, which pays
  x402 through the CLI and so needs no exported key) · a one-time offline deploy
  key. Which one a role gets is forced by a constraint, never chosen for taste —
  `ecrecover` demands an EOA account type, a cron job cannot hold an expiring
  email-OTP session, and a reader's key must never reach our server. The full map
  and its reasoning: [`docs/WALLETS.md`](WALLETS.md). *Your own safe vs the bank's
  vault vs the shop's float.*
- **PIN ceremony / passkey / `@circle-fin/w3s-pw-web-sdk`** — the browser flow where
  the user's PIN authorizes each on-chain action; the server never sees the key.
  *Tapping your own PIN at the terminal — the shop never learns it.*
- **challenge-response / challengeId** — the server mints a signed "challenge" the
  browser SDK executes under the user's PIN. *A one-time authorization slip you
  personally sign.*
- **faucet drip** — a small, one-per-wallet USDC stake handed out (from custody) so
  a new desk user can trade. *A free starter chip to get you playing.*
- **`PublicDesk` / `FuturesDesk` / `FuturesTape` / `QuoteCorridor`** — Terminal
  panels: the take-a-position widget, the maker's book, the live fill tape, and the
  bid/ask corridor chart. *The betting slip, the odds board, the ticker, and the
  price chart.*
- **`make deck` / marp** — the command that rendered the hackathon slides with the marp tool.
  The target is gone; its output is archived under `hackathon/`. *The old "export to
  slides" button, kept in the manual so the word still means something.*
- **LIVE on Arc mainnet** — ACR is not just buildable, it's **running in production**
  on Arc's public network (chain 5042), since 2026-09-27. *The shop is open, not just
  built — and the money is real.*
- **Vercel** — the host serving the Terminal (`arc-compute-rate.vercel.app`). *The
  landlord for the storefront website.*
- **Render** — the host serving the seller API. THREE services, not one: `acr-api-1fto` (testnet) and `acr-probe-free` (mainnet) serve all 50 routes, while `acr-api-mainnet.onrender.com` serves 44 and answers 404 for `/par` and `/operator/*`. It
  posts a Circle-signed oracle price every hour. *The landlord for the back office.*
- **arcscan (`testnet.arcscan.app`)** — Arc's block explorer, where anyone can look
  up a contract or transaction. *The public land registry for the chain.*
- **the deployed contracts** — the live addresses on Arc: **ACROracle
  `0x4f00…2609`**, **AttestationRegistry `0x23ae…dFb7`**, **ACRFutures
  `0x29d9…42fe`** (self-rolling; series 3 in the latest bundle), and
  **FeedAccessAttestor `0xe671…FD47`**. *The shops' street addresses.*
- **Circle Gateway receipts / batch UUID** — every real x402 payment produces a
  durable receipt carrying Circle's Gateway batch identifier, saved in-repo. *The
  stamped, filed copy of each sale.*
- **P-256 / ECDSA signature (webhooks)** — Circle signs its webhook callbacks with a
  P-256 (an elliptic-curve, ECDSA) key; the API verifies that signature before
  trusting the event. *Checking the wax seal on a letter before you act on it.*
- **heartbeat cron / `futures-heartbeat.yml` / `futures-lifecycle.yml`** — scheduled
  GitHub Actions that keep the futures book alive 24/7 — the heartbeat
  self-provisions collateral and trades; the lifecycle rolls and settles series.
  *An automatic caretaker who keeps the lights on and the shelves stocked.*
- **keepalive (cron)** — a scheduled ping that stops the free-tier host from
  sleeping. *Nudging the shop so it doesn't nap between customers.*
- **"cron fires only from the default branch"** — a GitHub gotcha: scheduled
  workflows run only from `main`, so automation is armed only once merged. *The
  timer only counts down once the plan is filed at head office.*
- **rate limiter / per-identity (`ratelimit.py`, `DESK_BUDGETS`)** — desk request
  budgets keyed on the user's wallet/id, not the shared proxy IP (which collapses
  every visitor into one bucket behind a server-side proxy). *Giving each customer
  their own tab instead of one shared tab for the whole street.*
- **attested-market decode (`ACR_ARC_ATTESTED_ONLY`)** — a live-tape mode where the
  on-chain transfer *amount is the price signal* (one settlement = a published
  quantity) and events from non-attested sellers are dropped — attestation earns
  index inclusion (the registry flywheel, made literal). *Only listed vendors count,
  and each sale's total tells you the unit price.*
- **CI jobs (python · contracts · agent · terminal)** — the four independent
  GitHub-CI checks that must pass on every push. *Four inspectors who each sign off
  before anything ships.*

## Autonomous agents & the self-owning venue

- **autonomous hedger (`scripts/hedger.py`, `GET /hedger`)** — a single agent that
  runs a four-step loop: pay a real x402 nanopayment for the latest `ACR-INF` print,
  read its own on-chain ACRFutures position, compute the gap to its mandate, and buy
  the difference. It is the demand side made whole — an agent that *reads the rate,
  then trades on what it read*. It trades in one direction only: `circle wallet
  execute` cannot build a transaction carrying a negative `int256`, so this agent can
  open and increase a position and cannot reduce one. *A trader who buys today's
  price sheet, checks what it already owns, and places one order to close the
  shortfall — and can only ever buy more, never sell back.*
- **mandate / `TARGET`** — the position the hedger is told to hold (in contracts);
  `gap = TARGET − position` sizes each trade, and only a POSITIVE gap can be acted on
  from an agent wallet, so the mandate is raised to meet the position rather than the
  position sold back to the mandate. *The instruction: "hold at least this much."*
- **agent wallet** — a Circle wallet an agent signs through (email/OTP), with **no
  exportable private key**; the hedger signs every payment and trade this way. *A
  company card the agent can spend with but can never photocopy.*
- **`feasible_qty`** — the shared sizing helper (from `index_api.desk`) that clamps a
  desired trade to what margin and book room actually allow. *Checking your wallet and
  the shelf before deciding how much to buy.*
- **venue keeper (`services/index_api/index_api/keeper.py`)** — the in-process loop
  (runs beside the press) that keeps the futures venue trading and rolls it across
  expiries. *The caretaker who both works the counter and opens tomorrow's stall.*
- **self-rolling venue / `roll_if_needed` / `openSeries` / `onlyOwner`** — because
  ACRFutures ownership was migrated to the maker's own Circle wallet, and `openSeries`
  is `onlyOwner`, the keeper can open the successor series unattended — the venue
  *owns itself*. *The shop holds its own keys, so it can unlock itself each morning.*
- **`migrate_venue_owner.py` / `pendingOwner` / `acceptOwnership`** — the one-time tool
  that handed venue ownership from the retiring deploy EOA to the maker wallet using
  the safe **two-step** transfer (propose, then accept). *Signing the deed over to the
  new owner, who must counter-sign to take it.*
- **"shape read from the chain" (`live_indices`)** — the keeper derives the tradable
  roster from `read_all_series()` (unsettled, unexpired) and reads the mark on-chain,
  instead of trusting a configured constant. *Reading today's board off the wall, not
  last week's memo.*
- **`build_role_signer` / role signer** — the router that maps each job
  (**maker · taker · poster · owner**) to *its own* Circle Developer-Controlled wallet,
  deliberately **ignoring the ambient `.env` key**. *Every role carries its own badge;
  none can borrow the master key.*
- **FeedAccessAttestor (`contracts/src/FeedAccessAttestor.sol`, `0xe671…FD47`)** — a
  fifth on-chain contract. The seller signs an EIP-712 **`FeedAccess`** struct
  (`payer`, `beneficiary`, `paidUntil`, `amountUsdc`, `nonce`) with the same poster
  Circle wallet that signs oracle prints; anyone relays **`redeem(...)`**, which
  `ecrecover`s the authorized signer and records `paidUntil[beneficiary]`, so
  **`hasFeedAccess(addr)`** becomes an on-chain fact. The seller *signs, it does not
  decide* — it can only attest wallets that actually paid (appear in
  `/marketplace/receipts` with `scheme == "exact"`). *A turnstile that opens for
  anyone holding a receipt the shop already signed.*
- **`MAX_ACCESS_WINDOW` (90 days)** — the contract's cap on how far any single grant
  may reach, so a signer compromise costs weeks, not a century (the live grant was
  re-minted from 7 → 60 days, inside the cap). *A gift card that can never be dated
  more than three months out.*
- **`attest_feed_access.py` / `DeployAttestor.s.sol`** — the script that reads the
  public receipts ledger and signs the grant, and the Foundry deploy for the contract.
- **durable receipts (`services/index_api/index_api/receipts_live.jsonl`, `_rehydrate`)**
  — the settlement ledger was moved out of gitignored/dockerignored `data/` into
  `services/…` so it ships **inside the image** and `/revenue` + `/marketplace/receipts`
  survive a restart; `revenue_usdc` now rounds to 6 dp (no float noise). *Keeping the
  sales book in the safe that moves with the shop, not on a desk that gets cleared.*
- **x402 live buyer / `GatewayPayer` (`apps/agent`, `x402-buy.yml`)** — the TypeScript
  buyer that runs **real** settlements against the live seller through Circle's Gateway
  (402 → sign EIP-3009 → settle), archiving each `PAYMENT-RESPONSE` receipt. *A real
  customer who actually pays at the till, not a demo shopper.*
- **wallets by role** — the live keys, each doing one job: **poster/press `0x8366…`**,
  **venue owner + maker `0x9D44…`**, **heartbeat taker `0xc972…`**, **readers** on
  their own **user-controlled** wallets, the **autonomous hedger** on a Circle **agent
  wallet**, and the **retired deploy EOA `0x3318…`**, which now holds **no authority**.
  *One badge per role, and the old master badge deactivated.*

## Instrument (Pillar 4)

- **Future (cash-settled)** — a contract to settle the *difference* vs the index
  at a future date — no goods change hands. *Betting on next month's gas price
  and just paying/collecting the difference, never taking delivery of fuel.*
- **Market maker** — someone who always posts a price to buy and a price to sell,
  creating liquidity. *The currency-exchange booth quoting both directions.*
- **Avellaneda–Stoikov** — a well-known recipe for setting those buy/sell quotes
  based on inventory and risk. *A rulebook for how wide to set the spread and
  when to lean.*

---

## The pipeline in plain words (flow ①→⑩)

1. **Exhaust** — machines pay each other; each payment (price, size, who, when)
   streams in.
2. **Batched tape** — those payments settle in delayed batches, which smears the
   timing (the "convolution").
3. **Deconvolve** — the Kalman filter un-smears the batching to recover what the
   price actually was moment to moment (Pillar 1).
4. **Clean** — throw out the fakes: self-trades, wash rings, and sybil clusters
   (funding-graph + Louvain).
5. **Robust estimate** — take the trimmed, size-weighted *middle* of what's left,
   so no whale or fake can drag it; attach an error range (CI).
6. **Hedonic** — adjust for quality so a frontier model and a cheap one are
   compared fairly → the constant-quality rate (Pillar 2).
7. **Print + bound** — publish the hourly rate **and** the dollar cost to move it
   1bp (Pillar 3).
8. **Oracle** — sign the rate and post it on-chain to `ACROracle` for other
   contracts to read.
9. **Settle** — the weekly future cash-settles against that on-chain rate.
10. **Agents pay for the rate** — machines pay a fraction of a cent (x402) to read
    the index. The index about machine commerce is bought *by* machines — the
    loop closes.

## Machine TCA — the ETHOnline 2026 continuity layer

- **ETHOnline 2026 · Continuity** — the hackathon track ACR entered as an existing project.
  Everything since the frozen baseline tag `v1.0-submission` is documented in `hackathon/ethonline-2026/CONTINUITY.md`;
  the diagram's band T is that work, drawn as a layer on the earlier product. *The extension
  built onto a house that was already standing, with the old walls left visible.*
- **Machine TCA (transaction-cost analysis) — `/tca/{payer}`, `/tca/human`, `by_seller`** — what
  an agent's purchases actually cost against the benchmark it could see at the moment each one
  settled: volume-weighted slippage, the dollars overpaid, and a breakdown by seller with the worst
  first. `/tca/human` is the same card across every wallet one person owns. *The receipt that
  says "you paid 3% over the going rate, mostly at that one shop".*
- **ReceiptMirror (`0xA9CD…DB65`)** — the contract every real x402 settlement is mirrored onto,
  so the tape of who paid whom, how much, for what, lives on Arc and not only in a seller's
  memory. A per-payer monotone guard refuses a receipt older than the last one recorded for that
  payer, so a keeper cannot withhold a settlement until a later print suits it. *Carbon copies
  of every till receipt, filed in a public cabinet that only accepts them in order.*
- **HumanIdMirror (`0x7f41…d8e5`) · cluster · rotation · resolutions** — the contract that records which
  payer wallets belong to one verified person, per 7-day rotation window, as an opaque cluster id
  `keccak(nullifier, salt, window)`. The salt is committed on chain (its hash), never published;
  the nullifier — World's per-person identifier — never appears. Ids change completely every
  window, so a resolution expires rather than lingers — the **resolutions** on chain are only
  ever this week's — and re-resolving is an operator chore
  (`make resolve-humans`) the ops console fails loudly on when missed. *A weekly guest list that
  groups rooms by guest without printing anyone's name, and is torn up every Monday.*
- **ACROracleV2 (`0xFCa0…FFEA`) · `policyHash` · `humanAdjustedBound`** — the second oracle,
  posted beside v1 (which the futures venue settles against and cannot change). Each v2 print
  carries the hash of the cleaning policy that produced it and a second manipulation bound
  denominated in people rather than wallets: sybils are free, humans are not. *The same price,
  stamped with the recipe used and an honest note on what it would cost a real person to move it.*
- **the `acr-tape` subgraph (The Graph · Studio `ethonline` v0.2.0) · indexed · mapping** —
  the index of Arc that turns settlements into numbers: `Settlement.slippageBp` is benchmarked
  in the mapping itself, at the settling block, against the arrival print — the last print
  posted before the settlement — so nothing downstream recomputes it and nothing can disagree.
  Seven data sources (both oracles, both mirrors, the venue, the registry, the attestor); daily
  rollups `SellerDay` / `PayerDay`; a `SellerWindow` denominated in people; `Settlement.human`
  stamped from HumanIdMirror. Substreams is not available for Arc (Studio-only), which is stated
  rather than skipped. *A public ledger-reader that writes "over/under the going rate" on every
  receipt the moment it is filed.*
- **ratings · fairness · human depth · `weight_covered_pct` · `human_share`** — a seller's grade
  from the same tape: how far its fills sit from the benchmark (fairness), how many distinct
  people — not wallets — bought from it (human depth), what share of its volume came from
  wallets the chain ties to a person, and what share of the published methodology's weights the
  grade actually rests on. Components with no data yet are excluded from the weighting rather
  than scored zero, so a grade never claims 100% coverage. *A shop review that also says how
  many of the reviewers were real, and how much of the scorecard was filled in.*
- **the reroute · `reroute_suggestion` · `--reroute`** — from `by_seller`, the arithmetic of
  moving volume from the seller a wallet overpaid most to the one it overpaid least, with the
  saving in basis points on past fills. Stated as a suggestion, never a promise: past fills are
  not future ones. With `--reroute`, the buyer agent reads its own TCA before buying, drops the
  worst seller from the run and pays the suggested one first — the signal deciding the next
  nanopayment. *"You would have saved 3% at the shop next door" — and then walking there.*
- **`make recompute` · sybils flagged** — re-derives the index from the settlements the subgraph
  indexed and compares it against what the chain published. On a demo tape where every payer
  buys from every seller the cleaning stack flags the whole tape as one funding community and a
  cleaned estimate is undefined; the script says so and prints the uncleaned figure, labelled.
  *Re-doing the sums from the public ledger to see whether the posted price holds up.*
- **the agent gate · `AGENT-CARD` · three tiers (anonymous · carded · human)** — a signed
  EIP-712 card an agent presents; the gate believes the key, not a registry, so nobody is
  enrolled. The card's domain names no `verifyingContract` — it binds to an `audience` string
  and a 15-minute lifetime instead — and may claim a human cluster the chain can confirm. Three
  tiers of rate limit follow: the shared host ceiling with no card, a budget per key with one,
  a budget per person when the claim is confirmed (never a silent downgrade: an unconfirmable
  claim is a 401). The tier rides on the receipt. *An ID card the robot made itself; the more
  it proves, the bigger its own allowance.*
- **Google Cloud Model Armor** — Google's screening service for prompt injection and
  jailbreaks, applied to carded callers' tape queries in both directions. `/armor/info` says
  which backend answered; the offline pattern floor is reported as a floor, never as the screen.
  *A bouncer reading messages in and out, and a sign saying whether the bouncer is the real one.*
- **World AgentKit · AgentBook · `HUMAN-PROOF` · CAIP-122 · Sandbox** — the proof that an agent
  acts for a real, unique person: the gate issues a challenge nonce, the agent's wallet signs a
  CAIP-122 message naming it, the gate recovers the signer and looks the wallet up in AgentBook
  (World's registry of agent wallets to humans), then derives the person's cluster for this
  window. The nonce is spent on use. All demo identities are World ID Sandbox ones, flagged
  `sandbox` onto the chain and into every count. `make prove-human` runs the whole path with
  nothing secret — judge-runnable. *Showing a ticket that proves a person is behind the robot,
  without showing the person.*
- **the MCP server (`mcp/`) · `check_spend` · `spend_report` · `can_i_pay` · `wallet_tca` ·
  `query_tape` · `skills/acr-analyst`** —
  eleven tools any MCP host (Claude, for one) can call: one vendor invoice priced against the
  going rate and the running total of every invoice priced on that machine, a wallet's TCA (for a
  wallet that bought from ACR; one it has never seen answers `seen: false`, not zero), a
  reroute suggestion, a
  seller's rating, the benchmark, the on-chain rate, any named subgraph operation, and — the one
  the surface could not answer at all for its first version — whether this agent can actually pay
  for a metered query, reported as seven rungs so the one in the way is named. `can_i_pay` asks the
  paywall for a price and declines to answer it, so it spends nothing and needs no key;
  `pay_and_read` does settle, and is not registered unless a payer key is set. Every call carries
  an agent card, signed for the chain the gate itself names; `wallet_tca("me")` signs the AgentKit
  challenge. The analyst skill is the same tape explained to an agent in English — "Ask the Tape".
  *A phrasebook and a phone line so an AI assistant can ask the market what things cost — and find
  out whether it can afford to.*
- **the loop closes** — settlement → mirrored on chain → indexed by the subgraph → measured as
  TCA → decided as a reroute → paid as the next Circle Gateway nanopayment, which is itself a
  settlement. Nothing in the chain is asserted; each link is a public record the next one reads.
  *The receipt teaches the buyer, the buyer changes shops, the new receipt lands on the pile.*
- **beancount — `GET /operator/ledger/{business}`** — the plain-text, double-entry accounting
  format the spend operator exports its decisions in, one transaction per payment, each carrying
  the rule that produced it and the hash the chain holds. Double-entry means every transaction's
  postings sum to zero, so a missing or invented figure fails arithmetic rather than review;
  `make verify-operator` re-checks that against the served file with its own reader, because
  importing the exporter's checker would let a bug pass its own output. A reroute is a note on the
  record, never a transaction, since no money moved. Each `/traction` row downloads its own.
  *The shop's own books, in a format an accountant's software can open and add up.*
- **audit (the six errors) — `GET /operator/audit/{business}`** — the check the ledger cannot do for
  itself. *Agents and Ledgers* argues that double entry's one built-in test is nearly worthless
  against an agent — "nearly every mistake an LLM can make with money passes it" — and names six that
  balance perfectly: **omission** (a payment nobody recorded), **commission** (the right amount to the
  wrong party), **principle** (the right amount in the wrong kind of account), **original entry** (the
  wrong amount on both sides, or a retry that paid twice), **compensating** (two mistakes that
  cancel), and **complete reversal** (the debit and the credit swapped). Each is searched for by name
  and reported with HOW MUCH IT SEARCHED, because nothing found in nothing examined reads exactly like
  a clean book. None of the six reads the ledger alone: omission compares our decisions against the
  sellers' own settlement tape, commission asks whether the payee ever served this business, principle
  asks the registry. *Counting the stock and ringing the bank, instead of checking that the two
  columns of the book add to the same number.*

---

*See also: `docs/ARCHITECTURE-DIAGRAM.md` (the visual blueprint), `docs/methodology.md` (the formal
spec), `IMPLEMENTATION.md` (how the code maps to the diagram).*
