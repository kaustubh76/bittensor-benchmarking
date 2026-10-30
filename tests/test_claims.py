"""The headline resistance claims, asserted.

Replaces the toothless ">10% VWAP / ACR < ½·VWAP" smoke check as the automated
gate on the numbers in docs/methodology.md §8 and IMPLEMENTATION.md. Two seeded,
deterministic scenarios:

  * paired 1-hour demo ($8k budget): naive VWAP dragged **>100%**, ACR within
    **±3%**, resistance **≥20×** — for every index.
  * 12-hour eval series: attack-window VWAP error **>40%**, ACR error **<3%**,
    ratio **≥20×**, and quiet-hour ACR error **<3%**.

Runtime is ~a minute; it runs in the default suite and in CI so the marketing
numbers cannot drift from the code.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "redteam"))
sys.path.insert(0, str(_ROOT / "scripts"))

from eval import build_gates, run_eval  # noqa: E402
from wash_attack import run_attack  # noqa: E402


@pytest.fixture(scope="module")
def demo_report():
    return run_attack(budget_usdc=8000.0, seed=11)


@pytest.fixture(scope="module")
def eval_metrics():
    return run_eval(hours=12, seed=21)


# --- paired demo scenario (the "Attack the Index" show) -----------------------

def test_demo_vwap_dragged_over_100pct(demo_report):
    for r in demo_report.results:
        assert r.vwap_swing_pct > 100.0, f"{r.index_id} VWAP only moved {r.vwap_swing_pct:.1f}%"


def test_demo_acr_barely_moves(demo_report):
    for r in demo_report.results:
        assert abs(r.acr_swing_pct) < 3.0, f"{r.index_id} ACR moved {r.acr_swing_pct:.2f}%"


def test_demo_resistance_ratio_at_least_20x(demo_report):
    for r in demo_report.results:
        assert r.resistance_ratio >= 20.0, f"{r.index_id} ratio only {r.resistance_ratio:.0f}×"


def test_demo_attack_cost_reported(demo_report):
    for r in demo_report.results:
        assert r.attack_cost_per_bp > 0.0


# --- 12h eval series (the judge chart) ---------------------------------------

def test_eval_series_gates(eval_metrics):
    gates = build_gates(eval_metrics, min_attack_vwap_err_bp=4000.0)
    failed = {k: g for k, g in gates.items() if not g["pass"]}
    assert not failed, f"eval gates failed: {failed}"


def test_eval_acr_far_more_accurate_than_vwap(eval_metrics):
    assert eval_metrics["attack_acr_err_bp"] < 300.0
    assert eval_metrics["attack_vwap_err_bp"] > 4000.0
    assert eval_metrics["resistance_ratio"] >= 20.0
