#!/usr/bin/env python
"""Run one oracle-poster cycle — estimate → EIP-712 sign → postPrint on Arc.

Builds the ``PrintStore`` exactly the way the API and ``gen_snapshot`` do, then
runs ``OraclePoster.post_once()`` (refresh + per-index post) and prints each
index's tx ref with its arcscan URL — or the ``offline:``/``error:`` marker the
poster returns when a post couldn't happen.

Timestamp care: the oracle enforces strictly monotone print timestamps, and the
store's print clock is ``cursor × step_s`` starting from 0 in a fresh process.
So before posting we read the newest on-chain timestamp and pin the refresh to
the next step past it — repeated ``make post-once`` runs (and the API's own
poster loop convention) stay monotone instead of reverting.

Exit codes: 0 = at least one print posted (or no oracle configured — offline is
the honest no-op); 1 = an oracle IS configured and every post failed.

    uv run python scripts/post_once.py            # or: make post-once
"""

from __future__ import annotations

import os
import sys

from acr_core import ALL_INDEX_IDS, get_settings
from index_api.poster import OraclePoster
from index_api.store import PrintStore

FALLBACK_EXPLORER = "https://explorer.arc.io"  # Arc mainnet; the chain profile overrides it


def _explorer(settings) -> str:
    """ACR_EXPLORER_BASE (settings field or raw env) with the arcscan fallback."""
    base = getattr(settings, "explorer_base", "") or os.environ.get("ACR_EXPLORER_BASE", "")
    return (base or FALLBACK_EXPLORER).rstrip("/")


def _next_monotone_ts(poster: OraclePoster, store: PrintStore) -> float | None:
    """The smallest ``k·step_s`` strictly beyond every on-chain print timestamp,
    across EVERY oracle the poster writes to.

    ``None`` (→ the store's own cursor) when offline or every oracle is empty.

    The max over both matters. Each contract enforces its own strict
    monotonicity, so a timestamp seeded from v1 alone is safe only while v1 is
    the furthest ahead — and if a backfill ever leaves v2 beyond v1's latest,
    every subsequent v2 post reverts "non-monotone ts" forever. That is a
    permanent stall, not a gap, and no later post can dig out of it.
    """
    clients = [c for c in (poster.client, poster.v2) if c is not None and c.can_post()]
    if not clients:
        return None
    latest = 0
    for client in clients:
        for iid in ALL_INDEX_IDS:
            back = client.read_latest(iid)
            if back:
                latest = max(latest, int(back["timestamp"]))
    if latest <= 0:
        return None
    return float((int(latest // store.step_s) + 1) * store.step_s)


def main() -> None:
    s = get_settings()
    configured = bool(s.oracle_address)
    explorer = _explorer(s)

    store = PrintStore()
    poster = OraclePoster(store)

    if not configured:
        print(
            "\n  no oracle configured (ACR_ORACLE_ADDRESS empty) — posting runs "
            "offline: payloads are built and logged, nothing is sent.\n"
        )

    ts = _next_monotone_ts(poster, store)
    refs = poster.post_once(ts=ts)

    print(f"\n  {'INDEX':<9} {'tx ref':<70}")
    print("  " + "-" * 78)
    posted = 0
    for iid, ref in zip(store.latest.keys(), refs, strict=True):
        if ref.startswith(("offline:", "error:")):
            print(f"  {iid:<9} {ref}")
            continue
        posted += 1
        print(f"  {iid:<9} {explorer}/tx/{ref}")

    if configured and posted == 0:
        print("\n  ✗ every post failed while an oracle is configured — check the RPC,")
        print("    the poster key's USDC gas balance, and `make verify-testnet`.\n")
        sys.exit(1)
    if posted:
        print(f"\n  ✓ posted {posted}/{len(refs)} prints — they read back via /onchain/<index>.\n")
    else:
        print("\n  offline cycle complete — configure the oracle to post for real.\n")


if __name__ == "__main__":
    main()
