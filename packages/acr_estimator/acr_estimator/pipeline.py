"""The estimator pipeline — one estimand, four pillars, one print.

Wires the stages in the blueprint's order (③ deconvolve, ④ clean, ⑤ robust
estimate, ⑥ hedonic, ⑦ print + bound) into a single ``ACRPrint`` per index:

    events ─► clean ─► hedonic-adjust ─► robust median (+CI)
                                     └─► volume-time bars ─► Kalman deconvolution
                                     └─► manipulation bound (Pillar 3)

Also exposes ``naive_vwap`` — the contaminated volume-weighted mean — because the
whole demo is ACR (this pipeline) standing still while naive VWAP is dragged
around by wash flow.
"""

from __future__ import annotations

import math
import warnings
from dataclasses import dataclass

import numpy as np
from acr_core import (
    ACRPrint,
    SellerAttestation,
    TapeEvent,
    get_settings,
    spec_for,
)
from scipy.stats import norm

from .bound import ManipulationBound, manipulation_bound
from .cleaning import CleaningResult, DegenerateGraphError, clean
from .hedonic import HedonicModel, adjust_prices
from .human_caps import human_bound
from .observation_model import ObservationModel, SmoothResult
from .robust import RobustEstimate, estimate
from .robustness import RobustnessDiagnostics, compute_robustness


@dataclass
class PrintDiagnostics:
    cleaning: CleaningResult
    hedonic: HedonicModel
    robust: RobustEstimate
    bound: ManipulationBound
    naive_vwap: float
    smoothed: SmoothResult | None
    n_raw: int
    n_clean: int
    #: Deconvolution's shrunk end-of-window log tilt and its 1σ.
    tilt: float = 0.0
    tilt_std: float = 0.0
    robustness: RobustnessDiagnostics | None = None


def naive_vwap(events: list[TapeEvent]) -> float:
    """Contaminated volume-weighted average price — the strawman baseline."""
    if not events:
        return float("nan")
    p = np.array([e.price for e in events])
    w = np.array([e.notional for e in events])
    return float(np.sum(p * w) / np.sum(w))


def _bar_series(
    events: list[TapeEvent],
    weights: np.ndarray,
    adjusted: np.ndarray,
    bar_volume: float,
    alpha: float,
) -> tuple[np.ndarray, float]:
    """Per-bar log-price observations from *cleaned* flow, for the deconvolution.

    Bars are built over the cleaned (post-cap) weights only, so zero-weight wash
    can no longer shift bar boundaries. The bar size adapts to the surviving
    volume so the smoother always sees enough observations (≥12) even when caps
    crush the cleaned weight to a few percent of raw.
    """
    from acr_core import trimmed_weighted_median, volume_time_bars

    kept = np.where(weights > 0)[0]
    if kept.size == 0:
        return np.empty(0), 1.0
    # kind="stable": ts ties are common once a tape rounds to whole seconds, and
    # the default quicksort permutes the tied block, changing which bar each
    # event lands in. `kept` already arrives in the canonical (ts, event_id)
    # order estimate_index established, so a stable sort preserves it exactly.
    order = kept[np.argsort([events[i].ts for i in kept], kind="stable")]
    ts = np.array([events[i].ts for i in order])
    w = weights[order]
    pr = adjusted[order]

    total = float(w.sum())
    n_bars = int(np.clip(total / max(bar_volume, 1e-9), 12, 96))
    bar_id = volume_time_bars(ts, w, total / n_bars)
    bar_id = np.minimum(bar_id, n_bars - 1)  # fold the boundary singleton in

    obs: list[float] = []
    durations: list[float] = []
    for b in np.unique(bar_id):
        m = bar_id == b
        if w[m].sum() <= 0:  # pragma: no cover - kept weights are all > 0
            continue
        obs.append(float(np.log(trimmed_weighted_median(pr[m], w[m], alpha))))
        tb = ts[m]
        durations.append(float(tb.max() - tb.min()) + 1e-9)
    avg_dur = float(np.mean(durations)) if durations else 1.0
    return np.array(obs), avg_dur


def estimate_index(
    index_id: str,
    events: list[TapeEvent],
    attestations: list[SellerAttestation],
    ts: float | None = None,
    settings=None,
    seed: int | None = None,
    single_venue: bool = False,
) -> tuple[ACRPrint, PrintDiagnostics]:
    """Produce one ACR print for ``index_id`` from a window of events.

    ``seed`` overrides ``settings.estimator_seed`` for the two stages that draw
    randomly — the Louvain partition and the bootstrap CI. It is threaded rather
    than defaulted per-function so the value that produced a print is the value
    the policy hash records, and a caller sweeping seeds cannot silently leave
    one stage behind on 0.

    ``single_venue`` declares that this window comes from one venue, where every
    sale is one aggregate buyer paying many sellers. That graph is a star, and a
    star has one Louvain community, which breaks both the sybil test and the
    cluster cap — see ``cleaning.clean`` for the two measured failure modes. On
    such a window this flag disarms those two stages and publishes
    ``attack_cost_per_bp=None``: a price is recoverable from single-venue flow,
    an attack cost is not, because an attack cost is a claim about how much
    counterparty diversity an adversary would have to manufacture and there is
    no diversity here to reason about. Left False, a fully-zeroed window raises
    ``DegenerateGraphError`` rather than a confusing error from ``robust``.
    """
    settings = settings or get_settings()
    seed = settings.estimator_seed if seed is None else seed
    spec = spec_for(index_id)
    svc = spec.service
    # Canonical order, before anything reads the list.
    #
    # Every downstream stage is order-sensitive in a way no seed fixes: the
    # funding graph is built by iterating events, so `list(G.nodes)` — which is
    # what Louvain shuffles and then visits greedily — is the arrival order; the
    # community edge-weight sums accumulate in that order and feed a threshold
    # comparison; the cluster-cap sums do the same; and the bootstrap's `p`
    # vector is positional, so a permutation redraws the same indices onto
    # different prices.
    #
    # Measured before this line existed, on the frozen scenario, forward vs
    # reversed: ACR-DATA moved 2.3 bp on value, 19.8 bp on ci_hi, and 37% on
    # attack_cost_per_bp — the last because an ULP in `raw_total` crosses a
    # `math.ceil` in the cluster count and moves the bound a whole cluster.
    # A published rate that depends on the order settlements happened to arrive
    # in is not a published rate.
    #
    # Sorted by (ts, event_id): ts alone ties constantly (the simulator draws
    # timestamps from a continuous distribution but the tape sources round), and
    # event_id is unique by construction, so this is a total order.
    window = sorted((e for e in events if e.service == svc), key=lambda e: (e.ts, e.event_id))
    if not window:
        raise ValueError(f"no events for {index_id}")
    # `or` would treat a settled_ts of 0.0 — a legitimate timestamp, and the
    # first second of any tape-relative clock — as missing.
    print_ts = ts if ts is not None else max(
        (e.settled_ts if e.settled_ts is not None else e.ts) for e in window
    )

    # ④ Clean.
    cr = clean(
        window,
        cluster_cap=settings.cluster_volume_cap,
        seed=seed,
        single_venue=single_venue,
    )
    kept_idx = np.where(cr.weights > 0)[0]
    kept_events = [window[i] for i in kept_idx]
    kept_weights = cr.weights[kept_idx]

    # A window that cleaning emptied because its funding graph was one community
    # is a degenerate graph, not a manipulated tape. Name it here rather than
    # letting `robust.estimate` report "cannot estimate from zero observations",
    # which sends the reader two modules away from the cause.
    if kept_idx.size == 0 and len(cr.communities) <= 1 and window:
        raise DegenerateGraphError(
            f"{index_id}: the funding graph for this window is a single community of "
            f"{len(cr.communities[0]) if cr.communities else 0} nodes, so every event was "
            f"flagged sybil and the window is empty. This is what single-venue flow looks "
            f"like (one aggregate buyer, many sellers). Pass single_venue=True to publish a "
            f"price without an attack cost, or widen the tape to more than one counterparty."
        )

    # ⑥ Hedonic: adjust surviving prices to constant quality.
    adjusted_all, hmodel = adjust_prices(window, attestations)
    adjusted = adjusted_all[kept_idx]

    # ⑤ Robust estimate on cleaned, quality-adjusted prices.
    rob = estimate(
        adjusted,
        kept_weights,
        alpha=settings.trim_alpha,
        n_bootstrap=settings.ci_bootstrap,
        ci_level=settings.ci_level,
        seed=seed,
    )
    value = rob.value

    # ③ Deconvolve: Kalman-smooth the per-bar series. The robust median sets the
    # level and the bootstrap CI; the smoother contributes a *shrunk* end-of-
    # window displacement and its variance flows into the interval — no silent
    # widening.
    smoothed: SmoothResult | None = None
    tilt = 0.0
    sig_t = 0.0
    ci_lo, ci_hi = rob.ci_lo, rob.ci_hi
    obs, avg_bar_dur = _bar_series(
        window, cr.weights, adjusted_all, settings.bar_volume_usdc, settings.trim_alpha
    )
    if obs.size >= 4:
        k = max(1, int(round(settings.batch_interval_seconds / avg_bar_dur)))
        om = ObservationModel(batch_width=min(k, obs.size), process_var=1e-3, obs_var=2e-2)
        smoothed = om.smooth(obs)
        latent = smoothed.latent
        denom = float(np.var(latent) + smoothed.variance[-1])
        # Shrink the raw displacement toward 0 when the latent barely moved
        # relative to the smoother's posterior uncertainty.
        lam = float(np.var(latent) / denom) if denom > 0 else 0.0
        tilt = float(np.clip(lam * (smoothed.last - float(np.mean(latent))), -0.05, 0.05))
        sig_t = lam * smoothed.last_std
        z = float(norm.ppf(0.5 + settings.ci_level / 2.0))
        value = float(rob.value * np.exp(tilt))
        ci_lo = float(rob.ci_lo * np.exp(tilt - z * sig_t))
        ci_hi = float(rob.ci_hi * np.exp(tilt + z * sig_t))

    value = max(value, 1e-9)
    # value ∈ [ci_lo, ci_hi] holds by construction (a monotone shift preserves the
    # rob.ci_lo ≤ rob.value ≤ rob.ci_hi ordering); clamp only guards float error.
    ci_lo = min(ci_lo, value)
    ci_hi = max(ci_hi, value)

    # ⑦ Manipulation bound — cap-aware, on the defended distribution.
    # fsum, not sum: this feeds `math.ceil` on the cluster count in bound.py,
    # so a last-bit difference in the total is not a last-bit difference in the
    # answer — it is one whole sybil cluster, and a visible step in cost_per_bp.
    raw_total = math.fsum(e.notional for e in window)
    mb = manipulation_bound(
        adjusted,
        kept_weights,
        alpha=settings.trim_alpha,
        fee_bps=settings.usdc_fee_bps,
        fee_flat=settings.usdc_fee_flat,
        cluster_cap=settings.cluster_volume_cap,
        raw_total=raw_total,
    )

    # ⑦b The same bound, denominated in PEOPLE rather than wallets.
    #
    # Only meaningful once some flow is resolved to verified humans, and it is
    # published as a LOWER bound because C_human rests on a floor in
    # anchors/_basket/C-HUMAN.json rather than a citable rate. None — never 0,
    # never the wallet bound — when it cannot be computed: ACROracleV2 reads 0
    # as "not computed", and defaulting to the wallet bound would claim
    # identities are as cheap to buy as wallets, which is false.
    #
    # Skipped outright on a degenerate graph: the wallet bound it rests on is
    # not a measurement there (see below), and scaling a non-measurement by a
    # cost-per-human would dress it up as one.
    if cr.degenerate_graph:
        hb = None
    else:
        try:
            hb = human_bound(
                adjusted,
                kept_weights,
                alpha=settings.trim_alpha,
                fee_bps=settings.usdc_fee_bps,
                fee_flat=settings.usdc_fee_flat,
                raw_total=raw_total,
                wallet_cost_per_bp=mb.cost_per_bp,
            )
        except (FileNotFoundError, ValueError) as exc:  # pragma: no cover - config
            warnings.warn(f"human bound unavailable ({exc}) — publishing without it",
                          RuntimeWarning, stacklevel=2)
            hb = None

    # Per-print robustness diagnostics (methodology §4, measured).
    node_comm = {n: i for i, c in enumerate(cr.communities) for n in c}
    comm_ids = [node_comm.get(e.seller, -1) for e in kept_events]
    robustness = compute_robustness(
        adjusted, kept_weights, comm_ids,
        alpha=settings.trim_alpha, cluster_cap=settings.cluster_volume_cap,
        sybil_clusters_required=mb.sybil_clusters_required, min_identities=mb.min_identities,
    )

    print_obj = ACRPrint(
        index_id=index_id,
        ts=print_ts,
        value=value,
        ci_lo=ci_lo,
        ci_hi=ci_hi,
        # None, never 0, on a degenerate graph: `types.ACRPrint` documents None
        # as "not computed" and ACROracleV2 reads 0 the same way, whereas
        # publishing the cap artefact would claim a defence that was never
        # measured. A price survives single-venue flow; an attack cost does not.
        attack_cost_per_bp=None if cr.degenerate_graph else mb.cost_per_bp,
        human_adjusted_bound=None if hb is None else hb.cost_usdc,
        humans_required=None if hb is None else hb.humans_required,
        n_obs=len(kept_events),
        trim_alpha=settings.trim_alpha,
    )
    diag = PrintDiagnostics(
        cleaning=cr,
        hedonic=hmodel,
        robust=rob,
        bound=mb,
        naive_vwap=naive_vwap(window),
        smoothed=smoothed,
        n_raw=len(window),
        n_clean=len(kept_events),
        tilt=tilt,
        tilt_std=sig_t,
        robustness=robustness,
    )
    return print_obj, diag


def estimate_all(
    events: list[TapeEvent],
    attestations: list[SellerAttestation],
    index_ids: tuple[str, ...] | None = None,
    ts: float | None = None,
    settings=None,
) -> dict[str, tuple[ACRPrint, PrintDiagnostics]]:
    from acr_core import ALL_INDEX_IDS

    ids = index_ids or ALL_INDEX_IDS
    out: dict[str, tuple[ACRPrint, PrintDiagnostics]] = {}
    for iid in ids:
        try:
            out[iid] = estimate_index(iid, events, attestations, ts=ts, settings=settings)
        except ValueError:
            continue
    return out
