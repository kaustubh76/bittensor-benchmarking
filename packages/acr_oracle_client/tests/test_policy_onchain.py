"""PolicyWallet round-trip — runs only when a local anvil node is reachable.

Deploys the real compiled PolicyWallet against a real ERC-20, and spends through
``PolicyClient`` exactly as the operator does. Skipped (not failed) without a
node, so the default ``make test`` stays hermetic.

The claim that matters most is the one no mock can make: **the digest Python
signs is byte-for-byte the digest the contract recovers against.** An EIP-712
type mismatch between the two sides recovers to a stranger, so the only symptom
would be "not owner signature" on an approval a human really did give — after
the gas was spent, and with no way to tell it from a forgery.

The second claim is that the authority ladder holds against Solidity rather than
against Python's opinion of it: below the threshold the agent pays, at the
threshold it cannot, and with the owner's signature it can.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from acr_oracle_client.policy import (
    APPROVAL_TYPES,
    USDC,
    PolicyClient,
    category_id,
    decision_hash,
)
from acr_oracle_client.signer import LocalKeySigner
from web3.logs import DISCARD

#: Overridable, because this machine runs more than one project. A parallel
#: session held 127.0.0.1:8545 with its own anvil for seven hours, and chain id
#: cannot tell two anvils apart — both are 31337 — so a run against the wrong
#: one looks exactly like a run against the right one, and deploys into whatever
#: state the other session has built up. CI starts anvil on the default and is
#: unaffected; a second chain just needs ACR_TEST_RPC.
RPC = os.environ.get("ACR_TEST_RPC", "http://127.0.0.1:8545")
# anvil's first two deterministic keys: the owner and the agent must be
# different parties or the threshold proves nothing.
OWNER_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
AGENT_KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"

_OUT = Path(__file__).resolve().parents[3] / "contracts/out"
WALLET_ARTIFACT = _OUT / "PolicyWallet.sol/PolicyWallet.json"
USDC_ARTIFACT = _OUT / "MockUSDC.sol/MockUSDC.json"

VENDOR = "0x3333333333333333333333333333333333333333"
CATEGORY = "infra"
CAP = 1_000 * USDC
PER_TX = 100 * USDC
MONTH = 30 * 24 * 3600


def _anvil():
    try:
        from eth_account import Account
        from web3 import Web3

        w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 2}))
        if not w3.is_connected():
            return None
        return w3, Account.from_key(OWNER_KEY), Account.from_key(AGENT_KEY)
    except Exception:
        return None


def _send(w3, acct, tx):
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    return w3.eth.wait_for_transaction_receipt(w3.eth.send_raw_transaction(raw), timeout=30)


def _deploy(w3, acct, artifact: Path, *args):
    art = json.loads(artifact.read_text())
    c = w3.eth.contract(abi=art["abi"], bytecode=art["bytecode"]["object"])
    tx = c.constructor(*args).build_transaction(
        {
            "from": acct.address,
            "nonce": w3.eth.get_transaction_count(acct.address),
            "gas": 5_000_000,
            "chainId": w3.eth.chain_id,
        }
    )
    return _send(w3, acct, tx).contractAddress


@pytest.fixture(scope="module")
def chain():
    node = _anvil()
    if node is None:
        pytest.skip(f"no anvil at {RPC}")
    if not WALLET_ARTIFACT.exists() or not USDC_ARTIFACT.exists():
        pytest.skip("contracts not built (run: cd contracts && forge build)")
    return node


@pytest.fixture(scope="module")
def deployed(chain):
    """A funded wallet with one configured category and the agent authorized."""
    w3, owner, agent = chain

    usdc_addr = _deploy(w3, owner, USDC_ARTIFACT)
    wallet_addr = _deploy(w3, owner, WALLET_ARTIFACT, usdc_addr)

    usdc_abi = json.loads(USDC_ARTIFACT.read_text())["abi"]
    wallet_abi = json.loads(WALLET_ARTIFACT.read_text())["abi"]
    usdc = w3.eth.contract(address=usdc_addr, abi=usdc_abi)
    wallet = w3.eth.contract(address=wallet_addr, abi=wallet_abi)

    def owner_tx(fn):
        return _send(
            w3,
            owner,
            fn.build_transaction(
                {
                    "from": owner.address,
                    "nonce": w3.eth.get_transaction_count(owner.address),
                    "gas": 1_000_000,
                    "chainId": w3.eth.chain_id,
                }
            ),
        )

    owner_tx(usdc.functions.mint(wallet_addr, 10_000 * USDC))
    owner_tx(wallet.functions.setAgent(agent.address, True))
    owner_tx(wallet.functions.setBudget(category_id(CATEGORY), CAP, PER_TX, MONTH))

    client = PolicyClient(
        rpc_url=RPC,
        wallet_address=wallet_addr,
        agent_signer=LocalKeySigner(AGENT_KEY),
        owner_signer=LocalKeySigner(OWNER_KEY),
    )
    return w3, wallet, usdc, client


# --- the parity claim ------------------------------------------------------

def test_the_digest_python_signs_is_the_digest_solidity_recovers_against(deployed):
    """Byte-for-byte, against the contract's own arithmetic. A mismatch here is
    invisible until a real approval is rejected as a forgery."""
    from eth_account.messages import _hash_eip191_message, encode_typed_data
    from web3 import Web3

    _, wallet, _, client = deployed
    record = {"billed": 250.0, "metered": 249.1, "par": 240.0, "rule": "over-par-reroute"}
    dh = decision_hash(record)
    deadline = client.deadline_in(600)
    nonce = client.approval_nonce()

    message = {
        "category": category_id(CATEGORY),
        "to": Web3.to_checksum_address(VENDOR),
        "amount": 250 * USDC,
        "decisionHash": dh,
        "nonce": nonce,
        "deadline": deadline,
    }
    ours = _hash_eip191_message(
        encode_typed_data(
            domain_data=client._domain(client._connect().eth.chain_id),
            message_types=APPROVAL_TYPES,
            message_data=message,
        )
    )
    theirs = wallet.functions.approvalDigest(
        category_id(CATEGORY),
        Web3.to_checksum_address(VENDOR),
        250 * USDC,
        dh,
        nonce,
        deadline,
    ).call()

    assert ours == theirs, "Python and Solidity disagree on the approval digest"


def test_the_domain_separator_matches_the_one_the_client_builds(deployed):
    """The separator pins the name, version, chain and address together. If the
    client's string ever drifts from the contract's, every signature it makes is
    valid for a wallet that does not exist."""
    from eth_abi import encode as abi_encode
    from web3 import Web3

    _, wallet, _, client = deployed
    d = client._domain(client._connect().eth.chain_id)
    expected = Web3.keccak(
        abi_encode(
            ["bytes32", "bytes32", "bytes32", "uint256", "address"],
            [
                Web3.keccak(
                    text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
                ),
                Web3.keccak(text=d["name"]),
                Web3.keccak(text=d["version"]),
                d["chainId"],
                d["verifyingContract"],
            ],
        )
    )
    assert wallet.functions.DOMAIN_SEPARATOR().call() == expected


# --- the authority ladder, against Solidity --------------------------------

def test_the_agent_pays_below_the_threshold(deployed):
    w3, wallet, usdc, client = deployed
    before = usdc.functions.balanceOf(VENDOR).call()

    record = {"billed": 40.0, "metered": 40.0, "par": 40.0, "rule": "at-par-pay-now"}
    client.spend(CATEGORY, VENDOR, 40, record)

    assert usdc.functions.balanceOf(VENDOR).call() == before + 40 * USDC
    b = client.budget(CATEGORY)
    assert b is not None and b["spent_usdc"] >= 40.0


def test_the_stored_commitment_is_the_hash_of_the_record_we_kept(deployed):
    """The euthyna claim: the chain holds a commitment to the reasoning. A record
    that no longer hashes to the stored value was edited after the payment."""
    w3, wallet, _, client = deployed
    record = {"billed": 11.0, "metered": 10.5, "par": 10.0, "rule": "metered-below-billed"}

    tx = client.spend(CATEGORY, VENDOR, 11, record)
    rcpt = w3.eth.get_transaction_receipt(tx)
    logs = wallet.events.Spent().process_receipt(rcpt, errors=DISCARD)

    assert len(logs) == 1
    assert bytes(logs[0]["args"]["decisionHash"]) == decision_hash(record)
    assert logs[0]["args"]["ownerApproved"] is False, "the agent acted alone"


def test_at_the_threshold_the_agent_alone_is_refused(deployed):
    _, _, usdc, client = deployed
    before = usdc.functions.balanceOf(VENDOR).call()

    with pytest.raises(Exception) as exc:
        client.spend(CATEGORY, VENDOR, 100, {"rule": "too-big"})
    assert "needs owner approval" in str(exc.value) or "revert" in str(exc.value).lower()

    assert usdc.functions.balanceOf(VENDOR).call() == before, "nothing moved"


def test_the_owners_signature_clears_it(deployed):
    w3, wallet, usdc, client = deployed
    before = usdc.functions.balanceOf(VENDOR).call()
    nonce_before = client.approval_nonce()

    record = {"billed": 250.0, "metered": 249.1, "par": 240.0, "rule": "escalated-approved"}
    tx = client.spend_approved(
        CATEGORY, VENDOR, 250, record, deadline=client.deadline_in(600)
    )

    assert usdc.functions.balanceOf(VENDOR).call() == before + 250 * USDC
    assert client.approval_nonce() == nonce_before + 1, "the approval was consumed once"

    logs = wallet.events.Spent().process_receipt(
        w3.eth.get_transaction_receipt(tx), errors=DISCARD
    )
    assert logs[0]["args"]["ownerApproved"] is True, "recorded as a human decision"
    assert bytes(logs[0]["args"]["decisionHash"]) == decision_hash(record)


def test_a_deadline_is_dated_on_the_chains_clock_not_the_hosts(deployed):
    """Measured 3,815 seconds apart on a local anvil. A deadline from
    ``time.time()`` expired an approval that had just been signed."""
    _, _, _, client = deployed
    chain_now = client.chain_now()
    assert chain_now is not None
    assert client.deadline_in(600) == chain_now + 600

    # And the contract agrees it has not expired.
    record = {"billed": 150.0, "metered": 150.0, "par": 150.0, "rule": "escalated-approved"}
    client.spend_approved(CATEGORY, VENDOR, 150, record, deadline=client.deadline_in(600))


def test_the_owner_pays_from_their_own_wallet_and_burns_no_nonce(deployed):
    """The escalation path that needs no signature. ecrecover cannot check a
    smart-contract account (docs/WALLETS.md C1), so an owner on a Circle PIN
    wallet could never clear spendApproved — but it can call the contract.

    The nonce assertion is the point: nothing is signed, so nothing can go stale
    between the owner deciding and the payment landing."""
    _, wallet, usdc, client = deployed
    before = usdc.functions.balanceOf(VENDOR).call()
    nonce_before = client.approval_nonce()

    record = {"billed": 300.0, "metered": 300.0, "par": 300.0, "rule": "owner-paid"}
    tx = client.spend_as_owner(CATEGORY, VENDOR, 300, record)

    assert usdc.functions.balanceOf(VENDOR).call() == before + 300 * USDC
    assert client.approval_nonce() == nonce_before, "a direct owner payment signs nothing"

    logs = wallet.events.Spent().process_receipt(
        client._connect().eth.get_transaction_receipt(tx), errors=DISCARD
    )
    assert logs[0]["args"]["ownerApproved"] is True, "recorded as a human decision"
    assert bytes(logs[0]["args"]["decisionHash"]) == decision_hash(record)
    assert logs[0]["args"]["actor"] == client.owner_signer.address, "the owner acted"


def test_the_agent_cannot_use_the_owners_path(deployed):
    """If it could, the threshold would be a suggestion."""
    from web3 import Web3

    _, wallet, usdc, client = deployed
    before = usdc.functions.balanceOf(VENDOR).call()

    fn = wallet.functions.spendAsOwner(
        category_id(CATEGORY), Web3.to_checksum_address(VENDOR), 300 * USDC,
        decision_hash({"rule": "nice try"}),
    )
    with pytest.raises(Exception) as exc:
        client._send(fn)  # defaults to the agent signer
    assert "not owner" in str(exc.value) or "revert" in str(exc.value).lower()
    assert usdc.functions.balanceOf(VENDOR).call() == before, "nothing moved"


# --- the phantom payment ---------------------------------------------------
#
# *Agents and Ledgers* names SolidInvoice for "the most complete write path, and
# no way to disprove a phantom payment", and this client could produce one.
#
# A PolicyWallet address is read with whatever RPC the press happens to hold. On
# a chain where that address has no code, a CALL does not revert — it succeeds
# and returns empty. So `eth_estimateGas` succeeds (measured against a live Arc
# node: 22026 gas), the transaction broadcasts, the receipt comes back
# `status: 1`, and `_send`'s only check passes. `ops_actions._settle` then writes
# a payment with a TRANSACTION HASH AS ITS EVIDENCE — into `moved_usdc`, the
# beancount export and the owner's statement.
#
# Nothing downstream can tell that from a real payment. The one thing that can
# is the absence of code at the address, which is why the guard tests exactly
# that and not the registry's `chain` label: when the label is the thing that
# went wrong, you have to compare something the label cannot fake.


def test_a_wallet_with_no_code_is_refused_rather_than_paid_into(deployed):
    """The money must not leave for an address that is not the contract."""
    _w3, _wallet, _usdc, client = deployed

    nowhere = PolicyClient(
        rpc_url=RPC,
        # Well-formed, checksummed, and nothing is deployed there.
        wallet_address="0x00000000000000000000000000000000DeaDBeef",
        agent_signer=LocalKeySigner(AGENT_KEY),
        owner_signer=LocalKeySigner(OWNER_KEY),
    )

    assert nowhere.wallet_status() == "not_on_this_chain"
    assert client.wallet_status() == "ok", "the real one still reads as fine"

    record = {"obligation_id": "phantom-1", "billed_usdc": 1.0}
    with pytest.raises(RuntimeError) as owner_err:
        nowhere.spend_as_owner(CATEGORY, "0x" + "cc" * 20, 1.0, record)
    assert "not on this chain" in str(owner_err.value).lower()

    with pytest.raises(RuntimeError) as agent_err:
        nowhere.spend(CATEGORY, "0x" + "cc" * 20, 1.0, record)
    assert "not on this chain" in str(agent_err.value).lower()


def test_the_status_says_which_of_the_four_reasons_it_is(deployed):
    """`budget()` returns None for four different reasons and always has.

    A reader cannot act on that: "no wallet", "the RPC is down", "this wallet is
    on another chain" and "that category has no budget" are four different
    things to do on a Monday morning, and the statement rendered all of them as
    the same gold chip.
    """
    _w3, _wallet, _usdc, client = deployed

    assert client.wallet_status() == "ok"

    no_wallet = PolicyClient(
        rpc_url=RPC, wallet_address=None,
        agent_signer=LocalKeySigner(AGENT_KEY), owner_signer=LocalKeySigner(OWNER_KEY),
    )
    assert no_wallet.wallet_status() == "no_wallet"

    dead_rpc = PolicyClient(
        rpc_url="http://127.0.0.1:1", wallet_address=client.wallet_address,
        agent_signer=LocalKeySigner(AGENT_KEY), owner_signer=LocalKeySigner(OWNER_KEY),
    )
    assert dead_rpc.wallet_status() == "no_rpc", "a dead node is not an absent contract"


def test_a_real_wallet_still_pays_after_the_guard(deployed):
    """The guard must not break the thing it protects."""
    w3, _wallet, usdc, client = deployed
    to = "0x" + "cc" * 20
    before = usdc.functions.balanceOf(w3.to_checksum_address(to)).call()
    client.spend(CATEGORY, to, 1.0, {"obligation_id": "real-1"})
    after = usdc.functions.balanceOf(w3.to_checksum_address(to)).call()
    assert after - before == 1 * USDC, "a real wallet still moves real money"
