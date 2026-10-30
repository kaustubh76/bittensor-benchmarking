"""Pillar 2 — Hedonic Adjustment: Case-Shiller methodology for compute.

Observed prices mix two things: *when* the trade happened (the market level we
want) and *what quality* was sold (frontier vs open model, tight vs loose
latency SLO — things we want to hold constant). A naive median of a window is
contaminated by shifts in the quality mix of who happened to trade.

The hedonic regression estimates the price premium attributable to each quality
feature, then re-prices every observation to a single reference quality:

    log p_adj = log p_obs  −  β·(x_obs − x_ref)

The resulting constant-quality prices feed the robust estimator, yielding a rate
that moves only when the *market* moves, not when the trade mix does. Quality
features come from the seller attestations in ``AttestationRegistry.sol`` — the
econometric flywheel: attest better metadata → get priced fairly → win flow.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from acr_core import ModelClass, SellerAttestation, TapeEvent

#: Reference quality the index is fixed at (a mid-tier, 250ms offering).
REF_MODEL_CLASS = ModelClass.MID
REF_LATENCY_MS = 250.0

_CLASS_ORDER = [ModelClass.FRONTIER, ModelClass.MID, ModelClass.SMALL, ModelClass.OPEN]


@dataclass
class HedonicModel:
    #: Coefficient on each class dummy (OPEN is the omitted baseline).
    class_coef: dict[ModelClass, float]
    #: Coefficient on centered log-latency.
    latency_coef: float
    #: Whether a well-posed fit was obtained (else adjustment is identity).
    fitted: bool
    r2: float = 0.0

    def _features(self, model_class: ModelClass, latency_ms: float) -> np.ndarray:
        dummies = [1.0 if model_class == c else 0.0 for c in _CLASS_ORDER[:-1]]
        log_lat = np.log(max(latency_ms, 1e-6)) - np.log(REF_LATENCY_MS)
        return np.array(dummies + [log_lat])

    def _beta(self) -> np.ndarray:
        return np.array(
            [self.class_coef.get(c, 0.0) for c in _CLASS_ORDER[:-1]] + [self.latency_coef]
        )

    def quality_logshift(self, model_class: ModelClass, latency_ms: float) -> float:
        """β·(x_ref − x_obs): the log-price adjustment toward reference quality."""
        if not self.fitted:
            return 0.0
        x_obs = self._features(model_class, latency_ms)
        x_ref = self._features(REF_MODEL_CLASS, REF_LATENCY_MS)
        return float(self._beta() @ (x_ref - x_obs))


def _seller_features(
    attestations: list[SellerAttestation],
) -> dict[str, tuple[ModelClass, float]]:
    out: dict[str, tuple[ModelClass, float]] = {}
    for a in attestations:
        out[a.seller] = (a.model_class, a.latency_slo_ms)
    return out


def fit_hedonic(
    events: list[TapeEvent],
    attestations: list[SellerAttestation],
) -> HedonicModel:
    """Weighted (by notional) regression of log-price on quality features."""
    feats = _seller_features(attestations)
    rows: list[np.ndarray] = []
    y: list[float] = []
    w: list[float] = []
    empty = HedonicModel(class_coef={}, latency_coef=0.0, fitted=False)
    tmpl = HedonicModel(class_coef={}, latency_coef=0.0, fitted=False)

    for e in events:
        mc, lat = feats.get(e.seller, (e.model_class, None))
        if mc is None or lat is None:
            continue
        rows.append(tmpl._features(mc, lat))
        y.append(np.log(e.price))
        w.append(e.notional)
    if len(rows) < 8:
        return empty

    X = np.column_stack([np.ones(len(rows)), np.array(rows)])
    yv = np.array(y)
    wv = np.array(w)
    try:
        import statsmodels.api as sm

        model = sm.WLS(yv, X, weights=wv).fit()
        coefs = model.params
        r2 = float(model.rsquared)
    except Exception:  # pragma: no cover - numeric fallback
        # Proper WLS via sqrt-weights: minimize Σ w·(y − Xβ)². (The prior
        # version scaled by w, which minimizes Σ w²·(·)² — the wrong problem —
        # and hardcoded r2 = 0.)
        sw = np.sqrt(wv)
        try:
            beta, *_ = np.linalg.lstsq(X * sw[:, None], yv * sw, rcond=None)
        except np.linalg.LinAlgError:
            return empty
        coefs = beta
        resid = yv - X @ beta
        ybar = float(np.average(yv, weights=wv))
        ss_res = float(np.sum(wv * resid**2))
        ss_tot = float(np.sum(wv * (yv - ybar) ** 2))
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0

    # coefs = [intercept, frontier, mid, small, latency]
    class_coef = {
        ModelClass.FRONTIER: float(coefs[1]),
        ModelClass.MID: float(coefs[2]),
        ModelClass.SMALL: float(coefs[3]),
    }
    return HedonicModel(
        class_coef=class_coef,
        latency_coef=float(coefs[4]),
        fitted=True,
        r2=r2,
    )


def adjust_prices(
    events: list[TapeEvent],
    attestations: list[SellerAttestation],
    model: HedonicModel | None = None,
) -> tuple[np.ndarray, HedonicModel]:
    """Return constant-quality prices per event (and the fitted model)."""
    if model is None:
        model = fit_hedonic(events, attestations)
    feats = _seller_features(attestations)
    adj = np.empty(len(events))
    for i, e in enumerate(events):
        mc, lat = feats.get(e.seller, (e.model_class, REF_LATENCY_MS))
        if mc is None:
            adj[i] = e.price
            continue
        shift = model.quality_logshift(mc, lat if lat is not None else REF_LATENCY_MS)
        adj[i] = e.price * np.exp(shift)
    return adj, model
