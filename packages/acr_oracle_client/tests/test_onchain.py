"""On-chain integration test — runs only when a local anvil node is reachable.

Deploys ACROracle from the forge artifact, posts a print, reads it back, and
asserts the round-trip matches. Skipped (not failed) when anvil isn't up or the
contracts haven't been built, so the default `make test` stays hermetic.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from acr_core import ACRPrint
from acr_oracle_client import OracleClient

#: Overridable, because this machine runs more than one project. A parallel
#: session held 127.0.0.1:8545 with its own anvil for seven hours, and chain id
#: cannot tell two anvils apart — both are 31337 — so a run against the wrong
#: one looks exactly like a run against the right one, and deploys into whatever
#: state the other session has built up. CI starts anvil on the default and is
#: unaffected; a second chain just needs ACR_TEST_RPC.
RPC = os.environ.get("ACR_TEST_RPC", "http://127.0.0.1:8545")
ANVIL_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
_OUT = Path(__file__).resolve().parents[3] / "contracts/out"
ARTIFACT = _OUT / "ACROracle.sol/ACROracle.json"
REGISTRY_ARTIFACT = _OUT / "AttestationRegistry.sol/AttestationRegistry.json"


def _deploy(w3, acct, artifact_path):
    art = json.loads(artifact_path.read_text())
    c = w3.eth.contract(abi=art["abi"], bytecode=art["bytecode"]["object"])
    tx = c.constructor().build_transaction(
        {"from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
         "gas": 3_000_000, "chainId": w3.eth.chain_id}
    )
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    rcpt = w3.eth.wait_for_transaction_receipt(w3.eth.send_raw_transaction(raw), timeout=30)
    return rcpt.contractAddress


def _anvil():
    try:
        from eth_account import Account
        from web3 import Web3

        w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 2}))
        if not w3.is_connected():
            return None
        return w3, Account.from_key(ANVIL_KEY)
    except Exception:
        return None


@pytest.mark.skipif(not ARTIFACT.exists(), reason="contracts not built (run forge build)")
def test_onchain_post_and_readback():
    conn = _anvil()
    if conn is None:
        pytest.skip(f"anvil not reachable at {RPC}")
    w3, acct = conn
    addr = _deploy(w3, acct, ARTIFACT)

    client = OracleClient(rpc_url=RPC, oracle_address=addr, private_key=ANVIL_KEY)
    p = ACRPrint(
        index_id="ACR-INF", ts=3600.0, value=0.5, ci_lo=0.49, ci_hi=0.51,
        attack_cost_per_bp=1234.0, n_obs=100,
    )
    txh = client.post(p)
    assert txh is not None
    back = client.read_latest("ACR-INF")
    assert back is not None
    assert abs(back["value"] - 0.5) < 1e-9
    assert back["ci_lo"] <= back["value"] <= back["ci_hi"]
    assert back["attack_cost_per_bp"] > 0


@pytest.mark.skipif(not REGISTRY_ARTIFACT.exists(), reason="contracts not built")
def test_onchain_attestation_flywheel():
    conn = _anvil()
    if conn is None:
        pytest.skip(f"anvil not reachable at {RPC}")
    w3, acct = conn
    from acr_core import ModelClass, SellerAttestation, Service
    from acr_oracle_client import RegistryClient

    reg_addr = _deploy(w3, acct, REGISTRY_ARTIFACT)
    # The deployer (acct) attests itself, then we read the metadata back.
    rc = RegistryClient(rpc_url=RPC, registry_address=reg_addr, private_key=ANVIL_KEY)
    att = SellerAttestation(
        seller=acct.address,
        service=Service.GPU,
        model_class=ModelClass.FRONTIER,
        latency_slo_ms=120.0,
        schema_id="gpu.v2",
    )
    assert rc.attest(att) is not None
    back = rc.get_attestation(acct.address)
    assert back is not None
    assert back.service == Service.GPU
    assert back.model_class == ModelClass.FRONTIER
    assert back.latency_slo_ms == 120.0
    assert back.schema_id == "gpu.v2"
    assert len(rc.all_attestations()) == 1
