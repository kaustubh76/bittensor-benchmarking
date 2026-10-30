"""Demo seller identities are deterministic and shared (attest ↔ seed alignment)."""

from __future__ import annotations

from acr_core import Service
from acr_oracle_client.demo_sellers import DEMO_SELLERS


def test_addresses_are_deterministic_and_unique():
    addrs = [d.address for d in DEMO_SELLERS]
    assert all(a.startswith("0x") and len(a) == 42 for a in addrs)
    assert len(set(addrs)) == len(addrs)  # distinct sellers
    # Stable across calls (same key → same address) — attest and seed must agree.
    assert addrs == [d.signer().address for d in DEMO_SELLERS]


def test_spread_covers_all_services():
    assert {d.service for d in DEMO_SELLERS} == {Service.INFERENCE, Service.GPU, Service.DATA}
