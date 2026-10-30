# Wallets — which Circle product does which job, and why it has to

ACR touches four different Circle wallet products. They are not
interchangeable, and the choice is never a preference: **every role in this
system has a constraint that picks its wallet type for it.** This document
records those constraints, so the next person does not "simplify" the design by
collapsing them into one.

The short version:

| Role | Wallet product | Account type | Who can sign |
|---|---|---|---|
| Oracle press — hourly EIP-712 prints | **Developer-controlled** | EOA | our backend, via API key + entity secret |
| Treasury · desk faucet · x402 `payTo` | **Developer-controlled** | EOA | our backend |
| Venue maker | **Developer-controlled** | EOA | our backend (unattended CI) |
| Keeper — roll · settle · withdraw | **Developer-controlled** | EOA | our backend (unattended CI) |
| Heartbeat taker | **Developer-controlled** | EOA | our backend (unattended CI) |
| Public Desk reader | **User-controlled** | SCA | the reader's PIN, client-side only |
| Autonomous hedger agent | **Circle agent wallet** | SCA (Circle-custodied) | the Circle CLI, under an email-OTP session |
| Contract deployer | raw key, offline, used once | EOA | a human, once |

---

## The constraints that force each choice

### C1 — `ecrecover` on-chain forces an **EOA** account type

`ACROracle.verify` and `AttestationRegistry.attestWithSig` recover the signer
with `ecrecover` (`contracts/src/ACROracle.sol:165`,
`contracts/src/AttestationRegistry.sol:86`). A smart-contract account signs via
ERC-1271, which `ecrecover` cannot check.

So the press wallet is a developer-controlled wallet created with
`account_type="EOA"` — custodied by Circle, but an EOA on-chain
(`scripts/create_circle_wallet.py:67-75`). "Custodied" and "EOA" are orthogonal:
Circle holds the key, the chain still sees an EOA.

### C2 — Unattended automation forces **developer-controlled**, not an agent wallet

A Circle agent wallet authenticates with **email + OTP and the session expires**
(`circle wallet status` reports an expiry). That is correct for an agent a human
is working with, and wrong for a cron job that must roll a series at 02:17 UTC
with nobody watching.

Developer-controlled wallets authenticate with an API key and an entity secret —
no session, no human. That is why the maker, the keeper and the heartbeat taker
are developer-controlled even though they are "bots".

### C3 — The reader's money forces **user-controlled**

On the Public Desk the key is derived and held in the reader's browser; the
server only mints `contractExecution` challenges that the reader's PIN
authorizes (`services/index_api/index_api/desk.py:1-14`). This server never
holds a reader's key, and no server-side script can stand in for the ceremony —
`make desk-e2e` drives a real browser for exactly this reason.

Gas is sponsored by Circle Gas Station. Sponsorship is only readable from the
ERC-4337 `UserOperationEvent`'s `paymaster` topic, **never** from `networkFee` —
an early draft of `scripts/desk_evidence.py` concluded "not sponsored" from the
fee alone and was wrong. Do not reintroduce the fee-based heuristic.

### C4 — x402 `exact` forces an EOA signature — but not an exported key

The x402 `exact` / `GatewayWalletBatched` scheme is EIP-3009, and the facilitator
`ecrecover`s the authorization. The repo concluded from this that "the buyer
needs a raw, exportable EOA key" (`docs/agent-runbook.md:26-30`).

**That conclusion is narrower than it reads.** It holds only when *our own code*
signs the authorization — which is what `apps/agent/src/payer.ts` does, passing a
raw `privateKey` into Circle's `GatewayClient`. When the **Circle CLI** pays
(`circle services pay`), Circle signs on the agent wallet's behalf through its
backing EOA, and nothing needs to be exported. `circle gateway balance` shows
that backing EOA explicitly.

So there are two valid buyer designs, and they answer different questions:

- **our own agent signs** → needs a raw exportable EOA (today's `apps/agent`);
- **the Circle agent wallet pays** → no key anywhere (`circle services pay`).

The second is what the autonomous hedger uses.

### C5 — The venue forces the maker and taker to be **different wallets**

`ACRFutures.trade` requires `msg.sender != s.maker`
(`contracts/src/ACRFutures.sol:242`) — the maker cannot take its own quote. The
maker and the heartbeat taker are therefore two separate developer-controlled
wallets, not one.

`openSeries` is `onlyOwner`, but it takes the maker as a **parameter**
(`ACRFutures.sol:174`), so moving the maker to a Circle wallet needed no
ownership transfer — only the next roll.

### The venue is now owned by a Circle wallet too

Done on 2026-08-03 via `scripts/migrate_venue_owner.py`, and verified on-chain
rather than from the script's own report:

```
owner        0x9D44A7Dd4e7bF173B3F13ee41E1B60C8e92388d2   (Circle custody, the maker)
pendingOwner 0x0000000000000000000000000000000000000000
```

The retiring EOA `0x33189c…` now holds **no authority over the venue at all** —
not owner, not pending owner. It keeps a small balance and nothing else.

Two-step ownership is what made this safe to attempt: `transferOwnership` only
nominates, so a failed Circle leg would have left the old owner in full control
with no window in which nobody owned the contract.

It also unlocked a capability rather than just tidying a diagram. Because the
maker is now the owner, `keeper.roll_if_needed` can open a successor series by
itself — before this it could only detect that a roll was due and shout for a
human, since the process does not hold the owner's key and should not. The
venue's entire lifecycle now runs unattended under custody signing.

---

## What is *not* available on testnet

**Agent-wallet spending policies are mainnet-only.** Circle's
`agent-wallet-policy` skill defines per-transaction / daily / weekly / monthly
USDC caps enforced by the CLI, but testnet chains are rejected, and setting a
policy requires a human OTP in an interactive terminal.

Every spend cap in ACR is therefore **application-level**: the agent's own
`--limit`, the desk's `MAX_QTY` and faucet caps, the hedger's position cap. We
do not claim platform-enforced budgets, because on Arc testnet we cannot have
them. Whether they are coming to developer-controlled wallets is an open
question for Circle, recorded in `CIRCLE-SESSION-QUESTIONS.md`.

---

## Why signing through Circle costs nothing in code

`FuturesClient` never touches a key. It takes a `Signer`
(`packages/acr_oracle_client/acr_oracle_client/futures.py:296-309`) and every
write funnels through one `_send`. `CircleWalletSigner.send_transaction`
(`signer.py:164-169`) discards the locally built nonce/gas/chainId and hands the
`to` + `data` to Circle's `contract_execution`, then polls to `CONFIRMED` for the
real on-chain hash.

This path is **proven in production**: the hourly oracle prints are Circle
`CONTRACT_EXECUTION` transactions, and the desk faucet moves USDC the same way
(`desk.py:_send_stake`).

The reason the venue scripts still used raw keys is mundane: they pass an
explicit `private_key`, and `build_signer` prefers a raw key over configured
Circle credentials whenever one is present (`signer.py:250-253`). Remove the key
and the same code signs through custody.

Two things do **not** come for free, and both are handled:

1. Each script derives its own address with `Account.from_key(...)`. That is an
   EOA-only assumption; it becomes `signer.address`.
2. The USDC `approve` step bypasses the `Signer` seam entirely and hand-signs
   with `eth_account` (`scripts/futures_roll.py:134-145`). It has to route
   through the signer like everything else.

---

## The agent wallet has two on-chain identities, and both are it

This is the detail that resolves C4, and it is worth knowing before reading the
ledger. Measured on 2026-08-03, the same agent doing one loop:

| Rail | Identity recorded | Why |
|---|---|---|
| The x402 settlement | `0x71e140d9…` — the **backing EOA** | `exact`/EIP-3009 needs a signature `ecrecover` can verify, so Circle signs with the SCA's backing EOA |
| The on-chain `Traded` event | `0x1Dc707E3…` — the **SCA** | `trade` reads `msg.sender`, and the userOp executes as the smart account |

So `/marketplace/receipts` shows a payer that is not the address on the trade,
and both are the same agent. The transaction's `from` is a third address again —
the ERC-4337 bundler — which is normal for a smart account and not a party to
anything.

This is why an agent wallet **can** buy x402 despite the EOA-only rule: it does
not sign with the SCA at all. The runbook's conclusion was right about the
protocol and wrong about the consequence.

## An integration bug worth reporting upstream

`circle services pay` fails from a clean environment with:

```
Error: Could not sign payment authorization.
  Hint: Failed during Gateway batched payment signature creation.
  Technical details: ReferenceError: crypto is not defined
```

The message reads like a wallet or protocol problem — it is a **missing Node
global**. The fix is `NODE_OPTIONS=--experimental-global-webcrypto`, which
`scripts/hedger.py` sets for every CLI call. Measured on Node **v26**, where
`crypto` is a global at the top level, so something in the CLI's signing path
runs without it. `circle wallet execute` is unaffected; only the payment leg.

Worth flagging because the symptom points away from the cause: it cost a live
run that reported "payment failed" while the trade beside it succeeded, and
nothing in the error suggests a runtime flag.

## `circle wallet execute` cannot carry a negative `int256`

The one above is a runtime flag you can set. This one changes what the wallet
can *do*, so it belongs beside the two identities rather than in a runbook
footnote. Measured 2026-08-08 on ARC-TESTNET against
`ACRFutures.trade(uint256,int256)`:

| Attempt | Result |
|---|---|
| `+1e16` (WAD, long) | estimates and returns a fee — the transaction builds |
| `-1e16` | `400 Fails to perform transaction estimation` |
| Two's complement, hex **and** decimal | the same 400 |
| `--` before the positional arguments | the same 400 |
| The identical call under `eth_call` | **succeeds** |
| An oversized *positive* quantity | `Estimate fee execution reverted` |

The last two rows are what make this a diagnosis rather than a guess. A contract
revert produces a **different** error, and the same call the CLI refuses to build
executes fine against the same venue over `eth_call`. So the failure is in
**building** the transaction, not in running it.

**What it means for the wallet.** An agent wallet on this path can open and
increase an on-chain position and cannot reduce one — it is a buy-side-only
signer for any signed-quantity ABI. Design around it three ways: give the agent a
one-directional mandate, keep an EOA route for the closing leg, or let the
position run to cash settlement at expiry.

**What it means for our hedger.** Its mandate is one-directional by necessity
rather than by choice, and the repair for an overshoot is to raise the mandate to
the position rather than sell the position back. That is available because we own
the mandate; it would not be if we owed it to a counterparty. `_fmt_qty`
(`scripts/hedger.py`) carries the same measurement in code, and
`skills/acr-hedge/SKILL.md` publishes it so nobody else has to find it twice.

## Why CI is the one place a raw key is still the *safer* choice

The obvious next step after moving the venue into custody is "put the Circle
credentials in GitHub Actions too, and delete every private key." That would
make things **worse**, and it is worth being explicit about why.

Compare what an attacker gets from a compromised runner:

| Credential in CI | What it controls |
|---|---|
| A raw EOA key | that one wallet's balance — a couple of USDC on a liveness bot |
| `ACR_CIRCLE_API_KEY` + `ACR_CIRCLE_ENTITY_SECRET` | **every** developer-controlled wallet: the treasury, the venue maker, *and* the wallet that signs oracle prints |

The entity secret is not a smaller blast radius than a private key — it is a
much larger one, and it includes the index's signing authority, which is the
integrity of the product rather than merely its money. `keepalive.yml` already
records this decision: it takes the custody *address* so it can check a balance,
never the credentials that could move it.

So the rule is **narrowest credential for the job**, not "custody everywhere":

- work that needs custody authority (posting prints, funding, opening or
  collateralising a series) runs from a **trusted host** — the deployed service,
  or an operator's machine — where the entity secret already lives;
- work that runs in **CI** gets either no credential at all (read-only checks) or
  a key scoped to a single low-value wallet it is allowed to drain.

That is why migrating the venue's *capital* to Circle wallets is the win, and
migrating CI's *liveness bot* would not be. A future improvement is to move the
heartbeat and the roll onto the trusted host entirely, at which point CI needs
no signing credential of any kind.

## Operational rules

- **One wallet, one job.** The failure this design replaces had a single raw EOA
  acting as venue maker, keeper, contract owner *and* x402 revenue address; a
  second one was both the heartbeat taker and the x402 buyer, shared across three
  workflows that can race each other's nonces.
- **The press and the faucet share a wallet today.** Documented at
  `desk.py:50-54`; the faucet floor is expressed as days of press runway so a
  drained faucet can never stop the oracle.
- **Never put the entity secret in CI.** `keepalive.yml` takes the custody
  *address* (`ACR_CUSTODY_ADDRESS`), not credentials, so a compromised runner
  cannot move custody funds.
- **Circle returns lowercase addresses.** web3 rejects a non-checksummed
  address, and the resulting error looks like RPC throttling rather than a format
  problem. Checksum at every boundary (`desk.py:609-617`).
- **Gateway settles in batches.** Each payment gets a settlement UUID; Gateway
  posts batched on-chain transactions. The honest description is "N x402
  payments + Gateway batch settlements", not "N on-chain transactions".
