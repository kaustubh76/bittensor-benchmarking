#!/usr/bin/env python
"""Run the ACR estimator end-to-end on simulated exhaust and print live rates.

    uv run python scripts/run_pipeline.py [--attack] [--seed N]

The vertical slice: simulate payment exhaust (optionally with a wash attack),
run the four-pillar estimator, and print each ACR index with its CI and
attack-cost-per-bp — the gold box, on your terminal.
"""

from __future__ import annotations

import argparse

from acr_core import ALL_INDEX_IDS, spec_for
from acr_estimator import estimate_all
from acr_sim import AttackConfig, SimConfig, simulate


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--attack", action="store_true", help="inject a wash-flow attack")
    ap.add_argument("--budget", type=float, default=8000.0)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--events", type=int, default=4000)
    ap.add_argument("--horizon", type=float, default=3600.0, help="window seconds (1 hourly print)")
    args = ap.parse_args()

    attack = (
        AttackConfig(budget_usdc=args.budget, target_multiplier=2.5, trade_notional=40.0)
        if args.attack
        else None
    )
    cfg = SimConfig(
        seed=args.seed,
        horizon=args.horizon,
        events_per_service=args.events,
        attack=attack,
    )
    res = simulate(cfg)

    print(f"\n  ACR — The Arc Compute Rate   (seed={args.seed}"
          f"{', ATTACK ON' if args.attack else ''})")
    print(f"  simulated {len(res.events):,} authorizations"
          f"{f', {res.n_adversarial:,} adversarial' if args.attack else ''}\n")
    print(f"  {'INDEX':<9} {'RATE':>12} {'UNIT':<12} {'CI(bp)':>7} "
          f"{'$/bp':>8} {'$/1% move':>10} {'VWAP':>10}  N")
    print("  " + "-" * 82)

    results = estimate_all(res.events, res.attestations)
    for iid in ALL_INDEX_IDS:
        if iid not in results:
            continue
        p, d = results[iid]
        spec = spec_for(iid)
        true = res.true_window_level(iid)
        err_bp = 1e4 * abs(p.value - true) / true
        print(
            f"  {iid:<9} {p.value:>12.5f} {spec.unit:<12} {p.ci_width_bp:>7.1f} "
            f"{p.attack_cost_per_bp:>8.3f} {d.bound.cost_to_move_1pct:>10,.2f} "
            f"{d.naive_vwap:>10.5f}  {p.n_obs}"
        )
        print(f"  {'':9} true {true:>7.5f}  err {err_bp:>5.0f}bp   "
              f"cleaned {d.cleaning.removed_fraction*100:>4.1f}%  "
              f"hedonic R²={d.hedonic.r2:.2f}")
    print()


if __name__ == "__main__":
    main()
