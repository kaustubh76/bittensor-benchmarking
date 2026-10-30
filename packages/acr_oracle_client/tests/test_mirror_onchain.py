"""ReceiptMirror round-trip — runs only when a local anvil node is reachable.

Deploys the real compiled ReceiptMirror, mirrors a settlement through
``MirrorClient`` exactly as the keeper does, and reads the record back. Skipped
(not failed) without a node, so the default `make test` stays hermetic.

The claim under test is the one the two-phase design exists to make: the arrival
anchor is committed in phase 1 and is out of reach in phase 2. Asserted against
a real chain rather than a mock, because the guard that enforces it is a
`require` in Solidity — a stub would only prove the stub agrees with itself.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from acr_core import Service
from acr_oracle_client import MirrorClient, settlement_id
from acr_oracle_client.signer import LocalKeySigner

#: Overridable, because this machine runs more than one project. A parallel
#: session held 127.0.0.1:8545 with its own anvil for seven hours, and chain id
#: cannot tell two anvils apart — both are 31337 — so a run against the wrong
#: one looks exactly like a run against the right one, and deploys into whatever
#: state the other session has built up. CI starts anvil on the default and is
#: unaffected; a second chain just needs ACR_TEST_RPC.
RPC = os.environ.get("ACR_TEST_RPC", "http://127.0.0.1:8545")
ANVIL_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
_OUT = Path(__file__).resolve().parents[3] / "contracts/out"
ARTIFACT = _OUT / "ReceiptMirror.sol/ReceiptMirror.json"

PAYER = "0x1111111111111111111111111111111111111111"
SELLER = "0x2222222222222222222222222222222222222222"


def _deploy(w3, acct):
    art = json.loads(ARTIFACT.read_text())
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


def _client(address: str) -> MirrorClient:
    # The anvil dev key deploys, so it is already an authorized signer — the
    # constructor trusts its deployer, same as FeedAccessAttestor.
    return MirrorClient(
        rpc_url=RPC, mirror_address=address, signer=LocalKeySigner(ANVIL_KEY)
    )


@pytest.mark.skipif(not ARTIFACT.exists(), reason="contracts not built (run forge build)")
def test_mirror_round_trip_and_the_arrival_anchor_survives_finalize():
    conn = _anvil()
    if conn is None:
        pytest.skip(f"anvil not reachable at {RPC}")
    w3, acct = conn
    address = _deploy(w3, acct)
    client = _client(address)
    assert client.configured()

    tx_ref = f"gateway-{time.time()}"
    # Anchored to the CHAIN's clock, not the wall's. The contract's freshness
    # guard compares against block.timestamp, and other suites in this run warp
    # anvil forward to test futures expiry — a wall-clock settledAt would then
    # look hours stale and revert, for reasons nothing to do with this test.
    settled_at = int(w3.eth.get_block("latest")["timestamp"]) - 30

    assert client.state_for(tx_ref) is None, "nothing mirrored yet"

    client.open_settlement(
        tx_ref=tx_ref, payer=PAYER, seller=SELLER, index_id="ACR-INF",
        amount_usdc=0.005229, settled_at=settled_at, synthetic=True,
    )
    state = client.state_for(tx_ref)
    assert state == {"opened": True, "finalized": False, "settled_at": settled_at}

    client.finalize_settlement(tx_ref=tx_ref, service=Service.INFERENCE, quantity=0.01)
    state = client.state_for(tx_ref)
    assert state["finalized"] is True
    # The whole point of splitting the write: phase 2 cannot reach settledAt.
    assert state["settled_at"] == settled_at

    # And the amounts landed in their declared scales — USDC 1e6, quantity WAD.
    row = client._contract().functions.settlements(settlement_id(tx_ref)).call()
    assert row[9] == 5229, "amountUsdc is USDC 1e6"
    assert row[10] == 10**16, "quantity is WAD 1e18 (0.01 units)"


@pytest.mark.skipif(not ARTIFACT.exists(), reason="contracts not built (run forge build)")
def test_a_signature_from_an_unauthorized_signer_is_refused_on_chain():
    """The security model, against the real `ecrecover` rather than a stub."""
    conn = _anvil()
    if conn is None:
        pytest.skip(f"anvil not reachable at {RPC}")
    w3, acct = conn
    address = _deploy(w3, acct)
    impostor = MirrorClient(
        rpc_url=RPC,
        mirror_address=address,
        # A valid key the contract has never authorized.
        signer=LocalKeySigner("0x" + "11" * 32),
    )
    with pytest.raises(Exception):  # noqa: B017 — reverts as "bad signer"
        impostor.open_settlement(
            tx_ref="forged", payer=PAYER, seller=SELLER, index_id="ACR-INF",
            amount_usdc=0.005,
            settled_at=int(w3.eth.get_block("latest")["timestamp"]) - 10,
        )
