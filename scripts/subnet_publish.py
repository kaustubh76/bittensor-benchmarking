#!/usr/bin/env python
"""Publish the subnet's real-tape consensus print into its on-chain commitment.

WHY A COMMITMENT AND NOT A WEIGHT VECTOR. Weights are a *ranking* -- they say
which miner the subnet believes, and on real single-venue tape there is nothing
to rank: `ChutesSource` has no latent to score accuracy against and no funding
graph to price an attack with, so the validator carries weights forward instead
of inventing an ordering (docs/BITTENSOR.md, departure (b)). What the subnet
does have on real tape is the thing the product is actually for: a price. A
commitment is where a price can live on a Bittensor chain, so this is the first
step on 602 that publishes the estimate rather than a league table.

WHAT IT COSTS. Measured on testnet at block 8335689: `Commitments.FieldDeposit`
is 0 and `Commitments.MaxFields` is 3, so a commitment reserves nothing and the
only cost is the extrinsic fee. That is why this is affordable on a wallet
holding tau0.0974 while a registration burn (tau0.0795) is nearly all of it.

DRY BY DEFAULT. Nothing is signed without `--publish`, and after a publish the
commitment is READ BACK off the chain and compared -- a receipt saying Success
is not evidence that storage holds what you sent, which this subnet learned the
expensive way when a `SetWeights` receipt turned out to be a sealed
commit-reveal ciphertext rather than a live weight vector.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "redteam"))

from acr_subnet import (  # noqa: E402
    CopycatMiner,
    FourPillarMiner,
    OverclaimMiner,
    Validator,
    VwapMiner,
    real_tape_epochs,
    run,
    synthetic_epochs,
)
from acr_subnet.adapters.bt_chain import (  # noqa: E402
    NETWORK_MAIN,
    NETWORK_TEST,
    Chain,
    commitment_payload,
)
from optimal_attack import run_optimal_attack  # noqa: E402


def compact(x: float) -> float:
    """Six significant figures. The byte budget is 128 and precision past the
    sixth digit of a rate quoted in dollars per 1k tokens is noise with a cost.
    """
    return float(f"{x:.6g}")


def print_payload(index_id: str, consensus) -> dict:
    """The commitment body. Short keys because every byte is one of 128."""
    return {
        "v": "acr/1",
        "i": index_id,
        "p": compact(consensus.value),
        "lo": compact(consensus.ci_lo),
        "hi": compact(consensus.ci_hi),
        "n": int(consensus.n_obs),
        "ts": int(consensus.ts),
        "src": "chutes",
    }


def build_print(index_id: str, warmup: int, seed: int, events: int):
    """The real-tape consensus print, produced the way `subnet_demo --real` does.

    Same order as that script and for the same reason: real single-venue tape
    cannot score anyone, so weights are earned on known truth first and the real
    epochs publish under them.
    """
    from acr_tape import ChutesSource

    src = ChutesSource()
    epochs = real_tape_epochs(index_id, src)
    if not epochs:
        raise SystemExit(
            f"no real-tape epochs for {index_id}. ChutesSource serves inference "
            "only -- try --index ACR-INF."
        )
    miners = [
        FourPillarMiner(uid=1),
        VwapMiner(uid=2, width_bp=500.0),
        CopycatMiner(uid=3),
        OverclaimMiner(uid=4, multiple=50.0),
    ]
    v = Validator(attack_fn=run_optimal_attack)
    if warmup > 0:
        run(index_id, miners, synthetic_epochs(
            index_id, warmup, seed=seed, events_per_service=events
        ), validator=v)
    rep = run(index_id, miners, epochs, validator=v)
    prints = rep.consensus_prints
    if not prints:
        raise SystemExit("the real-tape run produced no consensus print")
    return epochs, prints


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--index", default="ACR-INF")
    ap.add_argument("--netuid", type=int, default=602)
    ap.add_argument("--network", default=NETWORK_TEST)
    ap.add_argument("--wallet", default="acr-test")
    ap.add_argument(
        "--hotkey",
        default="miner",
        help="the hotkey whose commitment slot this writes. One slot per hotkey "
        "per subnet, so the validator's print and a miner's endpoint cannot "
        "share one. acr-test/miner is uid 0 on 602.",
    )
    ap.add_argument("--warmup", type=int, default=5)
    ap.add_argument("--seed", type=int, default=4242)
    ap.add_argument("--events", type=int, default=2500)
    ap.add_argument("--publish", action="store_true", help="actually sign and write")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    if args.network == NETWORK_MAIN:
        raise SystemExit(
            "refusing to publish on mainnet from this script: the only wallet "
            "here is an unprotected testnet key."
        )

    epochs, prints = build_print(args.index, args.warmup, args.seed, args.events)
    consensus = prints[-1]
    payload = print_payload(args.index, consensus)
    raw = commitment_payload(payload)

    print(f"index      : {args.index}")
    print(f"tape       : {epochs[0].label}   epochs {len(epochs)}   single_venue {epochs[0].single_venue}")
    print(f"print      : {consensus.value:.8f}  [{consensus.ci_lo:.8f}, {consensus.ci_hi:.8f}]")
    print(f"             n_obs {consensus.n_obs}   ts {consensus.ts:.0f}")
    print(f"attack cost: {consensus.attack_cost_per_bp!r}  (None is correct on single-venue tape)")
    print(f"payload    : {json.dumps(payload, separators=(',', ':'), sort_keys=True)}")
    print(f"bytes      : {len(raw)} of 128")

    if not args.publish:
        print("\ndry run. Nothing signed. Pass --publish to write it on chain.")
        return 0

    chain = Chain(network=args.network)
    print(f"\nchain      : {chain.head().cite()}")
    wallet = chain.wallet(args.wallet, args.hotkey)
    hotkey_ss58 = wallet.hotkey.ss58_address
    uid = chain.uid_of(args.netuid, hotkey_ss58)
    print(f"hotkey     : {hotkey_ss58}  uid {uid}")
    if uid is None:
        raise SystemExit(
            f"{hotkey_ss58} holds no uid on netuid {args.netuid}. A commitment "
            "from an unregistered hotkey is storage nobody reads."
        )

    result = chain.set_commitment(wallet, args.netuid, payload)
    print(f"receipt    : {result}")

    # The only thing that settles it.
    back = chain.commitments(args.netuid).get(hotkey_ss58)
    print(f"read back  : {getattr(back, 'value', None)!r}")
    print(f"             block {getattr(back, 'block', None)}  status {getattr(back, 'status', None)}")
    sent = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    if getattr(back, "value", None) == sent:
        print("\nVERIFIED: the chain holds exactly the payload that was sent.")
        return 0
    print("\nMISMATCH: the receipt is not the storage. Do not quote this print.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
