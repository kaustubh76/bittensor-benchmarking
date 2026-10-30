#!/usr/bin/env python
"""Re-sign v1's recent prints into ACROracleV2 — BEFORE the first live v2 post.

`postPrint` enforces strictly monotone economic timestamps. Once v2 takes one
live print, no earlier one can ever be inserted — so a v2 that starts empty
stays empty for its whole history, the subgraph's arrival ring has nothing in
it, and every settlement in the demo window comes back `benchmarked: false`.
This is the one step in the migration whose ORDER is not recoverable.

    ACR_ORACLE_V2_ADDRESS=0x… uv run python scripts/backfill_oracle_v2.py --dry-run
    ACR_ORACLE_V2_ADDRESS=0x… uv run python scripts/backfill_oracle_v2.py

Reads v1's history on chain (`historyLength`/`historyAt`) rather than over logs:
Arc caps `eth_getLogs` at ~15k blocks, so paging a year of prints would be dozens
of round trips against a throttled RPC.

Every backfilled print is stamped with a DISTINGUISHED policy hash — these were
re-signed now, not produced by the estimator then, and a tape that blurred the
two would invite exactly the re-derivation it exists to support. The window is
reconstructed as the nominal hour ending at each print's own timestamp, matching
what the live poster publishes.

Exit codes: 0 = backfilled (or a dry run produced a plan); 1 = nothing could be.
"""

from __future__ import annotations

import argparse
import hashlib
import sys

from acr_core import ALL_INDEX_IDS, ACRPrint, get_settings
from acr_oracle_client import OracleClient
from acr_oracle_client.client import ORACLE_V2, USDC, WAD, index_id_to_bytes32

#: These prints were re-signed during the migration rather than produced by the
#: estimator at the time. Anyone re-deriving the tape must be able to tell.
BACKFILL_POLICY = "0x" + hashlib.sha256(b"acr.oracle.v2.backfill").hexdigest()

#: The nominal window each print covers, matching PrintStore.window_s.
WINDOW_S = 3600


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--hours", type=float, default=48.0,
                    help="how far back to re-sign (default 48h)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan; sign and broadcast nothing")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    s = get_settings()
    if not s.oracle_address:
        print("set ACR_ORACLE_ADDRESS — the v1 oracle is the source to copy from")
        return 1
    if not s.oracle_v2_address:
        print("set ACR_ORACLE_V2_ADDRESS to the deployed ACROracleV2 (make deploy-oracle-v2)")
        return 1

    v1 = OracleClient(oracle_address=s.oracle_address)
    v2 = OracleClient(oracle_address=s.oracle_v2_address, schema=ORACLE_V2)
    if v1._connect() is None:
        print("  ✗ no RPC — cannot read v1's history")
        return 1
    if not args.dry_run and not v2.can_post():
        print("  ✗ no signer — set ACR_POSTER_PRIVATE_KEY or the Circle poster wallet")
        return 1

    c1 = v1._contract()
    plan: list[ACRPrint] = []
    # How many indices we actually managed to READ. An empty plan means two
    # opposite things — "v2 is already level" and "the RPC refused every read" —
    # and reporting the first when the second happened is a lie an operator
    # acts on. Count the successes so the two can be told apart.
    inspected = 0
    # Per INDEX, not global. Each oracle enforces monotonicity per index, so a
    # single max across all three compares every print against whichever index
    # happens to be furthest ahead — which every print already satisfies, making
    # the guard below vacuous.
    ceiling: dict[str, int] = {}

    for iid in ALL_INDEX_IDS:
        key = index_id_to_bytes32(iid)
        try:
            n = int(c1.functions.historyLength(key).call())
        except Exception as exc:  # noqa: BLE001
            print(f"  {iid}: could not read history ({str(exc)[:70]})")
            continue
        inspected += 1
        if n == 0:
            print(f"  {iid}: v1 has no history")
            continue

        newest = int(c1.functions.historyAt(key, n - 1).call()[4])  # .timestamp
        ceiling[iid] = newest
        cutoff = newest - int(args.hours * 3600)

        # Where v2 already stands, so a re-run resumes instead of reverting.
        existing = v2.read_latest(iid)
        floor = int(existing["timestamp"]) if existing else 0

        # Walk BACKWARDS from the newest and stop at the cutoff. Forwards from 0
        # meant one RPC round trip per print ever published — ~4,700 calls
        # against a throttled endpoint to find the ~144 that matter, which took
        # longer than the window it was trying to read.
        taken = 0
        rows: list[tuple[int, list]] = []
        for i in range(n - 1, -1, -1):
            row = c1.functions.historyAt(key, i).call()
            ts = int(row[4])
            if ts < cutoff:
                break
            if ts <= floor:
                continue
            rows.append((ts, row))
        for ts, row in reversed(rows):
            plan.append(
                ACRPrint(
                    index_id=iid,
                    ts=float(ts),
                    value=row[0] / WAD,
                    ci_lo=row[1] / WAD,
                    ci_hi=row[2] / WAD,
                    attack_cost_per_bp=row[3] / USDC,
                    policy_hash=BACKFILL_POLICY,
                    # Not computed then and not inventable now: the human bound
                    # is left absent rather than defaulted to the wallet bound.
                    human_adjusted_bound=None,
                    window_start=float(ts - WINDOW_S),
                    window_end=float(ts),
                )
            )
            taken += 1
        print(f"  {iid}: {taken} print(s) to re-sign (v1 has {n}, v2 stands at {floor})")

    if not plan:
        if not inspected:
            print("\n  ✗ could not read any index's history from v1 — not a no-op, a failure")
            return 1
        # Idempotent: the runbook tells an operator to run this, and re-running
        # a completed backfill is the normal case. Green, not red.
        print("\n  ✓ nothing to backfill — v2 is already level with v1")
        return 0

    # Ascending, because postPrint refuses a timestamp at or below the last one
    # for that index. Sorting by (index, ts) keeps each index's run monotone.
    plan.sort(key=lambda p: (p.index_id, p.ts))

    # A print beyond v1's newest FOR ITS OWN INDEX would put v2 ahead there, and
    # the poster seeds its cursor from the max across both oracles — so the next
    # live cycle would pick a timestamp v2 has already used and revert forever.
    # Refuse rather than create a state no later post can dig out of.
    over = [p for p in plan if p.ts > ceiling.get(p.index_id, 0)]
    if over:
        print(f"\n  ✗ {len(over)} print(s) would put v2 ahead of v1 — refusing")
        for p in over[:3]:
            print(f"    {p.index_id} ts={int(p.ts)} > v1 newest {ceiling.get(p.index_id, 0)}")
        return 1

    print(f"\n  {len(plan)} print(s), policy {BACKFILL_POLICY[:18]}…")
    if args.dry_run:
        for p in plan[:5]:
            print(f"    {p.index_id} ts={int(p.ts)} value={p.value:.6f} "
                  f"window=[{int(p.window_start)}, {int(p.window_end)})")
        if len(plan) > 5:
            print(f"    … and {len(plan) - 5} more")
        print("\n  dry run — nothing signed, nothing broadcast")
        return 0

    posted = failed = 0
    for p in plan:
        try:
            v2.post(p)
            posted += 1
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"    ✗ {p.index_id} ts={int(p.ts)}: {str(exc)[:90]}")

    print(f"\n  ✓ backfilled {posted}/{len(plan)}" + (f" · {failed} failed" if failed else ""))
    print("  v2 now has history. The poster may take live prints from here.")
    return 0 if posted else 1


if __name__ == "__main__":
    sys.exit(main())
