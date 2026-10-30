"""ACROracleV2 round-trip — runs only when a local anvil node is reachable.

The claim under test is digest parity: the Python client's EIP-712 encoding of
ten fields must produce the exact digest the contract computes. A mismatch does
not fail loudly — it recovers to a stranger, and the only symptom on chain is
"bad signer" on a post that should have worked. Checked against the contract's
own `printDigest`, not against a reimplementation of the hash.

Skipped (not failed) without a node, so the default `make test` stays hermetic.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from acr_core import ACRPrint
from acr_oracle_client import OracleClient
from acr_oracle_client.client import ORACLE_V2, PostPayload, print_domain

#: Overridable, because this machine runs more than one project. A parallel
#: session held 127.0.0.1:8545 with its own anvil for seven hours, and chain id
#: cannot tell two anvils apart — both are 31337 — so a run against the wrong
#: one looks exactly like a run against the right one, and deploys into whatever
#: state the other session has built up. CI starts anvil on the default and is
#: unaffected; a second chain just needs ACR_TEST_RPC.
RPC = os.environ.get("ACR_TEST_RPC", "http://127.0.0.1:8545")
ANVIL_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
_OUT = Path(__file__).resolve().parents[3] / "contracts/out"
ARTIFACT = _OUT / "ACROracleV2.sol/ACROracleV2.json"

POLICY = "0x" + "ab" * 32
# The audit's C2 fix: a v2 oracle is built with a per-print move bound (bps),
# the same 2 000 the mainnet deploy script defaults to.
MAX_MOVE_BPS = 2_000


def _deploy(w3, acct):
    art = json.loads(ARTIFACT.read_text())
    c = w3.eth.contract(abi=art["abi"], bytecode=art["bytecode"]["object"])
    tx = c.constructor(MAX_MOVE_BPS).build_transaction(
        {"from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
         "gas": 4_000_000, "chainId": w3.eth.chain_id}
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


def _print_at(ts: int, value: float = 0.5) -> ACRPrint:
    return ACRPrint(
        index_id="ACR-INF", ts=float(ts), value=value, ci_lo=value - 0.01, ci_hi=value + 0.01,
        attack_cost_per_bp=1234.0, n_obs=100,
        policy_hash=POLICY, human_adjusted_bound=None,
        window_start=float(ts - 3600), window_end=float(ts),
    )


@pytest.mark.skipif(not ARTIFACT.exists(), reason="contracts not built (run forge build)")
def test_v2_digest_parity_and_round_trip():
    conn = _anvil()
    if conn is None:
        pytest.skip(f"anvil not reachable at {RPC}")
    w3, acct = conn
    address = _deploy(w3, acct)

    # Chain time, not wall time: other suites warp anvil forward for futures
    # expiry, so a wall-clock economic timestamp would trip the skew guard.
    now = int(w3.eth.get_block("latest")["timestamp"])
    p = _print_at(now - 60)

    client = OracleClient(
        rpc_url=RPC, oracle_address=address, private_key=ANVIL_KEY, schema=ORACLE_V2
    )
    payload = PostPayload.from_print(p)

    # The parity check. If the Python encoding and the contract's disagree, the
    # signature recovers to a stranger and the post reverts "bad signer".
    from eth_account.messages import encode_typed_data
    from eth_utils import keccak

    domain = print_domain(w3.eth.chain_id, address, ORACLE_V2.version)
    signable = encode_typed_data(
        domain_data=domain, message_types=ORACLE_V2.types,
        message_data=payload.message(ORACLE_V2),
    )
    # SignableMessage carries the PARTS, not the digest: version 0x01, header =
    # the domain separator, body = the struct hash. The digest the contract
    # signs over is keccak(0x19 || 0x01 || domainSeparator || structHash).
    ours = keccak(b"\x19" + signable.version + signable.header + signable.body)
    contract = client._contract()
    theirs = contract.functions.printDigest(payload.as_args(ORACLE_V2)).call()
    assert ours == bytes(theirs), "python and contract disagree on the v2 digest"

    tx = client.post(p)
    assert tx is not None

    meta = contract.functions.printMeta(b"ACR-INF".ljust(32, b"\x00")).call()
    assert "0x" + meta[0].hex() == POLICY
    assert meta[1] == int(p.window_start)
    assert meta[2] == int(p.window_end)
    # None was carried to chain as the "not computed" sentinel, not as the
    # wallet bound — publishing that would claim humans cost the same as wallets.
    assert meta[3] == 0

    # C2 on the wire: a signer whose key was stolen can walk the rate, not jump
    # it. A 25% move against a 20% bound reverts; a 10% move lands.
    from web3.exceptions import ContractLogicError

    with pytest.raises(ContractLogicError, match="move exceeds bound"):
        client.post(_print_at(now - 30, value=0.625))
    assert client.post(_print_at(now - 30, value=0.55)) is not None


@pytest.mark.skipif(not ARTIFACT.exists(), reason="contracts not built (run forge build)")
def test_v2_refuses_a_print_with_no_policy():
    """The client fails before spending gas, and says why."""
    p = _print_at(1_785_000_000)
    p = p.model_copy(update={"policy_hash": None})
    with pytest.raises(ValueError, match="cleaning policy"):
        PostPayload.from_print(p).message(ORACLE_V2)


@pytest.mark.skipif(not ARTIFACT.exists(), reason="contracts not built (run forge build)")
def test_v2_refuses_a_print_with_no_window():
    p = _print_at(1_785_000_000).model_copy(update={"window_start": None})
    with pytest.raises(ValueError, match="window"):
        PostPayload.from_print(p).message(ORACLE_V2)


def test_a_v1_payload_is_unchanged_by_the_v2_work():
    """The regression that would be silent: v1 must still sign the same six
    fields in the same order, because a live oracle verifies them."""
    p = _print_at(1_785_000_000)
    assert list(PostPayload.from_print(p).message().keys()) == [
        "indexId", "value", "ciLo", "ciHi", "attackCostPerBp", "timestamp",
    ]
