#!/usr/bin/env python
"""Post the subnet's consensus print to ACROracle on a local chain, and read it back.

This is the step that makes the subnet a benchmark rather than a scoreboard. A
reference rate is a number something *settles against*, and until a consensus
print reaches a contract that enforces the benchmark invariants on chain, the
whole mechanism is a well-tested opinion.

    uv run python scripts/subnet_oracle.py --dry-run     # check the invariants, no tx
    uv run python scripts/subnet_oracle.py               # deploy, post, read back
    make subnet-oracle                                   # same, with its own anvil

Opt-in and self-skipping: no `forge build` output or no reachable node means it
exits 0 with a reason, because a missing local toolchain is not a failure of the
subnet.

**It targets ACROracle v1, deliberately.** `acr_subnet.consensus_print` produces
an `ACRPrint` with no `policy_hash` and no window, and ACROracleV2 requires both
(`require(p.policyHash != bytes32(0))`, `require(p.windowStart < p.windowEnd)`).
Inventing a policy hash to satisfy a v2 field would be claiming the print was
made under a cleaning policy it was not made under — and on single-venue tape
the honest answer is that it was made under a declared variant with no attack
cost at all. v1 takes the print as it is.

Two invariants are checked *before* any transaction, because finding out from a
revert costs a deploy and tells you less:

* **Timestamps must ascend per index.** `ACROracle` enforces
  `timestamp > prev.timestamp`, and the consensus print carries a Fixing-clock
  `ts` (3600, 7200, … or 86400-multiples on real tape), not unix time. The skew
  guard is one-sided — `timestamp <= block.timestamp + MAX_TS_SKEW` — so a small
  Fixing-clock value passes trivially; it is the ordering that can bite.
* **The interval must contain the value.** `require(ciLo <= value && value <= ciHi)`.
  `consensus_print` already nests them, and this asserts the property rather
  than trusting it.

One thing worth knowing rather than discovering: a print with
`attack_cost_per_bp=None` does **not** revert. `PostPayload.from_print` floors it
to `max(1, ...)` = 1 unit (1e-6 USDC), so `require(attackCostPerBp > 0)` passes
and the read-back shows `1e-06`. That is a wire-format floor, not a measurement,
and it is exactly why `docs/BITTENSOR.md` §3 insists the absent bound is carried
as `None` in the print and reported as a venue-concentration diagnostic instead.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

for _v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_v, "1")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "redteam"))

from acr_core import ALL_INDEX_IDS  # noqa: E402
from acr_subnet import (  # noqa: E402
    CopycatMiner,
    FourPillarMiner,
    OverclaimMiner,
    Validator,
    VwapMiner,
    run,
    synthetic_epochs,
)
from optimal_attack import run_optimal_attack  # noqa: E402

ARTIFACT = ROOT / "contracts" / "out" / "ACROracle.sol" / "ACROracle.json"

#: anvil's first dev account. Local only, never holds value. The v1 constructor
#: sets `isSigner[msg.sender] = true`, so deploying with this key makes it an
#: authorized signer and no `setSigner` call is needed.
ANVIL_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"

MAX_TS_SKEW = 900  # ACROracle.sol:33

#: Chains this script expects to be deploying to and spending `ANVIL_KEY` on.
#: Same set as `scripts/gen_snapshot.py`'s `LOCAL_CHAIN_IDS`, drawing the same
#: line for the same reason: anything else is somebody else's chain.
LOCAL_CHAIN_IDS = frozenset({31337, 1337})


def check_invariants(prints: list, block_ts: int | None) -> list[str]:
    """Everything the contract will refuse, checked before spending a deploy."""
    problems: list[str] = []
    if not prints:
        return ["no consensus prints to post"]

    last = None
    for p in prints:
        if not (p.ci_lo <= p.value <= p.ci_hi):
            problems.append(
                f"{p.index_id} ts {p.ts:.0f}: value {p.value} outside [{p.ci_lo}, {p.ci_hi}] "
                f"— ACROracle requires ciLo <= value <= ciHi"
            )
        if p.value <= 0:
            problems.append(f"{p.index_id} ts {p.ts:.0f}: value must be > 0")
        if last is not None and not p.ts > last:
            problems.append(
                f"{p.index_id}: ts {p.ts:.0f} does not exceed the previous {last:.0f} "
                f"— ACROracle requires strictly monotone economic timestamps per index"
            )
        last = p.ts
        if block_ts is not None and p.ts > block_ts + MAX_TS_SKEW:
            problems.append(
                f"{p.index_id} ts {p.ts:.0f} is more than {MAX_TS_SKEW}s past block time "
                f"{block_ts} — ACROracle refuses a print from the future"
            )
    return problems


def connect(rpc: str):
    """(w3, account) or None. Never raises — a missing node is a skip, not a bug."""
    try:
        from eth_account import Account
        from web3 import Web3

        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 4}))
        if not w3.is_connected():
            return None
        return w3, Account.from_key(ANVIL_KEY)
    except Exception:  # noqa: BLE001 - any import or transport failure is a skip
        return None


def deploy_oracle(w3, acct) -> str:
    """Deploy ACROracle from the forge artifact and return its address.

    Deployed fresh every run on purpose: `_latest` starts empty, so the first
    print of any `ts` is accepted and the monotonicity rule cannot trip over a
    leftover from an earlier run against a long-lived node.
    """
    art = json.loads(ARTIFACT.read_text())
    c = w3.eth.contract(abi=art["abi"], bytecode=art["bytecode"]["object"])
    tx = c.constructor().build_transaction(
        {
            "from": acct.address,
            "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": 3_000_000,
            "chainId": w3.eth.chain_id,
        }
    )
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    rcpt = w3.eth.wait_for_transaction_receipt(w3.eth.send_raw_transaction(raw), timeout=60)
    return rcpt.contractAddress


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--index", default="ACR-INF", choices=list(ALL_INDEX_IDS))
    ap.add_argument("--epochs", type=int, default=4)
    ap.add_argument("--seed", type=int, default=4242)
    ap.add_argument(
        "--rpc",
        default=os.environ.get("ACR_TEST_RPC", "http://127.0.0.1:8545"),
        help="node to use. Defaults to ACR_TEST_RPC, then 127.0.0.1:8545.",
    )
    ap.add_argument("--dry-run", action="store_true", help="check invariants, send nothing")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    print("ACR subnet → ACROracle (local)")
    print("=" * 72)

    # 1. Produce the consensus prints.
    miners = [
        FourPillarMiner(uid=1),
        VwapMiner(uid=2, width_bp=500.0),
        CopycatMiner(uid=3),
        OverclaimMiner(uid=4, multiple=50.0),
    ]
    epochs = synthetic_epochs(args.index, args.epochs, seed=args.seed, events_per_service=2000)
    if not epochs:
        print("  ✗ the simulator produced no epochs")
        return 1
    rep = run(args.index, miners, epochs, validator=Validator(attack_fn=run_optimal_attack))
    prints = rep.consensus_prints
    print(f"  consensus prints : {len(prints)} from {len(epochs)} epochs")
    for p in prints:
        bound = "None" if p.attack_cost_per_bp is None else f"{p.attack_cost_per_bp:.6f}"
        print(f"    ts {p.ts:>8.0f}  {p.value:.8f}  [{p.ci_lo:.8f}, {p.ci_hi:.8f}]  bound {bound}")

    # 2. Check what the contract would refuse, before paying to find out.
    conn = connect(args.rpc)
    block_ts = None
    if conn is not None:
        # Chain time, not wall time: other suites warp anvil forward, so a
        # wall-clock comparison would be checking the wrong clock.
        block_ts = int(conn[0].eth.get_block("latest")["timestamp"])
    problems = check_invariants(prints, block_ts)
    print(f"\n  invariants       : {'✓ all hold' if not problems else '✗'}")
    for pb in problems:
        print(f"    ✗ {pb}")
    if problems:
        return 1

    if args.dry_run:
        print("\n  (--dry-run: nothing sent)")
        return 0

    # 3. Skips, in the house style: a missing local toolchain is not a failure.
    if not ARTIFACT.exists():
        print(f"\n  — contracts not built, skipping the chain leg ({ARTIFACT.relative_to(ROOT)})")
        print("    build them with: cd contracts && forge build")
        return 0
    if conn is None:
        print(f"\n  — no node reachable at {args.rpc}, skipping the chain leg")
        print("    start one with: anvil --chain-id 31337 --port 8545")
        return 0

    w3, acct = conn
    chain_id = w3.eth.chain_id
    print(f"\n  node             : {args.rpc}  chain {chain_id}  block {w3.eth.block_number}")

    # A node we can reach is not a node we should deploy to. `--rpc` is explicit,
    # so someone deliberately aiming at a testnet is not blocked — but the
    # DEFAULT path lands on 127.0.0.1:8545, and on this machine that port was
    # held by another project's `anvil --chain-id 133`. The test suite now skips
    # there; this script would happily deploy a contract and spend a well-known
    # key on it. Warn loudly rather than refuse, and name the set.
    if chain_id not in LOCAL_CHAIN_IDS:
        print(f"  ! chain {chain_id} is not a local dev chain {sorted(LOCAL_CHAIN_IDS)}.")
        print(f"    About to deploy and send from the PUBLIC anvil dev key {acct.address}")
        print("    on a chain this script does not recognise. If that was not the")
        print("    intent, stop now and use `make subnet-oracle`, which starts its own")
        print("    node on SUBNET_ANVIL_PORT with an explicit --chain-id 31337.")

    from acr_oracle_client import OracleClient

    address = deploy_oracle(w3, acct)
    print(f"  ACROracle        : {address} (deployer is an authorized signer)")

    # Explicit args, not settings: the root conftest strips every ACR_* var, and
    # a script that only works with a populated .env is a script that works on
    # one machine. Every on-chain test in this repo passes these three directly.
    client = OracleClient(rpc_url=args.rpc, oracle_address=address, private_key=ANVIL_KEY)

    posted = 0
    for p in prints:
        try:
            tx = client.post(p)
        except RuntimeError as exc:
            print(f"    ✗ ts {p.ts:.0f} reverted: {exc}")
            return 1
        if tx is None:
            print(f"    — ts {p.ts:.0f} not posted (client offline: no signer or no address)")
            continue
        posted += 1
        print(f"    ✓ ts {p.ts:>8.0f} posted  tx {tx[:18]}…")

    if not posted:
        print("  ✗ nothing was posted")
        return 1

    # 4. Read it back off the chain, which is the only proof that matters.
    back = client.read_latest(args.index)
    print(f"\n  read back from chain ({posted} posted)")
    if not back:
        print("    ✗ the oracle returned no print")
        return 1
    last = prints[-1]
    print(f"    value            : {back['value']:.8f}   (posted {last.value:.8f})")
    print(f"    ci               : [{back['ci_lo']:.8f}, {back['ci_hi']:.8f}]")
    print(f"    timestamp        : {back['timestamp']}   (posted {last.ts:.0f})")
    print(f"    attack_cost_per_bp: {back['attack_cost_per_bp']}")
    if last.attack_cost_per_bp is None:
        print("      ^ 1e-06 is the WIRE FLOOR, not a measurement. The print carried")
        print("        None; ACROracle refuses a zero bound, so PostPayload floors it.")
        print("        Seller diversity is reported as a concentration diagnostic instead.")

    ok = abs(back["value"] - last.value) / last.value < 1e-9
    print(f"\n  {'✓' if ok else '✗'} the chain agrees with the print the subnet produced")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
