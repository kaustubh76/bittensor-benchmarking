"""FeedAccessAttestor — an on-chain right to the feed, minted by the seller's signer.

`FeedAccessAttestor.sol` turns a payment the chain cannot see (an off-chain x402
settlement, or a plain USDC transfer to the seller) into a fact it can:
`hasFeedAccess(who)`. The seller signs an EIP-712 `FeedAccess` struct with the
same custody wallet that signs prints; anyone relays it to `redeem`; the chain
records `paidUntil[beneficiary]`.

This module is the one place the struct, the ABI and the signing live. The
operator script (`scripts/attest_feed_access.py`) and the API's Desk pass
(`index_api.feedpass`) both mint through it, so a typehash drift shows up in
both at once — the only symptom of a drift is "bad signer" on a redeem that
should have worked, which is why there is exactly one copy.
"""

from __future__ import annotations

from typing import Any

ATTESTOR_ABI = [
    {"type": "function", "name": "accessDigest", "stateMutability": "view",
     "inputs": [{"name": "payer", "type": "address"},
                {"name": "beneficiary", "type": "address"},
                {"name": "paidUntilTs", "type": "uint64"},
                {"name": "amountUsdc", "type": "uint256"},
                {"name": "nonce", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bytes32"}]},
    {"type": "function", "name": "redeem", "stateMutability": "nonpayable",
     "inputs": [{"name": "payer", "type": "address"},
                {"name": "beneficiary", "type": "address"},
                {"name": "paidUntilTs", "type": "uint64"},
                {"name": "amountUsdc", "type": "uint256"},
                {"name": "nonce", "type": "uint256"},
                {"name": "v", "type": "uint8"}, {"name": "r", "type": "bytes32"},
                {"name": "s", "type": "bytes32"}],
     "outputs": []},
    {"type": "function", "name": "hasFeedAccess", "stateMutability": "view",
     "inputs": [{"name": "who", "type": "address"}],
     "outputs": [{"name": "", "type": "bool"}]},
    {"type": "function", "name": "paidUntil", "stateMutability": "view",
     "inputs": [{"name": "", "type": "address"}],
     "outputs": [{"name": "", "type": "uint64"}]},
]

#: The EIP-712 types the contract hashes. Must match FEED_ACCESS_TYPEHASH exactly.
FEED_ACCESS_TYPES = {
    "FeedAccess": [
        {"name": "payer", "type": "address"},
        {"name": "beneficiary", "type": "address"},
        {"name": "paidUntil", "type": "uint64"},
        {"name": "amountUsdc", "type": "uint256"},
        {"name": "nonce", "type": "uint256"},
    ]
}

DOMAIN_NAME = "ACR Feed Access"
DOMAIN_VERSION = "1"


def domain(chain_id: int, attestor: str) -> dict[str, Any]:
    from eth_utils import to_checksum_address

    return {
        "name": DOMAIN_NAME,
        "version": DOMAIN_VERSION,
        "chainId": int(chain_id),
        "verifyingContract": to_checksum_address(attestor),
    }


def attestor_contract(w3, attestor: str):
    from eth_utils import to_checksum_address

    return w3.eth.contract(address=to_checksum_address(attestor), abi=ATTESTOR_ABI)


def sign_feed_access(
    signer, w3, attestor: str, *, payer: str, beneficiary: str, paid_until: int,
    amount_units: int, nonce: int,
) -> tuple[int, bytes, bytes]:
    """Sign the struct and PROVE it recovers to our own signer before anyone
    broadcasts it — a signature that recovers to a stranger is the drift symptom."""
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    from eth_utils import to_checksum_address

    payer = to_checksum_address(payer)
    beneficiary = to_checksum_address(beneficiary)
    dom = domain(w3.eth.chain_id, attestor)
    message = {
        "payer": payer, "beneficiary": beneficiary, "paidUntil": int(paid_until),
        "amountUsdc": int(amount_units), "nonce": int(nonce),
    }
    v, r, s = signer.sign_typed_data(dom, FEED_ACCESS_TYPES, message, "FeedAccess")
    recovered = Account.recover_message(
        encode_typed_data(domain_data=dom, message_types=FEED_ACCESS_TYPES, message_data=message),
        vrs=(v, int.from_bytes(r, "big"), int.from_bytes(s, "big")),
    )
    if recovered.lower() != signer.address.lower():
        raise RuntimeError("the FeedAccess signature does not recover to our own signer — refusing")
    return v, r, s


def relay_redeem(
    signer, w3, attestor: str, *, payer: str, beneficiary: str, paid_until: int,
    amount_units: int, nonce: int, v: int, r: bytes, s: bytes,
) -> str:
    """Broadcast `redeem` through the signer's own transaction path. Idempotent at
    the contract: a second redeem with the same inputs reverts "nonce used"."""
    from eth_utils import to_checksum_address

    c = attestor_contract(w3, attestor)
    return signer.send_transaction(
        w3,
        {
            "to": to_checksum_address(attestor),
            "data": c.encode_abi(
                "redeem",
                args=[to_checksum_address(payer), to_checksum_address(beneficiary),
                      int(paid_until), int(amount_units), int(nonce), v, r, s],
            ),
        },
    )


def access_of(w3, attestor: str, who: str) -> tuple[bool, int]:
    """(hasFeedAccess, paidUntil) — two witnesses, read together."""
    from eth_utils import to_checksum_address

    c = attestor_contract(w3, attestor)
    addr = to_checksum_address(who)
    return bool(c.functions.hasFeedAccess(addr).call()), int(c.functions.paidUntil(addr).call())
