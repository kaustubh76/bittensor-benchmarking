"""Client for ``AttestationRegistry.sol`` — the econometric flywheel, on-chain.

Sellers attest EIP-712 service metadata (model class, latency SLO, schema);
Pillar 2's hedonic regression reads that metadata back as its feature matrix.
This client writes attestations and reads them back as ``SellerAttestation``
domain objects, so the same estimator can source quality features from the
simulator *or* from the live registry. Offline-tolerant, like ``OracleClient``.
"""

from __future__ import annotations

import logging

from acr_core import ModelClass, SellerAttestation, Service, get_settings

from .signer import Signer, build_signer

log = logging.getLogger("acr_oracle_client.registry")

#: EIP-712 typed-data schema for a meta-attestation — matches
#: ``AttestationRegistry.ATTESTATION_TYPEHASH``.
ATTESTATION_TYPES = {
    "Attestation": [
        {"name": "seller", "type": "address"},
        {"name": "service", "type": "uint8"},
        {"name": "modelClass", "type": "uint8"},
        {"name": "latencySloMs", "type": "uint32"},
        {"name": "schemaId", "type": "bytes32"},
        {"name": "nonce", "type": "uint256"},
        {"name": "deadline", "type": "uint64"},
    ]
}


def registry_domain(chain_id: int, registry_address: str) -> dict:
    """The EIP-712 domain ``AttestationRegistry`` constructs (name/version fixed)."""
    from web3 import Web3

    return {
        "name": "ACR AttestationRegistry",
        "version": "1",
        "chainId": int(chain_id),
        "verifyingContract": Web3.to_checksum_address(registry_address),
    }

#: Contract encodings (must match AttestationRegistry.sol / ArcSource).
SERVICE_TO_CODE: dict[Service, int] = {Service.INFERENCE: 0, Service.GPU: 1, Service.DATA: 2}
CODE_TO_SERVICE: dict[int, Service] = {v: k for k, v in SERVICE_TO_CODE.items()}
CLASS_TO_CODE: dict[ModelClass, int] = {
    ModelClass.FRONTIER: 0,
    ModelClass.MID: 1,
    ModelClass.SMALL: 2,
    ModelClass.OPEN: 3,
}
CODE_TO_CLASS: dict[int, ModelClass] = {v: k for k, v in CLASS_TO_CODE.items()}

REGISTRY_ABI = [
    {
        "type": "function",
        "name": "attest",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "service", "type": "uint8"},
            {"name": "modelClass", "type": "uint8"},
            {"name": "latencySloMs", "type": "uint32"},
            {"name": "schemaId", "type": "bytes32"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "getAttestation",
        "stateMutability": "view",
        "inputs": [{"name": "seller", "type": "address"}],
        "outputs": [
            {
                "type": "tuple",
                "name": "",
                "components": [
                    {"name": "seller", "type": "address"},
                    {"name": "service", "type": "uint8"},
                    {"name": "modelClass", "type": "uint8"},
                    {"name": "latencySloMs", "type": "uint32"},
                    {"name": "schemaId", "type": "bytes32"},
                    {"name": "timestamp", "type": "uint64"},
                    {"name": "exists", "type": "bool"},
                ],
            }
        ],
    },
    {
        "type": "function",
        "name": "attestWithSig",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "seller", "type": "address"},
            {"name": "service", "type": "uint8"},
            {"name": "modelClass", "type": "uint8"},
            {"name": "latencySloMs", "type": "uint32"},
            {"name": "schemaId", "type": "bytes32"},
            {"name": "deadline", "type": "uint64"},
            {"name": "v", "type": "uint8"},
            {"name": "r", "type": "bytes32"},
            {"name": "s", "type": "bytes32"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "nonces",
        "stateMutability": "view",
        "inputs": [{"name": "", "type": "address"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "sellerCount",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "sellerAt",
        "stateMutability": "view",
        "inputs": [{"name": "i", "type": "uint256"}],
        "outputs": [{"name": "", "type": "address"}],
    },
]


def schema_to_bytes32(schema_id: str) -> bytes:
    raw = schema_id.encode("utf-8")[:32]
    return raw.ljust(32, b"\x00")


def bytes32_to_schema(b: bytes) -> str:
    return b.rstrip(b"\x00").decode("utf-8", errors="replace")


class RegistryClient:
    def __init__(
        self,
        rpc_url: str | None = None,
        registry_address: str | None = None,
        private_key: str | None = None,
        signer: Signer | None = None,
    ) -> None:
        settings = get_settings()
        self.rpc_url = rpc_url or settings.arc_rpc_url
        self.registry_address = registry_address or (settings.registry_address or None)
        self.private_key = private_key
        self.signer = signer or build_signer(settings, private_key=private_key)
        self._w3 = None

    def _connect(self):
        if self._w3 is not None:
            return self._w3
        try:
            from web3 import Web3

            self._w3 = Web3(Web3.HTTPProvider(self.rpc_url, request_kwargs={"timeout": 5}))
        except Exception as exc:  # pragma: no cover - env dependent
            log.warning("RegistryClient: web3 unavailable (%s)", exc)
            self._w3 = None
        return self._w3

    def _contract(self):  # pragma: no cover - live chain
        w3 = self._connect()
        return w3.eth.contract(
            address=w3.to_checksum_address(self.registry_address), abi=REGISTRY_ABI
        )

    def connected(self) -> bool:
        w3 = self._connect()
        if w3 is None or not self.registry_address:
            return False
        try:
            return bool(w3.is_connected())
        except Exception:  # pragma: no cover - env dependent
            return False

    def attest(self, a: SellerAttestation, wait: bool = True) -> str | None:  # pragma: no cover - live chain
        """Submit an attestation as ``msg.sender`` (must equal ``a.seller``)."""
        if not self.connected() or self.signer is None:
            log.info("RegistryClient offline: would attest %s", a.seller)
            return None
        w3 = self._connect()
        c = self._contract()
        tx = c.functions.attest(
            SERVICE_TO_CODE[a.service],
            CLASS_TO_CODE[a.model_class],
            int(a.latency_slo_ms),
            schema_to_bytes32(a.schema_id),
        ).build_transaction(
            {
                "from": self.signer.address,
                "nonce": w3.eth.get_transaction_count(self.signer.address),
                "chainId": w3.eth.chain_id,
            }
        )
        tx_hash = self.signer.send_transaction(w3, tx)
        if wait:
            rcpt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
            if rcpt.status != 1:
                raise RuntimeError(f"attest reverted for {a.seller}")
        return tx_hash

    def attest_with_sig(
        self,
        a: SellerAttestation,
        deadline: int,
        wait: bool = True,
        seller_signer=None,
    ) -> str | None:  # pragma: no cover - live chain
        """Meta-attest: ``seller_signer`` EIP-712-signs the ``Attestation`` (with
        the seller's on-chain nonce + a deadline) and the relayer (``self.signer``)
        submits + pays gas. This is what ``attestWithSig(seller, …, v,r,s)`` is
        for: one funded relayer can register many seller identities.

        ``seller_signer`` defaults to ``self.signer`` (self-attest, the original
        behaviour). Pass a distinct signer to attest *for another seller* while
        relaying from the funded wallet."""
        if not self.connected() or self.signer is None:
            log.info("RegistryClient offline: would attestWithSig %s", a.seller)
            return None
        w3 = self._connect()
        c = self._contract()
        signer_for_seller = seller_signer or self.signer
        seller = signer_for_seller.address
        nonce = c.functions.nonces(w3.to_checksum_address(seller)).call()
        message = {
            "seller": seller,
            "service": SERVICE_TO_CODE[a.service],
            "modelClass": CLASS_TO_CODE[a.model_class],
            "latencySloMs": int(a.latency_slo_ms),
            "schemaId": schema_to_bytes32(a.schema_id),
            "nonce": int(nonce),
            "deadline": int(deadline),
        }
        v, r, s = signer_for_seller.sign_typed_data(
            registry_domain(w3.eth.chain_id, self.registry_address),
            ATTESTATION_TYPES, message, "Attestation",
        )
        tx = c.functions.attestWithSig(
            seller, message["service"], message["modelClass"], message["latencySloMs"],
            message["schemaId"], int(deadline), v, r, s,
        ).build_transaction(
            {
                "from": self.signer.address,  # the relayer sends + pays gas
                "nonce": w3.eth.get_transaction_count(self.signer.address),
                "chainId": w3.eth.chain_id,
            }
        )
        tx_hash = self.signer.send_transaction(w3, tx)
        if wait:
            rcpt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
            if rcpt.status != 1:
                raise RuntimeError(f"attestWithSig reverted for {a.seller}")
        return tx_hash

    def get_attestation(self, seller: str) -> SellerAttestation | None:  # pragma: no cover - live chain
        if not self.connected():
            return None
        c = self._contract()
        try:
            addr, svc, mc, lat, schema, ts, exists = c.functions.getAttestation(
                c.w3.to_checksum_address(seller)
            ).call()
        except Exception:
            return None
        if not exists:
            return None
        return SellerAttestation(
            seller=addr,
            service=CODE_TO_SERVICE.get(svc, Service.INFERENCE),
            model_class=CODE_TO_CLASS.get(mc, ModelClass.OPEN),
            latency_slo_ms=float(lat),
            schema_id=bytes32_to_schema(schema),
            ts=float(ts),
        )

    def seller_count(self) -> int:  # pragma: no cover - live chain
        """Number of attested sellers on-chain (a single read — cheap proof)."""
        if not self.connected():
            return 0
        return int(self._contract().functions.sellerCount().call())

    def all_attestations(self) -> list[SellerAttestation]:  # pragma: no cover - live chain
        if not self.connected():
            return []
        c = self._contract()
        try:
            n = c.functions.sellerCount().call()
        except Exception:
            return []
        out: list[SellerAttestation] = []
        for i in range(n):
            seller = c.functions.sellerAt(i).call()
            a = self.get_attestation(seller)
            if a is not None:
                out.append(a)
        return out
