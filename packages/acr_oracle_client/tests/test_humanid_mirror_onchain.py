"""HumanIdMirror round-trip — runs only when a local anvil node is reachable.

Deploys the real compiled contract and drives it through `HumanIdMirrorClient`
exactly as `scripts/resolve_humans.py` does. Skipped (not failed) without a node,
so the default `make test` stays hermetic.

The claims under test are the ones only a real chain can settle: that a repeat
resolution is a no-op rather than a revert (the resolver is a loop that reruns),
that provenance cannot be flipped from sandbox to verified, and that our
off-chain EIP-712 encoding produces the byte-identical digest the contract
computes — asserted against `resolutionDigest` rather than against a
reimplementation of it, because agreeing with ourselves proves nothing.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from acr_oracle_client.humanid import HumanIdMirrorClient, cluster_id, salt_commitment
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
ARTIFACT = _OUT / "HumanIdMirror.sol/HumanIdMirror.json"

SALT = "0x" + "22" * 32
NULLIFIER = "0x" + "11" * 32
WALLET_A = "0x1111111111111111111111111111111111111111"
WALLET_B = "0x2222222222222222222222222222222222222222"


def _deploy(w3, acct):
    art = json.loads(ARTIFACT.read_text())
    c = w3.eth.contract(abi=art["abi"], bytecode=art["bytecode"]["object"])
    # The constructor takes the salt COMMITMENT and reverts on zero — the salt
    # itself must never reach a constructor argument, because those land in
    # contracts/broadcast/, which is committed.
    tx = c.constructor(salt_commitment(SALT)).build_transaction(
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


def _client(address: str, key: str = ANVIL_KEY) -> HumanIdMirrorClient:
    return HumanIdMirrorClient(
        rpc_url=RPC, mirror_address=address, signer=LocalKeySigner(key)
    )


@pytest.mark.skipif(not ARTIFACT.exists(), reason="contracts not built (run forge build)")
def test_a_resolution_round_trips_and_a_repeat_is_a_no_op():
    conn = _anvil()
    if conn is None:
        pytest.skip(f"anvil not reachable at {RPC}")
    w3, acct = conn
    client = _client(_deploy(w3, acct))
    assert client.configured()

    # The CHAIN's window, not this host's clock — `record` compares against
    # block.timestamp and a drifted clock would revert for an unrelated-looking
    # reason.
    window = client.chain_window()
    cluster = cluster_id(NULLIFIER, SALT, window)

    assert client.cluster_of(WALLET_A, window) is None, "nothing resolved yet"
    client.record(WALLET_A, cluster, window, sandbox=True)
    assert client.cluster_of(WALLET_A, window) == cluster

    # The resolver reruns; "already correct" must not read as "failed".
    client.record(WALLET_A, cluster, window, sandbox=True)
    assert client.cluster_of(WALLET_A, window) == cluster


@pytest.mark.skipif(not ARTIFACT.exists(), reason="contracts not built (run forge build)")
def test_a_fleet_shares_one_cluster():
    conn = _anvil()
    if conn is None:
        pytest.skip(f"anvil not reachable at {RPC}")
    w3, acct = conn
    client = _client(_deploy(w3, acct))
    window = client.chain_window()
    cluster = cluster_id(NULLIFIER, SALT, window)

    client.record(WALLET_A, cluster, window, sandbox=True)
    client.record(WALLET_B, cluster, window, sandbox=True)
    assert client.cluster_of(WALLET_A, window) == client.cluster_of(WALLET_B, window)


@pytest.mark.skipif(not ARTIFACT.exists(), reason="contracts not built (run forge build)")
def test_provenance_cannot_be_laundered_from_sandbox_to_verified():
    """Against the real `require`, not a stub that would only agree with itself."""
    conn = _anvil()
    if conn is None:
        pytest.skip(f"anvil not reachable at {RPC}")
    w3, acct = conn
    client = _client(_deploy(w3, acct))
    window = client.chain_window()
    cluster = cluster_id(NULLIFIER, SALT, window)

    client.record(WALLET_A, cluster, window, sandbox=True)
    with pytest.raises(Exception):  # noqa: B017 — reverts "provenance mismatch"
        client.record(WALLET_B, cluster, window, sandbox=False)


@pytest.mark.skipif(not ARTIFACT.exists(), reason="contracts not built (run forge build)")
def test_our_digest_is_the_contracts_digest():
    """The check that stops a silent encoding drift.

    `record` refuses to broadcast unless our EIP-712 digest equals the one
    `resolutionDigest` computes. Feed it a client whose salt-derived cluster is
    fine but whose signer is not authorized, and the failure must be the
    contract's "bad signer" — proving the digest agreed and the SIGNATURE was
    what the chain rejected.
    """
    conn = _anvil()
    if conn is None:
        pytest.skip(f"anvil not reachable at {RPC}")
    w3, acct = conn
    address = _deploy(w3, acct)
    window = _client(address).chain_window()
    cluster = cluster_id(NULLIFIER, SALT, window)

    impostor = _client(address, key="0x" + "11" * 32)
    with pytest.raises(Exception) as exc:  # noqa: B017
        impostor.record(WALLET_A, cluster, window, sandbox=True)
    assert "disagrees with the contract" not in str(exc.value)


@pytest.mark.skipif(not ARTIFACT.exists(), reason="contracts not built (run forge build)")
def test_a_wrong_salt_is_caught_before_it_writes_anything():
    """A wrong salt derives clusters that match nothing, so every surface would
    read "no humans" while every transaction succeeded. Loud beats silent."""
    conn = _anvil()
    if conn is None:
        pytest.skip(f"anvil not reachable at {RPC}")
    w3, acct = conn
    client = _client(_deploy(w3, acct))
    assert client.salt_matches(SALT) is True
    assert client.salt_matches("0x" + "ee" * 32) is False


@pytest.mark.skipif(not ARTIFACT.exists(), reason="contracts not built (run forge build)")
def test_an_unauthorized_signer_is_caught_before_anything_is_signed():
    """The gap a digest check cannot close.

    A digest proves our ENCODING matches the contract's. Authorization is a
    different fact, and a dry run that checked only the first would report
    success for a key the contract will reject — costing a reverted transaction
    whose reason ("bad signer") points at the signature rather than at the
    signer set. This was found against a real deployment, not imagined.
    """
    conn = _anvil()
    if conn is None:
        pytest.skip(f"anvil not reachable at {RPC}")
    w3, acct = conn
    address = _deploy(w3, acct)

    # The deployer is a signer by construction…
    assert _client(address).signer_authorized() is True
    # …and a valid key the contract has never heard of is not.
    stranger = _client(address, key="0x" + "11" * 32)
    assert stranger.signer_authorized() is False


# --- reading must not require a write credential ------------------------------


def test_a_read_needs_an_address_and_a_write_needs_a_key():
    """`readable()` and `configured()` are different questions.

    While every view gated on `configured()`, `cluster_of` returned None whenever no
    WRITE key was present — and None there means "this wallet belongs to no human".
    A missing credential was therefore indistinguishable from an unresolved wallet,
    so a read-only deployment reported nobody as verified while every call succeeded.
    Found by a demo, not by a test, which is why this one exists.
    """
    from acr_core.config import ACRSettings
    from acr_oracle_client.humanid import HumanIdMirrorClient

    s = ACRSettings(_env_file=None)
    addr = "0x" + "11" * 20

    reader = HumanIdMirrorClient(mirror_address=addr, signer=None, settings=s)
    assert reader.readable() is True, "an address is enough to call a view"
    assert reader.configured() is False, "but not enough to sign a transaction"

    nowhere = HumanIdMirrorClient(mirror_address=None, signer=None, settings=s)
    assert nowhere.readable() is False
    assert nowhere.configured() is False


def test_the_gate_verifies_a_claim_against_a_mirror_it_can_only_read():
    """The consumer side, asserted as BEHAVIOUR rather than as source text.

    A mirror that is readable and not writable must still get its claim checked.
    The first version of this test grepped `_check_human` for "configured()" and
    failed on a COMMENT that mentioned it — a test of prose, which would have gone
    green the moment someone reworded the explanation and red without a bug.
    """
    from acr_core.config import ACRSettings
    from index_api.agentgate import AgentGate

    CLUSTER = "0x" + "ab" * 32

    class ReadOnlyMirror:
        """Exactly the deployment this fix is for: an address, no key."""

        def readable(self) -> bool:
            return True

        def configured(self) -> bool:
            return False

        def cluster_of(self, wallet: str, window: int):  # noqa: ARG002
            return bytes.fromhex(CLUSTER[2:])

    s = ACRSettings(_env_file=None, agent_audience="acr-index-api")
    gate = AgentGate(settings=s, mirror=ReadOnlyMirror())

    from acr_oracle_client.agentcard import mint
    from acr_oracle_client.signer import LocalKeySigner

    signer = LocalKeySigner("0x" + "22" * 32)
    card = mint(signer.address, name="n", role="reader",
                audience="acr-index-api", ttl_s=300, human_cluster=CLUSTER)
    cluster, note = gate._check_human(card)

    assert cluster == CLUSTER, "a readable mirror must confirm the claim"
    assert note == "", f"no excuse expected, got {note!r}"


@pytest.mark.skipif(not ARTIFACT.exists(), reason="contracts not built (run forge build)")
def test_resolve_window_records_once_then_reports_already():
    """The loop the script AND the press run (`resolve_window`), against the real
    contract: a second pass over the same window writes nothing, and a dry run
    signs and checks without broadcasting."""
    from acr_oracle_client.humanid import resolve_window

    conn = _anvil()
    if conn is None:
        pytest.skip(f"anvil not reachable at {RPC}")
    w3, acct = conn
    client = _client(_deploy(w3, acct))
    window = client.chain_window()
    pairs = [(WALLET_A, NULLIFIER, True), (WALLET_B, NULLIFIER, True)]

    first = list(resolve_window(client, pairs, SALT, window, dry_run=False))
    assert [r.status for r in first] == ["recorded", "recorded"]
    assert all(r.tx for r in first)
    want = cluster_id(NULLIFIER, SALT, window)
    assert all(client.cluster_of(w, window) == want for w, _, _ in pairs)

    again = list(resolve_window(client, pairs, SALT, window, dry_run=False))
    assert [r.status for r in again] == ["already", "already"] and not any(r.tx for r in again)

    fresh = "0x" + "cd" * 20
    (dry,) = resolve_window(client, [(fresh, NULLIFIER, True)], SALT, window, dry_run=True)
    assert dry.status == "dry-run" and dry.tx is None
    assert client.cluster_of(fresh, window) is None, "a dry run must not write"
