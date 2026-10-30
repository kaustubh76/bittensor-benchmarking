"""Cached attack comparison for the Terminal's 'Attack the Index' panel.

Runs one paired clean/attacked hourly-window scenario and an hourly error series
(ACR vs naive VWAP under a mid-window attack). Cached so the Terminal endpoint
is cheap to poll.
"""

from __future__ import annotations

from functools import lru_cache

from acr_core import ALL_INDEX_IDS, get_settings, spec_for
from acr_estimator import estimate_index, naive_vwap
from acr_sim import AttackConfig, SimConfig, simulate

HOUR = 3600.0


@lru_cache(maxsize=1)
def attack_snapshot(seed: int = 11, budget: float = 8000.0) -> dict:
    settings = get_settings()
    # Exhibit sim size is configurable — the default is rich; a memory-constrained
    # cloud box lowers ACR_ATTACK_SIM_EVENTS_PER_SERVICE to shrink the peak.
    n = settings.attack_sim_events_per_service
    clean = simulate(SimConfig(seed=seed, horizon=HOUR, events_per_service=n))
    atk = AttackConfig(budget_usdc=budget, target_multiplier=2.5, trade_notional=40.0)
    attacked = simulate(
        SimConfig(seed=seed, horizon=HOUR, events_per_service=n, attack=atk)
    )

    per_index = []
    for iid in ALL_INDEX_IDS:
        svc = spec_for(iid).service
        if not any(e.service == svc for e in clean.events):
            continue
        pc, dc = estimate_index(iid, clean.events, clean.attestations, settings=settings)
        pa, da = estimate_index(iid, attacked.events, attacked.attestations, settings=settings)
        per_index.append(
            {
                "index_id": iid,
                "true": clean.true_window_level(iid),
                "acr_clean": pc.value,
                "acr_attacked": pa.value,
                "vwap_clean": dc.naive_vwap,
                "vwap_attacked": da.naive_vwap,
                "vwap_swing_pct": 100 * (da.naive_vwap - dc.naive_vwap) / dc.naive_vwap,
                "acr_swing_pct": 100 * (pa.value - pc.value) / pc.value,
            }
        )

    # Hourly error series (12h, attack h4-8) for the chart.
    series = _error_series(seed=seed + 1)
    return {
        "per_index": per_index,
        "series": series,
        "usdc_burned": attacked.usdc_attacked,
        "n_adversarial": attacked.n_adversarial,
    }


def _error_series(seed: int, hours: int = 12, atk_from: int = 4, atk_to: int = 8) -> list[dict]:
    settings = get_settings()
    svc = spec_for("ACR-INF").service
    attack = AttackConfig(
        budget_usdc=6000.0,
        target_multiplier=2.5,
        trade_notional=40.0,
        t_start=atk_from * HOUR,
        t_end=atk_to * HOUR,
    )
    # Per-hour density scaled from the exhibit config (default 2500 → 2000/hr,
    # preserving the original rich series; cloud lowers it to cut the memory peak).
    per_hour = settings.attack_sim_events_per_service * 4 // 5
    res = simulate(
        SimConfig(seed=seed, horizon=hours * HOUR, events_per_service=hours * per_hour, attack=attack)
    )
    events = [e for e in res.events if e.service == svc]
    out = []
    for h in range(hours):
        t0, t1 = h * HOUR, (h + 1) * HOUR
        window = [e for e in events if t0 <= e.ts < t1]
        if len(window) < 50:
            continue
        true = res.true_window_level("ACR-INF", window)
        # Same shape the LIVE run emits (index_api.demo). The archived exhibit
        # is what a reader sees before they press anything, so a thinner
        # snapshot would make the recorded run look less accountable than a
        # fresh one — the opposite of the point.
        p, d = estimate_index("ACR-INF", window, res.attestations, ts=t1, settings=settings)
        vwap = naive_vwap(window)
        out.append(
            {
                "hour": h,
                "true": true,
                "acr": p.value,
                "vwap": vwap,
                "acr_err_bp": 1e4 * abs(p.value - true) / true,
                "vwap_err_bp": 1e4 * abs(vwap - true) / true,
                "attack": atk_from <= h < atk_to,
                "acr_ci_lo": p.ci_lo,
                "acr_ci_hi": p.ci_hi,
                "attack_cost_per_bp": p.attack_cost_per_bp,
                "n_raw": len(window),
                "n_obs": p.n_obs,
                "cleaned_pct": 100.0 * d.cleaning.removed_fraction,
                "sybil_clusters": len(d.cleaning.sybil_communities),
            }
        )
    return out
