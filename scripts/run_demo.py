#!/usr/bin/env python
"""Live demo — ATTACK THE INDEX. The five-step script from the blueprint.

    uv run python scripts/run_demo.py

    1) baseline: ACR printing hourly
    2) unleash the wash-flow bot
    3) naive VWAP: swings wildly
    4) ACR: barely moves
    5) attack-cost counter burns USDC live — "try to move my number; here's the bill"
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "redteam"))

from wash_attack import run_attack  # noqa: E402


def bar(pct: float, width: int = 30) -> str:
    filled = min(width, int(abs(pct) / 100 * width))
    return ("#" * filled).ljust(width)


def main() -> None:
    budget = float(sys.argv[1]) if len(sys.argv) > 1 else 8000.0
    print("\n  ══════════════════════════════════════════════════════════════")
    print("   ACR — LIVE DEMO · ATTACK THE INDEX")
    print("  ══════════════════════════════════════════════════════════════\n")

    print("  [1] Baseline — ACR printing hourly on clean flow …")
    print(f"  [2] Unleashing wash-flow bot (budget ${budget:,.0f}) …\n")
    report = run_attack(budget_usdc=budget)
    print(f"      → {report.n_adversarial:,} adversarial authorizations injected")
    print(f"      → attacker burned ${report.usdc_burned:,.2f} in USDC fees\n")

    print("  [3][4] naive VWAP vs [5] ACR — how far each was moved:\n")
    print(f"  {'INDEX':<9} {'true':>9} {'VWAP move':>11}  {'ACR move':>10}  resist")
    print("  " + "-" * 62)
    for r in report.results:
        print(
            f"  {r.index_id:<9} {r.true_level:>9.5f} "
            f"{r.vwap_swing_pct:>+9.1f}% |{bar(r.vwap_swing_pct)[:12]}"
        )
        print(
            f"  {'':9} {'':>9} {r.acr_swing_pct:>+9.2f}% |{bar(r.acr_swing_pct)[:12]}"
            f"  {r.resistance_ratio:>5.0f}x"
        )
    print()
    worst = max(report.results, key=lambda r: abs(r.vwap_swing_pct))
    print("  ── THE BILL ─────────────────────────────────────────────────")
    print(f"   VWAP was dragged {worst.vwap_swing_pct:+.1f}% on {worst.index_id}.")
    print(f"   ACR moved {worst.acr_swing_pct:+.2f}% — "
          f"{worst.resistance_ratio:.0f}× more resistant.")
    print(f"   Attacker spent ${report.usdc_burned:,.2f} and moved the rate essentially nothing.")
    print('   "Try to move my number — here\'s the bill."')
    print("  ══════════════════════════════════════════════════════════════\n")


if __name__ == "__main__":
    main()
