"""The gate — the tiers, the refusals, and the limit that counts people.

The interesting test here is not that a good card passes. It is that three
different keys belonging to ONE human land in one rate-limit bucket, because that
is the only thing that makes a quota mean anything when keys are free.

The mirror is stubbed so every branch is reachable, including the two that a live
chain cannot produce on demand: an unconfigured mirror and an unreachable one.
Those are exactly the cases that must NOT upgrade a caller on the strength of an
unverified claim.
"""

from __future__ import annotations

import hashlib
import time

import pytest
from acr_core.config import ACRSettings
from acr_oracle_client.agentcard import MAX_TTL_S, encode_header, mint, sign_card, with_window
from acr_oracle_client.signer import LocalKeySigner
from fastapi import HTTPException
from index_api.agentgate import TIER_CARDED, TIER_HUMAN, AgentGate

CHAIN = 5042002
FLEET = "0x" + "aa" * 32      # one human, three wallets
SOLO = "0x" + "bb" * 32       # a different human
LAST_WEEK = "0x" + "cc" * 32  # the fleet's id in the previous window

WALLETS = ("acr-buyer-1", "acr-buyer-2", "acr-buyer-3")


class _Mirror:
    """Enough HumanIdMirrorClient to drive the gate, and nothing more."""

    def __init__(self, by_window=None, configured=True, raises=None):
        self._by_window = by_window or {}
        self._configured = configured
        self._raises = raises

    def configured(self) -> bool:
        return self._configured

    def readable(self) -> bool:
        # The gate asks this one, because verifying a claim is a view call. The
        # stub's single flag means "usable at all", so both answer from it.
        return self._configured

    def cluster_of(self, wallet: str, window: int):
        if self._raises is not None:
            raise self._raises
        hexed = self._by_window.get(int(window), {}).get(wallet.lower())
        return bytes.fromhex(hexed[2:]) if hexed else None


def _signer(label: str) -> LocalKeySigner:
    return LocalKeySigner("0x" + hashlib.sha256(f"acr-buyer::{label}".encode()).hexdigest())


def _settings(**kw) -> ACRSettings:
    return ACRSettings(_env_file=None, arc_chain_id=CHAIN, agent_audience="acr-index-api", **kw)


def _gate(mirror=None, window=2958) -> AgentGate:
    g = AgentGate(settings=_settings(), mirror=mirror or _Mirror())
    # current_window() reads the clock; pin it so the rotation tests are not
    # hostage to the day they run on.
    import index_api.agentgate as mod

    mod.current_window = lambda: window  # type: ignore[assignment]
    return g


class _Req:
    class url:
        path = "/graph/query"

    def __init__(self):
        self.state = type("S", (), {})()


def _present(gate, signer, **kw):
    opts = {"name": "demo", "role": "reader", "audience": "acr-index-api", "ttl_s": 300}
    opts.update(kw)
    card = mint(signer.address, **opts)
    return gate.verify(_Req(), encode_header(card, sign_card(card, signer, CHAIN)))


def _fleet_mirror(window=2958) -> _Mirror:
    return _Mirror({
        window: {_signer(w).address.lower(): FLEET for w in WALLETS}
               | {_signer("acr-buyer-4").address.lower(): SOLO},
        window - 1: {_signer(w).address.lower(): LAST_WEEK for w in WALLETS},
    })


# --- the point of the whole module -------------------------------------------


def test_three_keys_belonging_to_one_human_share_one_rate_limit_bucket():
    """THE sybil answer. Keys are free, so a limit keyed on the agent address is
    evadable by anyone willing to run eth-account in a loop. Keyed on the human it
    is not, and this is the assertion that says so."""
    gate = _gate(_fleet_mirror())
    idents = {_present(gate, _signer(w), human_cluster=FLEET).ident for w in WALLETS}
    assert len(idents) == 1
    assert idents == {FLEET.lower()}


def test_a_different_human_gets_a_different_bucket():
    """The corollary, and it has to be checked: a scheme that collapsed everyone
    into one bucket would also pass the test above."""
    gate = _gate(_fleet_mirror())
    fleet = _present(gate, _signer("acr-buyer-1"), human_cluster=FLEET)
    solo = _present(gate, _signer("acr-buyer-4"), human_cluster=SOLO)
    assert fleet.ident != solo.ident


def test_an_unbound_card_is_keyed_on_the_key_and_says_so():
    gate = _gate(_fleet_mirror())
    a = _present(gate, _signer("acr-buyer-1"))
    assert a.tier == TIER_CARDED
    assert a.cluster is None
    assert a.ident == _signer("acr-buyer-1").address.lower()
    assert a.human_note == ""  # claimed nothing, so there is nothing to explain


# --- the human claim ---------------------------------------------------------


def test_a_matching_claim_reaches_the_human_tier():
    a = _present(_gate(_fleet_mirror()), _signer("acr-buyer-1"), human_cluster=FLEET)
    assert a.tier == TIER_HUMAN
    assert a.cluster == FLEET.lower() or a.cluster.lower() == FLEET.lower()


def test_claiming_another_humans_cluster_is_refused():
    with pytest.raises(HTTPException, match="does not record"):
        _present(_gate(_fleet_mirror()), _signer("acr-buyer-1"), human_cluster=SOLO)


def test_claiming_last_windows_id_says_to_re_mint_rather_than_calling_it_false():
    """Cluster ids are keccak(nullifier, salt, WINDOW), so the same human's id
    changes completely every 7 days. An agent that cached its id is the likeliest
    mistake by far, and "your claim is false" sends them looking in the wrong
    place. Measured while building this: the fleet was 0xd9e05794… in window 2957
    and 0x5bf3b922… in 2958, with no resemblance."""
    with pytest.raises(HTTPException, match="rotate weekly"):
        _present(_gate(_fleet_mirror()), _signer("acr-buyer-1"), human_cluster=LAST_WEEK)


def test_an_agent_resolved_to_nobody_is_refused():
    gate = _gate(_Mirror({2958: {}}))
    with pytest.raises(HTTPException, match="not resolved to any human"):
        _present(gate, _signer("acr-buyer-1"), human_cluster=FLEET)


def test_an_unconfigured_mirror_declines_the_tier_instead_of_granting_it():
    """A claim nobody checked must never become a tier. This is the case a live
    chain cannot produce on demand, and the one where a careless `except` would
    silently upgrade every caller."""
    a = _present(_gate(_Mirror(configured=False)), _signer("acr-buyer-1"), human_cluster=FLEET)
    assert a.tier == TIER_CARDED
    assert a.cluster is None
    assert "not configured" in a.human_note


def test_an_unreachable_mirror_declines_the_tier_and_is_not_a_500():
    a = _present(
        _gate(_Mirror(raises=TimeoutError("rpc down"))), _signer("acr-buyer-1"),
        human_cluster=FLEET,
    )
    assert a.tier == TIER_CARDED
    assert "unreachable" in a.human_note


# --- every other refusal -----------------------------------------------------


def test_a_card_signed_by_someone_else_is_refused():
    gate = _gate()
    card = mint(_signer("acr-buyer-1").address, name="d", role="reader",
                audience="acr-index-api", ttl_s=300)
    header = encode_header(card, sign_card(card, _signer("acr-buyer-2"), CHAIN))
    with pytest.raises(HTTPException, match="does not match its agent"):
        gate.verify(_Req(), header)


def test_a_card_addressed_elsewhere_is_refused():
    """This is what stands in for `verifyingContract`. Without it, a card minted
    for any other ACR-domain service is presentable here."""
    with pytest.raises(HTTPException, match="addressed to"):
        _present(_gate(), _signer("acr-buyer-1"), audience="someone-elses-api")


def test_an_expired_card_is_refused():
    gate = _gate()
    s = _signer("acr-buyer-1")
    card = mint(s.address, name="d", role="reader", audience="acr-index-api",
                ttl_s=60, now=int(time.time()) - 600)
    with pytest.raises(HTTPException, match="has expired"):
        gate.verify(_Req(), encode_header(card, sign_card(card, s, CHAIN)))


def test_a_card_from_the_future_is_refused():
    gate = _gate()
    s = _signer("acr-buyer-1")
    card = mint(s.address, name="d", role="reader", audience="acr-index-api",
                ttl_s=300, now=int(time.time()) + 3600)
    with pytest.raises(HTTPException, match="not valid yet"):
        gate.verify(_Req(), encode_header(card, sign_card(card, s, CHAIN)))


def test_a_card_claiming_an_unbounded_lifetime_is_refused_at_the_gate_too():
    """`mint` refuses it, but a hostile agent does not use `mint`. The gate must
    hold the bound independently, or the only thing enforcing it is our own
    client library."""
    gate = _gate()
    s = _signer("acr-buyer-1")
    good = mint(s.address, name="d", role="reader", audience="acr-index-api", ttl_s=300)
    forged = with_window(good, expires_at=good.issued_at + MAX_TTL_S * 10)
    with pytest.raises(HTTPException, match="exceeds the"):
        gate.verify(_Req(), encode_header(forged, sign_card(forged, s, CHAIN)))


def test_an_unknown_role_is_refused_at_the_gate_too():
    gate = _gate()
    s = _signer("acr-buyer-1")
    good = mint(s.address, name="d", role="reader", audience="acr-index-api", ttl_s=300)
    forged = with_window(good, role="superuser")
    with pytest.raises(HTTPException, match="unknown role"):
        gate.verify(_Req(), encode_header(forged, sign_card(forged, s, CHAIN)))


def test_a_malformed_header_is_a_401_not_a_500():
    with pytest.raises(HTTPException, match="malformed agent card"):
        _gate().verify(_Req(), "this is not base64")


# --- the challenge and what /agent/info reports ------------------------------


def test_the_challenge_names_everything_an_agent_needs_to_mint_one():
    """An agent author should not have to read our source to build a card."""
    exc = _gate().challenge(_Req())
    body = exc.detail
    assert body["header"] == "AGENT-CARD"
    assert body["audience"] == "acr-index-api"
    assert body["chain_id"] == CHAIN
    assert body["domain"] == {"name": "ACR Agent Card", "version": "1"}
    assert body["max_ttl_seconds"] == MAX_TTL_S
    assert "reader" in body["roles"]
    assert body["human_binding"]["optional"] is True
    assert exc.status_code == 401


def test_info_reports_whether_the_human_tier_is_even_reachable():
    """A gate that cannot check claims and one that grants the tier freely look
    identical from outside. /humanid/info reports its salt for the same reason."""
    assert _gate(_fleet_mirror()).info()["human_binding_verifiable"] is True
    assert _gate(_Mirror(configured=False)).info()["human_binding_verifiable"] is False


def test_counters_separate_cards_from_human_tier_grants():
    gate = _gate(_fleet_mirror())
    _present(gate, _signer("acr-buyer-1"))
    _present(gate, _signer("acr-buyer-2"), human_cluster=FLEET)
    info = gate.info()
    assert info["cards_verified"] == 2
    assert info["human_tier_granted"] == 1
