# Canteen submission update — ACR / Agoranomos

*Tameion Agents Hackathon · Canteen × Circle × Arc · window 27 September –
17 October 2026. Written 13 October. Every figure below names the command or the
chain read that produced it, so a judge can re-run any of them.*

The form may be submitted repeatedly before the deadline. This file is the text
behind each submission, kept in the repo so the figures a judge reads here are
the figures on the form.

---

## The three traction questions, answered

### How many businesses have you onboarded?

**Two real, one sandbox.**

| slug | what it is | chain | has a PolicyWallet |
|---|---|---|---|
| `acr-fleet` | the buyer-agent fleet whose bills the operator prices | testnet 5042002 | yes |
| `acr-operator` | ACR's own treasury, onboarded 12 October | testnet 5042002 | yes |
| `sandbox` | fixtures, excluded from every count | testnet | no |

`GET /operator/businesses` · counts are computed from the registry at request
time, never maintained by hand. The sandbox row is excluded from `real()` by
construction, which is why the page says one or two and never three.

**One of those two is us**, and we say so in the registry row itself rather than
in a footnote. Canteen's FAQ Q3 allows it ("Yours counts"), and a reader who
cannot tell our own treasury from a customer's cannot judge the number.

### How much value has the agent moved?

**0.158773 USDC of agent-decided payments**, on Arc testnet.

| | USDC | source |
|---|---|---|
| 19 logged decisions | 0.154363 | `operator_decisions.jsonl`, and the **on-chain `spent` of `0xA755f87B…` agrees to the last decimal** |
| one live `decide()` run | 0.004410 | tx `0x05e01c72ddcf713227c22cf6e635f18c995d521d634463bf300f1e4e1c0d3446` |
| **total** | **0.158773** | |

**Separately, and never added to that line: 0.150000 USDC moved as a capability
proof** — the three authorities of the contract, exercised once each so that the
escalation path is a fact rather than a diagram. Those are not business
obligations and counting them as value moved would be the exact inflation this
project refuses elsewhere.

**And what the agent ASSESSED is the larger number: 0.719597 USDC priced**, with
**0.059193 USDC of overpay found by rerouting**. `moved` and `priced` are
different claims and the product keeps them apart on `/traction`; so does this.

> **A gap we would rather state than have found.** `GET /operator/traction`
> reports `moved_usdc` of **0.154363** — it computes from the decision log, and
> the four payments on the second wallet were never archived because the press
> runs on a tier with no disk. So the live page **undercounts**, and the
> reconciliation is the table above. Both wallets are in the repo; finding them
> explained is different from finding them.

### What problems are you solving for them?

A business's software bills arrive already totalled by the vendor that counted
them. We meter consumption independently, price the bill against **published
third-party list prices** rather than our own index, and let an agent pay,
reroute, hold or escalate — inside a contract it cannot talk its way past.

That is Prior Art #06 directly: *"a seller who supplies both the goods and the
measuring cup will eventually supply a smaller cup."* Our standard measure is
`anchors/` — dated files of real vendor list prices, each row carrying its source
URL and the line quoted — and `GET /par` is the agoranomos' cup, open to anyone
with no key, no card and no payment.

Measured on the 19 decisions: **6 of 19 were reroutes** rather than payments,
and the overpay found that way is the 0.059193 above. An agent that pays
everything is a cron job; the refusals are the product.

---

## 1. The PolicyWallet integration

A contract the agent spends **through**, not a policy it is asked to respect.

**It refuses rather than clamps.** A payment over budget reverts; it is never
quietly reduced to what fits. A clamped payment is a decision nobody made and
nobody can review.

**Three authorities, and the threshold is literal.** `perTxLimit` is **0.050000
USDC** on the live wallet, and `spend()` requires `amount < perTxLimit` —
strictly, so 0.049999 is the agent's largest unilateral payment and 0.050000
escalates. All three were exercised on chain:

| rung | USDC | who sent it | tx |
|---|---|---|---|
| `spend` | 0.030000 | the agent, alone | `0x0c160702df907fa23468d565c8abfa57f10a239fd024c0ee39f49c01527e8827` |
| `spendApproved` | 0.080000 | the agent **relaying the owner's EIP-712 signature** | `0xd256b1d7c9a991e25d14e6bbc52575641fdf119bee6fc54884d5f9604ed84ed6` |
| `spendAsOwner` | 0.040000 | the owner's **own wallet** | `0x36bc7233e050ed1b5754d61f8c407c675515bd01436438fe087207df24a23c4e` |

`approvalNonce` went **0 → 1**, which is the first time `spendApproved` has
executed on any PolicyWallet on any chain.

`spendAsOwner` exists for a reason worth naming: `ecrecover` cannot validate a
smart-contract account, so an owner whose wallet is a Circle PIN-secured SCA
could never clear `spendApproved`. Calling the contract directly needs no
signature scheme at all.

**What still binds above the threshold.** An owner approval lifts the
per-transaction ceiling, **not the budget** — the period cap still refuses, so
approving one invoice cannot silently reopen a category the owner closed. And
`paused` outranks every authority including the owner's own wallet.

**The ten ordered refusals.** `decide()` is a pure function with ten refusals in
order — duplicate, payable, meter, counterparty, agreement, benchmarked, price,
timing, budget, cash — plus an eleventh outcome outside it. The two worth
calling out:

- **Budget is permission; cash is money.** `cap - spent` reads healthy on an
  empty wallet, so the cash rung reads the **actual ERC-20 balance** through the
  wallet's own `usdc()` getter. `eth_getBalance` would read Arc's 18-decimal
  native pot and be wrong by 1e12 — a difference that looks like a fat finger.
- **The counterparty screen reports its source.** A verdict of `clear` produced
  by comparing against a list of zero addresses is indistinguishable from a real
  sanctions check unless the backend is in the record. It is.

Live wallet, read today: `0xa67F188746f479A9d93D112BeeB7aadB90bE834b` on
**Arc testnet 5042002**, holding 19.845590 USDC, `paused` false, cap 1.000000
per 30 days, 0.845590 remaining. `isAgent(owner())` is **false** — the contract
refuses one key being both authorities, which is what makes the escalation mean
anything.

There is a second PolicyWallet, `0xA755f87B…`, holding the history behind the 19
logged decisions. We had recorded its owner key as lost; on 13 October we found
its `owner()` is the **arc-canteen CLI wallet**, so it is reachable after all.
We stay on the newer wallet by choice — a clean budget and an owner key held
directly — and the correction is in the repo rather than quietly dropped,
because a judge reading two wallets deserves to know which claim moved.

---

## 2. What moved during the window — the delta

Canteen's FAQ Q6 judges a project already in flight on **progress since**, so
this section is the delta: **198 commits across 378 files, 27 September to
13 October**. Four are worth a judge's time because each was a measurement that
contradicted something we believed.

**The seller ladder stopped at a healthy host.** The terminal's walk signal was
`status >= 500`. A **404** is not a host failure — so the ladder stopped on the
first press, which answers 44 routes and 404s `/par` and all five `/operator/*`,
while the next press in the same list serves all 50. `/spend` rendered an
absence with the rows one line below it. The fix distinguishes "no such
business" from "this deployment predates the route" by asking the host's own
`/openapi.json`. Measured consequence: the live default chain went from
`absent` five times out of five to `ok` five out of five.

**A six-day warning reached CI and never the product.** The market basket has a
thirty-day cliff, and past it `/par` stops using published prices and reprices a
stranger's bill against our own sellers at 20×–1250× off market. A warning at
0.8 of that window existed — in a script, so the only thing that ever consulted
it was `make ci`. For six days the gate would go red while the product answered
`ok`. The basket now states its own age and the band it is in, and
`make anchors-live` asks a **running press** rather than the working tree,
because the gate read the repo while the press reads what was baked into its
image.

**A number that could not say its age.** `/par` shipped `fetched_at` as a bare
unix float, so nothing on screen could tell a one-day-old basket from a
twenty-nine-day-old one. It now carries `age_s` beside `max_age_s`, and the page
reads `2026-10-06 · 7 days ago · 30 day limit`.

**The MCP plugin told an agent a benchmark had fallen back when it had not.**
Adding the `aging` band broke a consumer we own: its refusal branch emitted *"its
comparison fell back to whatever `benchmarked_against` names"* for a basket that
was still pricing against the market. An integrating agent acting on that would
discard a sound verdict. Fixed, and the vocabulary is now a membership test that
fails safe.

**On tooling, two gates that did not exist before this window.**
`make rehearse` walks the product's own path against the live hosts and reports
what a viewer sees rather than whether a request returned 200;
`make rehearse-pages` drives the real pages in a headless browser, because every
page here is client-rendered and a green API proves nothing about the screen.
The first one found a live fault on its first run. The second one found that
`/spend`'s cash figure cannot be read on the press that holds the wallets.

### Circle and Arc, by which product does which job

| product | the job it does here |
|---|---|
| **Contracts** | `PolicyWallet` — the budget the agent cannot exceed, and the three authorities above |
| **Gateway** | the x402 paywall on metered reads; settlement is off-chain, returning a batch UUID rather than a tx hash |
| **Wallets** | custody for the press signer and the venue's maker/taker, kept apart so one credential is not four jobs |
| **Paymaster** | third-party fee sponsorship, decoded from the ERC-4337 `UserOperationEvent` paymaster topic rather than inferred |
| **CCTP / Bridge** | inbound funding from seven chains |
| **USDC on Arc** | native gas, which is why 19 decisions and four ladder payments cost cents rather than mattering |

One framing correction we make in our own docs, because it is easy to overclaim:
Arc makes USDC the native gas token, so "gasless, the vendor never holds a gas
token" is true of **everyone** on Arc and claims nothing. The real Paymaster
claim is third-party sponsorship, and that is the one we prove.

---

## 3. The Warns integration

A third party now signs the decision an on-chain payment commits to.

Warns reaches its own verdict on an invoice, signs it, and returns an id. The
record carries **`warns_decision_id` inside the hash** and **`warns_signature`
outside it**:

- The id is hashed, so the on-chain commitment names *which* decision authorised
  the payment. `spendApproved` binds `decisionHash` into the EIP-712 digest it
  recovers, so the owner's signature then covers a **Warns-signed decision**
  rather than just an amount and a payee.
- The signature cannot be hashed — it signs that hash, and a signature inside the
  record it signs is circular. It rides beside the commitment, not in it.

Verified end to end: the id moves the hash (`0x21cc9844…` → `0x68cd27b4…`) and
the signature leaves it unchanged, which is what makes it signable at all.

**It needed no contract change.** Their premise was that the contract hashes the
record; it does not. `decisionHash` is a `bytes32` **parameter**, and the only
on-chain rule is that it is non-zero. So a new field is a field, not a migration.

**The conflict we found before they hit it.** A TypeScript reimplementation of
our hash provably cannot work:

```
python  {"at":1791801414.0,"metered_quantity":10.0,...}
node    {"at":1791801414,  "metered_quantity":10,  ...}
```

Python keeps the `.0` on a whole-valued float, JavaScript drops it, and **from
JSON alone a reimplementation cannot tell which fields are floats**. Four hashed
fields hold whole floats in our live archive. So they sign the 32 bytes we
compute, there is one implementation, and `tests/test_decision_hash_contract.py`
**fails if anyone adds a second one** in TS, Solidity, JS, Go or Rust.
`tests/fixtures/decision_hash_vectors.json` publishes six conformance vectors so
a mismatch is loud.

We also proved the six payments already committed on chain still re-derive their
original hashes after the field was added — adding a dataclass field is safe for
history; writing one into the stored rows would not be.

The partner-facing version is [`INTEGRATION-WARNS.md`](INTEGRATION-WARNS.md).

---

## Where to look, and what is not finished

**Hands-on:** `https://arc-compute-rate.vercel.app` — the network switch is
asymmetric on purpose; the operator pages are on **testnet**, which is where the
decisions and the PolicyWallet live.

**One call, no key, no account, no payment:**

```bash
curl -s 'https://acr-api-1fto.onrender.com/par?unit=%24%2F1k%20tokens&billed_usdc=0.02&quantity=10'
```

Read `basket.status` before the verdict: `ok` and `aging` price against the
market, the SCREAMING values mean it fell back to our own sellers, and
`benchmarked_against` says which happened.

**Not finished, stated plainly:**

- **Mainnet is deployed, the press is unfunded.** Seven contracts have code on
  chain 5042; the press wallet holds 0.005273 USDC, so it cannot post, and the
  last mainnet print is around 37 hours old. All the operator traction above is
  testnet, which Canteen's own rules count.
- **`settled_on_time` is 0 of 0.** The entitlements register is wired and empty,
  so the timing rung cannot fire in production. A register row asserts money is
  owed, and we will not invent one.
- **`/operator/traction` undercounts**, as reconciled above.
- **The `HELD` figure on `/spend` reads unknown**, because the press that holds
  the PolicyWallets runs an image without the `usdc()` getter the balance read
  needs. The number is computable; that host is not ours to redeploy.
