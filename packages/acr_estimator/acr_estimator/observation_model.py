"""Pillar 1 — Observation Model: deconvolving the batching operator.

The stage no other team will know exists. The observed tape is the latent price
process *convolved* with the Gateway batching schedule plus noise:

    y_t = H · x_{t-k+1:t}  +  v_t,     H = box average of width k,
    x_t = x_{t-1} + w_t                (latent log-price, local-level).

Naively averaging ``y`` measures the batch scheduler. We instead write a linear
Gaussian state-space whose state carries the last ``k`` latent values, so the
box average is a *known* measurement map — and run a Kalman filter + RTS
smoother to invert it. The smoother output is the deconvolved latent estimate:
what the price *was*, not what the batcher reported.

Hand-rolled in numpy (no hard dependency on filterpy/pykalman) so the math is
auditable line by line — the methodology paper is the product.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class SmoothResult:
    #: Deconvolved latent series (log-space), same length as observations.
    latent: np.ndarray
    #: Posterior variance of the latent at each step.
    variance: np.ndarray

    @property
    def last(self) -> float:
        return float(self.latent[-1])

    @property
    def last_std(self) -> float:
        return float(np.sqrt(self.variance[-1]))


class ObservationModel:
    """Augmented local-level Kalman filter/smoother deconvolving a box operator.

    Parameters
    ----------
    batch_width : k, the number of latent steps a single settlement batch
        averages over (``batch_interval / bar_dt``, rounded, >= 1).
    process_var : latent innovation variance per step (how fast the true price
        can move). Larger -> smoother trusts observations more.
    obs_var : measurement noise variance on each batched observation.
    """

    def __init__(
        self,
        batch_width: int = 1,
        process_var: float = 1e-3,
        obs_var: float = 1e-2,
    ) -> None:
        self.k = max(1, int(batch_width))
        self.process_var = float(process_var)
        self.obs_var = float(obs_var)
        self._build()

    def _build(self) -> None:
        k = self.k
        F = np.zeros((k, k))
        F[0, 0] = 1.0  # random-walk latent
        for j in range(1, k):
            F[j, j - 1] = 1.0  # shift history down
        self.F = F
        Q = np.zeros((k, k))
        Q[0, 0] = self.process_var
        self.Q = Q
        self.H = np.full((1, k), 1.0 / k)
        self.R = np.array([[self.obs_var]])

    def smooth(self, observations: np.ndarray) -> SmoothResult:
        y = np.asarray(observations, dtype=float).ravel()
        n = y.size
        if n == 0:
            return SmoothResult(np.empty(0), np.empty(0))
        k = self.k
        F, Q, H, R = self.F, self.Q, self.H, self.R

        # --- forward Kalman filter ---
        x = np.full(k, y[0])
        P = np.eye(k) * 1.0
        xf = np.zeros((n, k))
        Pf = np.zeros((n, k, k))
        xp = np.zeros((n, k))
        Pp = np.zeros((n, k, k))

        for t in range(n):
            if t == 0:
                x_pred, P_pred = x, P
            else:
                x_pred = F @ xf[t - 1]
                P_pred = F @ Pf[t - 1] @ F.T + Q
            xp[t], Pp[t] = x_pred, P_pred

            # S is 1×1 (scalar innovation variance) — a guarded reciprocal, no inv.
            S = float((H @ P_pred @ H.T + R)[0, 0])
            K = (P_pred @ H.T) / (S if abs(S) > 1e-12 else 1e-12)  # (k, 1)
            resid = y[t] - (H @ x_pred)
            x_upd = x_pred + (K @ resid).ravel()
            P_upd = (np.eye(k) - K @ H) @ P_pred
            xf[t], Pf[t] = x_upd, P_upd

        # --- RTS backward smoother ---
        xs = xf.copy()
        Ps = Pf.copy()
        eye_k = np.eye(k)
        for t in range(n - 2, -1, -1):
            # C = Pf[t] Fᵀ Pp[t+1]⁻¹ via a jittered solve rather than a raw inverse
            # of a possibly near-singular predicted covariance.
            Pp1 = Pp[t + 1] + eye_k * 1e-12
            A = Pf[t] @ F.T
            C = np.linalg.solve(Pp1.T, A.T).T
            xs[t] = xf[t] + C @ (xs[t + 1] - xp[t + 1])
            Ps[t] = Pf[t] + C @ (Ps[t + 1] - Pp[t + 1]) @ C.T

        latent = xs[:, 0]
        variance = Ps[:, 0, 0]
        return SmoothResult(latent=latent, variance=variance)
