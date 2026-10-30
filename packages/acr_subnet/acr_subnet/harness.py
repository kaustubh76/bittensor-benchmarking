"""Run the subnet in-process: N miners, M epochs, no network and no chain.

This is what makes the mechanism testable. A real subnet's epoch loop is
dominated by things that have nothing to do with whether the incentive is
correct — axon serving, dendrite timeouts, weight extrinsics, deregistration.
Those matter for deployment and tell you nothing about whether the scoring rule
pays the right miner. Here the loop is a function call, so the question the
mechanism exists to answer can be asked in a unit test.

Epoch construction is the only subtle part, and it is subtle for a reason given
in ``validator.py``: a synthetic epoch carries a latent the miners cannot see,
so accuracy is scorable; a real-tape epoch does not, so it is not.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np
from acr_core import ACRPrint, get_settings, spec_for

from .miner import Miner
from .protocol import Score
from .validator import Epoch, EpochResult, Validator


@dataclass
class Standing:
    """A miner's cumulative position across the run."""

    uid: int
    strategy: str
    epochs_scored: int = 0
    submissions: int = 0
    total_weight: float = 0.0
    mean_total: float = 0.0
    mean_accuracy: float | None = None
    mean_resistance: float | None = None
    mean_attainability: float | None = None
    coverage: float | None = None
    gate_failures: int = 0
    detail: dict[str, float] = field(default_factory=dict)


@dataclass
class RunReport:
    index_id: str
    results: list[EpochResult] = field(default_factory=list)
    standings: list[Standing] = field(default_factory=list)

    def by_strategy(self, name: str) -> Standing | None:
        for s in self.standings:
            if s.strategy == name:
                return s
        return None

    @property
    def consensus_prints(self) -> list[ACRPrint]:
        return [r.consensus for r in self.results if r.consensus is not None]


def synthetic_epochs(
    index_id: str,
    n: int,
    *,
    seed: int,
    hours: float = 1.0,
    events_per_service: int = 2_500,
) -> list[Epoch]:
    """Consecutive windows of ONE continuous latent path.

    Slicing a single simulation rather than re-simulating per epoch, and the
    distinction turns out to decide whether the mechanism works. With
    independent per-epoch draws every window starts from the same reference
    level, so the published rate barely moves between epochs — and a miner that
    simply resubmits last epoch's consensus then *beats* every real estimator,
    because the consensus is an aggregate of estimates and therefore better than
    any single one of them, while costing the copier nothing. Measured: the
    copycat took 2.06 of the weight against the four-pillar miner's 1.89.

    That was an artefact of the harness, not a property of copying. A real tape
    has one latent process that drifts, so a print from the previous window is
    stale by exactly the amount the level moved. Slicing one path restores that,
    which is the honest reason copying loses here: not that it is impossible,
    but that it is a window late.

    The base seed is the validator's secret. ``scripts/gen_evalset.py`` records
    that seed 21 is the *favourable* published one (46x resistance there, 24x on
    a seed nobody chose), which is exactly why a validator must not score
    against a seed its miners can read.
    """
    from acr_sim import SimConfig, simulate

    spec = spec_for(index_id)
    step = 3600.0 * hours
    cfg = SimConfig(
        seed=seed,
        horizon=step * n,
        # Per-service event count is for the whole path, so scale it by the
        # number of windows to keep each window's depth comparable to a
        # single-window run.
        events_per_service=max(1, events_per_service * n),
    )
    res = simulate(cfg)
    events = res.events_for(index_id)
    if not events:
        return []

    out: list[Epoch] = []
    for k in range(n):
        t0, t1 = k * step, (k + 1) * step
        window = [e for e in events if t0 <= e.ts < t1]
        if len(window) < 50:  # too thin to estimate -- eval.py uses the same floor
            continue
        try:
            truth = res.true_window_level(index_id, window)
        except ValueError:
            truth = None
        out.append(
            Epoch(
                index_id=index_id,
                number=k,
                events=window,
                attestations=res.attestations,
                truth=truth,
                ts=t1,
                single_venue=False,
                label=f"synthetic(seed={cfg.seed}, one path sliced into {n}, {spec.unit})",
            )
        )
    return out


def real_tape_epochs(index_id: str, source, *, step_s: float | None = None) -> list[Epoch]:
    """Epochs from a live tape source, one per native aggregation period.

    No ``truth``: see ``validator.py``. ``single_venue`` follows the source's own
    declaration rather than a caller's guess, so a source that knows its funding
    graph is a star says so and the estimator is configured accordingly.
    """
    events = source.collect()
    if not events:
        return []
    attest = source.attestations()
    step = step_s or getattr(source, "native_step_s", 3600.0)
    single = bool(getattr(source, "requires_single_venue", False))

    svc = spec_for(index_id).service
    window = [e for e in events if e.service is svc]
    if not window:
        return []

    lo = min(e.ts for e in window)
    hi = max(e.ts for e in window)
    out: list[Epoch] = []
    k = 0
    t = lo - (lo % step)
    while t <= hi:
        chunk = [e for e in window if t <= e.ts < t + step]
        if chunk:
            out.append(
                Epoch(
                    index_id=index_id,
                    number=k,
                    events=chunk,
                    attestations=attest,
                    truth=None,
                    ts=t + step,
                    single_venue=single,
                    label=f"real-tape({type(source).__name__})",
                )
            )
            k += 1
        t += step
    return out


def _mean(vals: Sequence[float | None]) -> float | None:
    xs = [v for v in vals if v is not None]
    return float(np.mean(xs)) if xs else None


def run(
    index_id: str,
    miners: Sequence[Miner],
    epochs: Sequence[Epoch],
    validator: Validator | None = None,
) -> RunReport:
    """Run every epoch and accumulate standings."""
    v = validator or Validator()
    rep = RunReport(index_id=index_id)
    previous: ACRPrint | None = None

    for ep in epochs:
        res = v.run_epoch(ep, miners, previous_consensus=previous)
        rep.results.append(res)
        if res.consensus is not None:
            previous = res.consensus

    per_uid: dict[int, list[Score]] = {}
    for res in rep.results:
        for sc in res.scores:
            per_uid.setdefault(sc.miner_uid, []).append(sc)

    settings = get_settings()
    for m in miners:
        scs = per_uid.get(m.uid, [])
        cov_hist = v.coverage.get(m.uid, [])
        st = Standing(
            uid=m.uid,
            strategy=m.name,
            epochs_scored=len(scs),
            submissions=len(scs),
            total_weight=float(
                sum(r.weights.get(m.uid, 0.0) for r in rep.results)
            ),
            mean_total=float(np.mean([s.total for s in scs])) if scs else 0.0,
            mean_accuracy=_mean([s.accuracy for s in scs]),
            mean_resistance=_mean([s.resistance for s in scs]),
            mean_attainability=_mean([s.attainability for s in scs]),
            coverage=(sum(1 for c in cov_hist if c) / len(cov_hist)) if cov_hist else None,
            gate_failures=sum(1 for s in scs if not s.calibrated),
        )
        for key in ("err_bp", "ci_width_bp", "bp_per_usd", "declared_over_measured"):
            vals = [s.detail[key] for s in scs if key in s.detail]
            if vals:
                st.detail[key] = float(np.mean(vals))
        rep.standings.append(st)

    rep.standings.sort(key=lambda s: -s.total_weight)
    # Keep the trim alpha that produced the consensus discoverable from a report.
    rep.standings and rep.standings[0].detail.setdefault("trim_alpha", settings.trim_alpha)
    return rep
