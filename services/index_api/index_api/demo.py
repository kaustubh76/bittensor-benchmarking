"""Interactive "Attack the Index" run for the Terminal's Attack Lab.

``POST /demo/attack/start`` claims the single run slot and kicks a worker
thread; ``GET /demo/attack/status`` returns the state with the error series
accumulated so far, so the UI watches the attack unfold hour by hour.

The simulation itself is synchronous and fast: it runs once up front, then the
loop *replays* it one simulated hour at a time — computing each hour's ACR
estimate, naive VWAP, and true level exactly as ``attack.py``'s cached exhibit
does — pacing each step to ~2s of wall time so the exercise unfolds live. The
verdict is derived from the same series the viewer watched (honest by
construction). The cached ``attack_snapshot`` exhibit is untouched.
"""

from __future__ import annotations

import bisect
import threading
import time
from dataclasses import dataclass, field

from acr_core import get_settings, spec_for
from acr_estimator import estimate_index, naive_vwap
from acr_sim import AttackConfig, SimConfig, simulate

HOUR = 3600.0
HOURS_TOTAL = 12
ATK_FROM = 4
ATK_TO = 8
PACE_S = 2.0  # minimum wall-clock seconds per simulated hour
# Watchdog: a run with no progress for this long is DISPLAYED as errored. The
# window must cover the upfront simulate() + the hour-0 estimator bootstrap,
# which a throttled 0.5-CPU cloud box can stretch well past the old 30s.
STEP_TIMEOUT_S = 90.0


def events_per_service(settings) -> int:
    """Total sim events per service for a live run — same scaling as the cached
    exhibit (attack.py): 4/5 of the configured per-hour budget × 12 hours, so
    ACR_ATTACK_SIM_EVENTS_PER_SERVICE bounds BOTH paths (a 512MB box sets ~400
    to avoid the OOM spike the config documents)."""
    per_hour = settings.attack_sim_events_per_service * 4 // 5
    return HOURS_TOTAL * per_hour


@dataclass
class AttackRun:
    state: str = "idle"  # idle | running | done | error
    params: dict | None = None
    hour: int = 0
    series: list[dict] = field(default_factory=list)
    usdc_burned: float = 0.0
    n_adversarial: int = 0
    verdict: dict | None = None
    error: str | None = None
    stepped_at: float = 0.0  # monotonic time of last progress (watchdog)
    #: Which half of the run we are in. The tape is built by ONE blocking
    #: simulate() before the estimator loop starts, during which `hour` is
    #: pinned at 0 — so a viewer on a slow box watched a dead page for tens of
    #: seconds with no way to tell it from a hang. STEP_TIMEOUT_S is 90s
    #: precisely because that phase is slow; naming it is the cheapest honesty
    #: on the page.
    phase: str = "idle"  # idle | simulating | estimating | done
    started_at: float = 0.0  # wall clock, for "this run began N ago"
    #: Known BEFORE the loop, so every counter can carry a denominator instead
    #: of climbing toward a number the viewer cannot see.
    n_adversarial_total: int = 0
    usdc_total: float = 0.0
    #: Why the budget knob does not move the outcome. `generate_wash_flow`
    #: takes min(n_wash_trades, budget/per_trade_fee) — and at a 0.0041 fee
    #: every budget in the accepted range affords millions of trades, so the
    #: 12,000 cap always binds. Reporting both makes an inert control legible
    #: instead of suspicious.
    trade_cap: int = 0
    budget_affords: int = 0


_lock = threading.Lock()
_run = AttackRun()
# True from try_start until the worker actually exits. The watchdog may flip
# the DISPLAYED state to error, but the slot stays claimed while the thread is
# alive — a second start during the false-error window would otherwise run two
# concurrent full sims (an OOM on a 512MB box) interleaving into one series.
_busy = False


def status() -> dict:
    """Copy-on-read snapshot of the current run (never hands out live refs)."""
    with _lock:
        if _run.state == "running" and time.monotonic() - _run.stepped_at > STEP_TIMEOUT_S:
            _run.state = "error"
            _run.error = "attack run stalled"
        return {
            "state": _run.state,
            "params": dict(_run.params) if _run.params else None,
            "hour": _run.hour,
            "hours_total": HOURS_TOTAL,
            "series": [dict(p) for p in _run.series],
            "usdc_burned": _run.usdc_burned,
            "n_adversarial": _run.n_adversarial,
            "verdict": dict(_run.verdict) if _run.verdict else None,
            "error": _run.error,
            # --- added so the page can show its work rather than assert it ---
            "phase": _run.phase,
            "started_at": _run.started_at or None,
            "elapsed_s": (
                round(time.time() - _run.started_at, 1) if _run.started_at else None
            ),
            "n_adversarial_total": _run.n_adversarial_total or None,
            "usdc_total": _run.usdc_total or None,
            "trade_cap": _run.trade_cap or None,
            "budget_affords": _run.budget_affords or None,
            "pace_s": PACE_S,
        }


def try_start(budget_usdc: float, target_multiplier: float, seed: int) -> bool:
    """Claim the single run slot; False while a worker is in flight (even if
    the watchdog has already flipped the displayed state to error)."""
    global _run, _busy
    with _lock:
        if _busy or _run.state == "running":
            return False
        _busy = True
        _run = AttackRun(
            state="running",
            params={
                "budget_usdc": budget_usdc,
                "target_multiplier": target_multiplier,
                "seed": seed,
            },
            stepped_at=time.monotonic(),
            # Wall clock, not monotonic: `stepped_at` answers the watchdog's
            # question ("is it stuck?"), this one answers the reader's ("when
            # did this run happen?"). They are different clocks on purpose.
            started_at=time.time(),
            phase="simulating",
        )
        return True


def execute(budget_usdc: float, target_multiplier: float, seed: int) -> None:
    """Entry point for the worker thread — surfaces failures as state, never
    raises, and ALWAYS releases the run slot on exit."""
    global _busy
    try:
        _execute(budget_usdc, target_multiplier, seed)
    except Exception as exc:  # pragma: no cover - defensive
        with _lock:
            _run.state = "error"
            _run.error = str(exc)
    finally:
        with _lock:
            _busy = False


def _execute(budget_usdc: float, target_multiplier: float, seed: int) -> None:
    settings = get_settings()
    svc = spec_for("ACR-INF").service
    # Announce the blocking phase BEFORE entering it. Everything below this
    # line up to the loop is one un-paced simulate() that can run for tens of
    # seconds on a 0.5-CPU box, with `hour` stuck at 0 the whole time.
    with _lock:
        _run.phase = "simulating"
        _run.stepped_at = time.monotonic()
    res = simulate(
        SimConfig(
            seed=seed,
            horizon=HOURS_TOTAL * HOUR,
            events_per_service=events_per_service(settings),
            attack=AttackConfig(
                budget_usdc=budget_usdc,
                target_multiplier=target_multiplier,
                trade_notional=40.0,
                t_start=ATK_FROM * HOUR,
                t_end=ATK_TO * HOUR,
            ),
        )
    )
    events = [e for e in res.events if e.service == svc]
    adv_ts = sorted(e.ts for e in res.events if e.is_adversarial)
    n_adv = len(adv_ts)

    # The totals are known here, before a single hour is estimated. Publishing
    # them turns every counter from a number climbing toward nothing into a
    # fraction of a stated whole.
    from acr_sim.adversary import AttackConfig as _AC

    fee = settings.usdc_fee_flat + settings.usdc_fee_bps * 1e-4 * 40.0
    with _lock:
        _run.phase = "estimating"
        _run.n_adversarial_total = n_adv
        _run.usdc_total = res.usdc_attacked
        _run.trade_cap = _AC.n_wash_trades
        _run.budget_affords = int(budget_usdc / fee) if fee > 0 else 0
        _run.stepped_at = time.monotonic()

    for h in range(HOURS_TOTAL):
        step_started = time.monotonic()
        t0, t1 = h * HOUR, (h + 1) * HOUR
        window = [e for e in events if t0 <= e.ts < t1]
        point = None
        if len(window) >= 50:
            true = res.true_window_level("ACR-INF", window)
            # `d` used to be discarded. It is the entire diagnostics object —
            # the record of what the estimator actually DID this hour — and it
            # is the difference between a page that asserts the index resists
            # manipulation and one that shows the cleaner removing 95% of a
            # poisoned hour. Same field names as scripts/eval.py so the live
            # run and the CI eval cannot drift apart.
            p, d = estimate_index("ACR-INF", window, res.attestations, ts=t1, settings=settings)
            vwap = naive_vwap(window)
            point = {
                "hour": h,
                "true": true,
                "acr": p.value,
                "vwap": vwap,
                "acr_err_bp": 1e4 * abs(p.value - true) / true,
                "vwap_err_bp": 1e4 * abs(vwap - true) / true,
                "attack": ATK_FROM <= h < ATK_TO,
                "acr_ci_lo": p.ci_lo,
                "acr_ci_hi": p.ci_hi,
                "attack_cost_per_bp": p.attack_cost_per_bp,
                "n_raw": len(window),
                "n_obs": p.n_obs,
                "cleaned_pct": 100.0 * d.cleaning.removed_fraction,
                "sybil_clusters": len(d.cleaning.sybil_communities),
                "step_ms": None,  # filled below, once the step is actually done
            }
        # Burn is prorated by adversarial prints landed so far — the counter
        # the viewer watches climb during the attack window.
        seen = bisect.bisect_right(adv_ts, t1)
        burned = res.usdc_attacked * (seen / n_adv) if n_adv else 0.0

        with _lock:
            if point is not None:
                # The real cost of the estimator for this hour — cleaning,
                # Louvain, the 500-sample bootstrap and the Kalman pass — as
                # opposed to the PACE_S the loop then sleeps off.
                point["step_ms"] = round(1000 * (time.monotonic() - step_started), 1)
                _run.series.append(point)
            _run.hour = h + 1
            _run.usdc_burned = burned
            _run.n_adversarial = seen
            # The verdict is a pure function of the series, so there is no
            # reason to withhold it until the end. Ticking it live lets a
            # reader watch resistance form instead of being handed a result.
            _run.verdict = _verdict(_run.series)
            _run.stepped_at = time.monotonic()

        time.sleep(max(0.0, PACE_S - (time.monotonic() - step_started)))

    with _lock:
        _run.verdict = _verdict(_run.series)
        _run.state = "done"
        _run.phase = "done"
        _run.stepped_at = time.monotonic()


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _verdict(series: list[dict]) -> dict:
    """Swing of each statistic from its pre-attack mean to its attack-window
    mean, plus peak errors — all from the series the viewer watched."""
    pre = [p for p in series if p["hour"] < ATK_FROM]
    atk = [p for p in series if p["attack"]]
    pre_vwap, atk_vwap = _mean([p["vwap"] for p in pre]), _mean([p["vwap"] for p in atk])
    pre_acr, atk_acr = _mean([p["acr"] for p in pre]), _mean([p["acr"] for p in atk])
    vwap_swing = 100.0 * (atk_vwap - pre_vwap) / pre_vwap if pre_vwap else 0.0
    acr_swing = 100.0 * (atk_acr - pre_acr) / pre_acr if pre_acr else 0.0
    return {
        "vwap_swing_pct": vwap_swing,
        "acr_swing_pct": acr_swing,
        "resistance": abs(vwap_swing) / max(0.01, abs(acr_swing)),
        "peak_vwap_err_bp": max((p["vwap_err_bp"] for p in series), default=0.0),
        "peak_acr_err_bp": max((p["acr_err_bp"] for p in series), default=0.0),
    }
