# Judge walkthrough: ACR in ten minutes

ACR is two things that share one engine. The **Arc Compute Rate** is a reference
rate for machine compute, printed hourly on Arc mainnet with its own attack cost.
The **spend operator** (the Tameion build) is an agent that holds a business's
USDC inside an on-chain budget, prices every bill against that kind of
benchmark, pays what clears policy and escalates the rest to a person.

Open the stops below in order. Each one says what to click, what you should
see, and which judging criterion it is evidence for. Nothing needs an account.

## Before you start: two chains, one switch

| | Chain | What lives there |
|---|---|---|
| The rate, the x402 paywall, the futures venue | Arc **mainnet** (`5042`) | seven contracts, live since 2026-09-27 |
| The spend operator and its one real business | Arc **testnet** (`5042002`) | test USDC, stated as test USDC |

The terminal opens on mainnet. The **testnet →** button in the masthead switches
every page to testnet (it sets a cookie and reloads); **testnet · leave →**
switches back. Page links cannot carry the chain, so use the button.

## 1. The rate itself (mainnet)

Open <https://arc-compute-rate.vercel.app>.

- **See:** the ACR-INF print, large, with a chip saying whether it is current
  and the dateline saying how old it is. A stale print says so; it is never
  labelled live.
- **Do:** drag across the number, or click it and press ←. It rolls back
  through the last 24 fixings with a caption, and the chip switches to
  "est. (sim) · history" because those values are the estimator's series, not
  the on-chain print. Press Esc or click away to return.
- **Do:** scroll to *Today's fixing* and press **embed this rate**. Pick a form.
  The image shown is live from this deployment; the three badges at the top of
  the repository README are the same thing.
- **Criterion:** innovation (a benchmark that publishes its own attack cost),
  Circle tools (prints are EIP-712 signed and posted on Arc).

## 2. Is this bill fair? (mainnet)

Open <https://arc-compute-rate.vercel.app/check?unit=%24%2F1k+tokens&billed_usdc=0.02&quantity=10>.

- **See:** a verdict on 0.02 USDC for 10 thousand tokens, against published
  list prices from sellers ACR does not operate. Every price links to the page
  it came from, and the basket's age is stated.
- **See:** *What the agent would do*: the same decision ladder the spend
  operator runs, applied to your bill. Nothing is paid.
- **Do:** press **copy link** and paste it anywhere: the link unfurls with the
  verdict as its preview image.
- **Criterion:** agentic sophistication (the ladder is real), traction (no
  account, no key: a stranger can use it).

## 3. A decision waiting for you (testnet)

Press **testnet →**, then open <https://arc-compute-rate.vercel.app/spend?business=sandbox>.

- **See:** *Waiting on you*: four escalations the agent would not decide alone,
  each with the rule that stopped it: a 140 USDC bill over the per-payment
  limit, a counterparty on a sanctions list, a bill for more than the meter
  counted, and a bill over an agreed price.
- **Note:** *Sandbox Studio* is a demonstration. Its rows are produced by the
  real ladder (`scripts/gen_sandbox_decisions.py`, checked in CI), and it is
  excluded from every traction figure.
- **Criterion:** agentic sophistication (escalation is a designed outcome, not
  an error).

## 4. The real business (testnet)

Still on testnet, open <https://arc-compute-rate.vercel.app/spend?business=acr-fleet>.

- **See:** the statement for ACR's own buyer-agent fleet over 30 days: 19
  decisions, 18 decided by the agent, 1 escalated and settled by the owner,
  0.154363 test USDC moved, 0.059193 USDC identified as recoverable.
- **See:** the budget read from the PolicyWallet contract
  (`0xA755f87BD00c90DBFc9DdfD6651e4c3071665b32`): a cap the agent cannot exceed
  because the contract will not let it.
- **Do:** open **price it yourself** on any decision: it lands on `/check` with
  that bill filled in.
- **Do:** download the ledger. Every transaction in it sums to zero, and
  `GET /operator/audit/acr-fleet` on the testnet API searches for the six
  errors a balanced ledger cannot see.
- **Criterion:** traction (honestly first-party, tier `own`), Circle tools.

## 5. Who it runs for (testnet)

Open <https://arc-compute-rate.vercel.app/traction>.

- **See:** every figure counted from the registry and the decision log at
  request time, as pairs of counts rather than rates: settled by the agent
  versus by the owner, owner resolutions versus agreements.
- **Criterion:** traction.

## 6. The machine loop (mainnet)

Press **testnet · leave →**, then open <https://arc-compute-rate.vercel.app/loop>.

- **Do:** **Drive it** walks a payment through pay, mirror, index, measure,
  decide, pay again. Slide the threshold and watch the reroute decision flip.
- **Do:** in *Who is calling*, run the same request anonymous, with an agent
  card, and as a demo human, and compare the three answers.
- **Criterion:** agentic sophistication, innovation (a rate limit denominated
  in people rather than wallets).

## 7. Buy it like a machine (mainnet)

Open <https://arc-compute-rate.vercel.app/developers>.

- **Do:** **Pay $0.0001 & query**: an x402 nanopayment through Circle Gateway.
- **Do:** copy the MCP config and run `npx -y acr-mcp`: eleven tools, including
  `check_spend` (a verdict on any invoice) and `can_i_pay` (whether your agent
  can pay, and which step is in the way, spending nothing).
- **Criterion:** Circle tools (Gateway x402, developer-controlled wallets,
  the Agent Stack buyer SDK).

## What is real, and what is a demonstration

- **Real, mainnet:** the contracts, the hourly prints, the x402 paywall and its
  settlements. Addresses are in the [README](../README.md#live-right-now).
- **Real, testnet:** the spend operator's 19 decisions for `acr-fleet` and the
  PolicyWallet. It is our own fleet (tier `own`); we have no third-party
  customer yet and do not claim one.
- **Demonstration:** `sandbox`, excluded from every count.
- **The delta this event judges:** `git diff tameion-baseline..HEAD --stat`.
  The window's start is a tag, not a claim.

### Bittensor (SN51 and SN64), which this list used to omit entirely

This doc exists to say what is real, and until now it did not mention Bittensor
once — while the README described two subnets as live inputs. Four different
statuses, which are easy to blur together:

- **Real, live, unauthenticated HTTP.** Both upstreams answer with no API key.
  **SN64 (Chutes, netuid 64)** publishes USD billed per model per day and the
  tokens that bought it; dividing one by the other is a **realized** $/1k-token
  price — a transacted number, not a rate card — and it is the tape behind
  `ACR_TAPE_SOURCE=chutes`. **SN51 (Lium, netuid 51)** publishes a price per GPU
  per executor, which prices six accelerator classes in the ACR-GPU anchor
  basket that `/par` benchmarks an outsider's bill against.
- **Read this part twice.** The published **ACR-GPU index value is still
  simulator-sourced**. SN51 powers the `/par` *benchmark*, not the *print*, and
  that is deliberate: `price_per_gpu` is an **ask** on inventory that is ~95%
  idle (446 of 468 GPUs free in the captured fleet), and an ask is not a
  transaction. Anyone reading "subnet 51 is wired in" as "the GPU index is
  priced off Bittensor" has it wrong.
- **Real, Bittensor testnet.** ACR is registered as **netuid 602** and has set
  weights there — five extrinsics on 2026-10-30, each confirmed by reading chain
  state back rather than by its receipt. The keys are testnet-only and hold no
  value. Not registered on Bittensor mainnet.
- **Demonstration.** `make subnet-demo` *and* `make subnet-demo-real` both read
  committed fixtures and make **zero network calls** — "real" there means the
  recorded subnet-64 tape rather than the simulator, not a live fetch. The live
  captures are `make subnet-tape` and `make anchors-fetch`; `make subnet-canary`
  is what asks whether either upstream still looks like the thing we parse.

The full argument, including what the mechanism is and is not, is in
[BITTENSOR.md](BITTENSOR.md).

## Check it yourself

```bash
make verify-operator     # the spend operator, against the live testnet API
make verify-live         # every pillar, against the live deployment
make eval-gate           # the attack-resistance numbers, CI-gated
```

The full argument is in [TAMEION.md](TAMEION.md); the method behind the rate is
[methodology.md](methodology.md).
