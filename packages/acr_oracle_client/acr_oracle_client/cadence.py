"""How to read the press's cadence out of ``PricePosted`` events.

Lives here, next to ``OracleClient.recent_posts`` which produces the input,
because two callers need the SAME answer and they run in different places:
``scripts/print_gaps.py`` measures cadence from a laptop, and the Terminal's
systems ledger reports it from inside the press. ``scripts/`` is not in the
Docker image, so the service cannot import it — and duplicating a sixty-second
constant in two files is exactly how the dashboard and the gate start
disagreeing about what "a gap" means.

The whole subtlety is in ``press_runs``: the poster writes one print PER INDEX
per run, landing a few seconds apart across adjacent blocks. Diffing raw event
timestamps therefore yields a pile of 0.1-minute non-gaps and hides the real
hole. Collapsing to runs first is what makes the number mean "how long was the
press silent".
"""

from __future__ import annotations

#: Two prints closer together than this belong to the same press run. The
#: poster emits three (one per index) within a few blocks; anything inside a
#: minute is one visit to the press, not three.
RUN_COALESCE_S = 60.0


def press_runs(posts: list[dict]) -> list[tuple[float, set[str]]]:
    """Collapse ``PricePosted`` events into distinct press runs.

    ``posts`` are ``recent_posts()`` dicts — each carrying ``at_wall`` (the
    BLOCK's wall clock, not the print's sim-relative data timestamp) and
    ``index_id``. Returns ``[(at_wall, {index_id, …}), …]`` oldest first.
    """
    by_wall: dict[float, set[str]] = {}
    for p in posts:
        by_wall.setdefault(float(p["at_wall"]), set()).add(str(p["index_id"]))
    runs: list[tuple[float, set[str]]] = []
    for w in sorted(by_wall):
        if runs and w - runs[-1][0] < RUN_COALESCE_S:
            runs[-1][1].update(by_wall[w])
        else:
            runs.append((w, set(by_wall[w])))
    return runs


def run_gaps_min(runs: list[tuple[float, set[str]]], since: float = 0.0) -> list[float]:
    """Minutes between consecutive press runs, counting only runs at or after
    ``since`` (so a pre-fix tape can be printed without being judged)."""
    return [
        (runs[i][0] - runs[i - 1][0]) / 60
        for i in range(1, len(runs))
        if runs[i][0] >= since
    ]


def index_gaps_min(posts: list[dict], index_id: str, since: float = 0.0) -> list[float]:
    """Minutes between consecutive prints OF ONE INDEX.

    A series settles against its own index, so an index that only gets pressed
    every third run is staler than the run cadence implies — which is the gap
    that actually decides whether the venue can settle.
    """
    ts = sorted({float(p["at_wall"]) for p in posts if p["index_id"] == index_id})
    return [(ts[i] - ts[i - 1]) / 60 for i in range(1, len(ts)) if ts[i] >= since]
