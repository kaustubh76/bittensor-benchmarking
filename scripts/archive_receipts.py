#!/usr/bin/env python3
"""Pull the seller's live settlement ledger into the committed archive.

    uv run python scripts/archive_receipts.py            # append what production settled since the last archive
    uv run python scripts/archive_receipts.py --check    # exit 1 if production holds rows the archive does not

THE FREE TIER HAS NO DISK. Every real Gateway settlement the deployed seller
records lives in that container's memory and in a JSONL the next restart erases;
what survives is the archive this script writes, which ships INSIDE the image and
is rehydrated at boot (`x402.py::_rehydrate`). Until this file existed the pull
was a hand-run curl before each redeploy, and a redeploy without it silently
dropped every settlement since the last one — including the first rows ever
stamped with the tier the buyer's card earned.

Only REAL schemes are archived (the same rule `_rehydrate` applies), rows are
deduplicated on `tx_ref`, and the archive's line shape is `asdict(PaymentReceipt)`
so a legacy line and a new one parse the same way. Run it before `make` redeploys
the image; `--check` belongs in the redeploy script's preflight.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "services" / "index_api" / "index_api" / "receipts_live.jsonl"
# Arc MAINNET, and OVERRIDABLE. This was hardcoded to the Arc testnet seller
# (`acr-api-1fto`, now suspended and answering 503), so the script could not be
# pointed anywhere else without editing it.
API = os.environ.get("ACR_API_URL", "https://acr-api-mainnet.onrender.com").rstrip("/")
#: What `_rehydrate` will accept back. Dev and sim rows never reach the archive.
REAL_SCHEMES = {"exact", "circle", "gateway"}
#: The dataclass's fields, in its order — the archive is `asdict(PaymentReceipt)`.
FIELDS = ("payer", "amount_usdc", "tx_ref", "network", "scheme", "settled_at",
          "resource", "seller", "unit", "quantity", "tier")


def _live(api: str) -> list[dict]:
    with urllib.request.urlopen(f"{api}/marketplace/receipts", timeout=60) as r:
        return list(reversed(json.load(r)["receipts"]))  # oldest first, like the file


def _archived(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]


def _row(r: dict) -> dict:
    out = {k: r.get(k) for k in FIELDS if k in r}
    out.setdefault("seller", "")
    out.setdefault("unit", "")
    out.setdefault("quantity", 0.0)
    out.setdefault("tier", "")
    out.setdefault("settled_at", 0.0)
    out.setdefault("resource", "")
    return {k: out[k] for k in FIELDS}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--api", default=API)
    ap.add_argument("--archive", default=str(ARCHIVE))
    ap.add_argument("--check", action="store_true", help="report, do not write; exit 1 when rows are missing")
    a = ap.parse_args()
    path = Path(a.archive)

    have = _archived(path)
    seen = {r["tx_ref"] for r in have}
    live = _live(a.api)
    new = [r for r in live if r.get("scheme") in REAL_SCHEMES and r.get("tx_ref") and r["tx_ref"] not in seen]
    skipped = [r for r in live if r.get("scheme") not in REAL_SCHEMES]

    print(f"archive   {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}: {len(have)} row(s)")
    print(f"live      {a.api}: {len(live)} row(s), {len(skipped)} non-real skipped")
    print(f"missing   {len(new)} row(s) production holds that the archive does not")
    for r in new:
        print(f"  + seq? {r['tx_ref'][:8]}… {r['payer'][:8]}… ${r['amount_usdc']:.6f} {r.get('resource','')}"
              f"{' · ' + r['tier'] if r.get('tier') else ''}")
    if a.check:
        return 1 if new else 0
    if not new:
        print("nothing to do")
        return 0
    with path.open("a") as f:
        for r in new:
            f.write(json.dumps(_row(r), separators=(",", ":")) + "\n")
    print(f"wrote     {len(new)} row(s); archive now {len(have) + len(new)}. Commit it, then rebuild the image.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
