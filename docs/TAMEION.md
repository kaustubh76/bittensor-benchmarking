# Tameion — the spend operator

> **Judging this?** [WALKTHROUGH.md](WALKTHROUGH.md) is the ten-minute path through the
> live product, in order. This page is the argument behind it.

*Canteen × Circle × Arc, 27 September – 10 October 2026. This file is maintained
during the event; the frozen material from earlier events lives in
[`hackathon/`](../hackathon/).*

ACR is a reference rate for machine compute. The Tameion build puts a **spend
operator** on top of it: an agent that holds a business's USDC inside an
on-chain budget it cannot exceed, meters what was actually consumed, checks
every price against what other sellers are really charging, screens the
counterparty, weighs what the wallet actually holds against what is already
dated, pays what clears policy, escalates what is not its call, and writes a
double-entry ledger a human can open.

**Where the pages are, stated exactly, because three documents used to disagree.**
The terminal **is** deployed — <https://arc-compute-rate.vercel.app> answers 200,
and so do `/spend` and `/traction`. What those two pages show in production is
*nothing*, and the reason is a host split rather than a bug:

> This paragraph named `arccomputerate.in` and cited its 200 as the proof, from
> 2026-10-08 to 10-09. The 200 was real and meant nothing: that name never
> resolved to this deployment and the 200 came from Hostinger's parked-domain
> page. A status code cannot tell you *which* server answered.

| | |
|---|---|
| the operator's data | lives on the **testnet** API, `acr-api-1fto` (chain `5042002`), which serves all five `/operator/*` routes. Every link in the tables below points there |
| the terminal | is built for Arc **mainnet** and refuses a press reporting a different chain id (`lib/apiBase.ts` `chainMismatch`). That guard exists because a resumed testnet seller once served live testnet data under a mainnet masthead |
| the **mainnet** API | is TWO presses. `acr-probe-free` serves all 50 routes including every `/operator/*` and `/par`; `acr-api-mainnet` serves **44 and none of them is `/operator/*`** — its image predates them. Measured 2026-10-12; `make verify-drift` reports both |

So `/spend` reads "No businesses onboarded yet" on a product that has one business and nineteen
decisions. The copy is honest about what it can see; what it can see is the wrong host. **That is
fixed in the terminal rather than by a redeploy** (`9c7033d`): the seller ladder's walk
signal was `status >= 500`, so a **404** stopped it on the first candidate even though
`acr-probe-free` — same chain, next in the list — serves every one of these routes. A
healthy host that does not carry a route now gets walked past. Until then the API links
below are the live surface, and `make api && make terminal` renders the pages locally against
whichever press you point `ACR_API` at.

## The claim we are not making

*Agents and Ledgers* — the analysis this event asks every team to read — says a
balanced ledger proves almost nothing: *"That equality is the ledger's one
built-in check and nearly every mistake an LLM can make with money passes it."*
It names six, and says the controls that catch them live **outside** the ledger.

So we do not claim the books balance and stop there. `GET /operator/audit/{business}`
searches for all six by their own names, and not one of the checks reads the
ledger alone:

| The error | What we check, and against what |
|---|---|
| omission | the sellers' own settlement tape, for a payment with no decision |
| commission | whether the payee ever served this business at all |
| principle | the registry's declared categories, not the exporter's own mapping |
| original entry | billed against paid, and the retry that pays twice |
| compensating | over- and under-counts that cancel across a period |
| complete reversal | a posting whose direction is inverted and still sums to zero |

And a seventh, reported beside them because it is **not** one of the essay's six:
the **phantom payment** — the fictitious entry the essay says nobody can
disprove, naming SolidInvoice for *"the most complete write path, and no way to
disprove a phantom payment."* We could produce one: on a chain where the budget
contract has no code, a call neither reverts nor fails to estimate, so a
transaction broadcasts, returns `status: 1`, and gets written down as a payment
with a hash as its evidence. The client now refuses to send at all, and the
audit looks for one anyway — a control nobody audits is a control nobody can
show you. Every claimed payment is put back to the chain; a node that cannot
answer is "could not tell", never a finding.

Every check reports **what it searched**, because `found: 0` over nothing
examined is indistinguishable from a clean book. A settlement with no payee is
reported as unattributable rather than counted clean, since it cannot be matched
to any decision either way.

## The delta, computed rather than asserted

FAQ Q6 judges the gap: *"where was the product and how many users did it have
when Tameion began, and where are both at the end?"* So the window's start is a
tag, not a claim:

```bash
git diff tameion-baseline..HEAD --stat     # what changed
git log --oneline tameion-baseline..HEAD   # and why, one commit at a time
```

At the baseline ACR was live on Arc mainnet with the benchmark, the public tape,
the TCA engine and signed decision receipts — and **zero businesses**. The
operator, the registry, the statement, the ledger and the traction page are all
new in the window.

## What is live, and what is not

| | |
|---|---|
| **Who it runs for** | [`GET /operator/traction`](https://acr-api-1fto.onrender.com/operator/traction) — counted from the registry and the decision log at request time |
| **One business's money** | [`GET /operator/statement/acr-fleet`](https://acr-api-1fto.onrender.com/operator/statement/acr-fleet) — the statement, the budgets, and the queue waiting on its owner |
| **The ledger** | `GET /operator/ledger/{business}` — beancount, every transaction summing to zero, and a closing `balance` assertion with a declared tolerance. Each `/traction` row links to its own, as a download |
| **What the ledger cannot check** | `GET /operator/audit/{business}` — the six errors a trial balance cannot see, searched for by name. Rendered at the foot of `/spend` |
| **Prove it** | `make verify-operator` — every honesty property above, re-checked against a live deployment, including whether the queue can actually be cleared. Some checks are conditional on what the deployment has, so it reports the count it ran rather than promising one |

**Stated plainly, because a reviewer will check.** The loop runs on **Arc
testnet**, where the fleet's real settlement history gives the meter genuine
consumption to count. Those settlements **predate the window** — what is
in-window is the onboarding and every decision since.

The operator now spends. `PolicyWallet`
[`0xA755f87BD00c90DBFc9DdfD6651e4c3071665b32`](https://testnet.arcscan.app/address/0xA755f87BD00c90DBFc9DdfD6651e4c3071665b32)
holds the fleet's USDC under a 1 USDC cap with a 0.05 USDC per-payment limit.
A live run paid five obligations on its own authority, rerouted three to cheaper
sellers, and escalated the one bill at or above that limit; the owner settled
that one with `spendAsOwner`, from a different key, because the contract refuses
an approval signed by the thing being approved. So the log holds **six** rows
carrying a transaction and five of them are the agent's — worth saying plainly,
because counting `intent: pay` in the log gives six and this sentence says five. The contract's own `spent` figure and the
decision log agree.

Everything here is **test USDC on Arc testnet**. Canteen say test USDC counts
and real USDC on mainnet counts for more; the two are reported apart, never
summed, and `moved_usdc` is what was actually paid rather than what was
assessed — `priced_usdc` is the second number, and collapsing them would be the
most tempting lie available on a traction page.

## The figures the briefs ask for, by name

RFB 4 and RFB 5 name their traction metrics in words, so here is each one
mapped to the field that answers it. The values are not copied into this file:
a doc that restates a count is a doc that will contradict the page, and the page
is [`GET /operator/traction`](https://acr-api-1fto.onrender.com/operator/traction).

| The brief asks for | The field | Read it at |
|---|---|---|
| businesses operated | `businesses.businesses`, with `businesses.mainnet` and `businesses.testnet` beside it because the two are never summed | `/operator/traction` |
| total USDC received and paid out | `by_chain[].moved_usdc` and `by_chain[].received_usdc` | `/operator/traction`, per business on `/traction` |
| obligations settled without a human touching them | `work.autonomy.settled_by_agent` beside `settled_by_owner` | both, per business |
| …and settled on time | `work.autonomy.settled_on_time` of `settled_with_a_due_date` | both |
| decisions made vs escalated | `work.decided` and `work.escalated`, two numbers rather than a ratio | both |
| how often the human agreed | `work.agreement.owner_agreed` of `owner_resolutions` | both |
| addresses monitored | `work.compliance.addresses_screened` — **screened**, see below | `/operator/traction` |
| alerts generated and resolved | `work.compliance.alerts_raised`, `alerts_resolved` | `/operator/traction` |
| risk events caught before the transaction | `work.screening.risk_events_caught`, beside `paid_unscreened` | both |
| compliance reports generated | no counter — the artifact is `GET /operator/audit/{business}` | see below |

**Three of these are zero, and the zeros are load-bearing.**

`received_usdc` is 0 because the fleet is the buyer on every settlement it
appears in and has never once been the seller. It is computed from the sellers'
own tape rather than asserted, so it stops being zero the moment the fleet is
paid — and the function is tested against a treasury that *is* paid, which is
what makes today's zero a measurement instead of an absence.

`settled_on_time` is 0 **of 0**, because no obligation on the real tape carries a
due date. The cause is narrower than this document used to claim, and worth
naming exactly: it is not that nothing *can* supply one. `obligations_for` reads
x402 settlements, which have no due date by construction — a payment that already
happened cannot be late. The feeder that does supply one,
`obligations_from_entitlements`, reads a committed register of vendor invoices,
and **no real customer has sent us one**. The register exists now and the ladder
reaches check 7 through it, but its only rows are `sandbox`, which
[`businesses.py`](../services/index_api/index_api/businesses.py) excludes from
every figure on this page. So the denominator moves when a business hands over an
invoice export, not when we write more code.

`autonomy()` will not count a bill with no due date as punctual. The alternative
turns "we do not know when this was due" into evidence of promptness, and a
perfect on-time rate over an empty denominator is the single most flattering
number available on a traction page.

`alerts_raised` is 0 because every counterparty put to a screen came back clear.
The field is named **screened** and not *monitored* on purpose: the screen runs
at decision time, before money moves, and nothing re-screens an address on a
schedule. `counterparty.py` records monitoring as an aspiration rather than a
feature, and claiming the brief's word would be the overclaim this whole
document exists to refuse.

**"Compliance reports generated" has no counter, deliberately.** The artifact is
[`GET /operator/audit/{business}`](https://acr-api-1fto.onrender.com/operator/audit/acr-fleet):
the six errors a trial balance cannot see, searched for by name, generated on
request from the decision log and the sellers' tape — never cached, so it cannot
report a verdict that was true last week. Counting how many times we served it
would measure our own traffic and call it compliance.

## Which Circle product does which job here

[`docs/WALLETS.md`](WALLETS.md) is the full map. What matters for the operator is
narrower, and one line of it was not true until recently.

| leg of the loop | what settles it |
|---|---|
| the fleet buying compute | **Circle Gateway**, x402 v2 — the seller's gate verifies and settles through `CircleFacilitator`, and the batch reference is what the receipt tape records |
| the operator paying a bill | a `PolicyWallet` call signed by a **Circle developer-controlled wallet** — the same channel the hourly oracle prints go out through |
| the owner clearing an escalation | a raw key, necessarily — see below |
| screening a counterparty | **not a Circle product**: self-hosted OpenSanctions (`yente`) when configured, a local denylist otherwise |

**The payment channel is now on the record.** Circle's developer-controlled
wallet and a raw local key build *identical* calldata, so no reader of the
chain, the log or the ledger could tell which one paid — and five payments on
Arc testnet went out from a raw EOA. Every decision written **from now on**
carries `paid_via` (`circle` · `local`), set before the record is hashed, so the
commitment says which channel was authorised. `/ops` reports it per business and
warns on a raw key. That is the same argument `screen_backend` won: a verdict
without its source is a claim without a basis.

**"From now on" is doing real work in that sentence.** The field was added after
those payments were made, and the archive is immutable by design — so **none of
the nineteen rows** `/operator/traction` serves today carries `paid_via`, and
nor do they carry `screen_backend`, `commitment_verdict`, `held_usdc`, `due_usdc`
or `discrepancy_usdc`. The sandbox and local logs carry all of them. Two
consequences worth stating rather than discovering: `held_back_usdc` and
`overbilled_usdc` are 0.0 on the real business because the fields they sum are
absent, not because nothing was held back; and `verify_operator.py`'s check that
no decision records a screen backend of `"off"` **passes by omission**, because
the archive records no backend at all. Re-running the ladder over the same
inputs would produce rows at the current schema, which is the fix — rewriting the
archive in place would not be.

**Two limits, stated rather than papered over.** The owner's leg is a raw key by
construction: `ACR_OWNER_PRIVATE_KEY` wins over a Circle owner wallet in
`build_role_signer`, and `spendApproved` can never work with a Circle smart
account because `ecrecover` cannot check a contract — so `spendAsOwner` is the
only Circle-compatible owner route. And the screen is not Circle's: nothing in
Circle's SDK surface here screens an address, so `addresses_screened` is counted
against a denylist or OpenSanctions and named for what it is.

## A budget is a limit; an agreement is a commitment

The `PolicyWallet` says *"you may spend at most 1 USDC on machine-services, and
at most 0.05 in any one payment."* That is a **limit**. It is the right thing to
put on chain — the contract says so itself: *"two numbers per category, because
a third would be a number somebody could get wrong"*, and *"REFUSE, NEVER
CLAMP"* — and it is not what an accounts-payable clerk reconciles against.

A clerk matches three things: what was agreed, what arrived, and what was
billed. Prior Art #03 calls it the symbolon, after the object broken in two
whose halves had to fit. This repo had two of the three — the vendor's invoice,
and our own meter counting what actually arrived (Prior Art #06). The missing
half was the agreement.

`operator_commitments.jsonl` is the register: payee, service, unit price, max
quantity, window. It is **signed off chain and committed on chain** — the
agreement's hash rides inside the decision record that `PolicyWallet` already
hashes into the paying transaction, so the chain commits to *"this payment was
made against that agreement"* with no new contract and no redeploy.

**It also unblocks something.** The agent escalates any bill it cannot price
above a 1 USDC ceiling, and a benchmark needs two independent sellers of the
same unit — which a contractor's hourly rate and a SaaS seat price can never
have. So every bill of real size was unpayable by construction, whatever its
kind. A market needs competitors. **An agreement needs none, because it is what
we agreed.** The price question now has three answers rather than two, and the
ceiling is the last resort instead of the only one.

Four orderings, each a decision worth arguing with:

- **The meter outranks the agreement.** An agreement says what we would pay for
  work done. It does not say the work was done.
- **The budget outranks the agreement.** A commitment is not authority to exceed
  the wallet, and the contract refuses rather than paying what is left.
- **The agreement outranks the market.** A bill inside a commitment is not
  rerouted, because we are honouring something we wrote down rather than
  shopping. What the cheaper offer would have been worth is *recorded* — "a
  cheaper offer exists at 0.2, worth 84 USDC if this is renegotiated" — so a
  human can reopen the agreement. Breaking it is their call, not the agent's.
- **The window outranks everything.** An expired agreement is not a cheaper one;
  it is none.

`held_back_usdc` is what this earns: money a vendor asked for, outside an
agreement we had written down, that did not leave the wallet. It sits directly
beneath `saved_usdc` on `/spend` because the two look alike and are not — a
reroute's saving is measured against another seller's **offer** and nothing was
bought, which is why the ledger refuses to book it as income. One is a
counterfactual. The other is defensible against the bank statement, and they
must never be added together.

## A budget is permission; cash is money

The operator spent most of this build checking every bill against a **budget**:
`cap - spent`, read from `PolicyWallet`'s own counters and enforced on chain.
That is the right check and it answers the wrong question. A budget is
permission. It reads 988 of 1,000 remaining on a wallet holding 2 USDC, the
payment then reverts on chain, and an owner could read a healthy allowance
beside an escalation queue the wallet could not possibly settle.

RFB 4's list of what the agent decides opens with *"whether there is enough
liquidity to cover what is due, and what is due next"*, so the ladder gained a
rung — check 9, after the budget — and the statement gained a section. Three
things about it are deliberate:

- **It reads the ERC-20 view, at six decimals.** On Arc, USDC is the native gas
  token at 18 decimals *and* an ERC-20 at `0x3600…` at 6. `PolicyWallet` holds
  and `transfer`s the ERC-20, so that is the pile `spend` can actually move;
  `eth_getBalance` would answer confidently about a different one.
  [`PolicyWallet.sol`](../contracts/src/PolicyWallet.sol)'s own comment states
  the stakes: *"Mixing them is a 1e12 error that looks like a fat finger."* The
  token address comes from the wallet's `usdc()` getter rather than from
  configuration, so the figure cannot drift from what `spend` transfers.
- **A read that failed is not an empty wallet.** `held_usdc` is `null` when the
  chain could not be reached, never `0.0`, and the statement prints the reason.
  An unfunded wallet and an unreachable node are different things to do on a
  Monday morning, and a confident zero would escalate every bill in the queue
  on the strength of an RPC that happened to time out. So this check fails
  **open**, which the counterparty screen deliberately does not — and the
  asymmetry is the point. A screen that cannot answer is withholding
  information about *this* payee, which is a reason to stop that payment. A
  balance that cannot be read is no information about any of them, and
  stopping on it would halt the whole queue the moment a node wobbled.
- **A bill with no due date is counted apart, never folded in.** *"We do not
  know when this is due"* is not *"it is due later"*, and adding it to the
  dated total would make the figure look more precise while covering less. The
  count of undated bills is printed beside the total so the number can say what
  it does not include.

The outcome is `escalate` with a recommendation of `hold`, not `refuse`: the
bill may be perfectly sound and the owner may fund the wallet, so waiting is
the honest advice when the only thing missing is money. Refusing would record a
judgement about an invoice on the strength of our own bank balance.

**What this is not.** It is not the forecast described in the next section.
There is no burn rate here, no runway in days and no yield figure — it is what
is held *now* against what is dated *now*, inside a thirty-day window. Calling
it a forecast would be the overstatement this file keeps refusing.

## Idle cash: what it would take, and what we did not build

RFB 1 asks for an agent that puts idle reserves to work. Prior Art #02 is the
Parable of the Talents, where the servant who buried the silver is the only one
rebuked — *"you ought to have deposited my money with the bankers"* — and it
names the hard part correctly: **not the yield, the timing.** Redeem too late and
payroll does not clear.

We did not build it, and this is the research rather than an excuse.

**USYC is reachable on Arc, and we checked rather than taking the page's word.**
Read first-hand over the public testnet RPC on 2026-10-04 (chain 5042002):
`0xe9185F0c5F296Ed1797AaE4238D26CCaBEadb86C` holds 183 bytes of proxy code,
answers `symbol() == "USYC"`, `decimals() == 6`, and reports a testnet supply of
1,377,750.228757. The Teller that mints and redeems it,
`0x9fdF14c5B14173D74C08Af27AebFf39240dC105A`, is deployed too — `buy()` takes
USDC and gives USYC, `sell()` goes back. Mainnet USYC is
`0x8a5D989Bbb96929F689B0200f435f53dA42bF490`.

**What stops it is an allowlist, not a contract.** Circle's own documentation is
explicit: USYC is for institutions outside the United States, with eligibility
restrictions and a $100,000 minimum. On testnet the path is narrower but still
gated — get testnet USDC from the faucet, then **open a Circle Support ticket
asking for the Arc testnet wallet to be allowlisted, which takes 24 to 48
hours**, and only then can the Teller be called. We could not read our own
eligibility to confirm it: the Teller's allowlist accessor is not under any of
the three usual names, so the real ABI is needed and guessing at it would be
worse than saying so.

**So the honest position.** The capability is three lines of contract call
behind a human approval with a two-day lead time. The part that is actually
ours, and the part most treasury bots guess at, is the **forecast**: how much is
provably idle past the longest committed outflow. We already hold what that
needs — the obligations, their windows, the agreements they were made under, and
now the balance itself — and none of it requires USYC. The section above is the
first half of that measurement and stops short of the second on purpose: what
is held against what is dated is a reading, and *how much is provably idle* is a
claim about the future. If the allowlist lands, the yield leg is
small. If it does not, the forecast is still worth having and still honest,
because it reports a number without asserting a rate.

What we will not do is claim the loop. `operator.py`'s timing rule already says
*"the cash is worth more here"* with no rate behind it, and that sentence is an
assertion looking for evidence. Putting a yield figure next to it without the
flow that earned it would be the same overclaim one step further on.

## The four decisions worth arguing with

**PAR is observed quotes, never the published index.**
[`anchors/GAP.md`](../anchors/GAP.md) records this project's own index reference
levels sitting 20× to 1250× away from real market prices, deliberately frozen
because they seed the simulator and the tape's price pin. Measuring a real
vendor's bill against that print reads as a ~9,990 bp discount on an invoice
that is in fact above the going rate. So the benchmark is built from prices we
watched somebody offer or accept for the same service, at the same **unit** —
measured on our own tape, no resource has more than one seller, while `$/1k
tokens` has four spanning 1.3×. The index survives on the statement only as
basis points, labelled, because bp is scale-invariant and dollars are not.

**The budget is a contract, and the threshold is a key.** `PolicyWallet` holds
the USDC: per-category caps, a per-transaction limit, and refusal rather than
clamping. Above the limit the agent's `spend` reverts and the only path is
`spendAsOwner`, which the contract gates on `msg.sender == owner` — the owner's
own wallet sends the transaction. Not a signature the agent relays and not a
flag in a database. `ecrecover` cannot check a smart-contract account
([`WALLETS.md`](WALLETS.md) C1), so a Circle PIN-secured wallet could never
clear a signature path; calling the contract needs no signature scheme at all,
and nothing can go stale between deciding and paying.

**A screen that cannot answer is never "clear".** Counterparty risk has three
states, and the middle one is the point: `unknown` is not `clear`, because a
screening service that times out must not read as a clean bill of health. An
unknown does not stop the business by default — it is recorded on every
decision — and `ACR_SCREEN_REQUIRED=1` turns it into an escalation.

**The ledger carries what a ledger misses.** *Agents and Ledgers* opens: a
ledger checks that debits equal credits, not that the vendor was right, the
invoice real, or the retry unpaid twice. Every transaction here is annotated
with the rule that fired, what we independently metered, the par it was checked
against, what the screen said, and the hash the chain holds. A reroute moved no
money, so it is a note and not a transaction: booking an avoided overpay as
income would be inventing a credit in the format that makes it look official.

## Running it

```bash
make operator-run BUSINESS=acr-fleet        # dry run: price, meter, decide
make ledger BUSINESS=acr-fleet              # the beancount file
make verify-operator                        # prove a deployment
```

`--live` is refused for a business with no `PolicyWallet` rather than doing a dry
run under the wrong name.

### Without being asked

Those are all a person typing. The operator also runs on its own clock inside
the press, which is what makes "settled without a human touching them" a thing
the record shows rather than a thing the design permits:

| `ACR_OPERATOR_AUTORUN` | what happens |
|---|---|
| `off` | nothing. The default every checkout, laptop and CI run gets |
| `dry` | it wakes on its cadence, prices, meters, screens and decides — and pays nothing |
| `live` | it also pays what clears policy |

`dry` is not a formality. The first question about any new loop is whether it
ticks at all on that host, and that should be answerable before a payment
depends on it — the deployed press currently reports `checked_at: null` for all
three of the venue keeper's chores, which is exactly the state `dry` exists to
rule out. `GET /health` carries the operator's mode, its last verdict and both
clocks (when it was last checked, and when it last did work); `/ops` reports the
same and warns when an armed loop has missed a period.

**In the press and not in GitHub Actions**, for the reason this repo already
wrote down when it retired two scheduled workflows on 2026-08-03: a cron'd run
would need the credentials in CI, which is a strictly larger blast radius than
the scoped key it would replace. A cadence in Actions would also be inert on any
branch but the default.

Three things it will not do. It never clears an escalation — that stays behind
the token-gated `POST /ops/actions`, because a loop approving its own
escalations would make the autonomy figure meaningless rather than better. Its
interval has a floor no environment variable can lower, so a typo cannot turn it
into a tight loop against a `PolicyWallet`. And a tick that cannot read the
settlement tape says so and bills nothing, rather than reporting that there is
nothing to bill.
