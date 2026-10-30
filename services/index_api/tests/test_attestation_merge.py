"""PrintStore merges on-chain AttestationRegistry records into the estimator set."""

from __future__ import annotations

import acr_oracle_client
from acr_core import ModelClass, SellerAttestation, Service
from acr_sim import SimConfig
from acr_tape import SimSource
from index_api.store import PrintStore


def _small_store() -> PrintStore:
    return PrintStore(source=SimSource(config=SimConfig(seed=1, horizon=3600.0, events_per_service=200)))


def _onchain():
    return [
        SellerAttestation(
            seller="0xOnChainSeller", service=Service.GPU, model_class=ModelClass.FRONTIER,
            latency_slo_ms=120.0, schema_id="gpu@1",
        )
    ]


def test_merges_onchain_when_registry_configured(monkeypatch):
    class FakeReg:
        def all_attestations(self):
            return _onchain()

    monkeypatch.setattr(acr_oracle_client, "RegistryClient", lambda *a, **k: FakeReg())
    st = _small_store()
    monkeypatch.setattr(st.settings, "registry_address", "0xREGISTRY")
    st._load()
    sellers = {a.seller for a in st._attest}
    assert "0xOnChainSeller" in sellers  # real on-chain metadata is now in the estimator set
    assert len(st._attest) >= 1


def test_no_registry_leaves_attestations_untouched(monkeypatch):
    st = _small_store()
    monkeypatch.setattr(st.settings, "registry_address", "")  # scrubbed default
    st._load()
    # Only the tape's own (sim) attestations — no chain read attempted.
    assert all(a.seller != "0xOnChainSeller" for a in st._attest)


def test_onchain_overrides_tape_for_same_seller(monkeypatch):
    # An on-chain record for a seller the tape also knows wins (freshest truth).
    tape_seller = None
    st = _small_store()
    st._load()  # populate once to discover a real sim seller id
    if st._attest:
        tape_seller = st._attest[0].seller
    st._events = None  # force a reload

    override = [
        SellerAttestation(
            seller=tape_seller or "0xSeller", service=Service.DATA, model_class=ModelClass.SMALL,
            latency_slo_ms=999.0, schema_id="override@1",
        )
    ]

    class FakeReg:
        def all_attestations(self):
            return override

    monkeypatch.setattr(acr_oracle_client, "RegistryClient", lambda *a, **k: FakeReg())
    monkeypatch.setattr(st.settings, "registry_address", "0xREGISTRY")
    st._load()
    match = [a for a in st._attest if a.seller == (tape_seller or "0xSeller")]
    assert match and match[0].schema_id == "override@1"
