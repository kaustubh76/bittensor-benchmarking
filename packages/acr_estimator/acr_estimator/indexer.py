"""Tape Indexer — raw event ingestion on a volume-time clock.

Clock choice is a modeling decision, not a detail. Sampling in *volume time*
(equal traded notional per bar) instead of clock time makes the observation
cadence stationary: a burst of flow and a quiet stretch contribute comparable
information per bar. Malachite's deterministic sub-second finality is what lets
us trust the tick timestamps enough to do this at all — no reorg ambiguity in
the tape.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from acr_core import TapeEvent, volume_time_bars


@dataclass
class Bar:
    """A volume-time bar: a contiguous slab of ~equal traded notional."""

    index: int
    events: list[TapeEvent] = field(default_factory=list)

    @property
    def notional(self) -> float:
        return sum(e.notional for e in self.events)

    @property
    def t_start(self) -> float:
        return min(e.ts for e in self.events)

    @property
    def t_end(self) -> float:
        return max(e.ts for e in self.events)

    @property
    def settled_ts(self) -> float:
        """Representative settlement time of the bar (batching-aware)."""
        st = [e.settled_ts for e in self.events if e.settled_ts is not None]
        return float(np.mean(st)) if st else self.t_end

    @property
    def prices(self) -> np.ndarray:
        return np.array([e.price for e in self.events])

    @property
    def weights(self) -> np.ndarray:
        return np.array([e.notional for e in self.events])


def build_bars(events: list[TapeEvent], bar_volume: float) -> list[Bar]:
    """Partition time-sorted events into volume-time bars of ~``bar_volume``."""
    if not events:
        return []
    evs = sorted(events, key=lambda e: e.ts)
    notional = np.array([e.notional for e in evs])
    ts = np.array([e.ts for e in evs])
    bar_ids = volume_time_bars(ts, notional, bar_volume)
    bars: dict[int, Bar] = {}
    for e, b in zip(evs, bar_ids, strict=True):
        bars.setdefault(int(b), Bar(index=int(b))).events.append(e)
    return [bars[k] for k in sorted(bars)]
