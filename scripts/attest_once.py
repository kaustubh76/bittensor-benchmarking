#!/usr/bin/env python
"""Post real seller attestations to ``AttestationRegistry`` on Arc testnet.

The registry is deployed but starts empty, so the marketplace catalog's
``provider.attestation`` block, the sellers page, and the hedonic quality
adjustment have nothing on-chain to read. This registers a small set of seller
identities for real: each is a deterministic ephemeral EOA that EIP-712-signs
its own ``Attestation``, and the funded poster relays + pays gas via
``attestWithSig`` (one funded wallet, many sellers — exactly what the meta-tx
path is for).

Seeded so it is reproducible; re-running is a no-op'ish re-attest (the contract
overwrites a seller's record and the seller nonce advances). Needs
``ACR_REGISTRY_ADDRESS`` + a funded ``ACR_POSTER_PRIVATE_KEY`` (gas is USDC on
Arc). Offline (no registry / no key) it logs what it would do and exits 0.

    ACR_REGISTRY_ADDRESS=0x… uv run python scripts/attest_once.py   # or: make attest-once
"""

from __future__ import annotations

import os
import sys
import time

from acr_core import SellerAttestation, get_settings
from acr_oracle_client import RegistryClient
from acr_oracle_client.demo_sellers import DEMO_SELLERS

FALLBACK_EXPLORER = "https://explorer.arc.io"  # Arc mainnet; the chain profile overrides it

#: Pace between attestations + retry transient RPC errors — the public Arc RPC
#: rate-limits (HTTP 429) back-to-back calls.
PACE_S = 3.0
RETRIES = 4


def _with_retry(fn):
    """Call fn(), retrying on transient RPC errors (429 / timeouts) with backoff."""
    last = None
    for i in range(RETRIES):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - surface after retries
            last = exc
            msg = str(exc).lower()
            if "429" in msg or "too many" in msg or "timeout" in msg or "rate" in msg:
                time.sleep(PACE_S * (i + 1))
                continue
            raise
    raise last


def _explorer(settings) -> str:
    base = getattr(settings, "explorer_base", "") or os.environ.get("ACR_EXPLORER_BASE", "")
    return (base or FALLBACK_EXPLORER).rstrip("/")


def main() -> None:
    s = get_settings()
    explorer = _explorer(s)
    configured = bool(s.registry_address)

    relayer = RegistryClient()  # signer = funded poster key from settings
    if not configured or not relayer.connected() or relayer.signer is None:
        print(
            "\n  registry not configured / offline (ACR_REGISTRY_ADDRESS + a funded "
            "ACR_POSTER_PRIVATE_KEY required) — nothing sent.\n"
        )
        # Still show the seller book that WOULD be attested.
        for d in DEMO_SELLERS:
            print(
                f"  would attest {d.address}  {d.service.name}/{d.model_class.name}  "
                f"slo={d.latency_slo_ms}ms  {d.schema_id}"
            )
        print()
        return

    deadline = int(time.time()) + 3600
    print(f"\n  {'SELLER':<44} {'service/class':<20} tx")
    print("  " + "-" * 100)
    posted = 0
    for i, d in enumerate(DEMO_SELLERS):
        if i:
            time.sleep(PACE_S)  # pace so the public RPC doesn't 429
        seller_signer = d.signer()
        label = f"{d.service.name}/{d.model_class.name}"
        a = SellerAttestation(
            seller=seller_signer.address,
            service=d.service,
            model_class=d.model_class,
            latency_slo_ms=float(d.latency_slo_ms),
            schema_id=d.schema_id,
        )
        try:
            tx = _with_retry(
                lambda a=a, ss=seller_signer: relayer.attest_with_sig(
                    a, deadline=deadline, seller_signer=ss
                )
            )
        except Exception as exc:
            print(f"  {seller_signer.address:<44} {label:<20} ✗ {exc}")
            continue
        if tx is None:
            print(f"  {seller_signer.address:<44} {label:<20} offline")
            continue
        posted += 1
        print(f"  {seller_signer.address:<44} {label:<20} {explorer}/tx/{tx}")

    # Read back the on-chain registry as proof (single call, retryable).
    time.sleep(PACE_S)
    count = _with_retry(relayer.seller_count)
    print(f"\n  registry now holds {count} attestation(s) on-chain.")
    if configured and posted == 0:
        print("\n  ✗ every attestation failed — check the RPC + poster USDC gas balance.\n")
        sys.exit(1)
    print(f"  ✓ attested {posted}/{len(DEMO_SELLERS)} sellers — the catalog's provider.attestation now reads them.\n")


if __name__ == "__main__":
    main()
