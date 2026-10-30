#!/usr/bin/env python
"""Evaluation harness — the money-shot chart: naive-VWAP error vs ACR error.

Runs a multi-hour simulation with a wash attack switched on for a window of
hours, estimates ACR hourly, and scores both ACR and naive VWAP against the
window's true level. Emits a JSON series (for the Terminal) and prints the
headline metric: mean absolute error, attack hours vs quiet hours.

    uv run python scripts/eval.py [--index ACR-INF] [--hours 12] [--out PATH]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from acr_core import get_settings, spec_for
from acr_estimator import estimate_index, naive_vwap
from acr_sim import AttackConfig, SimConfig, simulate

HOUR = 3600.0


def run_eval(
    index: str = "ACR-INF",
    hours: int = 12,
    attack_from: int = 4,
    attack_to: int = 8,
    budget: float = 6000.0,
    seed: int = 21,
) -> dict:
    """Run the hourly ACR-vs-VWAP error series and return series + metrics.

    Shared by the CLI and ``tests/test_claims.py`` so the gate checks the exact
    numbers the committed ``eval.json`` reports.
    """
    horizon = hours * HOUR
    svc = spec_for(index).service
    settings = get_settings()

    # No budget, or no window, means NO adversary — not an adversary of zero
    # size. Passing an AttackConfig with t_start == t_end used to stack every
    # wash print on a single instant while labelling each hour quiet.
    attack = (
        None
        if budget <= 0.0 or attack_to <= attack_from
        else AttackConfig(
            budget_usdc=budget, target_multiplier=2.5, trade_notional=40.0,
            t_start=attack_from * HOUR, t_end=attack_to * HOUR,
        )
    )
    res = simulate(
        SimConfig(seed=seed, horizon=horizon, events_per_service=hours * 2500, attack=attack)
    )
    events = [e for e in res.events if e.service == svc]

    series: list[dict] = []
    acr_errs, vwap_errs, atk_acr, atk_vwap, quiet_acr = [], [], [], [], []
    # Calibration, which nothing in this repo has ever measured. Every print
    # ships a 95% interval (methodology section 4) and no code checks how often
    # it contains the truth. Coverage alone is gamed by widening the band, so
    # the Winkler interval score is carried beside it: it charges for width AND
    # for missing, so a wider interval only scores better if it starts covering.
    covered, widths_bp, winklers_bp, abs_errs = 0, [], [], []
    for h in range(hours):
        t0, t1 = h * HOUR, (h + 1) * HOUR
        window = [e for e in events if t0 <= e.ts < t1]
        if len(window) < 50:
            continue
        true = res.true_window_level(index, window)
        p, d = estimate_index(index, window, res.attestations, ts=t1, settings=settings)
        vwap = naive_vwap(window)
        acr_err = 1e4 * abs(p.value - true) / true
        vwap_err = 1e4 * abs(vwap - true) / true
        is_attack = attack_from <= h < attack_to
        series.append({
            "hour": h, "true": true, "acr": p.value, "acr_ci_lo": p.ci_lo,
            "acr_ci_hi": p.ci_hi, "vwap": vwap, "acr_err_bp": acr_err,
            "vwap_err_bp": vwap_err, "attack": is_attack,
            "attack_cost_per_bp": p.attack_cost_per_bp, "n_obs": p.n_obs,
            "cleaned_pct": 100 * d.cleaning.removed_fraction,
        })
        acr_errs.append(acr_err)
        vwap_errs.append(vwap_err)
        abs_errs.append(acr_err)
        covered += int(p.ci_lo <= true <= p.ci_hi)
        widths_bp.append(1e4 * (p.ci_hi - p.ci_lo) / true)
        # Winkler, alpha = 1 - ci_level, expressed in bp of the truth.
        alpha = 1.0 - settings.ci_level
        penalty = max(p.ci_lo - true, 0.0) + max(true - p.ci_hi, 0.0)
        winklers_bp.append(1e4 * ((p.ci_hi - p.ci_lo) + (2.0 / alpha) * penalty) / true)
        (atk_acr if is_attack else quiet_acr).append(acr_err)
        if is_attack:
            atk_vwap.append(vwap_err)

    attack_acr = sum(atk_acr) / len(atk_acr) if atk_acr else 0.0
    attack_vwap = sum(atk_vwap) / len(atk_vwap) if atk_vwap else 0.0
    quiet = sum(quiet_acr) / len(quiet_acr) if quiet_acr else 0.0
    n = len(series)
    ordered = sorted(abs_errs)
    return {
        "index": index,
        "series": series,
        "mean_acr_err_bp": sum(acr_errs) / len(acr_errs) if acr_errs else 0.0,
        "mean_vwap_err_bp": sum(vwap_errs) / len(vwap_errs) if vwap_errs else 0.0,
        "attack_acr_err_bp": attack_acr,
        "attack_vwap_err_bp": attack_vwap,
        "quiet_acr_err_bp": quiet,
        # None, not inf: with no adversary there is no ratio to report, and
        # json.dumps writes a bare `Infinity` that JSON.parse rejects — which
        # would reach the Terminal the first time a quiet scenario is published.
        "resistance_ratio": (attack_vwap / attack_acr) if attack_acr else None,
        "n_adversarial": res.n_adversarial,
        "usdc_burned": res.usdc_attacked,
        # Calibration and shape. Additive: build_gates reads four keys and is
        # untouched by anything below.
        "n_windows": n,
        "ci_coverage_n": covered,
        "ci_coverage": (covered / n) if n else None,
        "mean_ci_width_bp": (sum(widths_bp) / n) if n else None,
        "mean_winkler_bp": (sum(winklers_bp) / n) if n else None,
        "median_abs_err_bp": (ordered[len(ordered) // 2]) if ordered else None,
        "max_hour_err_bp": max(abs_errs) if abs_errs else None,
        "cleaned_pct_mean": (
            sum(h["cleaned_pct"] for h in series) / n if n else None
        ),
    }


def build_gates(
    m: dict,
    max_attack_acr_err_bp: float = 300.0,
    min_attack_vwap_err_bp: float = 4000.0,
    min_ratio: float = 20.0,
    max_quiet_acr_err_bp: float = 300.0,
) -> dict:
    """Apply the claim thresholds to a ``run_eval`` result."""
    return {
        "attack_acr_err_bp": {"value": m["attack_acr_err_bp"], "max": max_attack_acr_err_bp,
                              "pass": m["attack_acr_err_bp"] <= max_attack_acr_err_bp},
        "attack_vwap_err_bp": {"value": m["attack_vwap_err_bp"], "min": min_attack_vwap_err_bp,
                               "pass": m["attack_vwap_err_bp"] >= min_attack_vwap_err_bp},
        "resistance_ratio": {"value": m["resistance_ratio"], "min": min_ratio,
                             "pass": m["resistance_ratio"] >= min_ratio},
        "quiet_acr_err_bp": {"value": m["quiet_acr_err_bp"], "max": max_quiet_acr_err_bp,
                             "pass": m["quiet_acr_err_bp"] <= max_quiet_acr_err_bp},
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", default="ACR-INF")
    ap.add_argument("--hours", type=int, default=12)
    ap.add_argument("--attack-from", type=int, default=4)
    ap.add_argument("--attack-to", type=int, default=8)
    ap.add_argument("--budget", type=float, default=6000.0)
    ap.add_argument("--seed", type=int, default=21)
    ap.add_argument("--out", default="scripts/_out/eval.json")
    # --check turns the eval into a CI gate: assert the headline resistance
    # claims and exit non-zero on any breach. Thresholds are the measured
    # numbers with a safety margin (see docs/methodology.md §8).
    ap.add_argument("--check", action="store_true", help="assert claim thresholds, exit 1 on breach")
    ap.add_argument("--max-attack-acr-err-bp", type=float, default=300.0)
    ap.add_argument("--min-attack-vwap-err-bp", type=float, default=4000.0)
    ap.add_argument("--min-ratio", type=float, default=20.0)
    ap.add_argument("--max-quiet-acr-err-bp", type=float, default=300.0)
    args = ap.parse_args()

    m = run_eval(args.index, args.hours, args.attack_from, args.attack_to, args.budget, args.seed)
    gates = build_gates(
        m, args.max_attack_acr_err_bp, args.min_attack_vwap_err_bp,
        args.min_ratio, args.max_quiet_acr_err_bp,
    )
    all_pass = all(g["pass"] for g in gates.values())
    ratio = m["resistance_ratio"]

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "index_id": args.index,
        "unit": spec_for(args.index).unit,
        "series": m["series"],
        "summary": {
            "mean_acr_err_bp": m["mean_acr_err_bp"],
            "mean_vwap_err_bp": m["mean_vwap_err_bp"],
            "attack_acr_err_bp": m["attack_acr_err_bp"],
            "attack_vwap_err_bp": m["attack_vwap_err_bp"],
            "quiet_acr_err_bp": m["quiet_acr_err_bp"],
            "resistance_ratio": ratio,
            "n_adversarial": m["n_adversarial"],
            "usdc_burned": m["usdc_burned"],
        },
        "gates": gates,
    }
    out.write_text(json.dumps(payload, indent=2))

    s = payload["summary"]
    print(f"\n  ACR evaluation — {args.index}  ({args.hours}h, attack h{args.attack_from}-{args.attack_to})")
    print(f"  wrote {len(m['series'])} hourly prints → {out}\n")
    print(f"  {'':14}{'ACR':>12}{'naive VWAP':>14}")
    print(f"  {'mean err (bp)':14}{s['mean_acr_err_bp']:>12.1f}{s['mean_vwap_err_bp']:>14.1f}")
    print(f"  {'ATTACK err(bp)':14}{s['attack_acr_err_bp']:>12.1f}{s['attack_vwap_err_bp']:>14.1f}")
    print(f"\n  Under attack, naive VWAP errs {ratio:.1f}× more than ACR.")
    print(f"  Attacker burned ${s['usdc_burned']:,.2f} across {s['n_adversarial']:,} wash txs.\n")

    if args.check:
        print("  gate checks:")
        for name, g in gates.items():
            bound = f"<= {g['max']:.0f}" if "max" in g else f">= {g['min']:.0f}"
            print(f"    {'PASS' if g['pass'] else 'FAIL'}  {name:22} {g['value']:.1f} {bound}")
        if not all_pass:
            print("\n  EVAL GATE FAILED\n")
            sys.exit(1)
        print("\n  eval gate passed ✓\n")


if __name__ == "__main__":
    main()
