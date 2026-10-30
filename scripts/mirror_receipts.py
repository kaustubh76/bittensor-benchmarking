#!/usr/bin/env python
"""Mirror settled x402 receipts onto ReceiptMirror, so the subgraph has a tape.

Circle Gateway settles off-chain and returns a batch UUID, not a transaction, so
nothing on Arc records that a payment happened. Without this there is no
settlement event to index, and transaction-cost analysis has no basis at all.

The service runs this continuously as a keeper chore; this script is the
operator's copy of the same code path — for a first run, a backlog, or a check.

    ACR_RECEIPT_MIRROR_ADDRESS=0x… uv run python scripts/mirror_receipts.py --dry-run
    ACR_RECEIPT_MIRROR_ADDRESS=0x… uv run python scripts/mirror_receipts.py

Reads the seller's own public ledger (``/marketplace/receipts``) by default, or
a local JSONL with ``--ledger``. Needs a signer: ``ACR_POSTER_PRIVATE_KEY`` or
the Circle poster wallet — the same wallet that signs oracle prints, so the tape
and the prints it is measured against share one trust anchor.

Exit codes: 0 = the tape is up to date (something was mirrored, or there was
nothing eligible left to mirror); 1 = it could not be brought up to date — no
address, no signer, or an unreadable ledger. "Nothing to mirror" is the normal
steady state of a recurring chore, so it is a success: a wrapper that treats a
caught-up mirror as a failure teaches its operator to ignore the exit code.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

from acr_core import get_settings
from acr_oracle_client import MirrorClient

# Arc MAINNET, and OVERRIDABLE — this was hardcoded to the Arc testnet host,
# suspended since 2026-09-15, so the script could not be pointed elsewhere.
API = os.environ.get("ACR_API_URL", "https://acr-api-mainnet.onrender.com").rstrip("/")


def _from_api(base: str) -> list[dict]:
    req = urllib.request.Request(
        f"{base.rstrip('/')}/marketplace/receipts", headers={"User-Agent": "acr-mirror"}
    )
    with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310
        body = json.loads(r.read() or b"{}")
    return list(body.get("receipts") or [])


def _from_ledger(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    rows = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue  # a corrupt line is not a reason to mirror nothing
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--api", default=API, help="seller API to read receipts from")
    ap.add_argument("--ledger", default="", help="read a local JSONL instead of the API")
    ap.add_argument("--dry-run", action="store_true",
                    help="sign and self-check, but do not broadcast")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    s = get_settings()
    if not s.receipt_mirror_address:
        print("set ACR_RECEIPT_MIRROR_ADDRESS to the deployed ReceiptMirror "
              "(make deploy-mirror)")
        return 1

    client = MirrorClient(settings=s)
    if client.signer is None:
        print("  ✗ no signer — set ACR_POSTER_PRIVATE_KEY or the Circle poster wallet")
        return 1

    rows = _from_ledger(args.ledger) if args.ledger else _from_api(args.api)
    print(f"  {len(rows)} receipt(s) from {args.ledger or args.api}")

    # Import late: this pulls in the service package, which the estimator-only
    # install does not need.
    from index_api.mirror import mirror_once, mirrorable
    from index_api.x402 import PaymentReceipt

    receipts, dropped = [], {}
    for row in rows:
        try:
            r = PaymentReceipt(**{k: v for k, v in row.items() if k != "seq"})
        except TypeError:
            continue  # an unknown field means a newer ledger than this build
        receipts.append(r)
        ok, why = mirrorable(r)
        if not ok:
            dropped[why] = dropped.get(why, 0) + 1

    for why, n in sorted(dropped.items(), key=lambda kv: -kv[1]):
        print(f"    {n:4d} not mirrored — {why}")

    verdict = mirror_once(receipts, client=client, dry_run=args.dry_run)
    if verdict is None:
        # Distinct from the failures above (no address / no signer), which have
        # already returned 1 with their own reason. Reaching here means the
        # ledger was read and nothing in it still needs mirroring.
        print("\n  ✓ nothing to mirror — every eligible settlement is already on chain")
        return 0
    print(f"\n  {verdict}")
    if args.dry_run:
        print("  dry run — signed and self-checked, nothing broadcast")
    return 0


if __name__ == "__main__":
    sys.exit(main())
