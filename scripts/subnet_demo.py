#!/usr/bin/env python
"""Run the ACR subnet locally — four competing miners, scored, weighted, published.

No chain, no netuid, no TAO, no network. The mechanism is arithmetic over the
ACR estimator, so it runs in-process and deterministically, which is what makes
it testable rather than merely demonstrable.

    uv run python scripts/subnet_demo.py                 # synthetic epochs
    uv run python scripts/subnet_demo.py --real          # live Chutes tape (offline fixture)
    uv run python scripts/subnet_demo.py --epochs 8 --index ACR-GPU

What it is supposed to show, in order of how much it matters:

1. **The four-pillar miner outranks the naive VWAP miner.** Not by accuracy —
   on quiet tape a VWAP is close to the level and scores respectably — but
   because the VWAP has no cleaning stage and the resistance component measures
   exactly that. If this ordering ever inverts, the resistance component has
   stopped working and the subnet is paying for the wrong thing.
2. **The over-claiming miner is penalised** despite submitting the same estimate
   as the honest one, because the validator tries its declared bound and finds
   it cannot be met.
3. **The copycat earns little**, because the only print it can see is last
   epoch's published consensus and the level has moved since.

The synthetic epochs carry a latent the miners cannot see, which is where
accuracy is scorable. `--real` runs against Bittensor subnet 64 flow, where no
latent exists and accuracy is therefore not scored at all — see
`acr_subnet.validator` for why manufacturing one would be dishonest.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

for _v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_v, "1")

ROOT = Path(__file__).resolve().parents[1]
# redteam/ is not a package -- the same sys.path insertion scripts/run_demo.py
# and tests/test_claims.py use. Kept here in the script rather than inside
# acr_subnet: a library that mutates sys.path at import time breaks whatever
# else is in the process.
sys.path.insert(0, str(ROOT / "redteam"))

from acr_core import ALL_INDEX_IDS  # noqa: E402
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
from optimal_attack import run_optimal_attack  # noqa: E402


def fmt(v: float | None, width: int = 7, places: int = 3) -> str:
    return f"{chr(8212):>{width}}" if v is None else f"{v:{width}.{places}f}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--index", default="ACR-INF", choices=list(ALL_INDEX_IDS))
    ap.add_argument("--epochs", type=int, default=6)
    ap.add_argument(
        "--seed",
        type=int,
        default=4242,
        help="the validator's private seed. Deliberately not 21: gen_evalset.py "
        "records that as the favourable published seed (46x resistance there, 24x "
        "on a seed nobody chose), and a validator scoring against a seed miners "
        "can read is not scoring out of sample.",
    )
    ap.add_argument("--real", action="store_true", help="use live Chutes tape instead of the simulator")
    ap.add_argument("--events", type=int, default=2500, help="simulated events per service per epoch")
    ap.add_argument(
        "--warmup",
        type=int,
        default=5,
        help="with --real: scoring epochs to run first, to establish weights. Real "
        "single-venue tape cannot score anyone (no latent, and no sybil defence to "
        "measure), so it publishes under weights earned on known truth — the same "
        "order a real benchmark is run in. 0 disables.",
    )
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    miners = [
        FourPillarMiner(uid=1),
        VwapMiner(uid=2, width_bp=500.0),
        CopycatMiner(uid=3),
        OverclaimMiner(uid=4, multiple=50.0),
    ]

    print(f"ACR subnet — local run · index {args.index}")
    print("=" * 78)

    if args.real:
        from acr_tape import ChutesSource

        src = ChutesSource()
        epochs = real_tape_epochs(args.index, src)
        if not epochs:
            print("\n  ✗ no real-tape epochs for this index.")
            print("    ChutesSource serves inference only, so ACR-GPU and ACR-DATA have")
            print("    no real tape yet. Lium's GPU prices are asks on idle inventory,")
            print("    which belong in anchors/ rather than here. Try --index ACR-INF,")
            print("    or refresh the fixture:")
            print("      uv run python scripts/bt_snapshot.py --capture-fixture")
            return 1
        print(f"tape      : {epochs[0].label}")
        print(f"epochs    : {len(epochs)} (one per {src.native_step_s/3600:.0f}h, the upstream period)")
        print("accuracy  : NOT SCORED — real flow has no latent to score against.")
        print("            The epoch measures resistance and honesty only.")
        print(f"single_venue: {epochs[0].single_venue} — one aggregate buyer, so the")
        print("            cleaning stage's self-referential checks are disarmed and")
        print("            no attack cost is published. A price survives single-venue")
        print("            flow; an attack cost does not.")
    else:
        epochs = synthetic_epochs(
            args.index, args.epochs, seed=args.seed, events_per_service=args.events
        )
        if not epochs:
            print("  ✗ the simulator produced no events for this index")
            return 1
        print(f"tape      : {epochs[0].label}")
        print(f"epochs    : {len(epochs)}  (validator seed {args.seed}, private to it)")
        print("accuracy  : scored against the simulator's latent level, which the")
        print("            miners cannot see — there is no answer in the world to copy.")

    v = Validator(attack_fn=run_optimal_attack)

    if args.real and args.warmup > 0:
        warm = synthetic_epochs(
            args.index, args.warmup, seed=args.seed, events_per_service=args.events
        )
        if warm:
            wrep = run(args.index, miners, warm, validator=v)
            print(f"\nwarm-up   : {len(warm)} scoring epochs on known truth, to earn weights")
            for s in wrep.standings:
                print(f"            {s.strategy:<13} weight {s.total_weight:7.4f}")

    rep = run(args.index, miners, epochs, validator=v)

    print("\n--- scoreboard " + "-" * 62)
    print(f"{'strategy':<13}{'weight':>9}{'score':>9}{'acc':>9}{'resist':>9}"
          f"{'honest':>9}{'cover':>8}{'gate':>6}")
    for s in rep.standings:
        cov = "    — " if s.coverage is None else f"{s.coverage:6.2f}"
        print(
            f"{s.strategy:<13}{s.total_weight:9.4f}{s.mean_total:9.4f}"
            f"{fmt(s.mean_accuracy, 9, 4)}{fmt(s.mean_resistance, 9, 4)}"
            f"{fmt(s.mean_attainability, 9, 4)}{cov:>9}{s.gate_failures:>6}"
        )

    print("\n--- detail " + "-" * 66)
    for s in rep.standings:
        bits = []
        if "err_bp" in s.detail:
            bits.append(f"err {s.detail['err_bp']:.1f} bp")
        if "ci_width_bp" in s.detail:
            bits.append(f"band {s.detail['ci_width_bp']:.0f} bp")
        if "declared_over_measured" in s.detail:
            bits.append(f"claim/actual {s.detail['declared_over_measured']:.2f}x")
        print(f"  {s.strategy:<13} submitted {s.submissions}/{len(epochs)}"
              + (("  " + ", ".join(bits)) if bits else ""))

    cons = rep.consensus_prints
    print(f"\n--- consensus prints ({len(cons)}) " + "-" * 50)
    for c in cons[:6]:
        bound = "None (single-venue: no graph to price an attack against)" \
            if c.attack_cost_per_bp is None else f"${c.attack_cost_per_bp:.6f}/bp"
        print(f"  ts {c.ts:>10.0f}  {c.value:.8f}  "
              f"[{c.ci_lo:.8f}, {c.ci_hi:.8f}]  {c.ci_width_bp:7.1f} bp  bound {bound}")
    for r in rep.results:
        if r.consensus is None and r.note:
            print(f"  epoch {r.epoch.number}: no print — {r.note}")

    # The orderings that are the point of the run.
    fp = rep.by_strategy("four-pillar")
    vw = rep.by_strategy("naive-vwap")
    oc = rep.by_strategy("overclaim")
    # On real tape the print carries no attack cost, because the funding graph is
    # a star and the cleaning stage has nothing to compare against. What IS
    # observable is the thing an attack cost is a claim about: how many
    # independent sellers the venue has. Published beside the print, never as it.
    if args.real:
        from acr_tape import venue_concentration

        vc = venue_concentration()
        if vc is not None:
            print("\n--- venue concentration (netuid 64) " + "-" * 40)
            print("  Not an attack cost. A direct measurement of seller diversity,")
            print("  denominated in identities — see docs/BITTENSOR.md §3.")
            print(f"    hotkeys / coldkeys   : {vc.n_hotkeys} / {vc.n_coldkeys}")
            print(f"    multi-hotkey actors  : {vc.multi_hotkey_coldkeys}"
                  f"  (largest controls {vc.largest_cluster})")
            print(f"    second faces         : {vc.sybil_headroom} of {vc.n_hotkeys} hotkeys"
                  f" are not a distinct seller")
            print(f"    identity HHI         : {vc.identity_hhi:.4f}  (diffuse)")
            print(f"    stake HHI            : {vc.stake_hhi:.4f}  (concentrated)")
            print(f"    hotkeys with stake   : {vc.staked_hotkeys} of {vc.n_hotkeys}")
            print(f"    ip co-location signal: {'usable' if vc.ip_usable else 'UNUSABLE'}"
                  f"  ({vc.distinct_ips} real addresses across {vc.n_hotkeys} hotkeys)")

    # ACR-TAO: the same rate in TAO, and what a subnet's alpha buys of its own
    # work. A numeraire change, not a fourth index — see acr_subnet.denomination.
    if args.real and cons:
        from acr_subnet import cross_check_tao_usd, denominate
        from acr_tape import load_fixture, pool_from_fixture

        fx = load_fixture() or {}
        tao_usd = fx.get("tao_price_usd")
        if tao_usd:
            d = denominate(cons[-1], float(tao_usd), pool_from_fixture(64))
            if d is not None:
                print("\n--- ACR-TAO (the same print, in TAO) " + "-" * 39)
                print("  A change of currency, not a new index: the good is the same unit")
                print("  of inference. It moves when TAO moves even if compute does not,")
                print("  which is why ACR's own estimand is a dollar rate.")
                print(f"    USD per 1k tokens  : {d.usd_per_unit:.8f}")
                print(f"    TAO/USD            : {d.tao_usd:,.4f}  (via {d.tao_usd_source})")
                print(f"    TAO per 1k tokens  : {d.tao_per_unit:.12f}")
                print(f"    1 TAO buys         : {d.units_per_tao:,.0f}k tokens")
                if d.alpha_per_unit is not None:
                    print(f"    netuid {d.netuid} alpha price: {d.alpha_price_tao:.6f} TAO")
                    print(f"    alpha per 1k tokens: {d.alpha_per_unit:.8f}")
                    print("      ^ what a subnet's own token buys of the work it delivers.")
                    print("        No existing Bittensor metric is this: an alpha price is a")
                    print("        claim on future emission, not a price of delivered compute.")
                # The Chutes side of this cross-check lives in its OWN fixture
                # under `tao_usd`. Nothing has ever written `chutes_tao_usd`, so
                # the corroboration `denomination.cross_check_tao_usd` documents
                # as load-bearing silently never ran. `bt_snapshot.read_taostats`
                # already reads it from the right place.
                pricing = load_fixture(ROOT / "tests" / "fixtures" / "bt" / "chutes_pricing.json")
                chutes_tao = (pricing or {}).get("tao_usd") or None
                if chutes_tao:
                    div, ok = cross_check_tao_usd(float(tao_usd), float(chutes_tao))
                    print(f"    cross-check        : {div * 100:.2f}% vs chutes /pricing"
                          f"  {'ok' if ok else 'DIVERGENT'}")

    print("\n--- what this run claims " + "-" * 52)
    if fp and vw:
        ok = fp.total_weight > vw.total_weight
        print(f"  {'✓' if ok else '✗'} four-pillar outranks naive VWAP: "
              f"{fp.total_weight:.4f} vs {vw.total_weight:.4f}")
    if fp and oc:
        ok = fp.total_weight > oc.total_weight
        print(f"  {'✓' if ok else '✗'} honest bound outranks the over-claimed one: "
              f"{fp.total_weight:.4f} vs {oc.total_weight:.4f}"
              + (f"  (claim/actual {oc.detail.get('declared_over_measured', 0):.1f}x)"
                 if oc.detail.get("declared_over_measured") else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
