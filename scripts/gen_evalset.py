#!/usr/bin/env python
"""Frozen eval sets — held-out scenarios with their measured values pinned.

`make eval-gate` asks "are the published claims still true" against one-sided
thresholds with 1.4-4x of headroom: the measured 124.1 bp could double to 248
and every gate would still pass green. `tests/golden/` asks "did the engine's
output move" with zero tolerance, on one window. Neither asks "did quality get
worse", which is the question a benchmark has to keep answering.

This is the dataset that answers it, and the reason it uses HELD-OUT seeds:
seed 21 is already what the eval gate and test_claims measure, and it is the
lucky one. On held-out seeds ACR-INF's resistance ratio is 24.4 and 24.1, not
the quoted 46.0 — a 22% margin over the published 20x gate rather than 130%. A
change that helps seed 21 and hurts everything else currently passes every gate
in the repository.

    uv run python scripts/gen_evalset.py            # write tests/eval/
    uv run python scripts/gen_evalset.py --check    # compare, exit 1 on drift

Values are pinned with a per-field tolerance and a DIRECTION, not by re-applying
the gate's thresholds. +-5% on 124.1 bp is +-6.2 bp against the gate's 176 bp of
slack: a 28x tightening that still survives a numpy point release. Two fields are
pinned exactly (`n_windows`, `cleaned_pct_mean`) because they are deterministic
and any move is a real change in the cleaning stack rather than sampling noise.

`attack_vwap_err_bp` is a CONTROL: it must move in neither direction. Every
change that lowers ACR's error should be checked against the naive baseline
standing still, or a weakened simulated attack reads as an estimator win.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Before the acr_* imports: the first `import numpy` loads libopenblas and reads
# these, and two LAPACK paths sit on the hot path (the smoother's solve and the
# SVD behind statsmodels' WLS). See conftest.py.
for _v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_v, "1")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from acr_core import ALL_INDEX_IDS  # noqa: E402
from eval import run_eval  # noqa: E402

EVALSET_SCHEMA = "acr.evalset/1"
TOOL_VERSION = 1
EVAL_DIR = Path(__file__).resolve().parents[1] / "tests" / "eval"

#: Five scenarios, each earning its place by catching something the others
#: cannot. `published` keeps the quoted numbers honest; `heldout_a` is a pure
#: seed hold-out; `heldout_b` changes the attack SHAPE, not just the draw;
#: `quiet` is the only scenario in the repo that measures ACR against truth with
#: no adversary present at all; `heavy` catches a defence tuned to exactly $6k.
SCENARIOS: list[dict] = [
    {"name": "published", "seed": 21, "hours": 12, "attack_from": 4, "attack_to": 8, "budget": 6000.0},
    {"name": "heldout_a", "seed": 101, "hours": 12, "attack_from": 4, "attack_to": 8, "budget": 6000.0},
    {"name": "heldout_b", "seed": 202, "hours": 12, "attack_from": 2, "attack_to": 10, "budget": 6000.0},
    {"name": "quiet", "seed": 303, "hours": 6, "attack_from": 0, "attack_to": 0, "budget": 0.0},
    {"name": "heavy", "seed": 404, "hours": 12, "attack_from": 4, "attack_to": 8, "budget": 24000.0},
]

#: field -> (tolerance kind, tolerance, direction). "lower"/"higher" say which
#: way is an improvement; "control" must not move either way; "exact" pins.
FIELDS: dict[str, tuple[str, float, str]] = {
    "attack_acr_err_bp": ("rel", 0.05, "lower"),
    "attack_vwap_err_bp": ("rel", 0.05, "control"),
    "quiet_acr_err_bp": ("rel", 0.05, "lower"),
    "resistance_ratio": ("rel", 0.05, "higher"),
    "median_abs_err_bp": ("rel", 0.05, "lower"),
    "max_hour_err_bp": ("rel", 0.10, "lower"),
    "mean_ci_width_bp": ("rel", 0.05, "report"),
    "mean_winkler_bp": ("rel", 0.05, "lower"),
    "ci_coverage_n": ("abs", 1.0, "higher"),
    "n_windows": ("exact", 0.0, "exact"),
    "cleaned_pct_mean": ("exact", 0.0, "exact"),
}


def run_scenarios(indices=ALL_INDEX_IDS) -> dict[str, dict[str, dict]]:
    """Every scenario for every index — THE producer.

    `rate.py` imports this rather than re-running the same simulations, so the
    eval-set check and the quality gate cost one pass between them, not two.
    """
    out: dict[str, dict[str, dict]] = {}
    for index_id in indices:
        out[index_id] = {}
        for sc in SCENARIOS:
            out[index_id][sc["name"]] = run_eval(
                index=index_id, hours=sc["hours"], attack_from=sc["attack_from"],
                attack_to=sc["attack_to"], budget=sc["budget"], seed=sc["seed"],
            )
    return out


def capture(indices=ALL_INDEX_IDS, results=None) -> dict[str, dict]:
    """Reduce the scenario runs to the pinned fields, per index."""
    from acr_estimator.cleaning import POLICY_VERSION, policy_hash

    results = results or run_scenarios(indices)
    docs: dict[str, dict] = {}
    for index_id in indices:
        expected: dict[str, dict] = {}
        for sc in SCENARIOS:
            r = results[index_id][sc["name"]]
            row: dict[str, dict] = {}
            for field, (kind, tol, direction) in FIELDS.items():
                v = r.get(field)
                # A quiet scenario has no attack, so the attack fields are
                # OMITTED rather than pinned to a zero that would read as
                # "measured, and perfect".
                if v is None:
                    continue
                entry: dict = {"v": round(float(v), 6), "dir": direction}
                if kind == "rel":
                    entry["tol_rel"] = tol
                elif kind == "abs":
                    entry["tol_abs"] = tol
                    entry["of"] = r.get("n_windows")
                else:
                    entry["exact"] = True
                row[field] = entry
            expected[sc["name"]] = row
        docs[index_id] = {
            "schema": EVALSET_SCHEMA,
            "tool_version": TOOL_VERSION,
            "index_id": index_id,
            "policy_hash": policy_hash(),
            "policy_version": POLICY_VERSION,
            # Asserted, not assumed: re-anchoring moves the price level and
            # nothing else, so nothing pinned here is a function of it. Measured
            # by re-anchoring ACR-INF 667x and watching every bp figure hold.
            "anchor_sensitive_fields": [],
            "scenarios": SCENARIOS,
            "expected": expected,
        }
    return docs


def compare(fresh: dict, frozen: dict) -> list[str]:
    """Drift, respecting each field's own tolerance."""
    out: list[str] = []
    for scenario, row in fresh["expected"].items():
        prev = frozen.get("expected", {}).get(scenario)
        if prev is None:
            out.append(f"{scenario}: not in the frozen set")
            continue
        for field, entry in row.items():
            was = prev.get(field)
            if was is None:
                out.append(f"{scenario}.{field}: newly pinned ({entry['v']})")
                continue
            v, base = entry["v"], was["v"]
            if entry.get("exact"):
                if v != base:
                    out.append(f"{scenario}.{field}: {base} -> {v} (pinned exactly)")
            elif "tol_abs" in entry:
                if abs(v - base) > entry["tol_abs"]:
                    out.append(f"{scenario}.{field}: {base} -> {v}")
            else:
                lim = abs(base) * entry["tol_rel"]
                if abs(v - base) > lim:
                    out.append(f"{scenario}.{field}: {base} -> {v} (>{entry['tol_rel']:.0%})")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="compare, do not write")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    docs = capture()

    if not args.check:
        for index_id, doc in docs.items():
            (EVAL_DIR / f"{index_id}.json").write_text(
                json.dumps(doc, indent=2, sort_keys=True) + "\n"
            )
            pub = doc["expected"]["published"]
            print(f"  {index_id:<9} pinned {len(doc['expected'])} scenario(s); "
                  f"published attack={pub['attack_acr_err_bp']['v']} bp "
                  f"coverage={pub['ci_coverage_n']['v']:.0f}/{pub['n_windows']['v']:.0f}")
        return 0

    drift: list[str] = []
    for index_id, doc in docs.items():
        path = EVAL_DIR / f"{index_id}.json"
        if not path.exists():
            print(f"  ✗ {path.name} missing — run `make evalset`")
            return 1
        drift += [f"{index_id}.{d}" for d in compare(doc, json.loads(path.read_text()))]
    if drift:
        print(f"  ✗ the eval set drifted in {len(drift)} place(s):")
        for d in drift:
            print(f"    {d}")
        print("\n  If this is deliberate, re-run `make evalset` in the SAME commit.")
        return 1
    print(f"  ✓ {len(docs)} index(es) reproduce their frozen eval set")
    return 0


if __name__ == "__main__":
    sys.exit(main())
