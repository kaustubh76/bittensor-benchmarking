"""Memory on a 512 MiB tier — read it, give it back, and act before the kill.

The production press was OOM-killed at 12:58 on 2026-09-13, three minutes after
a settlement. The hourly press is the reason the number moves: it re-simulates
the tape, runs the estimator's bootstrap and Louvain, and rebuilds the attack
snapshot — a ~150 MiB transient that glibc keeps as fragmented heap after the
work is done, so the NEXT press lands on top of the last one's leftovers.
Measured on the deploy that first exposed rss: 297 MiB at boot, 471 MiB one
press later, on a 512 MiB limit.

Three things, each ordinary on small containers:

* ``rss_mib()``      — the resident set, from /proc where it exists.
* ``trim()``         — ``gc.collect()`` then glibc's ``malloc_trim(0)``, which
                       returns freed arena memory to the OS. Called after the
                       press and after the ops ledger, the two heavy jobs.
* ``guard(limit)``   — on the warm tick: past 90 % of the limit, drop the
                       graph-query cache and trim, and say so on /ops. A stutter
                       instead of a kill; a kill loses every memory-only receipt.

``MALLOC_ARENA_MAX=2`` in the Dockerfile is the fourth: ``asyncio.to_thread``
runs the heavy jobs on a thread pool, and glibc gives each thread its own arena
(up to 8 × cores), which is the fragmentation this whole file is about.
"""

from __future__ import annotations

import ctypes
import gc
import logging
import os
import sys
import time

log = logging.getLogger(__name__)

#: The instance's limit (Render free tier), and the fraction past which the
#: guard acts. The ops console's own warning sits lower, at 80 %.
MEMORY_LIMIT_MIB = float(os.environ.get("ACR_MEMORY_LIMIT_MIB", "512"))
GUARD_FRAC = float(os.environ.get("ACR_MEMORY_GUARD_FRAC", "0.9"))

#: The last guard action, for /ops: when, at what reading, what it freed.
last_action: dict | None = None


def rss_mib() -> float | None:
    """Resident set size in MiB, or None where the platform cannot say."""
    try:
        try:
            with open("/proc/self/statm") as f:
                pages = int(f.read().split()[1])
            return pages * os.sysconf("SC_PAGE_SIZE") / 1_048_576
        except (FileNotFoundError, ValueError, IndexError, OSError):
            import resource

            peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # macOS reports bytes, Linux KiB.
            return peak / (1_048_576 if sys.platform == "darwin" else 1024)
    except Exception:  # noqa: BLE001 - a diagnostic must never take a section down
        return None


def trim() -> float | None:
    """Collect, then ask glibc to hand freed heap back. Returns MiB freed, or
    None when it could not be measured. Safe anywhere; a no-op on non-glibc."""
    before = rss_mib()
    gc.collect()
    try:
        libc = ctypes.CDLL("libc.so.6")
        libc.malloc_trim(0)
    except (OSError, AttributeError):
        pass  # not glibc (macOS, musl): gc alone is what there is
    after = rss_mib()
    return (before - after) if before is not None and after is not None else None


def guard(limit_mib: float | None = None, frac: float | None = None) -> dict | None:
    """Act past the threshold; return what was done, or None when nothing was."""
    global last_action
    limit = MEMORY_LIMIT_MIB if limit_mib is None else limit_mib
    threshold = (GUARD_FRAC if frac is None else frac) * limit
    before = rss_mib()
    if before is None or before < threshold:
        return None
    from acr_tape import graph_client

    dropped = graph_client.cache_bytes()
    graph_client.drop_cache()
    freed = trim()
    after = rss_mib()
    last_action = {
        "at": time.time(),
        "before_mib": round(before),
        "after_mib": round(after) if after is not None else None,
        "freed_mib": round(freed) if freed is not None else None,
        "dropped_cache_bytes": dropped,
    }
    log.warning("memory guard: %.0f MiB of %.0f — dropped %d B of cache, freed %s MiB",
                before, limit, dropped, last_action["freed_mib"])
    return last_action
