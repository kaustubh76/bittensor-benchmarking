#!/usr/bin/env python
"""Freeze the estimator's output for a fixed scenario — the instrument.

Nothing in this repository pins an estimator output to a literal. Six tests
assert determinism, and every one of them calls the function twice **in the same
process on the same list object**, so a change that shifts every published print
by 50 bp — deterministically, reproducibly — passes all of them green.

That is the gap this closes. `tests/golden/*.json` records what the engine
actually produces today, and `tests/test_golden.py` fails when it moves. A
deliberate change then shows up as a diff a human reads and re-blesses, rather
than as silence.

    uv run python scripts/gen_golden.py            # write tests/golden/
    uv run python scripts/gen_golden.py --check    # exit 1 if anything moved

**Re-blessing is a decision, not a chore.** Regenerating this file is how a
change to the published rate gets recorded; do it in the same commit as the
change, and say in the message which numbers moved and why.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Single-threaded BLAS, and it has to happen HERE — before the acr_* imports
# below, because the first `import numpy` is what loads libopenblas and reads
# these variables. Setting them inside a package that numpy has already pulled in
# does nothing at all.
#
# Two LAPACK paths sit on the hot path: the k x k `np.linalg.solve` in the RTS
# smoother, and the larger SVD behind statsmodels' WLS in the hedonic stage,
# which factorises a ~2500 x 5 design on every print. Thread count changes how
# either is blocked, which changes summation order, which changes the low bits.
for _v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_v, "1")

from acr_core import ALL_INDEX_IDS  # noqa: E402
from acr_estimator import estimate_index  # noqa: E402
from acr_sim import SimConfig, simulate  # noqa: E402

GOLDEN_DIR = Path(__file__).resolve().parents[1] / "tests" / "golden"

#: The frozen scenario. Deliberately NOT the eval scenario (seed 21) or the demo
#: scenario (seed 11): those two are already gated on their own headline numbers,
#: and a golden file that moved whenever they did would be read as "the gate
#: changed" rather than "the engine changed".
SCENARIO = {"seed": 5, "horizon": 3600.0, "events_per_service": 2500}

#: How many digits of each float to record. float64 carries ~17 significant
#: digits; recording all of them pins the last bits, which differ across BLAS
#: builds and would make this file fail for reasons that are not about the
#: engine. 12 is far tighter than any real regression and loose enough to
#: survive a numpy point release.
DIGITS = 12


#: How far a recorded float may move before it counts as the ENGINE moving.
#:
#: 12 recorded digits was chosen to "survive a numpy point release", and it does
#: not survive a different interpreter build: CI pins Python 3.11 and this machine
#: runs 3.13, and `max_cluster_influence_bp` differed in the 12th decimal on all
#: three indices — a relative difference of 7.5e-14 to 1.4e-12. Only that field
#: moved, which is diagnostic rather than surprising: it is the most
#: ill-conditioned path in the capture, so it is where a different BLAS blocking
#: shows up first.
#:
#: 1e-9 relative is roughly seven orders of magnitude tighter than any engine
#: change worth catching — the arrival-order bug this file was built to find moved
#: `attack_cost_per_bp` by 37% — and comfortably looser than a cross-build last-bit
#: difference. Exact equality on a float across two numpy builds is not a property
#: anything can hold; asserting it made the golden fail for reasons that are not
#: about the engine, which is precisely what its own docstring warns against.
REL_TOL = 1e-9


def _r(x: float | None) -> float | None:
    return None if x is None else round(float(x), DIGITS)


def moved_fields(frozen: dict, fresh: dict) -> list[str]:
    """Which recorded fields actually moved, per index.

    Ints and None compare exactly — `n_obs` changing by one IS the engine
    changing. Floats compare by relative difference, for the reason above.
    """
    out: list[str] = []
    for index_id, row in fresh.get("indices", {}).items():
        prev = frozen.get("indices", {}).get(index_id)
        if prev is None:
            out.append(f"{index_id}: missing from the baseline")
            continue
        for key, now in row.items():
            was = prev.get(key)
            if isinstance(now, float) and isinstance(was, float):
                scale = max(abs(was), abs(now), 1e-12)
                if abs(now - was) / scale > REL_TOL:
                    out.append(f"{index_id}.{key}: {was} -> {now}")
            elif was != now:
                out.append(f"{index_id}.{key}: {was} -> {now}")
    return out


def capture() -> dict:
    """Run the frozen scenario and reduce it to the numbers worth pinning."""
    res = simulate(SimConfig(**SCENARIO))
    out: dict = {"scenario": SCENARIO, "digits": DIGITS, "indices": {}}
    for index_id in ALL_INDEX_IDS:
        p, d = estimate_index(index_id, res.events, res.attestations, ts=3600.0)
        out["indices"][index_id] = {
            # The print itself — what actually reaches the chain.
            "value": _r(p.value),
            "ci_lo": _r(p.ci_lo),
            "ci_hi": _r(p.ci_hi),
            "attack_cost_per_bp": _r(p.attack_cost_per_bp),
            "n_obs": p.n_obs,
            "trim_alpha": _r(p.trim_alpha),
            # The stages behind it, so a diff says WHERE the print moved rather
            # than only that it did.
            "naive_vwap": _r(d.naive_vwap),
            "n_raw": d.n_raw,
            "n_clean": d.n_clean,
            "tilt": _r(d.tilt),
            "tilt_std": _r(d.tilt_std),
            "excluded_fraction": _r(d.cleaning.excluded_fraction),
            "cap_scaled_fraction": _r(d.cleaning.cap_scaled_fraction),
            "n_communities": len(d.cleaning.communities),
            "n_sybil_flagged": len(d.cleaning.flagged_sybil),
            "n_self_dealing": len(d.cleaning.flagged_self_dealing),
            "n_wash": len(d.cleaning.flagged_wash),
            "hedonic_r2": _r(d.hedonic.r2),
            "bound_cost_per_bp": _r(d.bound.cost_per_bp),
            "bound_clusters": d.bound.sybil_clusters_required,
            "bound_identities": d.bound.min_identities,
            "flip_fraction": _r(d.robustness.single_cluster_flip_fraction),
            "max_cluster_share": _r(d.robustness.max_cluster_share),
            "max_cluster_influence_bp": _r(d.robustness.max_cluster_influence_bp),
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="compare, do not write")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    path = GOLDEN_DIR / "estimator.json"
    fresh = capture()

    if not args.check:
        path.write_text(json.dumps(fresh, indent=2, sort_keys=True) + "\n")
        print(f"  wrote {path.relative_to(GOLDEN_DIR.parents[1])}")
        for iid, row in fresh["indices"].items():
            print(f"    {iid:<9} value={row['value']}  n_obs={row['n_obs']}  "
                  f"cost/bp={row['attack_cost_per_bp']}")
        return 0

    if not path.exists():
        print(f"  ✗ {path} does not exist — run `make golden` first")
        return 1
    old = json.loads(path.read_text())
    moved = [f"    {m}" for m in moved_fields(old, fresh)]
    if moved:
        print(f"  ✗ the engine moved in {len(moved)} place(s):")
        print("\n".join(moved))
        print("\n  If this is deliberate, re-run `make golden` in the SAME commit")
        print("  and say in the message which numbers moved and why.")
        return 1
    print("  ✓ the engine reproduces its frozen output exactly")
    return 0


if __name__ == "__main__":
    sys.exit(main())
