"""Red-team harness — attack our own index and bill the attacker.

The same wash-flow bot that threatens the index in production powers the live
demo. This module runs the paired scenario (clean vs attacked) on identical
honest flow and reports, for each index:

  * how far naive VWAP was dragged,
  * how far ACR moved (barely), and
  * the USDC the attacker burned to achieve it — "here's the bill".

It is intentionally deterministic (seeded) so the demo replays identically.
"""

from __future__ import annotations

from dataclasses import dataclass

from acr_core import ALL_INDEX_IDS, get_settings, spec_for
from acr_estimator import estimate_index
from acr_sim import AttackConfig, SimConfig, simulate


@dataclass
class IndexAttackResult:
    index_id: str
    true_level: float
    acr_clean: float
    acr_attacked: float
    vwap_clean: float
    vwap_attacked: float
    attack_cost_per_bp: float
    n_adversarial: int

    @property
    def vwap_swing_pct(self) -> float:
        return 100.0 * (self.vwap_attacked - self.vwap_clean) / self.vwap_clean

    @property
    def acr_swing_pct(self) -> float:
        return 100.0 * (self.acr_attacked - self.acr_clean) / self.acr_clean

    @property
    def resistance_ratio(self) -> float:
        """How many times more an attack moves VWAP than ACR."""
        a = abs(self.acr_swing_pct)
        return abs(self.vwap_swing_pct) / a if a > 1e-9 else float("inf")


@dataclass
class AttackReport:
    results: list[IndexAttackResult]
    usdc_burned: float
    n_adversarial: int
    budget: float
    seed: int

    def by_id(self, index_id: str) -> IndexAttackResult:
        return next(r for r in self.results if r.index_id == index_id)


def run_attack(
    seed: int = 11,
    horizon: float = 3600.0,
    events_per_service: int = 3000,
    budget_usdc: float = 8000.0,
    target_multiplier: float = 2.5,
) -> AttackReport:
    settings = get_settings()
    base_cfg = SimConfig(seed=seed, horizon=horizon, events_per_service=events_per_service)
    clean = simulate(base_cfg)

    attack = AttackConfig(
        budget_usdc=budget_usdc, target_multiplier=target_multiplier, trade_notional=40.0
    )
    atk_cfg = SimConfig(
        seed=seed, horizon=horizon, events_per_service=events_per_service, attack=attack
    )
    attacked = simulate(atk_cfg)

    results: list[IndexAttackResult] = []
    for iid in ALL_INDEX_IDS:
        svc = spec_for(iid).service
        if not any(e.service == svc for e in clean.events):
            continue
        pc, dc = estimate_index(iid, clean.events, clean.attestations, settings=settings)
        pa, da = estimate_index(iid, attacked.events, attacked.attestations, settings=settings)
        results.append(
            IndexAttackResult(
                index_id=iid,
                true_level=clean.true_window_level(iid),
                acr_clean=pc.value,
                acr_attacked=pa.value,
                vwap_clean=dc.naive_vwap,
                vwap_attacked=da.naive_vwap,
                attack_cost_per_bp=pa.attack_cost_per_bp or 0.0,
                n_adversarial=attacked.n_adversarial,
            )
        )

    return AttackReport(
        results=results,
        usdc_burned=attacked.usdc_attacked,
        n_adversarial=attacked.n_adversarial,
        budget=budget_usdc,
        seed=seed,
    )
