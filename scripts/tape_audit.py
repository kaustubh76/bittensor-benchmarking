#!/usr/bin/env python
"""Measure what REAL Arc settlement flow actually yields as an index tape.

The published indices run on a calibrated simulator, and the repo's stated
reason is that real on-chain USDC flow "carries no compute-price signal". That
is a strong claim to leave as prose, and it is the single most load-bearing
honesty claim in the project — so this measures it instead.

Runs ``ArcSource`` against live Arc in both decodes and reports, per index:
how many real settlements were observed, how many survive the estimator's
cleaning stage, and whether a print can be produced at all.

  * **legacy decode** (default) pins every event's price to the index reference
    level and back-solves size, so it deliberately fabricates no price signal —
    expect observations but no dispersion.
  * **attested-market decode** (``ACR_ARC_ATTESTED_ONLY=1``) prices each event
    as ``notional / arc_unit_qty`` against the seller's on-chain attestation.
    This is the only mode that derives a price from real settlement amounts.

Read-only: no writes, no posting, no state. Exit code is always 0 — a thin
real tape is a finding to publish, not a build failure.

    uv run python scripts/tape_audit.py                    # == make tape-audit
    TAPE_AUDIT_BLOCKS=50000 uv run python scripts/tape_audit.py
"""

from __future__ import annotations

import os
import sys

from acr_core import ALL_INDEX_IDS, get_settings, spec_for

BLOCKS = int(os.environ.get("TAPE_AUDIT_BLOCKS", "20000"))


def _audit(label: str, attested_only: bool, blocks: int) -> dict:
    from acr_estimator.pipeline import estimate_index
    from acr_tape import ArcSource

    s = get_settings()
    print(f"\n  {label}")
    print(f"  {'-' * len(label)}")
    src = ArcSource(
        rpc_url=s.arc_rpc_url,
        lookback_blocks=blocks,
        attested_only=attested_only,
    )
    try:
        events = list(src.stream())
    except Exception as exc:
        print(f"    ✗ could not read the chain: {str(exc)[:120]}")
        return {}
    try:
        attestations = src.attestations()
    except Exception:
        attestations = []

    print(f"    {len(events)} settlement events over the last {blocks} blocks, "
          f"{len(attestations)} on-chain seller attestations")
    if not events:
        print("    (no USDC transfers decoded in the window — try TAPE_AUDIT_BLOCKS=100000)")
        return {}

    prices = sorted({round(e.price, 8) for e in events})
    print(f"    distinct prices: {len(prices)}"
          + (f"  → {prices[:4]}{'…' if len(prices) > 4 else ''}" if prices else ""))
    if len(prices) == 1:
        print("    ⚠ every event carries the SAME price — this decode recovers notional "
              "only, so there is nothing for an estimator to estimate.")

    out = {}
    for iid in ALL_INDEX_IDS:
        svc = spec_for(iid).service
        window = [e for e in events if e.service == svc]
        if not window:
            print(f"    {iid:<9} 0 events → no print")
            out[iid] = {"events": 0, "print": None}
            continue
        try:
            p, diag = estimate_index(iid, events, attestations)
            kept = int(round(len(window) * (1 - diag.cleaning.removed_fraction)))
            print(f"    {iid:<9} {len(window):>5} events → {kept:>5} survive cleaning "
                  f"→ print {p.value:.6f} (n_obs {p.n_obs})")
            out[iid] = {"events": len(window), "kept": kept, "print": p.value}
        except Exception as exc:
            print(f"    {iid:<9} {len(window):>5} events → NO PRINT ({str(exc)[:60]})")
            out[iid] = {"events": len(window), "print": None}
    return out


def main() -> None:
    s = get_settings()
    print("Real-tape audit — what live Arc settlement flow yields as an index")
    print(f"  rpc {s.arc_rpc_url}")
    print(f"  usdc {s.usdc_address}  registry {s.registry_address or '(unset)'}")
    print(f"  published tape source: {s.tape_source}")

    legacy = _audit("legacy decode (price pinned to the reference level)", False, BLOCKS)
    attested = _audit("attested-market decode (price = notional / published unit qty)", True, BLOCKS)

    print("\n  verdict")
    print("  -------")
    priced = [iid for iid, r in attested.items() if r.get("print") is not None]
    if not priced:
        print("    No index can be published from real Arc flow in this window.")
        print("    The simulated tape is not a convenience — it is the only source that")
        print("    produces a number, and the honest label on the published index is 'sim'.")
    else:
        print(f"    Real flow supports a print for: {', '.join(priced)}.")
        print("    Consider publishing these as a labelled real-tape series ALONGSIDE the")
        print("    simulated index rather than replacing it — the counts above are the")
        print("    honest measure of how thin that signal is.")
    if legacy and not any(r.get("print") for r in legacy.values()):
        print("    The legacy decode produces no usable price, as documented — it recovers")
        print("    notional and fabricates no price signal, by design.")
    sys.exit(0)


if __name__ == "__main__":
    main()
