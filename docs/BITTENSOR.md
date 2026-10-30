# ACR on Bittensor

Two separate things, and the first matters more than the second.

1. **Bittensor is where the real prices are.** Subnets 64 and 51 sell exactly what
   ACR-INF and ACR-GPU measure, and they publish prices over unauthenticated HTTP.
   Wiring them in replaces `tape_source: sim` with observed market data.
2. **A subnet is how the rate stops having an administrator.** The estimator runs as
   competing miners scored on accuracy, manipulation resistance and honesty, so the
   published rate is a consensus rather than our opinion.

Registering a netuid is **not** part of this. It costs **259.04 TAO** — read from
`/api/subnet/registration_cost/latest/v1` at block 9388680 on 2026-10-29, where it
arrives as `259043206400` RAO. An earlier draft of this document said ~349 TAO, taken
from an unofficial dashboard; that figure was wrong. It is volatile and a sunk
cost that becomes the subnet's initial pool reserve rather than a deposit — check
`btcli subnet lock_cost` before quoting any figure) and buys the index nothing in
correctness. Everything below runs locally, offline, with no TAO.

```bash
make subnet-demo        # four competing miners, scored and weighted. Offline.
make subnet-demo-real   # publishes from the recorded subnet 64 tape (offline fixture)
make subnet-gate        # asserts the subnet pays the right miner
make subnet-oracle      # posts the consensus print to ACROracle on its own anvil
make subnet-snapshot    # refresh the tape fixtures (network; manual, never CI)
```

## 0 · The chain loop

`make subnet-oracle` deploys `ACROracle` on a local anvil, posts each consensus print,
and reads the last one back. That step is what makes this a benchmark rather than a
scoreboard: a reference rate is a number something *settles against*, and until a print
reaches a contract that enforces the benchmark invariants, the mechanism is a
well-tested opinion.

It targets **v1 deliberately**. `consensus_print` produces an `ACRPrint` with no
`policy_hash` and no window, and `ACROracleV2` requires both. Inventing a policy hash
to satisfy a v2 field would claim the print was made under a cleaning policy it was not
made under — and on single-venue tape the honest answer is that it was made under a
declared variant with no attack cost at all.

Two invariants are checked **before** any transaction, because a revert costs a deploy
and tells you less than a named assertion: timestamps must ascend per index
(`ACROracle` enforces `timestamp > prev.timestamp`), and the interval must contain the
value. The Fixing-clock `ts` is safe — the skew guard is one-sided
(`ts <= block.timestamp + 900`), so 3600 against a real block timestamp passes
trivially. Ordering is the only timestamp risk.

**One thing not to misread.** A print carrying `attack_cost_per_bp = None` does not
revert: `ACROracle` refuses a zero bound, so `PostPayload.from_print` floors it to one
unit and the read-back shows `1e-06`. That is a wire-format floor, **not a
measurement**. Reading it as "this feed costs a millionth of a dollar to move" is
exactly backwards — it means no bound was computed at all.

**Port note.** The target runs its own anvil on `SUBNET_ANVIL_PORT` (8547) with an
explicit `--chain-id 31337`, rather than inheriting whatever is on 8545. On the
development machine 8545 was occupied by another project's `anvil --chain-id 133`,
which made `scripts/futures_roll.py` fail with `chain id 133 == 31337` — those four
tests now skip with that reason named, and they pass against a correct node.

---

## 1 · The data

Measured 2026-10-29. All unauthenticated. These are live third-party services, so
re-measure before relying on any figure here.

### Chutes — netuid 64, inference → **ACR-INF**

| Endpoint | What it gives |
|---|---|
| `api.chutes.ai/invocations/usage` | USD billed + invocation count, per model per day. ~12 days of history |
| `api.chutes.ai/invocations/stats/llm` | `total_input_tokens`, `total_output_tokens`, `average_tps`, `average_ttft`, per model per day (payload >5 MB) |
| `llm.chutes.ai/v1/models` | per-model list price, in **USD and TAO**, per 1M tokens |
| `api.chutes.ai/pricing` | `tao_usd`, plus per-GPU USD/TAO per hour and per second |

Joining the first two on `(chute_id, date)` gives **realized `$/1k tokens`** — what
buyers actually paid, not a rate card. That is the only genuinely transacted series
either subnet exposes, and it is the one ACR ingests (`acr_tape.ChutesSource`).

`average_ttft` is the first non-simulated value the hedonic stage's `latency_slo_ms`
has ever carried. Measured spread: 0.66 s to 73 s across 186 of 218 priceable rows.

### Lium (ex-Celium) — netuid 51, GPU rental → `anchors/`, **not** tape

`lium.io/api/executors` serves ~160 executors, 17–19 GPU models, `price_per_gpu`
$0.20–$12.40/hr, with `gpu_class`, `tier`, `reliability_score`, `nvlink`, `location`
and `miner_hotkey`. (`api.lium.io` and `celiumcompute.ai` do not resolve.)

It is deliberately **not** a tape source. `price_per_gpu` is an **ask** on inventory
that is ~93% idle (measured: 582 GPUs, 544 free), and an unexecuted offer from a
seller with no buyers is a reservation price. `docs/methodology.md` §1 defines the
estimand as what a buyer *would pay*, estimated from what buyers *did* pay.

It belongs in `anchors/`, and it falsified that basket's own note — **now corrected and
live**. `anchors/_basket/ACR-GPU.json` is a mixed basket at `version: 2`: six Lium
accelerator classes priced over HTTP beside the four curated rate cards, with
`TOOL_VERSION = 2` recording that the rule moved.

Three judgements the switch rests on, all enforced in the basket rather than in the
fetcher so a reader can check them without reading code:

* **Grouped by accelerator class, not per executor.** 94 secure executors give the basket
  a 44× spread, and `anchors.aggregate`'s own docstring says a basket spanning 13× has no
  single price. It would also make every `/par` response a 94-quote payload whose
  `best_seller` is an opaque executor UUID. One row per class keeps it comparable —
  measured spread 7.5×.
* **On-demand only.** The basket always said spot and marketplace bids are a different
  product; Lium labels them `tier`, so the rule is now enforceable rather than
  aspirational. 36 of 130 executors were spot and excluded.
* **A comparable accelerator tier.** H100/H200/B200/B300 — the datacenter parts the
  curated rows are. Including Lium's consumer RTX cards would give a **49.5× gap on a 44×
  spread**, and that figure is disclosed in the basket note rather than buried, because
  excluding rows is exactly the move a reader should be able to check.

**The published gap moved 19.90× → 10.04×**, and it *narrowed*: the real market is pricier
than the curated rate cards implied. `rule.min_priced` went 4 → 10 in the same change, or
the ratchet would have let the Bittensor rows vanish silently and re-medianed the four
rate cards as if nothing had changed. The literal string "20x to 1250x" was load-bearing
prose in **20 files / 24 occurrences** and is now "10x to 1250x"; the four TypeScript
sites are comments, so no terminal or MCP assertion depended on it. (One site was missed
and carried "20x" until 2026-10-30: `tests/test_anchor_gates.py`. A string replaced in 20
files by hand is a string that needs a check, which is why `GAP.md` is generated.)

Real corroboration worth keeping: Lium's H100 80GB HBM3 prices at **$3.00/hr**, between
RunPod's $1.99 and Lambda's $3.99.

Every Lium figure in this section — the 10.04× gap, the 7.5× spread, 36 of 130 spot
excluded, the 49.5× counterfactual — is a **snapshot dated 2026-10-29**, and Lium's
inventory moves fast: the executor count fell 169 → 160 → 132 within hours of the same
day, and distinct `miner_hotkey` with it (47 → 39). `anchors/GAP.md` carries the live
figure and is regenerated by `make anchors-report`; the numbers here are the ones the
switch was decided on, kept so the decision can be audited rather than because they are
current.
`anchors/_basket/ACR-GPU.json` carries `source.kind: "mixed"` — the rate-card rows stay
curated, the six Lium rows are fetched over HTTP. Its note records the old claim ("no GPU
cloud published a clean unauthenticated price JSON") as the thing that *stopped* being
true on 2026-10-29, rather than as the basket's current position.


### Taostats — chain structure, optional

Base `https://api.taostats.io`, header `Authorization: <key>` with **no `Bearer`
prefix**. Free tier: 1 key, **5 credits/min, 10,000/month** — so ≤3 calls per cycle
over a netuid allowlist, cached.

Verified with a live key on 2026-10-29. Every response is `{pagination, data}`.

| Route | Status | What it gives |
|---|---|---|
| `/v1/price?asset=tao` | **200** | TAO/USD. **New scheme — survives 9 Nov** |
| `/api/subnet/distribution/coldkey/v1?netuid=N` | **200** | coldkeys with hotkey counts |
| `/api/subnet/registration_cost/latest/v1` | **200** | lock cost, in RAO, with its block |
| `/api/dtao/subnet_emission/v1?netuid=N` | **200** | `tao_in_pool`, `alpha_in_pool`, `alpha_rewards`; 12,385 rows of history |
| `/api/dtao/pool/latest/v1?netuid=N` | **520** | persistently broken, server-side |

**The broken pool route does not matter**, which is why `ROUTES` does not list it. The
alpha price is the reserve ratio — Taostats' own definition — and the emission route
carries both reserves: netuid 64 reads 8,782,111 / 130,471,567 = **0.0673 TAO**, netuid
51 reads 16,779,644 / 135,914,403 = **0.1235 TAO**. A route that does not work has no
business in the table.

**RAO is not TAO.** `registration_cost` arrives as a *string* in RAO; dividing by 1e9
is the difference between `259043206400` and `259.04`.

**The credit budget binds, and it was measured binding.** A capture run hit the
per-minute limit mid-way and lost netuid 64's coldkey distribution — which is exactly
what the cap exists to make visible rather than silent. A 429 ends the cycle instead of
starting a retry storm, because a retry storm against a 5/min limit loses the month's
budget in an afternoon.

Worth keeping as a sanity check: Taostats says TAO = $248.57, Chutes `/pricing` says
$248.96. Two independent sources, 0.16% apart.

> ⚠ **The `/api/...` routes retire 2026-11-09.** The official SDK
> (`github.com/taostat/ts-sdk`) still points at them, so the replacements are not yet
> knowable. The route table is therefore one dict —
> `acr_tape.taostats_client.ROUTES` — and the cutover is a one-dict edit rather than a
> scattered-f-string hunt. Taostats stays optional: no key means every method returns
> `None`, nothing raises, and the subnet still runs and prints. The key belongs in
> `.env` as `ACR_TAOSTATS_API_KEY` and nowhere else; every test runs off a recorded
> fixture, so CI never needs it.

### What has no source

**ACR-DATA.** Nothing on Bittensor prices served bytes per MB. It stays on the
simulator and is labelled as such. Two honest indices beat four decorative ones.

---

## 2 · Two things the real tape is not

**It is daily, not hourly.** ACR is specified as an hourly fixing; Chutes aggregates
per day. The source declares `native_step_s = 86400` and the harness builds one epoch
per day. An intraday series starts when `make subnet-snapshot` starts polling forward,
not before.

**Its prices are emission-subsidised.** Chutes miners are paid in TAO emission as well
as by buyers, so the observed price sits below an unsubsidised cost of service (one
analysis puts break-even near $1.41/1M tokens against observed sub-$0.30 pricing). ACR
measures what a buyer pays, which is the right estimand — but the market being measured
is subsidised, and a reference rate exists to expose exactly that kind of distortion
rather than to hide it.

### An unexpected corroboration

Realized Chutes inference prices come out around **$0.0003/1k tokens**. ACR-INF's
nominal `reference_level` is **$0.50/1k** — roughly 1,450× higher.

`anchors/GAP.md` independently declares ACR-INF's gap as **1250×** (ceiling 1437.5),
measured from OpenRouter list prices. Two unrelated sources, same order of magnitude.
The anchors mechanism is working, and it says the nominal level needs rebasing.

---

## 3 · The cleaning stage has nothing to clean, and this is measured

Single-venue flow is one aggregate buyer paying many sellers. That funding graph is a
**star**, Louvain returns **one community**, and both the sybil test and the cluster cap
are then comparing a population to itself. The window is destroyed two different ways,
selected by crossing `SYBIL_MAX_SIZE`:

| Graph | Nodes | What `clean()` does |
|---|---|---|
| 40 sellers + 1 buyer | 41 | one community, too big to flag — but it is the only community, so the cap scales **every weight to `cluster_cap`** (0.05). 95% of notional removed, and the bound is understated ~20× because `bound.py` searches against post-cap weight while counting identities against raw |
| 39 sellers + 1 buyer | 40 | small enough to test, no boundary edges by construction, so `I/(I+B) == 1.0` clears `SYBIL_INTERNAL_RATIO` → **the whole window is flagged sybil and zeroed** |

Neither is a defence. Both are a structural test handed a population with no structure,
reporting its own artefact.

**And the real data lands on the fatal side.** Chutes has 19–24 priceable sellers per
day → 20–25 nodes → total zeroing. Lium has ~47 distinct `miner_hotkey` → cap collapse,
only six above the cliff, and filtering to actually-rented GPUs gives 11 hotkeys → 12
nodes → zeroing again.

### The fix, and why it is a pair

`acr_estimator.clean(..., single_venue=True)` disarms the two self-referential stages on
a one-community window and sets `CleaningResult.degenerate_graph`. `estimate_index` then
publishes a **price** and `attack_cost_per_bp=None`.

Both halves matter. A price is recoverable from single-venue flow — a trimmed,
quality-adjusted median of what buyers paid is a real number. An attack cost is not: it
is a claim about how much counterparty diversity an adversary would have to manufacture,
and there is no diversity here to reason about. `ACRPrint` documents `None` as "not
computed" and `ACROracleV2` reads 0 the same way, so refusing is expressible.

Left at the default `single_venue=False`, a fully-zeroed window now raises
`DegenerateGraphError` — named, and from the module that caused it, instead of
"cannot estimate from zero observations" surfacing out of `robust.py` two modules away.

Default-off, so `make golden-check` is unmoved: verified, the sim path still reproduces
`attack_cost_per_bp = 0.014528085255` exactly.

### What stands in for the absent bound

An earlier version of this section said Taostats' coldkey and IP distributions would
give the graph real structure and make the attack cost publishable. **That was wrong,
and the correction is worth more than the original claim.**

Two things were checked and both close the door:

* **No public endpoint joins chutes to the miners serving them.**
  `/instances/compute_history_csv` is 9.2 MB of `instance_id, compute_multiplier,
  started_at, ended_at`; `reconciliation_csv` is `instance_id, deleted_at`. The price
  tape's sellers are models and the metagraph's population is miners, so the bipartite
  seller graph does not exist on public data.
* **`ip` is not a co-location signal.** 256 of 256 rows carry one, which looks usable
  until you count: 8 real addresses, and 248 rows holding the literal string `"0"`.
  That is a redaction.

So the single-buyer star is permanent, `single_venue=True` stays, and
`attack_cost_per_bp` stays `None` on real tape. Settled, not pending.

What **is** directly observable is the thing an attack cost is ultimately a claim
about: how many independent sellers the venue has. `/miner/metagraph` is public and
needs no key, and `acr_tape.chutes_metagraph` turns it into a diagnostic published
beside the print and never as it:

| Measurement | netuid 64, 2026-10-29 |
|---|---|
| hotkeys / coldkeys | 256 / **207** |
| hotkeys per coldkey | `{1: 181, 2: 16, 3: 5, 4: 2, 6: 2, 8: 1}` |
| multi-hotkey actors | **26**, largest controlling **8** |
| second faces (`n_hotkeys - n_coldkeys`) | **49** — about one hotkey in five |
| identity HHI | **0.0070** (diffuse) |
| stake HHI | **0.2417** (concentrated) |
| hotkeys carrying any stake | **58 of 256** |
| `ip` usable | **no** |

The last two rows are the interesting pair: sellers are many and diffuse, while the
stake behind them is held by very few. An identity count on its own would read as
healthier than the venue is, which is why both are reported.

**The distinction from an attack cost is load-bearing, not pedantic.** An attack cost
says what an adversary must spend to move the print and depends on cleaning arithmetic
this tape cannot support. A concentration diagnostic is a direct measurement and
carries no claim about what moving the print would cost. It is denominated in
identities, which is the argument `acr_estimator.human_caps` already makes about
wallets — fees are rounding error, distinct identities are the scarce input — applied
to a venue whose identity structure is, for once, visible.

### The cross-check that validates both sources

Taostats' `/api/subnet/distribution/coldkey/v1?netuid=64` and Chutes'
`/miner/metagraph` are unrelated sources describing the same subnet, and **both report
207 coldkeys behind 256 hotkeys.** Pinned as a test
(`test_taostats.py::test_the_coldkey_count_agrees_with_the_chutes_metagraph`) precisely
because it is redundant: a disagreement would mean one of them is wrong, and that
should surface from a red suite rather than from a reader.

### A hedonic covariate that is real

`/chutes/gpu_count_history` gives `{chute_id, gpu_count}` for 19,069 chutes and joins
**19 of the 26** priced ones. The split is economically meaningful and bimodal — 10
priced chutes on 8 GPUs (DeepSeek-V3.2, Kimi-K3, GLM-5.2, Qwen3-235B, Qwen3.5-397B), 9
on 1 (gemma-4-31B and friends), nothing between.

It **cross-checks** `model_class_for` rather than replacing it: the name heuristic
answers for every row, the GPU join for 73%, so the name leads and the hardware
footprint fills in only where the name carries no size at all. A chute called
`[private]` is `OPEN` by absence of information, and knowing it runs on 8 GPUs is
strictly better information than that.

---

## 3b · ACR-TAO — the rate in TAO, and what alpha buys of its own work

`acr_subnet.denomination`. **A snapshot, not a live feed** — every figure below comes
from the committed fixtures in `tests/fixtures/bt/` as they stood on **2026-10-29**, and
will be wrong the moment `make subnet-snapshot` refreshes them. Regenerate the table with
`make subnet-demo-real`, which prints exactly these rows. They are dated here because
nothing audits them: `scripts/verify_claims.py` parses the suite counts and the on-chain
identifiers, not this, so an undated table would rot in the way this repo's claim gates
exist to prevent.

| | |
|---|---|
| USD per 1k tokens | 0.00014888 |
| TAO/USD | 248.5666 (Taostats) |
| TAO per 1k tokens | 0.000000598936 |
| **1 TAO buys** | **1.67 billion tokens** of inference |
| netuid 64 alpha price | 0.067311 TAO |
| **alpha per 1k tokens** | **0.0000089** |

**It is a change of numéraire, not a fourth index**, and it is deliberately not an entry
in `INDEX_REGISTRY`. Practically, `ALL_INDEX_IDS` is iterated by `gen_golden.py`,
`eval.py`, `rate.py`, `gen_evalset.py` and `anchors.py`, so a fourth id would demand its
own anchor basket, eval gate, golden entry and rate components. Principally it would be
wrong: ACR-INF prices one unit of inference and ACR-TAO prices the same unit in a
different currency. The *good* has not changed, only the money, and conversions do not get
their own index or every rate would need one entry per currency.

**The caveat is the point, not a disclaimer.** A TAO-denominated rate moves when TAO moves
even when compute is flat in dollars: halve TAO and ACR-TAO doubles while nothing happens
in the compute market. That is exactly why ACR's estimand (`methodology.md` §1) is a dollar
rate. So `tao_per_unit` is the right number for a TAO-native payer deciding what to spend,
and the **wrong** number for reading the compute market, because a move in it is two
signals added together and you cannot tell which moved.
`test_denomination.py::test_the_tao_rate_moves_when_tao_moves_and_compute_does_not`
asserts that directly — a test that demonstrates a limitation beats a sentence claiming one.

**`alpha_per_unit` is the genuinely new number.** It is how much of a subnet's alpha token
buys one unit of the work that subnet actually delivers. No existing Bittensor metric is
this: an alpha price is a claim on future *emission* — a number about the token — and has
never been quotable against delivered compute. Read it as a ratio, not a valuation: it says
what the token currently buys, not whether it is cheap.

**The two TAO/USD feeds are not averaged.** Taostats `/v1/price` and Chutes
`/pricing.tao_usd` agreed to 0.16% (248.57 vs 248.96). One is used, which one is recorded
on the result, and the other is a cross-check — because an average of a good feed and a
stale one has no provenance, looks healthier than either input, and hides the single thing
worth noticing.

---

## 4 · The mechanism

Each epoch, every miner submits a `RatePrint`: a value, an interval, and a **declared
cost to move its own print one basis point**. That last field is why this is not just a
number — it is a falsifiable claim, and the validator tries to falsify it.

Scored three ways, combined by **weighted geometric mean** so that an accurate but
trivially manipulable miner scores zero:

| Component | Weight | Question |
|---|---|---|
| accuracy | 0.50 | pinball loss against a latent the miner could not see |
| resistance | 0.35 | bp moved per USDC, re-running *that miner's* estimator on attacked tape |
| attainability | 0.15 | is the declared cost real, in **either** direction |

### Four departures from the frozen submission prose

`hackathon/bittensor-2026/PRODUCT-DESCRIPTION.md` is preserved as submitted. Four of its
statements produced a mechanism that pays the wrong miner, and the code diverges:

**(a) Calibration is a gate, not a scored component — and it has no lower bound.**
Winkler "charges for width and for missing together" only when coverage is near
nominal. This engine covers 1–2 windows in 12 against a nominal 95%
(`tests/rate/baseline.json`), where the `(2/α)·miss` term swamps the width term and
Winkler degenerates into "minimise the miss" — bought by widening. Scored as written, a
`naive_vwap` miner with a fabricated ±500 bp band outranks the real estimator. Worse, a
*floor* on coverage eliminates the field: at 0.50 the honest four-pillar miner was
zeroed alongside the strawman and nothing was published; at 0.10 a run that missed every
window did the same. So only the ceiling is enforced, coverage is reported on every
scoreboard, and fixing calibration is left to the competitive field — which is the
submission's own argument, and which requires the field to exist first.

**(b) Accuracy is dropped, not zeroed, on real tape.** There is no latent behind a live
market. Under a geometric mean a zero annihilates the product, so a real-tape epoch
would score everyone zero. Absent components are omitted and the remaining weights
renormalised — the idiom `scripts/rate.py` already uses. A real-tape epoch therefore
does not rank anyone; it **publishes**, under weights carried from the last epoch that
could score. That is how a benchmark is actually run: validate the methodology against
known truth, then apply it to live data.

**(c) Resistance is denominated in fees burned, not notional injected.**
`run_optimal_attack` returns `injected_raw` (notional) and `rel_move_realized`
(relative). Dividing one by the other overstates resistance by the reciprocal of the fee
rate — ~200× at `usdc_fee_bps = 1.0`. `attack_fees_usdc` prices what the harness
actually executes, which is not what `bound.cost_of` charges: the injected ring is
one-directional and self-funding, so there is no reverse leg and the proportional term is
charged twice over there. The dollars are cents either way, because wallets are nearly
free; the scarce input is **identities**, which is why the cluster count is reported.

**(d) Copying is detected and zeroed, not out-scored.** The submission says copying
gains nothing because the ground truth postdates the print. Measured, that is not
enough: a miner resubmitting the previous consensus earned 0.81 accuracy — a stale
reference rate really is almost as good on a slow-moving level — **and perfect
resistance**, because a print that never reads the tape cannot be moved by poisoning the
tape. Both are true, and together they paid the free-rider more than the only
participant doing the work. So a print equal to the last published consensus is rejected
outright. The honest claim is not that copying is impossible; it is that copying is
detectable and punished, and that a subnet where everyone copies has nothing to copy.

### Consensus

The published rate is the incentive-weighted α-trimmed median of the miners' prints —
the same robust statistic the index uses on individual trades, applied one level up to
aggregate estimators. A bounded-influence aggregator over bounded-influence estimates.

---

## 5 · What the local build is and is not

**Is:** the full mechanism, running in-process, deterministic, with 50 tests. Real
Chutes prices in, consensus prints out, `attack_cost_per_bp` correctly refused on
single-venue flow. No chain, no netuid, no TAO, no `bittensor` dependency.

**Is not:** registered. That is now the *only* remaining gap, and it is a funding gap
rather than a code one — see §6. The adapters under `acr_subnet/adapters/` are networked
and SDK-wrapped against the real chain; `RatePrint.commitment` still computes a hash that
nothing submits, because commit-reveal needs a netuid to commit to.

**Not yet measured:** whether real miners beat the published baseline. The reference
estimator is deliberately open, and until someone competes with it the subnet's central
claim — that a field produces a better rate than an administrator — is a design argument
rather than a result.


## 6 · On the real chain

Everything in §5 runs offline on purpose. This section is what happens when it does not.
The adapters live in `packages/acr_subnet/acr_subnet/adapters/` behind an optional extra
(`uv sync --extra bittensor`), so the 1759-test suite still runs on a machine that has
never heard of Bittensor.

**Measured against the public testnet, 2026-10-30.** Block height `8330314` rising to
`8334732` over the session. The metagraph for netuid 1 (`apex`, symbol α) read back
256 of 256 uids, 129 validators, tempo 99. A miner served a print from the real captured
Chutes tape — `0.00008449` $/1k tokens, ci `[0.00008395, 0.00009502]`, with
`declared_cost_per_bp` correctly `None` for the single-venue reason in §3.

### The SDK is not the one that is documented

`bittensor` 11.3.0 has **no `bt.Synapse`, no `bt.axon`, no `bt.dendrite`**. The
neuron-serving framework that `bittensor-subnet-template` and every tutorial still
describe is gone. Code written from memory of it fails at import rather than degrading.
What v11 has is a chain client whose extrinsics are classes — `RegisterSubnet`,
`BurnedRegister`, `ServeAxon`, `SetWeights`, `CommitWeights`/`RevealWeights` — submitted
through `Subtensor.submit_call(call, wallet)`.

The consequence is a cleaner split than the old framework allowed: a miner serves its
print over ordinary HTTP, and `ServeAxon` publishes only where to reach it. The chain
carries an address; it was never carrying the payload. `adapters/bt_chain.py` is the one
file that knows any of this, so a v12 break is one file.

### Resistance over the wire, and the hole in it

The in-process validator measures resistance by re-running each miner's estimator on an
attacked window, which it can do because it holds the object. A networked validator holds
a URL, so the miner must expose `POST /estimate` and be handed the window. Without that
route there is nothing to score at all: with no latent (`truth=None`), no measured cost
and no bp-per-USD, all three pillars drop, every score is zero, and `weights_from_scores`
correctly pays nobody.

With it, the mechanism works over HTTP and pays the right miner. `probe_worst_resistance`
sends one window per member of the adversary family (below) and keeps the worst, so the
networked path carries the same correction as the in-process one. Measured over real sockets
on a 2535-event synthetic epoch, 82 injected events per member and $0.1372 of fees:

| miner | moved under attack (worst member) | measured cost/bp | score | weight |
|---|---|---|---|---|
| four-pillar | 7.20 bp | $0.0190 | 0.5127 | **1.0** |
| naive-vwap | 62.40 bp | $0.0022 | 0.0000 | 0.0 |

The wire reproduces the in-process figures to every digit — 52.506 against 454.802 bp per
USDC, weights `{0: 0.669, 1: 0.331}` with the latent withheld — which is the cross-check that
matters here: the adapter and the engine are measuring the same thing, over a socket and in a
function call.

**This table used to say the opposite about resistance**, and the correction is instructive.
With a single in-band adversary it read four-pillar 17.19 bp against the VWAP's 2.89 bp, and
the note under it said the VWAP "moved *less* and still earned nothing… it was not resistance
that disqualified it but accuracy." The second half of that still holds — a miner cannot buy
the epoch by being hard to push around. The first half was an artefact of attacking only one
of the two estimators, and under the family the VWAP moves **8.7 times further** than the
estimator it was being compared against.

**The weakness, and the canary that now catches half of it.** A miner can tell
`/estimate` from `/print` and answer an attacked window with its clean value. The payoff is
not merely an absent measurement: the attack appears to have bought nothing, so `move_bp`
is 0 and the miner scores **maximally on the pillar it is cheating**. The in-process
harness has no such hole, because it holds the estimator object.

So when an answer does not move, the validator sends a second window — the clean one with
every price multiplied by `CANARY_FACTOR` (1.5) and **nothing else changed** — and checks
that the miner answers it differently. What makes this trust-free is that the only way to
pass is to estimate the window you were given: a miner hoping to spot the probe would have
to recognise a proportional rescale and respond proportionally, and a miner doing that is
estimating, which is the whole thing we wanted. There is no cheap tell to key off, unlike
`/estimate` versus `/print`.

Measured on the real Chutes tape, the four-pillar estimator tracks a uniform rescale
**exactly** — K=1.5 gives ratio 1.500000, K=2.0 gives 2.000000, K=0.5 gives 0.500000 —
which follows from the index being a trimmed median of prices. An echo sits at 1.0. The
check is deliberately *"did it move at all"* rather than *"did it track 1.5×"*: requiring
proportionality would punish a legitimate estimator shaped differently (a floor, a hedonic
term that does not scale), while requiring movement punishes only a **constant** responder,
which is exactly the attack. Honest deviation is 0.5 against a 0.01 bar — 50× headroom.

A caught echo is scored through the existing `score_print(is_copy=True)`, which already
means "this print is not an independent estimate" and already zeroes. That reuse is why
`scoring.py` remains untouched.

**What is still open, stated precisely.** The canary catches echo-by-constant. It does
*not* catch a miner that genuinely estimates but special-cases windows carrying injected
wash flow. And the second mitigation — interleaving unperturbed windows across epochs so a
miner cannot know which probe counts — is **not implemented**. Resistance measured over the
wire therefore remains weaker than resistance measured in process. This is not "closed".

### Two places the chain simply does not carry what is needed

**Nobody announces an axon.** On testnet netuid 1, **0 of 256** neurons had an axon at
all — every one `None`. That is the same shape as the mainnet Chutes finding in §2, where
248 of 256 miners reported `ip` as the string `"0"`. So the validator distinguishes
*unreachable* from *wrong*: a miner that published nothing has no estimate to be scored
zero for, and `score_replies` leaves it out rather than diluting every honest miner's
weight by however many neurons happened to be offline.

**On the real Chutes tape, resistance is unmeasurable too.** The cleaning-evading
adversary needs a funding graph to hide in, and a single-venue star has none — so
`build_attack_family` returns `[]` there (and the single-adversary `build_attack` returns
`None`) exactly as `attack_cost_per_bp` does, and for the same reason. On
Chutes, therefore, accuracy has no latent *and* resistance has no adversary, and an honest
validator pays nobody. The adapter reports this as `attack_note` rather than emitting an
empty weight map that would read as a judgement.

### ACR owns netuid 602

Registered on the public testnet on 2026-10-30. Read back from the chain rather than taken
from the submission receipt — subnet count went 602 → 603 and the new netuid's metagraph
names our keys as owner:

| | |
|---|---|
| netuid | **602** |
| extrinsic | `8334254-0005` |
| block hash | `0x4fdb40c63c3816a3a2749f48abca98fa7f5786091182267afe3f1e239c467004` |
| owner hotkey | `5DUScgoRzLpfdgeHe6fZAJCmsJgBGD9kkooJpbf3V7TQYYNX` |
| owner coldkey | `5HpCkEPEDsNZjSWVygVFLaGQvuZ4iY3Nh6ygNHWz3iqkyD9g` |
| cost | τ1 lock + τ0.001738029 fee |
| at registration | 1/256 uids, 64 validator slots, tempo 360, `subnet_is_active: False` |

The keys are **testnet-only** and were generated without a password on a development
machine; they must never hold value. The subnet's name reads `unknown` until
`SetSubnetIdentity` is called, which is cosmetic.

This matters for one specific reason: it is the first netuid where ACR setting weights is
*legitimate*. Everything before it would have meant injecting this project's prints into
somebody else's consensus.

### What ran on 602, and what each extrinsic is evidence of

Five extrinsics, each confirmed by reading state back rather than by its receipt:

| step | extrinsic | confirmed by |
|---|---|---|
| `RegisterSubnet` | `8334254-0005` | subnet count 602 → 603; our keys named as owner |
| `StartCall` | `8334305-0007` | `subnet_is_active` False → **True** |
| `BurnedRegister` | `8334309-0007` | uid 1 present in the metagraph, a second contestant |
| `SetWeights` #1 | `8334370-0005` | `weights.weights` → `{0: {0: 1.0}}` |
| `SetWeights` #2 | `8334718-0007` | an entry in `timelocked_weight_commits`, **not** a weight vector |

The last row is the one that taught something, and it is why the two are listed separately.

### A receipt attests a commitment, not a weight vector

`commit_reveal_enabled(602)` is **True** and `commit_reveal_period` is **1**. On such a
subnet a plain `SetWeights` is not stored as weights at all. It becomes an entry in
`timelocked_weight_commits`:

```
{1: [{'hotkey': '5DUScgo…YYNX', 'commit_block': 8334718,
      'reveal_round': 33518573, 'reveals_at': 2026-10-30 11:18:03 UTC,
      'ciphertext': '0xb1d995…4145535f47434d5f'}]}
```

The vector is **encrypted to a future drand beacon round** — that trailing
`4145535f47434d5f` is the ASCII `AES_GCM_` — and nobody, the chain included, can read it
until that round publishes. So `success=True` attests that a *sealed commitment* landed.
The weights are not live, and will not be until the reveal and then an epoch step.

Measured, because the delay is not small: commit #1 landed at block 8334370 and the epoch
step at 8334372 showed **every neuron on incentive 0.0**. The first step that acted on it
was **8334732** — about 72 minutes, one whole tempo, after the receipt said Success.

This project reported "`SetWeights` succeeded, the chain acted on it" off a receipt, twice,
before reading `timelocked_weight_commits`. `Chain.commit_reveal_enabled` and the
`weights_pending_reveal` key in `run_epoch`'s result exist so the adapter has to say which
of the two it means, and `stored_matches` reads back what consensus is *using* so a live run
reports the vector in force next to the one it just committed.

Checked against live state rather than a fixture, and in both directions: the vector in force
on 602 reads `{1: 1.0}`, which `stored_matches` **accepts** against the mechanism's full output
`{0: 0.0, 1: 1.0}` — the chain dropped uid 0's zero, and a uid absent from a weight vector is
paid nothing, which is what the zero said — and **rejects** against the superseded self-vote
`{0: 1.0}`. A check that cannot fail is not a check, so both branches were exercised on real
chain data.

The adapter's own entry point reaches 602 — not only the scripts that drove the runs above.
A dry run at block 8334827 resolved `own_uid: 0` through `Metagraph.by_hotkey`, read
`commit_reveal: true`, found `unannounced: 2` (neither neuron announces an axon, for the NAT
reason disclosed below), and therefore produced `weights: {}` with
`weights_carried_forward: true` — the honest output when there is nothing to query, and not
the same thing as nobody deserving payment:

```
uv run python -m acr_subnet.adapters.bt_validator --netuid 602 --wallet acr-test --hotkey miner
```

### The 54.94 Τ was the owner's cut, not a reward the ranking earned

A claim made here and withdrawn. The first run's readback showed uid 0 (the four-pillar
estimator) on `emission=54.940306566Τ` and uid 1 on zero, and that was reported as the
mechanism paying the better estimator. It was not.

At that block **every neuron's `incentive` and `dividends` were 0.0** — because, per above,
no weight vector had been applied yet. The subnet's emission split, read off the chain:

| share | hyperparameter | u64 fraction |
|---|---|---|
| owner | `owner_emission`, `owner_cut_enabled: True` | **0.179995** |
| validators | `validator_emission` | 0.410002 |
| miners | `miner_emission` | 0.410002 |

With incentive and dividends both zero, the only channel left is the **18% owner cut**,
which the subnet owner draws whatever the weights say. 54.94 Τ at an 18% cut implies a
per-epoch subnet emission near 305 Τ, which is consistent. It is not evidence about
estimators.

### A hypothesis about self-votes, refuted by the chain

The vector stored from commit #1 was `{0: {0: 1.0}}` — a **self-vote**, because the
`acr-test` hotkey is simultaneously the subnet owner, the only permitted validator, and the
host of the four-pillar estimator. The reading advanced here was that Yuma zeroes the
self-weight diagonal, so the vector was stored and then discarded, which would explain the
zeros.

**That was wrong, and the chain said so.** At the epoch step `last_step=8334732`, acting on
that same self-vote:

| uid | | incentive | consensus | dividends | rank | emission |
|---|---|---|---|---|---|---|
| 0 | the setter itself | **1.0** | 1.0 | 1.0 | 0.0 | 54.94 → **295.20 Τ** |
| 1 | the other neuron | 0.0 | 0.0 | 0.0 | 0.0 | 0.000 Τ |

Deliberately no estimator names in that table. By block 8334732 the two estimators had
already been swapped between the hotkeys for the run below, so the vector being *acted on*
was commit #1's — cast when uid 0 hosted four-pillar — while uid 0 was by then *serving*
naive-VWAP. The point here concerns the diagonal, not the estimators, and labelling the rows
would import a confusion that the next section exists to remove.

The self-vote was honoured in full. So the zeros at 8334372 meant *"no weights applied
yet"*, not *"discarded"* — one cause, not two — and a guard built on the diagonal-zeroing
premise would have encoded a falsehood. It was designed, and then not built, for that
reason. (`rank` staying 0.0 while `incentive` reads 1.0 is unexplained and not relied on
here.)

What the self-vote does leave is a confound: a validator that is also the winning miner
proves nothing about whether a *ranking* moves a reward, because the same key is on both
sides of it.

### Does the mechanism's ranking move a real reward?

The confound above is removable without spending anything. The scoring code never sees a
uid — `score_replies` ranks estimators, and the uid only decides where the weight lands —
so **which hotkey hosts which estimator is arbitrary metadata**. Swapping them makes the
vote point at somebody else.

So the second run served naive-VWAP on uid 0 (the validator's own key) and four-pillar on
uid 1. The mechanism, run identically — real sockets, real HTTP, the real cleaning-evading
adversary at 82 injected events and $0.0564 of fees — returned:

| uid | estimator | print | bp per $ | cost/bp | weight |
|---|---|---|---|---|---|
| 0 | naive-vwap | 0.469659 | 51.16 | $0.01954 | **0.0** |
| 1 | four-pillar | 0.510479 | 304.82 | $0.00328 | **1.0** |

Four-pillar first, as before: the ranking is indifferent to which socket it sat on, which is
the point. Committed as `8334718-0007` from signer uid 0 — a validator voting **against
itself**, the opposite of the self-dealing the first run could be accused of.

The prediction, recorded before the reveal so that the chain can refute it: when commit #2
reveals and the following epoch step runs, **incentive moves from uid 0 to uid 1**. uid 0
holds incentive 1.0 right now, off the stale self-vote, and it is the *worse* estimator — so
this is not a reward appearing from nothing but a reward being taken off one miner and given
to another because the mechanism said so.

**It moved.** The commitment revealed at 11:18:03 UTC (`timelocked_weight_commits` emptied,
`weights.weights` became `{0: {1: 1.0}}`), and the epoch step at `last_step = 8335092` applied
it. Read back at block 8335278:

| uid | estimator | incentive | consensus | dividends | | emission |
|---|---|---|---|---|---|---|
| 0 | naive-VWAP (and the validator) | 1.0 → **0.0** | 1.0 → **0.0** | **1.0** | paid as a *validator* | 147.600823615 Τ |
| 1 | four-pillar | 0.0 → **1.0** | 0.0 → **1.0** | 0.0 | paid as a *miner* | 147.600823615 Τ |

That is the whole claim of this project, executed on a public chain and verified by reading
state rather than a receipt: **the mechanism ranked two estimators, committed the ranking, and
the chain took the reward off the worse one and gave it to the better one.** The ranking came
from real sockets, real HTTP, the real cleaning-evading adversary and the real scoring engine;
the extrinsic was real; the reassignment is the chain's own.

The roles also separated exactly as they should. uid 0 kept `dividends = 1.0` with
`incentive = 0.0` — it is paid for *validating*, for having set the weights. uid 1 took
`incentive = 1.0` with `dividends = 0.0` — it is paid for *estimating*. A validator voting
against its own miner slot ends up compensated for the judgement rather than for the estimate,
which is the incentive separation the design wants and the first run could not demonstrate.

What this does **not** show: that the ranking was correct about the market. The epoch was
synthetic, for the reason given above — on the real Chutes tape no attack is constructible and
there is no latent, so the honest output is to pay nobody. What it shows is that a correct
ranking, once produced, reaches a real miner's balance.


### Without a latent, resistance-only scoring inverted the ranking — and why

The most important thing measured on netuid 602. It began as a negative result about the
mechanism, and the cause turned out to be narrower and fixable than it first looked: **the
adversary was aimed at one estimator.** Fixed below; the diagnosis is kept because the
measurement is the argument.

Two miners were served over real sockets and scored by the real engine with the real
cleaning-evading adversary (82 injected events, $0.0564 of fees). On a synthetic epoch
whose latent is **0.500000**, the four-pillar estimator printed 0.510479 (2.1% error) and
the naive VWAP printed 0.469659 (6.1% error) — the four-pillar is plainly the better
estimate. Yet:

| scored | four-pillar | naive-vwap | weights |
|---|---|---|---|
| **with** the latent | 0.3410 | 0.0000 | `{0: 1.0, 1: 0.0}` |
| **without** the latent | 0.3857 | **0.9360** | `{0: 0.29, 1: 0.71}` |

Drop accuracy and the mechanism pays the contaminated estimator **71%**.

The cause is **where the attack was aimed**, and the mechanism is exact.
`redteam/optimal_attack.build_injection` prices its wash at `m0 * (1 + 5 * rel_move)`, where
`m0` is the **trimmed weighted median** — the four-pillar estimator's own statistic. At
`rel_move = 1e-4` the injection sits just inside the trim band, which is the efficient way to
shift a median and a nearly pointless way to shift a volume-weighted mean. It moved the
four-pillar print 17.19 bp and the VWAP 2.89 bp, so the VWAP measured as six times more
resistant **while not being the estimator under attack**.

An earlier version of this section guessed the reason: that a VWAP "would be trivially moved
by large-size wash, which this attack does not attempt." **That guess was wrong, and
measuring it is what found the real cause.** Print size barely matters — sweeping
`cluster_size` from 2 to 41 at a fixed budget (prints from $100 down to $4.88) left the VWAP
moving *less* than four-pillar on every setting. What matters is the injection *price*.
Priced outside the trim band, on a $97,090 window at the then-flat $400 notional:

| injection price ÷ m0 | four-pillar | naive-VWAP |
|---|---|---|
| 1.00 — in-band, the only member there used to be | 5.000 bp | 2.890 bp |
| 1.50 | **9.541 bp** | 23.384 bp |
| 3.50 | **9.541 bp** | 105.444 bp |
| 11.00 | **9.541 bp** | 413.168 bp |

Two things in that table. The four-pillar estimator **saturates at 9.541 bp across four
orders of magnitude of injection price**: once the wash leaves the trim band it carries no
weight, and extra extremity buys the attacker nothing. That is the manipulation bound of §3,
visible as a flat line. A volume-weighted mean trims nothing, so it has no such cap and its
displacement keeps growing with the price. *That* is the sense in which the VWAP is not more
robust but differently vulnerable — and it is now measured rather than asserted.

**Whose bug this is.** On first reading this looked like a design-level flaw in the
mechanism. It is not, and the correction is worth stating precisely: departure (b) already
says that a real-tape epoch *"does not rank anyone; it **publishes**, under weights carried
from the last epoch that could score"* — and `validator.py` implements exactly that, holding
`last_weights` and updating the carry only when a scored epoch actually paid somebody.

The engine was right. **The networked validator was the thing that got it wrong**: its
`run_epoch` scored every epoch fresh and ranked on one that could not be scored, which is
the specific act departure (b) forbids. The inversion above is what that mistake buys.

So the measurement corroborates the design rather than indicting it: it is a concrete
demonstration of *why* ranking without a latent is unsafe, which is why the carry-forward
rule exists. The adapter now carries weights forward the same way, and refuses to publish a
ranking it could not earn.

**The fix, and it closes this.** `Validator` now carries an adversary **family** indexed by
target statistic — `ATTACK_FAMILY`, an `in-band` member (`rel_move=1e-4`, aimed at the
trimmed median) and an `out-of-band` member (`rel_move=0.1`, price 1.5× m0, aimed at anything
that does not trim) — and `measure_worst_resistance` scores each miner at its **worst case**
across them. Worst, not mean: resistance is a claim about what an attacker *can* do, and
averaging lets a good score against the attack aimed elsewhere pay for a bad one against the
attack aimed at you, which is precisely the credit the VWAP was collecting.

1.5× rather than something harsher, for two measured reasons. Four-pillar's number is
**invariant** to the multiple — 9.541 bp at 1.05× and at 11× — so the choice cannot flatter
it, and the smallest clearly-out-of-band price therefore *understates* the VWAP's exposure,
which is the safe direction for a gate. And `BP_PER_USD_REF` is 800, so a harsher member
floors the VWAP at exactly 0.0 and the component stops distinguishing bad from worse.

A second, independent defect surfaced while calibrating it: `attack_notional` was a flat
**$400**, and a flat dollar figure cannot mean the same thing on two windows of different
size. The attainability pillar is where that bit, because it compares a miner's declared
`attack_cost_per_bp` — the bound's cost for the *cheapest* attack achieving a reference move —
against the cost per bp the validator measures, and those are commensurable only if the probe
spends roughly what the bound prices. On these windows $400 was 0.33% of honest notional.
Mean attainability across five epochs by probe size, with the family in place:

| probe | % of honest notional | mean attainability | resistance ranked correctly |
|---|---|---|---|
| the old flat $400 | 0.33% | 0.452 | 5/5 |
| | 0.50% | 0.460 | 5/5 |
| **1.00%** | **1.00%** | **0.533** | **5/5** |
| | 2.00% | 0.383 | 5/5 |
| | 3.00% | 0.354 | 5/5 |

So `probe_notional` is now `max(attack_notional, 0.01 × honest notional)` — a fraction, with
the old figure kept as a floor for windows too thin for 1% to buy a measurable move. Note
resistance ranks correctly at **every** budget in that table: the family's discrimination
comes from aim, not spend, which is what frees this knob to serve attainability alone. (Before
the family the same sweep read 0.128 at 0.33% and 0.743 at 1%; most of that gap was the lone
in-band adversary, not the budget. Worth recording because the larger figure was projected
from the single-adversary measurement and is **not** what the family produces.)

**Result, on the same synthetic epochs that produced the inversion:**

| seed | resistance, four-pillar vs VWAP | weights without the latent | before |
|---|---|---|---|
| 4242 | 52.5 vs 454.8 bp/$ | **`{0: 0.669, 1: 0.331}`** | `{0: 0.29, 1: 0.71}` |
| 7 | 44.1 vs 417.8 bp/$ | **`{0: 0.657, 1: 0.343}`** | — |
| 99 | 144.3 vs 538.1 bp/$ | **`{0: 0.679, 1: 0.321}`** | — |

Resistance-only scoring now ranks the better estimator first on every seed, at roughly 2:1,
where it used to pay the contaminated one 71%. The margin on resistance itself went from 6×
*the wrong way* to between 3× and 9× the right way. With the latent, four-pillar still takes
`{0: 1.0, 1: 0.0}` on all three — the fix does not disturb the scorable case.

This does not retire the carry-forward rule. Departure (b) exists because a validator without
a latent cannot measure accuracy at all, and that remains true however well resistance
behaves; a mechanism that *can* rank on resistance alone is a safer mechanism, not a licence
to rank when it should be publishing.

**Still open, and narrower again.** A miner that declares no `attack_cost_per_bp` — as
`VwapMiner` does — is not scored on attainability at all, so declining to make a claim is
cheaper than making one and being measured. The fixes above moved the no-latent ranking far
enough that this no longer decides any epoch, so no scoring rule was changed for it; it is
recorded as an incentive hole, not a fixed bug. The principle it would be fixed under is one
the mechanism already uses: absence the *validator* causes (no latent on real tape) must not
be punished; absence the *miner* chooses should not be free.

A smaller, separate bug was found at the same time and **is** fixed: the adapter's
`score_replies` hardcoded `truth=None`, discarding a latent even on epochs that had one.
Right on real tape, needlessly wrong on synthetic, and it produced exactly the inversion
above. It now passes the epoch's own truth.

### What registration cost

`RegisterSubnet` is wired and refuses mainnet outright. Every price here was **measured
against the live chain** rather than taken from the guides, because the guides are wrong
about it.

**The faucet is not disabled. It is gone.** No faucet, drip or mint extrinsic exists
anywhere in `bittensor` 11.3.0, and `btcli wallet faucet` answers
`No such command 'faucet'`. The widely-repeated advice to *"ask the Bittensor Discord
community for faucet tokens"* describes a mechanism the SDK no longer has.

Prices read off testnet at block 8333832, and then what each one actually cost:

| path | quoted | paid | extrinsic |
|---|---|---|---|
| our own netuid | τ1.000000000 | **τ1 + τ0.001738029 fee** | `RegisterSubnet` |
| a hotkey on an *established* subnet | τ0.0005 | — | `BurnedRegister` |
| a hotkey on our own **fresh** subnet | — | **τ0.902987384** | `BurnedRegister` |
| a hotkey by proof-of-work | 0 TAO | not available | `PowRegister` — **see below** |

**The τ0.0005 figure is right and was still off by ~1800× in practice.** It is the
`min_burn` of an *established* subnet. A newly created subnet starts its burn near
`max_burn` (τ100 on 602) and decays toward `min_burn` over `burn_half_life`, which is 360
blocks here — so registering uid 1 on a subnet minutes old cost **τ0.902987384**, nearly the
whole τ1 that remained. Read at block 8334694, the same burn had decayed to **τ0.539024711**
against a balance of **τ0.097426113**: a third neuron was unaffordable for about three more
hours purely because of when it was asked for. Price the burn at the moment you intend to
register, not from a table and not from a different subnet.

τ1 is likewise a *live* price (`subnets.subnet_registration_cost()`) that moves with
registration rate. Re-read it; do not trust this table's number later.

**Proof-of-work is not the free path it looks like.** `PowRegister` exists and the SDK
ships a miner, but an attempt on netuid 356 (1/256 uids, `registration_allowed: True`) was
refused by the chain: `The subnet owner must enable PoW registration`. So
`registration_allowed` governs *burned* registration only, and PoW needs a separate opt-in
per subnet. Worse for anyone hoping to shop around: **no PoW flag appears among the 39
hyperparameters** `subnet_hyperparameters` returns, so there is no cheap way to find a
subnet where it is enabled — you would have to attempt mining on each one. A scan here
reported "none enabled" across the 25 emptiest subnets; that result is **void**, because
with no PoW key to read the test could never have returned a positive.

That was the blocker for most of this work — **registration was blocked on acquiring
testnet TAO**, there being no faucet to ask — and it is no longer: the coldkey was funded
with τ2, which bought a netuid, a second neuron and the weight commits above, with
τ0.097426113 left. The smallest useful amount is a burned hotkey registration on an
established subnet, not the τ1 a new netuid costs.

Both write paths (`SetWeights`, `ServeAxon`) default to dry-run, refuse mainnet, and refuse
an empty weight vector; `bt_miner.py` additionally refuses to announce a loopback address,
which would publish a promise no other validator could keep. One more guard came out of the
work above and is worth stating as a rule rather than a feature: **on a commit-reveal subnet
a write is not verified by its receipt**, so `adapters/bt_validator.stored_matches` compares
a committed vector against the one the chain is using, allowing for the two ways the chain
does not echo what you sent — it drops zero-weight entries, and it quantises to u16.

## 7 · Joining netuid 602, and the two things that made it joinable

Until 2026-10-30 this subnet had a working mechanism and was, in the only sense that
matters, closed: `subnets.subnet_identity(netuid=602)` returned `None`, no hotkey had
published a rate, and the validator found its miner because a human handed it a URL. All
three are now closed on chain. What follows is read back out of storage, not taken from
receipts.

### The identity

`SetSubnetIdentity` is a real Intent in v11, so it goes through `execute` and the policy
rail — unlike the commitment write below, which has no Intent at all. Owner-signed, and
the `SubnetIdentitySet` event names the netuid:

| | |
|---|---|
| `subnet_name` | `ACR Compute Rate` |
| `description` | `Constant-quality price index for AI inference compute.` |
| cost | **τ0.000082883** |

`subnet_url`, `github_repo`, `discord` and `subnet_contact` are deliberately **blank**. An
identity that advertises a Discord nobody runs is worse than one that says nothing: the
blank field is honest, the wrong one wastes somebody's afternoon.

### The rate, on chain, for the first time

A real Chutes ACR-INF print, committed and then read back at **block 8335720**:

```json
{"hi":0.000160114,"i":"ACR-INF","lo":8.64516e-05,"n":2,
 "p":0.000148896,"src":"chutes","ts":1036800,"v":"acr/1"}
```

**It cost nothing.** `Commitments.FieldDeposit` is **0** and `MaxFields` is **3**, so a
hotkey gets ~384 bytes across three fields and reserves nothing for them. The coldkey held
**τ0.097426113** at block 8334694, before the commitment, and still held **τ0.097426113**
when read back after it — so this one did not even cost a fee, unlike the identity write
above. Both figures are reads, which is the only reason the comparison is worth making.

The write needed the raw-call hatch, because **no `SetCommitment` Intent exists** among the
SDK's ~75 — and it must be signed by the **hotkey**, not the coldkey: `submit_call`
defaults to `signer="coldkey"` and the storage is keyed by `(netuid, hotkey)`. A
coldkey-signed commitment lands under the wrong key and reads as absent.

### Why the price goes in a commitment and not an axon

`ServeAxon(netuid, ip: str, port: int, …)` — **the axon record has no hostname field.**
Render, Fly and Vercel put many apps behind one address and route by `Host`/SNI, so a
request to the bare IP arrives at the platform's router and never at the app. A miner on a
managed host therefore cannot be announced as an axon *at all*; announcing one would
publish exactly the unkeepable promise `bt_miner` already refuses for loopback.

So `endpoints()` consults two channels — axon first, then the commitment — and counts a
neuron `unannounced` only when **both** are silent. That is the honest reading, and it is
not what the method did when it looked at axons alone.

### Three traps between "it works locally" and "it is deployed"

Each of these fails *quietly*, which is why they are written down.

**1 · `uv sync` installs a workspace member only when something depends on it.**
`members = ["packages/*", "services/*"]` declares membership, not installation. The first
deploy attempt reused the existing API image and died on
`ModuleNotFoundError: No module named 'acr_subnet'` — the package is dated 2026-10-30 and
every published tag of that image predates it. A workspace is not a guarantee that a
package is in your image.

**2 · The miner's tape lives under `tests/`, which `.dockerignore` excludes.**
`ChutesSource` resolves its default fixture as `parents[3] / "tests" / "fixtures" / "bt"`.
Excluding `tests/` is right for the API image and fatal here: the service boots, `/health`
says `ok`, and only `/print` tells the truth — 503, to a validator, on somebody else's
epoch. `deploy/subnet/Dockerfile` copies the 432K fixture tree and then **asserts the tape
at build time**, so a tapeless image cannot be built.

**3 · A free instance that sleeps is a dead miner, not a slow one.**
Render's free tier stops after ~15 idle minutes and cold-starts in tens of seconds.
`QUERY_TIMEOUT_S` is **5.0s**, short on purpose. The fix belongs on the miner's side — a
miner that cannot answer in five seconds *is* unreachable, and loosening the validator to
accommodate a hosting choice would be the mechanism bending to the plumbing. `bt_service`
therefore self-pings on a 60s loop, the pattern `index_api` already proved on this exact
platform, and it carries that code's hard-won guard: the task gets a done-callback that
**logs**, because the same loop there died silently at every boot for four weeks when an
unawaited `create_task` raised on its first line.

### The recipe

`make subnet-join` prints this against the live chain, which is the point — this project's
own notes record the burn table being wrong by ~1800× in practice, so the price is read at
the moment you intend to register rather than quoted from here. Measured at block
8336594: `registration_allowed` **True**, burn **τ0.013920908**, down from τ0.902987384
when uid 1 registered the same day.

```bash
make subnet-join                                   # live price + affordability
uv sync --extra bittensor
btcli wallet new_hotkey --wallet.name <yours> --wallet.hotkey miner
btcli subnet register --netuid 602 --network test   # burned registration

uv run python -m acr_subnet.adapters.bt_service --host 0.0.0.0 --port 8091
uv run python scripts/subnet_publish.py --endpoint https://your-host.example/ --publish
```

Note `subnet_hyperparameters` returns a **dict**, not an object. `getattr(hp, "...", False)`
never raises and never finds anything — it returns the default, so every subnet reads as
closed. `Chain.hyperparameters` normalises it in one place, and `Chain.registration_burn`
exists because the live burn is **not** among those 39 hyperparameters: they carry
`min_burn`, `max_burn`, `burn_half_life` and `burn_increase_mult`, and no current figure.
