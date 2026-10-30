#!/usr/bin/env python
"""Fold the seller's real x402 settlements into the durable archive.

A live payment lands in the seller's runtime receipt log — which sits on a
filesystem the production plan does not persist. So a real settlement is
visible until the next restart and then gone, and `/revenue` drops back to
zero over a gate that has genuinely been paid. That is the state this project
was in for a week.

The archive at `ACR_RECEIPT_ARCHIVE_PATH` is committed AND inside the image, so
it survives every restart. It lives under `services/` rather than `data/`
because that directory is excluded from both git and the Docker build — a file
there would never reach production, which is the trap this landed in first
time round.

This reads the seller's own ledger — the authoritative record, since the seller
is the side that ran verify and settle against Circle — and merges anything new
into that file.

Run it right after a live buy, then commit the file:

    uv run python scripts/x402_capture.py                  # == make x402-capture
    ACR_API_URL=https://… uv run python scripts/x402_capture.py
    X402_CAPTURE_DRY_RUN=1 …                               # show, write nothing

Exit codes: 0 = archive is up to date (with or without new rows); 1 = the
seller could not be read, or returned something that is not a ledger.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Arc MAINNET. The Arc testnet host this defaulted to (`acr-api-1fto`) has been
# suspended since 2026-09-15 and answers 503 — a default nothing in the repo
# shows you using is exactly how the press lost 48.9 hours of prints.
API = os.environ.get("ACR_API_URL", "https://acr-api-mainnet.onrender.com").rstrip("/")
ARCHIVE = Path(
    os.environ.get(
        "ACR_RECEIPT_ARCHIVE_PATH",
        # NOT under data/ — that directory is in .gitignore AND .dockerignore
        # ("secret-bearing … even by accident"), so a file there is neither
        # committed nor copied into the image. An archive that cannot reach
        # production cannot make anything durable, which is exactly the trap
        # this landed in first time round. It lives with the code that reads
        # it, like apps/terminal/lib/fallback.json.
        "services/index_api/index_api/receipts_live.jsonl",
    )
)
DRY_RUN = os.environ.get("X402_CAPTURE_DRY_RUN", "") not in ("", "0", "false")
TIMEOUT_S = float(os.environ.get("X402_CAPTURE_TIMEOUT_S", "120"))

#: Only genuine settlements belong here. A dev or sim row in the archive would
#: put invented revenue on a public counter — the one thing this file must
#: never do, since its whole purpose is to be believed.
REAL_SCHEMES = {"exact"}


def fetch_receipts(url: str) -> list[dict]:
    req = urllib.request.Request(url, headers={"User-Agent": "acr-x402-capture"})
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:  # noqa: S310
        body = json.loads(r.read() or b"{}")
    rows = body.get("receipts")
    if not isinstance(rows, list):
        raise ValueError("no receipts array in the response")
    return rows


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)
    print(f"seller  {API}\narchive {ARCHIVE}")

    try:
        rows = fetch_receipts(f"{API}/marketplace/receipts")
    except Exception as exc:  # noqa: BLE001 — a verdict beats a traceback
        print(f"  ✗ could not read the seller's ledger: {str(exc)[:120]}")
        sys.exit(1)

    existing: list[dict] = []
    if ARCHIVE.exists():
        for line in ARCHIVE.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                existing.append(json.loads(line))
            except Exception:
                continue  # a corrupt line must not cost us the whole archive
    known = {str(r.get("tx_ref")) for r in existing if r.get("tx_ref")}
    print(f"  {len(rows)} receipt(s) live, {len(existing)} already archived")

    fresh: list[dict] = []
    for r in rows:
        ref = str(r.get("tx_ref") or "")
        if not ref or ref in known:
            continue
        if r.get("scheme") not in REAL_SCHEMES:
            continue
        known.add(ref)
        # Store the archive's own shape — the seller's `seq` is a position in a
        # ring, not a property of the payment, and would be wrong on reload.
        fresh.append({
            "payer": r.get("payer", ""),
            "amount_usdc": float(r.get("amount_usdc", 0.0)),
            "tx_ref": ref,
            "network": r.get("network", ""),
            "scheme": r.get("scheme", ""),
            # Stamp only if the seller knew when; never invent a time.
            "settled_at": float(r.get("settled_at") or time.time()),
            "resource": r.get("resource", ""),
        })

    if not fresh:
        print("  ↩ nothing new — every real settlement the seller reports is already archived")
        sys.exit(0)

    for f in fresh:
        age = (time.time() - f["settled_at"]) / 60
        print(f"  + {f['tx_ref'][:24]}…  {f['amount_usdc']} USDC  {f['resource']}  "
              f"({age:.0f} min ago)")

    if DRY_RUN:
        print(f"\ndry run — would append {len(fresh)} settlement(s)")
        sys.exit(0)

    ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
    with ARCHIVE.open("a") as fh:
        for f in fresh:
            fh.write(json.dumps(f) + "\n")
    print(f"\nappended {len(fresh)} settlement(s) to {ARCHIVE}")
    print("COMMIT IT — the archive only survives a restart because it is in the image.")


if __name__ == "__main__":
    main()
