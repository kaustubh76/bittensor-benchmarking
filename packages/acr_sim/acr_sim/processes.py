"""Latent price processes — the estimand the whole pipeline tries to recover.

The true price of a machine service is modeled as a mean-reverting
(Ornstein–Uhlenbeck) process in log-space, optionally punctuated by regime
shifts. This is ``p*_t``: never directly observed, only glimpsed through the
noisy, batched, adversarial tape.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np


@dataclass
class OUParams:
    """Ornstein–Uhlenbeck parameters in log-price space."""

    #: Long-run mean (log level); defaults to log(reference_level) at build time.
    mu: float
    #: Mean-reversion speed (per unit time). Larger -> tighter to mu.
    theta: float = 0.15
    #: Instantaneous volatility of log-price.
    sigma: float = 0.04
    #: Probability per step of a regime shift in mu.
    regime_prob: float = 0.0
    #: Magnitude (std) of a regime shift applied to mu.
    regime_size: float = 0.10


@dataclass
class LatentPath:
    """A realized latent price path, sampled on a regular grid."""

    ts: np.ndarray
    log_price: np.ndarray
    #: The mu track (moves on regime shifts) for diagnostics.
    mu_track: np.ndarray = field(default_factory=lambda: np.empty(0))

    @property
    def price(self) -> np.ndarray:
        return np.exp(self.log_price)

    def price_at(self, t: float) -> float:
        """Interpolated *instantaneous* latent price (includes OU noise)."""
        return float(np.exp(np.interp(t, self.ts, self.log_price)))

    def level_at(self, t: float) -> float:
        """Interpolated *persistent* price level (the mean-reversion target μ).

        This is the price the estimator actually targets: a window of trades
        averages out the transient OU noise around μ, so a well-behaved estimator
        recovers ``exp(μ)`` at the window. Evaluation scores against this.
        """
        if self.mu_track.size == 0:  # pragma: no cover - defensive
            return self.price_at(t)
        return float(np.exp(np.interp(t, self.ts, self.mu_track)))


def simulate_ou(
    params: OUParams,
    horizon: float,
    dt: float,
    rng: np.random.Generator,
) -> LatentPath:
    """Exact-discretization OU simulation on ``[0, horizon]`` with step ``dt``.

    Uses the exact transition (no Euler bias):
        X_{t+dt} = mu + (X_t - mu) e^{-theta dt} + N(0, s^2),
        s^2 = sigma^2 (1 - e^{-2 theta dt}) / (2 theta).
    """
    n = int(np.floor(horizon / dt)) + 1
    ts = np.arange(n) * dt
    x = np.empty(n)
    mu_track = np.empty(n)
    x[0] = params.mu
    mu = params.mu
    mu_track[0] = mu

    decay = np.exp(-params.theta * dt)
    if params.theta > 0:
        var = params.sigma**2 * (1.0 - np.exp(-2.0 * params.theta * dt)) / (2.0 * params.theta)
    else:  # pragma: no cover - pure random walk fallback
        var = params.sigma**2 * dt
    std = np.sqrt(var)

    for i in range(1, n):
        if params.regime_prob > 0 and rng.random() < params.regime_prob:
            mu += rng.normal(0.0, params.regime_size)
        mu_track[i] = mu
        x[i] = mu + (x[i - 1] - mu) * decay + rng.normal(0.0, std)

    return LatentPath(ts=ts, log_price=x, mu_track=mu_track)
