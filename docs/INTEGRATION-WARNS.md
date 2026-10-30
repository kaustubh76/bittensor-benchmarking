# Warns ⇄ ACR: committing a payment to a signed decision

**2026-10-12.** Everything below was read off the chain or run, not recalled.
Addresses, limits and event shapes are live values and will move; the contract
in §2 is the part meant to be stable.

## 0. The one thing to get right

**Do not reimplement the decision hash.** ACR computes it; Warns signs the 32
bytes. That is not a convenience, it is the only correct arrangement, and §2
says why in one measurement.

## 1. The wallet

| | |
|---|---|
| `PolicyWallet` | `0xa67F188746f479A9d93D112BeeB7aadB90bE834b` |
| chain | Arc **testnet**, id `5042002` · RPC `https://rpc.testnet.arc.io` |
| explorer | `https://testnet.arcscan.app` |
| USDC | `0x3600000000000000000000000000000000000000`, 6 decimals |
| `owner()` | `0x784e6D2d4870174aD1aEa3d7446F2377D0c6A05d` |
| agent | `0x1547B0d3C334F264D28B087566B512AB5CD85379` (`isAgent` true) |
| state | `paused` false · `approvalNonce` 1 |

Source `contracts/src/PolicyWallet.sol`; ABI
`contracts/out/PolicyWallet.sol/PolicyWallet.json`; Python client
`packages/acr_oracle_client/acr_oracle_client/policy.py`.

`isAgent(owner())` is **false** and must stay false — the contract refuses to
let one key be both authorities, which is what makes the escalation below mean
anything.

> **Use `0xa67F1887…` above, not `0xA755f87BD00c90DBFc9DdfD6651e4c3071665b32`.**
> An earlier wallet, still referenced in some registry rows.
>
> **Corrected 2026-10-13.** This note previously said its owner key was lost and
> every `onlyOwner` path permanently dead. That was wrong: its `owner()` is
> `0x38DC1B5F05b90ccbe3881Aa6490eAa096a619CCE`, which is the **arc-canteen CLI
> wallet**, so the key is reachable. The wallet is usable — not paused,
> `approvalNonce` 0, and holding the history behind our 19 logged decisions.
>
> We point at `0xa67F1887…` by **choice**: a clean budget and an owner key held
> directly rather than through a hackathon tool. "Usable but not the one we use"
> is a different instruction from "ignore it", and you were given the wrong one.

## 2. The decision hash — and the premise to correct

**The contract does not hash the record.** `decisionHash` is a `bytes32`
*parameter* on all three spend paths, and the only on-chain rule is
`require(decisionHash != bytes32(0))`. The hash is computed off-chain, in
Python, in exactly one place:

```python
keccak256(b"acr.decision::" + json.dumps(record, sort_keys=True,
          separators=(",", ":"), ensure_ascii=False, default=str))
```

Good news first: **adding your `decisionId` needs no contract change, no
migration and no redeploy.** It is a field on the record.

Now the measurement that decides the integration shape:

```
python  {"at":1791801414.0,"metered_quantity":10.0,...}
node    {"at":1791801414,  "metered_quantity":10,  ...}
```

Python renders a whole-valued float with its `.0`; JavaScript does not. And
**from JSON alone a reimplementation cannot tell which fields are floats** —
four hashed fields hold whole floats in our live archive right now
(`discrepancy`, `metered_quantity`, `saving_usdc`, `vendor_quantity`). A
TypeScript port written in good faith produces different hashes for real
records, and the failure is silent.

So: **we hash, you sign.** `tests/fixtures/decision_hash_vectors.json` carries
six conformance vectors with their expected hashes, including the whole-float
case, a `null`, a non-ASCII vendor and the empty record.
`tests/test_decision_hash_contract.py` holds us to them and fails if anyone adds
a second implementation.

## 3. The flow

```
1. ACR decides          decide() -> ObligationDecision
2. ACR -> Warns         the record, and our decisionHash (32 bytes)
3. Warns -> ACR         decisionId + a signature over that hash
4. ACR stores           record.warns_decision_id  (INSIDE the hash)
                        record.warns_signature    (OUTSIDE the hash)
5. ACR re-hashes        the id changed the record, so the hash changes
6. ACR spends           spend / spendApproved / spendAsOwner, carrying the hash
```

Run end to end, with real values:

| step | hash |
|---|---|
| before the id | `0x21cc9844094458d3…` |
| **with the id** | `0x68cd27b475aa3bab…` ← the bytes32 sent to the contract |
| after the signature | `0x68cd27b475aa3bab…` (unchanged) |

**Why the id is in the hash and the signature is not.** The id belongs in it:
`spendApproved` binds `decisionHash` into the EIP-712 digest it recovers, so the
owner's signature then covers *a Warns-signed decision* rather than just an
amount and a payee. The signature cannot be in it — it signs that hash, and a
signature inside the record it signs is circular and unproducible. Step 3's
hash and step 5's being equal is what makes it signable at all.

Fields excluded from the hash: **`tx`, `paid_usdc`, `warns_signature`.** The
first two do not exist until after the money moves. Strip exactly this set when
re-deriving a hash from a stored row, or the record stops matching its own
commitment and the only reading left is "this was edited".

## 4. Limits, as read from the chain

Category `machine-services` = `0xb52ff52127d3d00ab35ee1c7c89af641e904e2ddd1b6eb239461efc49499b39b`

| | |
|---|---|
| `cap` | 1.000000 USDC per 30-day period (2592000s) |
| `perTxLimit` | **0.050000 USDC** — the escalation threshold |
| `spent` | 0.154410 · remaining 0.845590 |

Three rungs, and the threshold is **strict**:

- `spend(...)` — agent only, `require(amount < perTxLimit)`. So 0.049999 is the
  agent's largest unilateral payment and **0.050000 escalates**.
- `spendApproved(..., deadline, v, r, s)` — at or above the limit, carrying the
  owner's EIP-712 signature, relayed by the agent.
- `spendAsOwner(...)` — the owner's own wallet sends it. Exists because
  `ecrecover` cannot validate a smart-contract account, so an owner on a
  Circle PIN-secured SCA could never clear `spendApproved`.

Above the threshold **the period cap still binds** — an approval lifts the
per-transaction ceiling, not the budget — and **`paused` outranks everyone**,
including the owner's own wallet. Limits are `onlyOwner` via `setBudget` and can
be sized for the demo.

### Four things that will otherwise be met cold

1. **`approvalNonce` is global and sequential**, not per category. Two
   concurrent `spendApproved` flows race; the loser reverts `not owner
   signature` because the digest was built against a consumed nonce.
2. **Amounts finer than 6 decimals are refused, not rounded.** `usdc_units`
   raises rather than picking a rounding for you. Round before you send, so the
   rounding is a decision someone made.
3. **The category must exist** or `spend` reverts `no such budget`. Categories
   are `keccak256("acr.category::" + name)`, hashed rather than padded.
4. **Build the approval digest with the contract's own `approvalDigest()`
   view**, not a local reimplementation. The chain id lives inside the EIP-712
   domain separator, and a mismatch there fails as a signature error, which is
   a confusing place to learn about it.

## 5. The join key

```solidity
event Spent(bytes32 indexed category, address indexed to, address indexed actor,
            uint256 amount, bytes32 decisionHash, bool ownerApproved,
            uint256 spentInPeriod, uint256 cap, uint64 periodStart);
```

`decisionHash` is your join. `actor` is whoever sent the transaction and
`ownerApproved` separates the rungs, so which authority paid is readable from
the event alone. All four events on this wallet, read today:

| amount | rung | tx |
|---|---|---|
| 0.030000 | `spend` (agent) | `0x0c160702df907fa2…` |
| 0.080000 | `spendApproved` (agent relaying owner) | `0xd256b1d7c9a991e2…` |
| 0.040000 | `spendAsOwner` (owner) | `0x36bc7233e050ed1b…` |
| 0.004410 | `spend` (a real `decide()` run) | `0x05e01c72ddcf7132…` |

`eth_getLogs` on this RPC refuses a 14000-block range with `-32012 requested
range too large`; **5000 works**, so page.

## 6. Which language is which

- **Supplier-invoice rail** (PolicyWallet, direct USDC transfer): **Python** —
  `acr_oracle_client/policy.py`, `services/index_api/index_api/operator.py`.
  This is where the Warns integration lives.
- **x402 / Circle Gateway metered rail**: **TypeScript** —
  `apps/agent/src/payer.ts`, `mcp/src/pay.ts`.
- Python's x402 code is the **seller** side. Its one "buyer" pays with a dev
  mock header that the real Circle gate refuses by design, so it is not a
  Gateway payer.

## 7. Not done yet

`warns_decision_id` is empty on all 19 archived decisions and will stay empty
until the first Warns-signed obligation arrives — the field and its gates are
in place, the data is not. It is deliberately not rendered on `/spend` yet: a
column blank on every row trains a reader to ignore it.
