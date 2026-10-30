#!/usr/bin/env python
"""Extract the subgraph's ABIs from Foundry's own build artifacts.

The Terminal hand-inlines its ABIs (`apps/terminal/lib/onchain.ts`), and they
have drifted from the contracts before. The subgraph does not get to make that
mistake: `graph/abis/*.json` is generated from `contracts/out/`, so a contract
change that is not reflected in the mappings fails at `graph codegen` instead of
at query time.

    uv run python scripts/graph_abis.py      # or: make graph-abis

Needs `forge build` to have run (`make build-contracts`).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "contracts/out"
DEST = ROOT / "graph/abis"

#: contract name -> artifact path, relative to contracts/out.
CONTRACTS = {
    "ACROracle": "ACROracle.sol/ACROracle.json",
    "AttestationRegistry": "AttestationRegistry.sol/AttestationRegistry.json",
    "ACRFutures": "ACRFutures.sol/ACRFutures.json",
    "FeedAccessAttestor": "FeedAccessAttestor.sol/FeedAccessAttestor.json",
    "ReceiptMirror": "ReceiptMirror.sol/ReceiptMirror.json",
    "ACROracleV2": "ACROracleV2.sol/ACROracleV2.json",
    "HumanIdMirror": "HumanIdMirror.sol/HumanIdMirror.json",
}


def main() -> int:
    missing = [n for n, p in CONTRACTS.items() if not (OUT / p).exists()]
    if missing:
        print(f"missing artifacts for {', '.join(missing)} — run `make build-contracts`")
        return 1

    DEST.mkdir(parents=True, exist_ok=True)
    for name, rel in CONTRACTS.items():
        abi = json.loads((OUT / rel).read_text())["abi"]
        (DEST / f"{name}.json").write_text(json.dumps(abi, indent=2) + "\n")
        events = [e["name"] for e in abi if e.get("type") == "event"]
        print(f"  {name}: {len(events)} events → graph/abis/{name}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
