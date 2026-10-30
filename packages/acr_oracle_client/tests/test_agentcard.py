"""The card's crypto, and the claims it must not let through.

There is no contract to check the encoding against — the domain names none, which
is the point — so these tests stand in for the `resolutionDigest` cross-check
`humanid.py` gets for free. An encoding mistake recovers to a stranger, and the
only symptom downstream is a 401 on a call that should have worked.
"""

from __future__ import annotations

import hashlib

import pytest
from acr_oracle_client.agentcard import (
    CARD_DOMAIN_NAME,
    MAX_TTL_S,
    ROLES,
    ZERO32,
    AgentCard,
    agentcard_domain,
    card_digest,
    decode_header,
    encode_header,
    mint,
    recover_agent,
    scope_hash,
    sign_card,
    with_window,
)
from acr_oracle_client.signer import LocalKeySigner

CHAIN = 5042002


def _signer(label: str = "acr-buyer-1") -> LocalKeySigner:
    return LocalKeySigner("0x" + hashlib.sha256(f"acr-buyer::{label}".encode()).hexdigest())


def _card(signer, **kw) -> AgentCard:
    opts = {"name": "demo", "role": "reader", "audience": "acr-index-api", "ttl_s": 300}
    opts.update(kw)
    return mint(signer.address, **opts)


# --- the domain --------------------------------------------------------------


def test_the_domain_names_no_contract():
    """THE defining property. Every other ACR domain binds to a deployed address
    because a contract verifies it; this one is verified by whoever reads it, so
    there is no address to name. If a `verifyingContract` ever appears here, the
    card has stopped being permissionless and `audience` has become decorative."""
    d = agentcard_domain(CHAIN)
    assert "verifyingContract" not in d
    assert d == {"name": CARD_DOMAIN_NAME, "version": "1", "chainId": CHAIN}


def test_the_domain_does_not_collide_with_the_four_that_exist():
    from acr_oracle_client import client, humanid, mirror, registry

    taken = set()
    for mod in (client, humanid, mirror, registry):
        taken |= {
            line.split('"')[3]
            for line in open(mod.__file__).read().splitlines()
            if '"name": "ACR ' in line
        }
    assert CARD_DOMAIN_NAME not in taken, f"domain name collides: {sorted(taken)}"


# --- signing and recovery ----------------------------------------------------


def test_a_signature_recovers_to_the_agent_it_names():
    s = _signer()
    c = _card(s)
    assert recover_agent(c, sign_card(c, s, CHAIN), CHAIN).lower() == s.address.lower()


def test_a_card_signed_by_one_key_does_not_recover_to_another():
    a, b = _signer("acr-buyer-1"), _signer("acr-buyer-2")
    # b signs a card that NAMES a. The recovery must expose that.
    c = _card(a)
    assert recover_agent(c, sign_card(c, b, CHAIN), CHAIN).lower() != a.address.lower()


@pytest.mark.parametrize("field,value", [
    ("role", "owner"),
    ("audience", "some-other-service"),
    ("name", "somebody else"),
    ("expires_at", 99_999_999_999),
    ("human_cluster", "0x" + "ab" * 32),
    ("scope_hash", "0x" + "cd" * 32),
])
def test_tampering_with_any_signed_field_breaks_recovery(field, value):
    """Every field is inside the digest. A card whose role could be edited after
    signing would let a reader promote itself to owner."""
    s = _signer()
    c = _card(s)
    sig = sign_card(c, s, CHAIN)
    assert recover_agent(with_window(c, **{field: value}), sig, CHAIN).lower() != s.address.lower()


def test_a_card_for_one_chain_does_not_verify_on_another():
    """chainId stays in the domain even without a contract: a card minted for Arc
    must not be presentable to this same service deployed elsewhere."""
    s = _signer()
    c = _card(s)
    sig = sign_card(c, s, CHAIN)
    assert recover_agent(c, sig, 1).lower() != s.address.lower()


def test_the_digest_is_stable_and_32_bytes():
    s = _signer()
    c = _card(s, now=1_700_000_000)
    d = card_digest(c, CHAIN)
    assert len(d) == 32
    assert d == card_digest(c, CHAIN)  # no hidden nonce or clock in the encoding


# --- minting rules -----------------------------------------------------------


def test_minting_refuses_a_lifetime_over_the_bound():
    """A bearer card is replayable until it expires, so the TTL bound is the blast
    radius of a captured card. Refused at the signer, where it is cheap to fix."""
    with pytest.raises(ValueError, match="exceeds MAX_TTL_S"):
        _card(_signer(), ttl_s=MAX_TTL_S + 1)


def test_minting_refuses_a_card_that_expires_on_issue():
    with pytest.raises(ValueError, match="must be positive"):
        _card(_signer(), ttl_s=0)


def test_minting_refuses_an_unknown_role():
    with pytest.raises(ValueError, match="unknown role"):
        _card(_signer(), role="admin")
    for role in ROLES:
        assert _card(_signer(), role=role).role == role


def test_the_roles_match_the_venue_vocabulary():
    """The card's roles and the venue's service accounts must be the same words,
    or an operator learns two taxonomies for one idea."""
    from acr_oracle_client.signer import _ROLE_WALLET_FIELDS

    assert set(_ROLE_WALLET_FIELDS) <= set(ROLES)
    assert "reader" in ROLES  # the query path, which has no wallet


# --- scopes ------------------------------------------------------------------


def test_scope_order_does_not_change_the_hash():
    assert scope_hash(["b", "a"]) == scope_hash(["a", "b"])
    assert scope_hash(["a", "a", "b"]) == scope_hash(["a", "b"])


def test_no_scopes_is_the_zero_word_not_a_hash_of_nothing():
    """keccak("") is a real hash and would read as a claimed-but-empty scope set.
    The zero word says "claimed nothing", which is a different fact."""
    assert scope_hash([]) == ZERO32
    assert scope_hash(None) == ZERO32
    assert scope_hash(["   "]) == ZERO32


# --- the wire format ---------------------------------------------------------


def test_the_header_round_trips():
    s = _signer()
    c = _card(s)
    sig = sign_card(c, s, CHAIN)
    got_card, got_sig = decode_header(encode_header(c, sig))
    assert (got_card, got_sig) == (c, sig)


@pytest.mark.parametrize("bad,reason", [
    ("not base64 at all!!", "base64"),
    ("eyJ4IjoxfQ==", "missing the card"),                  # {"x":1}
    ("W10=", "did not decode to an object"),               # []
])
def test_a_malformed_header_says_what_is_wrong(bad, reason):
    """A gate answers 401 with this text. "Invalid" tells an agent author nothing;
    naming the defect is what makes the failure fixable."""
    with pytest.raises(ValueError, match=reason):
        decode_header(bad)


def test_a_header_without_a_0x_signature_is_refused():
    import base64
    import json

    raw = json.dumps({"card": _card(_signer()).to_json(), "signature": "nope"}).encode()
    with pytest.raises(ValueError, match="0x signature"):
        decode_header(base64.b64encode(raw).decode())


def test_a_card_is_frozen():
    """A verified card must not be editable by the code that received it."""
    import dataclasses

    with pytest.raises(dataclasses.FrozenInstanceError):
        _card(_signer()).role = "owner"  # type: ignore[misc]


def test_claims_human_is_false_for_the_zero_word():
    s = _signer()
    assert _card(s).claims_human is False
    assert _card(s, human_cluster="0x" + "11" * 32).claims_human is True


def test_a_truncated_cluster_is_refused_rather_than_read_as_a_human_claim():
    """The padding trap. `claims_human` compares STRINGS while `encode_typed_data`
    zero-pads short hex to 32 bytes, so a truncated all-zeros cluster used to sign
    identically to a real zero AND read as a claim — sending the gate to verify
    something nobody asserted. Refused at the edge instead."""
    import pytest
    from acr_oracle_client.agentcard import AgentCard

    good = AgentCard.from_json({
        "agent": "0x" + "11" * 20, "name": "n", "role": "reader", "audience": "a",
        "scope_hash": "0x" + "00" * 32, "human_cluster": "0x" + "00" * 32,
        "issued_at": 1, "expires_at": 2,
    })
    assert good.claims_human is False

    for field in ("scope_hash", "human_cluster"):
        with pytest.raises(ValueError, match=field):
            AgentCard.from_json({
                "agent": "0x" + "11" * 20, "name": "n", "role": "reader", "audience": "a",
                "scope_hash": "0x" + "00" * 32, "human_cluster": "0x" + "00" * 32,
                "issued_at": 1, "expires_at": 2,
                # 63 hex chars, one short. This is the exact shape that slipped
                # through: valid-looking, all zeros, and a "claim".
                field: "0x" + "0" * 63,
            })
