"""The agent card — who is calling, signed, with no registry to ask.

An agent presents a card. The card says which key it is, what role it is acting
in, which service it is for, and until when. The gate recovers the signature and
believes the key, not a list — so nobody has to be enrolled, and there is no
registry to run, gatekeep, or lose.

THE DOMAIN OMITS `verifyingContract`, AND THAT IS WHAT PERMISSIONLESS MEANS HERE.
The four domains this project already has — `ACR Oracle`,
`ACR AttestationRegistry`, `ACR Receipt Mirror`, `ACR Human Id Mirror` — each bind
to a deployed address, because each is verified BY that contract. A card is
verified by whoever reads it, so there is no address to name.

That buys openness and costs two things, both paid for in the struct rather than
hoped away:

  * A `{name, version, chainId}` domain is valid at ANY verifier on that chain.
    So the card carries `audience`, and a gate refuses a card addressed elsewhere.
    Without it, a card minted for some other ACR-domain service would be
    presentable here — which is precisely the replay a `verifyingContract`
    normally prevents.

  * A bearer credential is replayable inside its validity window by anyone who
    captures it. So `MAX_TTL_S` bounds that window. There is no per-call nonce on
    purpose: an agent cannot afford a round trip before every request, and the
    AgentKit verifier in this same codebase made the same call — freshness lives
    inside the signed message rather than in a table on our side.

AND A CARD PROVES IDENTITY, NOT SCARCITY. Keys are free, so a caller who wants
ten identities can have them, and a rate limit keyed on the agent address is
evadable by anyone willing to run `eth-account` in a loop. That is why
`humanCluster` exists: a card may CLAIM a human, the gate checks that claim
against `HumanIdMirror.clusterOf` on chain, and the limit keys on the human. Ten
keys belonging to one person then buy one budget. The claim is worth nothing
unverified, which is why this module only carries it and `agentgate` is the thing
that checks it.
"""

from __future__ import annotations

import base64
import binascii
import json
import time
from dataclasses import dataclass, replace

#: `("ACR <Thing>", "1")` — the house idiom, and this was the free slot.
CARD_DOMAIN_NAME = "ACR Agent Card"
CARD_DOMAIN_VERSION = "1"

#: The roles a card may claim. Four come from `_ROLE_WALLET_FIELDS` in
#: `signer.py` so the card and the venue's service accounts share one vocabulary
#: rather than drifting into two; `reader` is the fifth, for the query path, which
#: has no wallet because it spends nothing.
ROLES: tuple[str, ...] = ("maker", "taker", "poster", "owner", "reader")

#: The longest life a card may claim. A bearer credential is replayable until it
#: expires, so this is the blast radius of a captured card and not a convenience
#: knob. Fifteen minutes is long enough for an agent to make many calls on one
#: signature and short enough that a leaked card is worth little.
MAX_TTL_S = 900

#: Tolerance for a signer whose clock runs ahead of ours. Without it, two honest
#: machines disagreeing by a second make a valid card look not-yet-valid — and
#: "your clock is fast" is an unhelpful 401.
CLOCK_SKEW_S = 30

ZERO32 = "0x" + "00" * 32

AGENT_CARD_TYPES: dict[str, list[dict[str, str]]] = {
    "AgentCard": [
        {"name": "agent", "type": "address"},
        {"name": "name", "type": "string"},
        {"name": "role", "type": "string"},
        {"name": "audience", "type": "string"},
        {"name": "scopeHash", "type": "bytes32"},
        {"name": "humanCluster", "type": "bytes32"},
        {"name": "issuedAt", "type": "uint64"},
        {"name": "expiresAt", "type": "uint64"},
    ]
}


def agentcard_domain(chain_id: int) -> dict:
    """The EIP-712 domain a card is signed under.

    No `verifyingContract`, deliberately — see the module docstring. `chainId`
    stays because a card minted for Arc should not be presentable on another
    chain's deployment of this same service.
    """
    return {
        "name": CARD_DOMAIN_NAME,
        "version": CARD_DOMAIN_VERSION,
        "chainId": int(chain_id),
    }


def scope_hash(scopes) -> str:
    """keccak of the SORTED, de-duplicated scope list.

    Sorted because `["read","write"]` and `["write","read"]` are the same grant
    and must not produce two different cards; de-duplicated for the same reason.
    An empty list hashes to the zero word rather than to keccak("") so "no scopes
    claimed" is distinguishable from "claimed an empty string".
    """
    from eth_utils import keccak

    items = sorted({str(s).strip() for s in (scopes or []) if str(s).strip()})
    if not items:
        return ZERO32
    return "0x" + keccak(text=",".join(items)).hex()


@dataclass(frozen=True)
class AgentCard:
    """What an agent asserts about itself. Frozen: a verified card must not be
    editable by the code that received it."""

    agent: str
    name: str
    role: str
    audience: str
    scope_hash: str = ZERO32
    #: A CLAIM, worth nothing until `agentgate` checks it on chain.
    human_cluster: str = ZERO32
    issued_at: int = 0
    expires_at: int = 0

    @property
    def claims_human(self) -> bool:
        return self.human_cluster.lower() != ZERO32

    @property
    def ttl_s(self) -> int:
        return int(self.expires_at) - int(self.issued_at)

    def message(self) -> dict:
        """The EIP-712 message. Field names are the struct's, not Python's."""
        from web3 import Web3

        return {
            "agent": Web3.to_checksum_address(self.agent),
            "name": self.name,
            "role": self.role,
            "audience": self.audience,
            "scopeHash": self.scope_hash,
            "humanCluster": self.human_cluster,
            "issuedAt": int(self.issued_at),
            "expiresAt": int(self.expires_at),
        }

    def to_json(self) -> dict:
        """Wire form. Snake_case, because this crosses into HTTP where the rest
        of this service's JSON is snake_case; `message()` is the only place the
        struct's camelCase names appear."""
        return {
            "agent": self.agent,
            "name": self.name,
            "role": self.role,
            "audience": self.audience,
            "scope_hash": self.scope_hash,
            "human_cluster": self.human_cluster,
            "issued_at": int(self.issued_at),
            "expires_at": int(self.expires_at),
        }

    @classmethod
    def from_json(cls, d: dict) -> AgentCard:
        return cls(
            agent=str(d["agent"]),
            name=str(d.get("name") or ""),
            role=str(d.get("role") or ""),
            audience=str(d.get("audience") or ""),
            scope_hash=_bytes32(d.get("scope_hash"), "scope_hash"),
            human_cluster=_bytes32(d.get("human_cluster"), "human_cluster"),
            issued_at=int(d.get("issued_at") or 0),
            expires_at=int(d.get("expires_at") or 0),
        )


def _bytes32(value, field: str) -> str:
    """A 32-byte hex string, or a refusal naming the field.

    WHY THIS IS NOT PEDANTRY. `claims_human` is `human_cluster != ZERO32`, a STRING
    comparison — while `encode_typed_data` silently zero-pads a short hex value to
    32 bytes. So a truncated all-zeros cluster signs identically to a real zero and
    yet reads as "this card claims a human", sending the gate off to verify a claim
    nobody made and answering 401 where it should have answered `carded`.

    Found the hard way: a hand-wrapped copy of this module's own test vector lost a
    few base64 characters, and the signature check passed anyway — because the
    padding hid it. A field whose malformed form verifies is a field that must be
    validated at the edge rather than trusted.
    """
    raw = ZERO32 if value in (None, "") else str(value)
    body = raw[2:] if raw.lower().startswith("0x") else raw
    if len(body) != 64 or any(c not in "0123456789abcdefABCDEF" for c in body):
        raise ValueError(f"{field} must be 32 bytes of hex (0x + 64 chars), got {len(body)} chars")
    return "0x" + body.lower()


def mint(
    agent: str,
    *,
    name: str,
    role: str,
    audience: str,
    scopes=None,
    human_cluster: str = ZERO32,
    ttl_s: int = 300,
    now: int | None = None,
) -> AgentCard:
    """A card with its window already set. Refuses a TTL over the bound here
    rather than letting a gate discover it later — the mistake is cheaper to fix
    at the signer than at the verifier."""
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}; expected one of {', '.join(ROLES)}")
    if ttl_s <= 0:
        raise ValueError("ttl_s must be positive — a card that expires on issue is not a card")
    if ttl_s > MAX_TTL_S:
        raise ValueError(f"ttl_s {ttl_s} exceeds MAX_TTL_S {MAX_TTL_S}")
    t = int(time.time()) if now is None else int(now)
    return AgentCard(
        agent=agent, name=name, role=role, audience=audience,
        scope_hash=scope_hash(scopes), human_cluster=human_cluster,
        issued_at=t, expires_at=t + int(ttl_s),
    )


def card_digest(card: AgentCard, chain_id: int) -> bytes:
    """The EIP-712 digest a signature covers.

    Exposed rather than kept private because `humanid.py` proves its own encoding
    against the contract's `resolutionDigest` view for exactly this reason: an
    encoding mistake recovers to a stranger, and the only symptom is a 401 on a
    call that should have worked. There is no contract to check against here, so
    the tests check this against `eth_account`'s own recovery instead.
    """
    from eth_account.messages import encode_typed_data
    from eth_utils import keccak

    signable = encode_typed_data(
        domain_data=agentcard_domain(chain_id),
        message_types=AGENT_CARD_TYPES,
        message_data=card.message(),
    )
    return bytes(keccak(b"\x19" + signable.version + signable.header + signable.body))


def sign_card(card: AgentCard, signer, chain_id: int) -> str:
    """Sign with a LOCAL key. Returns a 65-byte `0x` signature, which is what
    travels in a header; the `(v, r, s)` triple the protocol returns is a
    contract-call shape.

    CIRCLE CUSTODY IS REFUSED, LOUDLY, AND THIS IS NOT A LIMITATION OF CARDS.
    `signer.full_eip712_json` builds the payload Circle signs and hardcodes a
    four-field `EIP712Domain` that includes `verifyingContract`. This domain has
    three fields on purpose — omitting `verifyingContract` is what makes the card
    permissionless — so a custody signer would be handed a type declaring a field
    the domain does not carry. The digest would differ, the signature would
    recover to the wrong address, and the gate would refuse it with "signature
    does not match" while every local test passed: `eth_account` infers the domain
    type from the keys actually present, so `LocalKeySigner` is immune to the bug
    that would break custody.

    Refusing here rather than fixing that function is deliberate. It signs every
    Circle-custody message in the system — prints, attestations, both mirrors — so
    changing how its domain type is derived risks the oracle going quiet with no
    local error, because a bad signature is a revert on chain rather than an
    exception here. That is a change that wants its own commit and a live posting
    run, not a line in a card feature.
    """
    # Matched by name rather than by import, the idiom `/health` already uses, so
    # this module stays free of a circular dependency on the signer backends.
    if type(signer).__name__ == "CircleWalletSigner":
        raise TypeError(
            "agent cards cannot be signed by Circle custody: full_eip712_json "
            "declares a 4-field EIP712Domain and the card domain has 3 "
            "(no verifyingContract). Use a local key — ACR_READER_PRIVATE_KEY."
        )
    v, r, s = signer.sign_typed_data(
        agentcard_domain(chain_id), AGENT_CARD_TYPES, card.message(), "AgentCard"
    )
    return "0x" + bytes(r).hex() + bytes(s).hex() + bytes([int(v)]).hex()


def recover_agent(card: AgentCard, signature: str, chain_id: int) -> str:
    """The address that actually signed, checksummed.

    A caller must compare this to `card.agent` itself. Returning the recovered
    address rather than a boolean is deliberate: a function that answered
    "valid: true" would invite being trusted without anyone checking WHICH key it
    validated, and the card's entire claim is about which key.
    """
    from eth_account import Account
    from eth_account.messages import encode_typed_data

    signable = encode_typed_data(
        domain_data=agentcard_domain(chain_id),
        message_types=AGENT_CARD_TYPES,
        message_data=card.message(),
    )
    return str(Account.recover_message(signable, signature=signature))


# --- the wire format ---------------------------------------------------------
#
# base64(JSON), matching how x402 carries PAYMENT-SIGNATURE and how the human
# gate carries HUMAN-PROOF. One encoding across three gates means an agent author
# writes one decoder.

def encode_header(card: AgentCard, signature: str) -> str:
    raw = json.dumps({"card": card.to_json(), "signature": signature},
                     separators=(",", ":"), sort_keys=True).encode()
    return base64.b64encode(raw).decode()


def decode_header(value: str) -> tuple[AgentCard, str]:
    """Parse a presented header.

    Raises `ValueError` with a reason on anything malformed, so a gate can answer
    401 with something an agent author can act on. Bare `Exception` would collapse
    "you sent nonsense" and "we broke" into one message.
    """
    try:
        payload = json.loads(base64.b64decode(value, validate=True))
    except (binascii.Error, ValueError, TypeError) as exc:
        raise ValueError("header is not base64-encoded JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("header did not decode to an object")
    card_raw, sig = payload.get("card"), payload.get("signature")
    if not isinstance(card_raw, dict):
        raise ValueError("header is missing the card object")
    if not isinstance(sig, str) or not sig.startswith("0x"):
        raise ValueError("header is missing a 0x signature")
    try:
        return AgentCard.from_json(card_raw), sig
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"card is missing or malformed: {exc}") from exc


def with_window(card: AgentCard, **kw) -> AgentCard:
    """A copy with fields replaced — used by tests to build the near-miss cases
    without hand-assembling eight fields each time."""
    return replace(card, **kw)
