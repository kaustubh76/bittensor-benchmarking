"""The Gateway batching operator — the convolution at the heart of the thesis.

Economic events happen in continuous time, but Circle Gateway settles *net*
positions on a schedule. So the tape the world observes is the latent flow
**convolved** with the batching schedule: a trade's economic timestamp and its
settlement timestamp differ, and many trades collapse into one settlement slot.

Naive VWAP over settlement time therefore measures the batch scheduler, not the
market. The observation model (Pillar 1) exists to deconvolve exactly this.
"""

from __future__ import annotations

import numpy as np


def assign_batches(
    ts: np.ndarray, batch_interval: float
) -> tuple[np.ndarray, np.ndarray]:
    """Map economic timestamps to (batch_id, settled_ts).

    A trade at economic time ``t`` settles at the end of the fixed-width window
    it falls into: ``settled_ts = (floor(t / interval) + 1) * interval``.
    """
    if batch_interval <= 0:
        raise ValueError("batch_interval must be positive")
    batch_id = np.floor(ts / batch_interval).astype(np.int64)
    settled_ts = (batch_id + 1) * batch_interval
    return batch_id, settled_ts


# NOTE: the box-average measurement map ``H`` lives in
# ``acr_estimator.observation_model.ObservationModel._build`` (built per print
# from the actual bar cadence). An earlier standalone ``batch_observation_matrix``
# helper here was never consumed and has been removed.
