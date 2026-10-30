#!/usr/bin/env python
"""The auto-rater — did quality regress, and what is the weakest guarantee?

Three gates now, and they ask three different questions:

    golden-check   did the engine's OUTPUT move at all?      zero tolerance
    eval-gate      are the PUBLISHED CLAIMS still true?      1.4-4x of slack
    rate           did QUALITY regress?                      two-sided, per part

The proof they do not overlap is one concrete case: `resistance_ratio` falling
46.0 -> 31.2 passes eval-gate (>= 20) and passes golden-check (different
scenario) and fails here. That gap is what this closes.

**The gate is the vector, not the score.** Each component is IMPROVED, SAME or
REGRESSED against a blessed baseline, and any REGRESSED fails the run. There is
no principled weight between "the interval covers" and "the attack is
expensive", so a weighted mean would make the weights the thing people tune —
which is the failure mode, not the fix. The repo already does it this way:
`build_gates` returns four named checks, not an average.

**The scalar is reported and never gates, and it is a `min`.** A mean answers
"how good on average", which nobody asks of a benchmark. A min answers "what is
the weakest guarantee", which is what a manipulation-resistance claim IS — and
improving anything except the worst component cannot move it, so it physically
cannot hide a regression rather than merely being unlikely to.

    uv run python scripts/rate.py                    # score, compare, exit 1 on regression
    uv run python scripts/rate.py --bless "reason"   # re-bless; refuses an empty reason
"""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

for _v in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_v, "1")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from acr_core import ALL_INDEX_IDS  # noqa: E402
from gen_evalset import SCENARIOS, run_scenarios  # noqa: E402

RATE_SCHEMA = "acr.rate/1"
BASELINE = Path(__file__).resolve().parents[1] / "tests" / "rate" / "baseline.json"

#: Normalisers. Each is a number that already means something in this project
#: rather than a knob: 300 bp and 20x are the published gate thresholds, and 600
#: is twice the gate because one bad hour is a settlement risk, not an average.
ERR_REF_BP = 300.0
RATIO_FLOOR = 20.0
RATIO_CEIL = 100.0
TAIL_REF_BP = 600.0
#: Full scale for the interval score. With alpha = 0.05 a miss of e bp costs
#: 40e, so an interval that misses by two thirds of the tolerated error (200 of
#: the gate's 300 bp) scores about 8000. That is the zero point.
#:
#: Chosen so the scale SATURATES AT NEITHER END: at 4000 the four worst
#: calibration components all clipped to exactly 0.0, and a `min` that is
#: floored cannot register the first improvement in the very place the work is
#: needed. This is an axis maximum, not a claim that 8000 is acceptable — the
#: raw Winkler and the raw coverage sit beside every score for that reason.
WINKLER_REF_BP = 8000.0

#: Per-component tolerance. A 12-sample statistic is not exact; 0.01 of a score
#: in [0,1] is about 3 bp of error, well inside sampling noise and well outside
#: any regression worth shipping.
TOL = 0.01


def _clip(x: float) -> float:
    return max(0.0, min(1.0, x))


@dataclass
class Component:
    key: str
    value: float
    baseline: float | None = None
    verdict: str = "NEW"
    #: The raw measurement behind the score, so a reader is not left holding a
    #: normalised number with no units.
    detail: str = ""


@dataclass
class RateReport:
    components: list[Component] = field(default_factory=list)
    score: float = 0.0
    argmin: str = ""
    regressed: list[str] = field(default_factory=list)
    improved: list[str] = field(default_factory=list)


def score_scenario(index_id: str, scenario: str, r: dict) -> list[Component]:
    """The sub-scores for one scenario, each normalised into [0, 1].

    Absent inputs produce no component rather than a zero: a quiet scenario has
    no attack, and scoring that as "resisted nothing perfectly" or "resisted
    nothing at all" would both be inventions.
    """
    out: list[Component] = []

    def add(name: str, value: float, detail: str) -> None:
        out.append(Component(key=f"{index_id}.{scenario}.{name}", value=round(value, 4),
                             detail=detail))

    quiet = r.get("quiet_acr_err_bp")
    if quiet is not None and r.get("n_windows"):
        add("accuracy", _clip(1.0 - quiet / ERR_REF_BP), f"{quiet:.1f} bp quiet error")

    atk = r.get("attack_acr_err_bp")
    if atk:
        add("resistance", _clip(1.0 - atk / ERR_REF_BP), f"{atk:.1f} bp under attack")

    ratio = r.get("resistance_ratio")
    if ratio:
        # Log, because 46 -> 92 is the same doubling as 20 -> 40 and should
        # score the same gain.
        add("separation", _clip(math.log(ratio / RATIO_FLOOR) / math.log(RATIO_CEIL / RATIO_FLOOR)),
            f"{ratio:.1f}x vs naive")

    tail = r.get("max_hour_err_bp")
    if tail is not None:
        add("tail", _clip(1.0 - tail / TAIL_REF_BP), f"{tail:.1f} bp worst hour")

    wink = r.get("mean_winkler_bp")
    if wink is not None:
        cov, n = r.get("ci_coverage_n"), r.get("n_windows")
        # Winkler, not raw coverage: coverage alone is bought by widening the
        # band, and Winkler charges for width AND for missing, so a wider
        # interval scores better only if it starts covering.
        add("calibration", _clip(1.0 - wink / WINKLER_REF_BP),
            f"Winkler {wink:.0f} bp, covers {cov}/{n}")
    return out


def score_tree(indices=ALL_INDEX_IDS, results=None) -> RateReport:
    results = results or run_scenarios(indices)
    rep = RateReport()
    for index_id in indices:
        for sc in SCENARIOS:
            rep.components += score_scenario(index_id, sc["name"], results[index_id][sc["name"]])
    worst = min(rep.components, key=lambda c: c.value)
    rep.score, rep.argmin = worst.value, worst.key
    return rep


def compare(rep: RateReport, baseline: dict) -> RateReport:
    """Every component judged against its blessed value. Higher is better for
    all of them by construction — the normalisers already carry the direction."""
    base = baseline.get("components", {})
    for c in rep.components:
        prev = base.get(c.key)
        if prev is None:
            c.verdict = "NEW"
            continue
        c.baseline = prev["v"]
        if c.value < prev["v"] - TOL:
            c.verdict = "REGRESSED"
            rep.regressed.append(c.key)
        elif c.value > prev["v"] + TOL:
            c.verdict = "IMPROVED"
            rep.improved.append(c.key)
        else:
            c.verdict = "SAME"
    return rep


def _commit() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=10).stdout.strip()
    except Exception:  # noqa: BLE001 - provenance is nice to have, never load-bearing
        return ""


def bless(rep: RateReport, note: str) -> Path:
    """Write the baseline. Refuses an empty note.

    `gen_golden.py` preaches that re-blessing is a decision and nothing enforces
    it. This enforces it: a baseline with no stated reason is a number whose
    provenance died with the shell that produced it.
    """
    if not note.strip():
        raise ValueError("re-blessing needs a reason — what moved, and why is it right?")
    BASELINE.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "schema": RATE_SCHEMA,
        "blessed_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "blessed_commit": _commit(),
        "note": note.strip(),
        "score": {"v": rep.score, "argmin": rep.argmin},
        "components": {c.key: {"v": c.value, "detail": c.detail} for c in rep.components},
    }
    BASELINE.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    return BASELINE


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bless", metavar="NOTE", default=None,
                    help="write the baseline with a stated reason")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    rep = score_tree()

    if args.bless is not None:
        try:
            path = bless(rep, args.bless)
        except ValueError as exc:
            print(f"  ✗ {exc}")
            return 1
        print(f"  blessed {len(rep.components)} component(s) -> {path.name}")
        print(f"  score {rep.score:.3f}  (limited by: {rep.argmin})")
        return 0

    if not BASELINE.exists():
        print("  ✗ no baseline — run `make rate-bless NOTE=\"...\"` first")
        return 1
    rep = compare(rep, json.loads(BASELINE.read_text()))

    print(f"  ACR score {rep.score:.3f}   (limited by: {rep.argmin})\n")
    for c in rep.components:
        if c.verdict in ("REGRESSED", "IMPROVED", "NEW"):
            was = f"{c.baseline:.3f} -> " if c.baseline is not None else ""
            print(f"    {c.verdict:<10} {c.key:<34} {was}{c.value:.3f}   {c.detail}")
    same = sum(1 for c in rep.components if c.verdict == "SAME")
    print(f"\n  {same} unchanged · {len(rep.improved)} improved · {len(rep.regressed)} regressed")
    if rep.regressed:
        print("\n  If a regression is a deliberate trade, re-bless with the reason:")
        print('    make rate-bless NOTE="…"')
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
