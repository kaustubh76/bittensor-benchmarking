#!/usr/bin/env python
"""Measure the real distribution of gaps between on-chain oracle prints.

This exists because ``app.SELF_URL`` makes a claim it cannot prove on its own:

    Whether a self-request resets the platform's idle timer is an EMPIRICAL
    question, not a guarantee ... The proof is the gap distribution measured
    afterwards, not this comment.

This is that measurement. The press posts hourly, and ``ACRFutures`` refuses to
settle against a print older than ``MAX_SETTLE_AGE`` (120 minutes) — so the
number that matters is not the average gap but the TAIL. One 216-minute hole
leaves the venue unsettleable for an hour, and an average of 70 minutes hides
it completely.

What it reads: ``PricePosted`` straight off Arc, paged backwards (Arc caps an
``eth_getLogs`` range at ~15000 blocks regardless of match count, so reach comes
only from paging). Nothing here touches the API — a gap is a property of the
chain, and asking the service whether it posted would be asking the suspect.

    uv run python scripts/print_gaps.py            # == make print-gaps
    GAP_PAGES=24 uv run python scripts/print_gaps.py    # reach further back

Two cadences get reported, because they answer different questions:

* **per press run** — how often the poster woke up;
* **per index** — how stale any ONE index got, which is what a settlement is
  actually checked against.

They differ whenever the press posts a subset of indices per run.

A caveat the numbers cannot carry themselves: a redeploy also wakes the box, so
a window containing deploys is not evidence about the self-ping. Measure across
a QUIET window — no deploys, no manual traffic — or the result is about your own
activity. ``--since`` marks the boundary so pre-fix gaps stop being counted
against a fix that was not yet running.

Exit codes: 0 = no gap exceeded the settle window; 1 = at least one did (or the
chain could not be read). GAP_STRICT=0 makes it report-only.
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from datetime import UTC, datetime

from acr_oracle_client import OracleClient
from acr_oracle_client.cadence import index_gaps_min, press_runs

#: ACRFutures.MAX_SETTLE_AGE — a print older than this cannot settle a series.
MAX_SETTLE_AGE_MIN = 120.0

PAGES = int(os.environ.get("GAP_PAGES", "16"))
LIMIT = int(os.environ.get("GAP_LIMIT", "400"))
STRICT = os.environ.get("GAP_STRICT", "1") not in ("", "0", "false")


def fmt(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%m-%d %H:%M")


def parse_since(raw: str) -> float:
    """``--since`` as an epoch. Accepts ``2026-08-03T08:23`` or a bare epoch."""
    raw = raw.strip()
    if not raw:
        return 0.0
    try:
        return float(raw)
    except ValueError:
        pass
    return datetime.fromisoformat(raw).replace(tzinfo=UTC).timestamp()


def summarize(label: str, gaps: list[float]) -> int:
    """Print a distribution; return how many gaps broke the settle window."""
    if not gaps:
        print(f"  {label}: no gaps to measure")
        return 0
    over = [g for g in gaps if g > MAX_SETTLE_AGE_MIN]
    print(
        f"  {label:22s} n={len(gaps):3d}  median {statistics.median(gaps):6.1f}  "
        f"mean {statistics.mean(gaps):6.1f}  max {max(gaps):7.1f}  "
        f"over={len(over)}"
    )
    return len(over)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--since",
        default=os.environ.get("GAP_SINCE", ""),
        help="only judge gaps after this UTC time (ISO or epoch) — e.g. the "
        "moment a fix went live. Earlier gaps are shown but not counted.",
    )
    args = ap.parse_args()
    since = parse_since(args.since)

    sys.stdout.reconfigure(line_buffering=True)
    c = OracleClient()
    print(f"oracle  {c.oracle_address or '(not configured)'}")
    if not c.oracle_address:
        print("  ✗ no ACR_ORACLE_ADDRESS — nothing to measure")
        sys.exit(1)
    print(f"paging  {PAGES} pages, limit {LIMIT}")
    if since:
        print(f"judging gaps after {fmt(since)} UTC only")

    t0 = time.time()
    posts = c.recent_posts(limit=LIMIT, pages=PAGES)
    print(f"read    {len(posts)} prints in {time.time() - t0:.1f}s\n")
    if len(posts) < 2:
        print("  ✗ fewer than two prints in reach — cannot measure a gap")
        sys.exit(1)

    # One PricePosted per index per run, so several events share a wall clock.
    # Collapse to distinct PRESS RUNS: that is the poster's actual cadence.
    # Shared with the Terminal's systems ledger (acr_oracle_client.cadence) so
    # this script and the dashboard cannot drift about what a "gap" is.
    runs = press_runs(posts)

    span_h = (runs[-1][0] - runs[0][0]) / 3600
    print(f"window  {fmt(runs[0][0])} → {fmt(runs[-1][0])} UTC  ({span_h:.1f}h)")
    print(f"        {len(runs)} press runs, {len(posts)} index prints\n")

    print("gap between consecutive press runs (minutes):")
    judged: list[float] = []
    for i in range(1, len(runs)):
        prev, (now, idxs) = runs[i - 1][0], runs[i]
        g = (now - prev) / 60
        counted = now >= since
        if counted:
            judged.append(g)
        mark = ""
        if g > MAX_SETTLE_AGE_MIN:
            mark = "  ✗ OVER THE SETTLE WINDOW" if counted else "  (before --since)"
        elif not counted:
            mark = "  (before --since)"
        print(f"  {fmt(prev)} → {fmt(now)}  {g:7.1f}{mark}   [{','.join(sorted(idxs))}]")

    print("\ndistribution:")
    breaches = summarize("press runs", judged)

    # A series settles against ITS index, so an index pressed only every third
    # run is staler than the press cadence implies.
    print("\nper index (what a settlement is actually checked against):")
    for iid in sorted({p["index_id"] for p in posts}):
        breaches = max(breaches, summarize(iid, index_gaps_min(posts, iid, since)))

    age = (time.time() - runs[-1][0]) / 60
    stale = age > MAX_SETTLE_AGE_MIN
    print(
        f"\nlast press {age:.0f} min ago — "
        f"{'STALE, the venue cannot settle' if stale else 'inside the settle window'}"
    )

    if not judged:
        print("\n⚠ no gaps after --since yet: too early to conclude anything.")
        sys.exit(0)
    if breaches or stale:
        print(f"\n✗ {breaches} gap(s) broke the {MAX_SETTLE_AGE_MIN:.0f}-minute settle window")
        sys.exit(1 if STRICT else 0)
    print(f"\n✓ every gap stayed inside the {MAX_SETTLE_AGE_MIN:.0f}-minute settle window")


if __name__ == "__main__":
    main()
