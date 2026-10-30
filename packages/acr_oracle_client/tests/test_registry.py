"""Hermetic RegistryClient tests — the relayer≠seller meta-attestation split.

No chain: web3 + contract are faked so we can assert that ``attest_with_sig``
EIP-712-signs as the *seller* while the *relayer* (self.signer) sends and pays.
"""

from __future__ import annotations

import types

from acr_core import ModelClass, SellerAttestation, Service
from acr_oracle_client import RegistryClient


class _FakeSigner:
    def __init__(self, address: str) -> None:
        self.address = address
        self.signed_messages: list[dict] = []
        self.sent_txs: list[dict] = []

    def sign_typed_data(self, domain, types_, message, primary_type):
        self.signed_messages.append(message)
        return 27, b"\x01" * 32, b"\x02" * 32

    def send_transaction(self, w3, tx):
        self.sent_txs.append(tx)
        return "0xdeadbeef"


def _fake_w3():
    eth = types.SimpleNamespace(
        chain_id=5042002,
        get_transaction_count=lambda addr: 0,
    )
    return types.SimpleNamespace(eth=eth, to_checksum_address=lambda a: a)


def _fake_contract():
    def attest_with_sig(*args):
        return types.SimpleNamespace(build_transaction=lambda tx: {**tx, "_args": args})

    functions = types.SimpleNamespace(
        nonces=lambda seller: types.SimpleNamespace(call=lambda: 0),
        attestWithSig=attest_with_sig,
    )
    return types.SimpleNamespace(functions=functions)


def _client(relayer: _FakeSigner) -> RegistryClient:
    rc = RegistryClient(registry_address="0x00000000000000000000000000000000000000A0", signer=relayer)
    rc.connected = lambda: True  # type: ignore[method-assign]
    rc._connect = lambda: _fake_w3()  # type: ignore[method-assign]
    rc._contract = lambda: _fake_contract()  # type: ignore[method-assign]
    return rc


def _att(seller: str) -> SellerAttestation:
    return SellerAttestation(
        seller=seller, service=Service.GPU, model_class=ModelClass.MID,
        latency_slo_ms=500.0, schema_id="gpu/h100@1",
    )


def test_attest_with_sig_relays_for_a_distinct_seller():
    relayer = _FakeSigner("0xRELAYER")
    seller = _FakeSigner("0xSELLER")
    rc = _client(relayer)

    tx = rc.attest_with_sig(_att("0xSELLER"), deadline=9_999_999_999, wait=False, seller_signer=seller)

    assert tx == "0xdeadbeef"
    # The SELLER signs the EIP-712 attestation (seller field = seller address)...
    assert seller.signed_messages and seller.signed_messages[0]["seller"] == "0xSELLER"
    assert not relayer.signed_messages
    # ...but the RELAYER sends the tx and pays gas.
    assert relayer.sent_txs and relayer.sent_txs[0]["from"] == "0xRELAYER"
    assert not seller.sent_txs


def test_attest_with_sig_defaults_to_self_attest():
    signer = _FakeSigner("0xSELF")
    rc = _client(signer)

    rc.attest_with_sig(_att("0xSELF"), deadline=9_999_999_999, wait=False)

    # No seller_signer → the signer is both seller and relayer (original behaviour).
    assert signer.signed_messages[0]["seller"] == "0xSELF"
    assert signer.sent_txs[0]["from"] == "0xSELF"
