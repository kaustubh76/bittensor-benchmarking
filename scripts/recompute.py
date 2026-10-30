#!/usr/bin/env python
"""Recompute a published print from the public tape, and diff it against chain.

The Verify pillar. Every ACR print is an estimate over a window of settlements;
this reads that window back out of the subgraph — the same public data anyone
else can query — re-runs the estimator, and reports whether the number the
oracle published falls inside the interval the public data supports.

    ACR_SUBGRAPH_URL=https://… uv run python scripts/recompute.py            # all indices
    ACR_SUBGRAPH_URL=https://… uv run python scripts/recompute.py --index ACR-INF
    ACR_SUBGRAPH_URL=https://… uv run python scripts/recompute.py --rederive-cleaning

What it can and cannot claim today is stated in the output rather than implied:
prices, arrival snapshots and slippage are fully reproducible from the subgraph;
the cleaning policy is re-derivable under the printed ``policyHash`` but cannot
yet be diffed against the keeper's own exclusions, because ACROracle v1 does not
carry that hash on chain. ``--rederive-cleaning`` re-runs the stack and prints
what it excludes, so a reader can reproduce it rather than take it on faith.

Exit codes: 0 = every compared print sits within its own confidence interval;
1 = a print fell outside, or nothing could be compared.
"""

from __future__ import annotations

import argparse
import sys

import numpy as np
from acr_core import ALL_INDEX_IDS, get_settings, spec_for
from acr_estimator import estimate_index
from acr_estimator.cleaning import POLICY_VERSION, clean, policy_hash
from acr_estimator.hedonic import adjust_prices
from acr_estimator.robust import estimate as robust_estimate
from acr_oracle_client import OracleClient
from acr_tape import GraphSource


def _explain_zero(index_id: str, events, attestations, s) -> None:
    """Say WHY a recompute has nothing to estimate from, and what the tape says
    before cleaning — so the Verify pillar produces a number a reader can compare,
    labelled as the uncleaned number it is, instead of a bare exit 1.

    On the real testnet tape every one of a handful of demo payers buys from every
    demo seller, so the funding graph is ONE community and the sybil rule zeroes
    every weight. That is the cleaning stack doing exactly what it is for on a
    tape that is, in fact, one funding source — not a bug, and not "the subgraph
    returned nothing", which is how an unexplained exit read."""
    svc = spec_for(index_id).service
    window = sorted((e for e in events if e.service == svc), key=lambda e: (e.ts, e.event_id))
    if not window:
        return
    cr = clean(window, cluster_cap=s.cluster_volume_cap, seed=s.estimator_seed)
    kept = int((cr.weights > 0).sum())
    print(f"  cleaning    kept {kept}/{len(window)} settlement(s); excluded {cr.excluded_fraction:.0%} of notional "
          f"(self-deal {len(cr.flagged_self_dealing)} · wash {len(cr.flagged_wash)} · sybil {len(cr.flagged_sybil)})")
    if len(cr.communities) == 1 and cr.flagged_sybil:
        print("  reason      the whole tape is ONE funding community: every payer buys from every seller, "
              "so the sybil rule — which exists for exactly that shape — excludes it all. "
              "A cleaned estimate is undefined on a tape this concentrated.")
    adjusted, _ = adjust_prices(window, attestations)
    rob = robust_estimate(adjusted, np.ones(len(window)), alpha=s.trim_alpha,
                          n_bootstrap=s.ci_bootstrap, ci_level=s.ci_level, seed=s.estimator_seed)
    print(f"  uncleaned   {rob.value:.6f}  CI [{rob.ci_lo:.6f}, {rob.ci_hi:.6f}]  "
          f"(robust estimate with NO exclusions — a comparison figure, not a print)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--index", default="", help="one index id; default every index")
    ap.add_argument("--rederive-cleaning", action="store_true",
                    help="re-run the cleaning stack and print what it excludes")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    s = get_settings()
    if not s.subgraph_url:
        print("set ACR_SUBGRAPH_URL to the deployed subgraph (make graph-deploy)")
        return 1

    src = GraphSource()
    events = src.collect()
    attestations = src.attestations()
    print(f"\ntape      {len(events)} settlement(s) · {len(attestations)} attestation(s)")
    print(f"source    {s.subgraph_url}")
    print(f"policy    {policy_hash(s)}  (v{POLICY_VERSION})")
    if not events:
        print("\n  ✗ the subgraph returned no settlements — nothing to recompute")
        return 1

    span = max(e.ts for e in events) - min(e.ts for e in events)
    print(f"window    {span / 3600:.1f}h of settlement flow")

    client = OracleClient()
    index_ids = (args.index,) if args.index else ALL_INDEX_IDS
    compared = 0
    outside = 0

    for index_id in index_ids:
        print(f"\n{index_id}")
        try:
            print_, diag = estimate_index(index_id, events, attestations, settings=s)
        except ValueError as exc:
            print(f"  · not recomputable — {exc}")
            _explain_zero(index_id, events, attestations, s)
            continue

        cr = diag.cleaning
        print(f"  recomputed  {print_.value:.6f}  CI [{print_.ci_lo:.6f}, {print_.ci_hi:.6f}]"
              f"  n={print_.n_obs}")
        print(f"  excluded    {len(cr.all_flagged)} address(es) · "
              f"{100 * cr.excluded_fraction:.1f}% of notional zeroed · "
              f"{100 * cr.cap_scaled_fraction:.1f}% cap-scaled")

        if args.rederive_cleaning:
            print(f"    self-deal {len(cr.flagged_self_dealing)} · "
                  f"wash {len(cr.flagged_wash)} · sybil {len(cr.flagged_sybil)}"
                  f" · {len(cr.communities)} communities")
            for addr in sorted(cr.all_flagged)[:8]:
                print(f"      excluded {addr}")
            if len(cr.all_flagged) > 8:
                print(f"      … and {len(cr.all_flagged) - 8} more")

        onchain = client.read_latest(index_id)
        if onchain is None:
            print("  on-chain    (no print yet — nothing to compare against)")
            continue

        compared += 1
        within = print_.ci_lo <= onchain["value"] <= print_.ci_hi
        drift_bp = 1e4 * (onchain["value"] - print_.value) / print_.value
        mark = "✔" if within else "✗"
        print(f"  on-chain    {onchain['value']:.6f}   {drift_bp:+.1f} bp vs recomputed")
        print(f"  {mark} the published print {'sits inside' if within else 'FALLS OUTSIDE'}"
              " the interval the public tape supports")
        if not within:
            outside += 1

    if args.rederive_cleaning:
        print("\n  note: these exclusions are re-derived from the public tape under the")
        print("  policy hash above. Diffing them against the KEEPER's own exclusions")
        print("  needs the print to carry that hash on chain, which ACROracle v1 does")
        print("  not — so this reproduces the rule, it does not yet cross-check it.")

    if not compared:
        print("\n  ✗ nothing was compared against chain")
        return 1
    print(f"\n  {compared} print(s) compared · {outside} outside interval")
    return 1 if outside else 0


if __name__ == "__main__":
    sys.exit(main())
