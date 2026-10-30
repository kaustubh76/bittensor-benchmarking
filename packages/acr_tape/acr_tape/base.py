"""The ``TapeSource`` interface — the seam between data and estimator.

The entire pipeline consumes ``TapeEvent``s through this one abstraction, so the
same estimator runs unchanged on:

  * ``SimSource``  — the calibrated simulator (available today), and
  * ``ArcSource``  — real Arc testnet flow (as it materializes).

This is what makes "methodology-first, liquidity-second" real: the estimator is
validated on simulation with published parameters and, byte-for-byte, on live
testnet exhaust.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator

from acr_core import SellerAttestation, TapeEvent


class TapeSource(ABC):
    """A source of observed market exhaust + seller attestations."""

    @abstractmethod
    def stream(self) -> Iterator[TapeEvent]:
        """Yield tape events in economic-timestamp order."""

    @abstractmethod
    def attestations(self) -> list[SellerAttestation]:
        """Return all seller attestations known to this source."""

    def range(self, t0: float, t1: float) -> list[TapeEvent]:
        """Materialize events with ``t0 <= ts < t1`` (default: filter stream)."""
        return [e for e in self.stream() if t0 <= e.ts < t1]

    def collect(self) -> list[TapeEvent]:
        """Materialize the entire stream (convenience for batch estimation)."""
        return list(self.stream())
