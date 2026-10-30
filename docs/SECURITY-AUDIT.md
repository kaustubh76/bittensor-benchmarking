# Security & transparency audit — before Arc mainnet

**Date:** 2026-09-18 (fixes landed the same day) · **Scope:** the seven contracts, the seller API, the terminal's server
routes, key custody, dependencies · **Chain state read from:** Arc testnet `5042002` at the
time of writing · **Status column:** updated by the commit that closes each finding.

This is one engineer plus the test suite. It is **not a substitute for an external audit** of
`ACRFutures` and `ACROracleV2` — the two contracts that hold and price other people's money —
and one is recommended below, after the contract changes here land.

## The question this audit asks

Until mainnet, every wallet on the venue was ours. After it, strangers post real USDC as
collateral, pay real USDC for prints, and trust a number we sign. So: **what does one
compromised key, one dead process, or one missing environment variable cost a user who is
not us?** Findings are ranked by that answer, not by how clever the bug is.

## Method

- Every tracked file and all 315 revisions scanned for credential-shaped literals. Values were
  classified by shape and never materialised. Result: none, in any revision; `.env` was never
  committed.
- All seven contracts read in full for access control, money flow and liveness.
- Owners, pending owners, pause state and **oracle signer sets read from the live chain with
  `cast call`**, not taken from documentation.
- Every write endpoint's authentication traced from route to check.
- Gate mode selection (`x402`, `humanid`) traced for fail-open behaviour.
- `npm audit --omit=dev` on all four Node packages; `pip-audit` on the full `uv` lock.
- The Foundry invariant suite and the user-facing surfaces checked for disclosure.

---

## Findings

| # | Severity | Finding | Status |
|---|---|---|---|
| C1 | **Critical** | One hot EOA owns five contracts and signs both oracles | **FIXED for mainnet** — `DeployMainnet.s.sol` revokes the deployer's signer bit on every contract in the deploy broadcast and starts the ownership hand-over; `make verify-mainnet` fails if either is undone. Testnet: still as found (advisory ⚠). |
| C2 | **Critical** | The oracle accepts any value a signer signs — no move bound | **FIXED in source** — `ACROracleV2.MAX_MOVE_BPS` (2 000); the mainnet venue settles against v2. Invariant `MoveNeverExceedsBound`; the anvil round trip in `test_oracle_v2_onchain.py` proves the revert through the Python client. The testnet oracles predate it. |
| C3 | **Critical** | Venue collateral is locked if the press dies after expiry — no escape hatch | **FIXED in source** — `ACRFutures.settleStale` after `SETTLE_GRACE` (7 days), callable by anyone. The testnet venue predates it. |
| H1 | High | Both gates fail open in `auto` mode, which is the default | **FIXED** — `acr_core.mainnet_guard`: on chain 5042 the API refuses to boot unless both modes are explicit and configured. |
| H2 | High | Testnet-only money surfaces have no mainnet kill-switch | **FIXED** — `/desk/faucet`, `/demo/buyer/start`, `/demo/attack/start` are 404 on mainnet regardless of environment; the terminal's `/api/buy` is 404 on mainnet unless `ACR_TERMINAL_BUYER=1`. |
| M1 | Medium | Settlement price is the caller's choice among post-expiry prints | **FIXED in source** — `settle` uses the first print posted at or after expiry (`firstPrintPostedAtOrAfter`, keyed on chain time). |
| M2 | Medium | Socialized-loss clearing is undisclosed to users | **DISCLOSED** — `docs/SECURITY.md`, linked from the README's *Known limitations*. |
| M3 | Medium | Owner powers are single-key, untimelocked, and unpublished | **DISCLOSED** — the owner-powers table in `docs/SECURITY.md`. Timelock / multisig: open until one exists on Arc. |
| M4 | Medium | `scopeHash` is signed but unenforced | **DISCLOSED** — `docs/SECURITY.md`; enforcement is open. |
| L1 | Low | CORS default `*` on the read API | **FIXED for mainnet** — the guard requires an explicit origin on chain 5042. |
| L2 | Low | Next.js 14.2.33 — critical advisory, fix is 16.3.5 | **FIXED** — on **16.3.6** with React 19 (branch `chore/next-16`). `npm audit` 32→30 findings, **1 critical → 0**; `postcss` cleared with it. The 5 remaining highs are Bridge Kit's Solana tree, measured as a 0.2 MB lazy chunk in code paths this app never executes. The upgrade also turned on ESLint for the first time (`next lint` had never had a config, so it checked nothing) and that found two real bugs: a hook called after an early return in `app/index/[id]/view.tsx`, and `Date.now()` during render in `FuturesDesk`. `make test-terminal` now lints. |
| L3 | Low | `cryptography` 49.0.0 and `aiohttp` 3.14.1 advisories | **FIXED** — `cryptography` 50.0.1, `aiohttp` 3.14.3. |

### C1 · One hot EOA owns five contracts and signs both oracles

Read live on 2026-09-18:

| contract | `owner()` | `pendingOwner()` |
|---|---|---|
| ACROracle `0x4f00e3BDd224F4c4b4958D54cD774E84B9092609` | `0x33189c643774ED2713EbFf5A6923e5fa42b96eE8` | none |
| ACROracleV2 `0xFCa038CEad7b9e9aa8fDAfc9e80253835fB8FFEA` | `0x33189c64…6eE8` | none |
| FeedAccessAttestor `0xe671a8E73900F1186448cFFeA9e730F5E50DFD47` | `0x33189c64…6eE8` | none |
| ReceiptMirror `0xA9CD5b9503aeA88EB343333E842D2860b263DB65` | `0x33189c64…6eE8` | none |
| HumanIdMirror `0x7f41faA38F35F1FABfc76Df5B1618fC8d0c0d8e5` | `0x33189c64…6eE8` | none |
| ACRFutures `0x29d97c629a8278f7ec4218ab0bd8baa9182642fe` | `0x9D44A7Dd4e7bF173B3F13ee41E1B60C8e92388d2` (maker custody wallet) | none |

`isSigner` on **both** oracles is `true` for two addresses: the press custody wallet
`0x8366968f84a343CF70941EBe858428643d825cb0` and `0x33189c64…6eE8` — which is the **deploy
key**, held as `DEPLOYER_PRIVATE_KEY` in a local `.env`. One key can therefore post any
settlement price, change the signer set, rebind human clusters, pause the feed and open
series. The README disclosed "the original deploy EOA is still an authorized signer"; on
testnet that was a footnote. On mainnet it is the whole risk.

### C2 · The oracle accepts any value a signer signs

`ACROracle.postPrint` (and V2) checks: not paused, `value > 0`, `ciLo ≤ value ≤ ciHi` — a
bound the *signer chooses* — `attackCostPerBp > 0`, timestamp not in the future and strictly
increasing, and `ecrecover` lands in the signer set. **Nothing bounds how far a print may move
from the previous one.** `ACRFutures.settle` reads one print. A single compromised signer sets
the settlement price of every open series to whatever drains the maker or, timed right, the
takers.

What is right about it, and stays: the EIP-712 domain binds `chainId` and
`verifyingContract`, so a testnet signature cannot replay on mainnet; monotone timestamps stop
replay within a chain; age is measured from `postedAt`, so a signer cannot backdate.

### C3 · Venue collateral is locked if the press dies after expiry

`settle` requires `age ≤ MAX_SETTLE_AGE` (2 h). `withdrawCollateral` before settlement must
leave initial margin on any open position. If the press stops after `expiryTs`, every trader's
margin is locked until a fresh print arrives, and **nothing in the contract provides a path if
it never does.** This is not hypothetical: on 2026-09-05 all three v1 prints were **93 hours**
stale (the poster wallet had run out of gas), and in August a sleeping free-tier host produced
**216-minute** gaps against a 120-minute window. Both were caught because the money was ours.
No test covers "oracle dead after expiry"; no user surface discloses it.

### H1 · Both gates fail open in `auto` mode

`services/index_api/index_api/x402.py` (mode `auto`): if the facilitator URL or pay-to does
not "look real" the gate becomes `Dev` — prints are free — with a log warning.
`humanid.py` (mode `auto`): no app id → `DevHumanVerifier`, which cannot prove a human exists —
anyone claims a per-person budget. Explicit `circle` / `agentkit` modes fail closed. **`auto`
is the default in `.env.example`.** A mainnet deploy with one variable missing or misspelled
gives prints away and dissolves the human tier, and the only signal is a warning.

### H2 · Testnet-only money surfaces have no mainnet kill-switch

No `is_mainnet` or chain-id branch exists anywhere in `services/` or `packages/`. On mainnet
these spend *our* real USDC on a stranger's request:

- `POST /desk/faucet` — 0.5 USDC from the custody wallet per browser session; session-keyed
  limit, Sybil-able by construction.
- `POST /demo/buyer/start` — up to 50 x402 purchases per call from the operator's buyer key,
  unauthenticated.
- Terminal `POST /api/buy` — per-request spend cap and allow-listed targets, **no throttle**
  across requests.

Each is small per call and unbounded across calls.

### M1 · Settlement price is the caller's choice

`settle` uses the *latest* print at the moment it is called, any time after expiry inside the
2 h window. Prints are hourly, so several post-expiry prints can exist; whoever calls picks the
one that favours them. The settlement print should be the first at or after `expiryTs`.

### M2 · Socialized-loss clearing is undisclosed

Margin is checked only at trade time; there is no maintenance margin or liquidation. On a
large move, losers' entitlements floor at zero and the shortfall haircuts winners pro-rata
(`settle`, pass 2). The clearing is sound and conserves the pot — but no user-facing surface
says a winner can be cut.

### M3 · Owner powers are single-key, untimelocked, and unpublished

`ownerRebind` (move a wallet to another human cluster), `openSettlementLate`, `setSigner`,
`setPaused`, `openSeries` (choose the maker). All correctly owner-only and event-emitting.
None is listed anywhere a user would look.

### M4 · `scopeHash` is signed but unenforced

In the agent card and its signature; the gate checks it against nothing. Known; the disclosure
left the README when hackathon material was archived. Re-stated in `docs/SECURITY.md`.

### L1–L3 · CORS, Next.js, Python dependencies

- CORS defaults to `*` (`acr_core/config.py`); every write route is separately gated.
- `npm audit --omit=dev`: terminal — **1 critical** (Next.js server-components DoS; fix
  `next@16.3.5`, a two-major upgrade), 3 high (`postcss` via Next; `undici`, `nanoid` fixable in
  place). agent — 4 high, all transitive through `@circle-fin/*` → `@solana/web3.js` /
  `@coral-xyz/anchor`; not fixable here, tracked upstream. graph — 10 high, all in build
  tooling (`@graphprotocol/graph-cli`, `axios`, `cross-spawn`, `glob`); not shipped. mcp — clean.
- `pip-audit`: `cryptography 49.0.0` (PYSEC-2026-3552 → 50.0.0; in the signature path) and
  `aiohttp 3.14.1` (three advisories → 3.14.3).

## Verified clean — stated so nobody re-audits it

- No credential in the tree or any revision; `.env` never committed.
- No upgradeability, `delegatecall` or `selfdestruct`. Two-step ownership on every contract.
  Hand-rolled `nonReentrant` used correctly with checks-effects-interactions. USDC is Arc's.
- Operator console (`/ops/actions`): token-gated with `hmac.compare_digest`, **disabled by
  default**, dry-run by default, spend-capped (2 / 5 USDC), audited, bad-key lockout.
- x402 replay defence is Circle's EIP-3009 nonce; webhook signatures are verified; the container
  runs as `uid 10001`; no secret value reaches a log call; CI runs with zero secrets.
- Invariants hold: `netContractsZero`, `collateralBackedByBalance`, monotone timestamps, value
  within CI, `HumanBoundNeverUndercutsWalletBound`.

## Needs a third party

- An **external contract audit** of `ACRFutures` and `ACROracleV2`, after the changes for
  C2, C3 and M1 land.
- A disclosure policy (root `SECURITY.md`) now; a bounty when there is money worth stealing.

## The mainnet ceremony, rehearsed

On 2026-09-18 the full deploy-and-custody flow was run against a local anvil exactly as the
runbook prescribes for mainnet: `DeployMainnet.s.sol` in one broadcast (venue on v2, press
signer set, deploy key's signer bit revoked on all five signing contracts, ownership transfer
started), `acceptOwnership()` from the new owner on all six owned contracts, then
`verify_deploy.py` with the custody checks enforced. Every check was green — owners equal the
expected owner, no pending transfers, the deploy key neither owns nor signs anything, the press
signs both oracles. The rehearsal also caught two paper cuts fixed in the same commit: a deploy
script that named the wrong environment variable, and a verifier message that did not say
which address it could not read.

## Re-running this audit

```bash
# owners and signers, live
for a in 0x4f00e3BDd224F4c4b4958D54cD774E84B9092609 0xFCa038CEad7b9e9aa8fDAfc9e80253835fB8FFEA; do
  cast call $a 'isSigner(address)(bool)' 0x33189c643774ED2713EbFf5A6923e5fa42b96eE8 --rpc-url $ACR_ARC_RPC_URL
done
uv run python scripts/verify_deploy.py            # asserts the expected owners and signers
cd apps/terminal && npm audit --omit=dev            # and apps/agent, mcp, graph
uv export --no-hashes --all-packages -o /tmp/req.txt && uvx pip-audit -r /tmp/req.txt
```
