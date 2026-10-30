"""Human-proof gate tests — and the honesty of what a proof is NOT allowed to do.

The interesting cases are the refusals: a replayed proof, an expired nonce, a
proof scoped to another app, a salt that does not match the mirror's commitment,
and every transport failure — each of which must end as a 401 or 503 and never as
a served answer. Plus the two disclosure properties the privacy claim rests on:
the nullifier must not reach a response or an unmasked log line, and the salt
must not reach either at all.
"""

from __future__ import annotations

import logging

import pytest
from acr_core.config import ACRSettings
from acr_oracle_client.humanid import cluster_id, current_window
from fastapi import HTTPException
from fastapi.testclient import TestClient
from index_api import tca as tca_mod
from index_api.humanid import (
    AgentKitVerifier,
    DevHumanVerifier,
    _Nonces,
    get_verifier,
    mask,
    reset_verifier,
    set_verifier,
)

NULLIFIER = "0x" + "11" * 32
SALT = "0x" + "22" * 32
# keccak256(SALT) — the value HumanIdMirror would have been deployed with.
COMMITMENT = "0xc4bd59e1394781d1c7bf20a2c0b30c2acc9fbdd52dc5e0d76917de4034ebdf59"


def _env(monkeypatch, **over):
    from acr_core import reset_settings

    base = {
        "ACR_HUMANID_MODE": "dev",
        "ACR_HUMANID_SALT": SALT,
        "ACR_HUMANID_SALT_COMMITMENT": COMMITMENT,
        "ACR_SUBGRAPH_URL": "https://example.invalid",
    }
    base.update(over)
    for k, v in base.items():
        monkeypatch.setenv(k, v)
    reset_settings()
    reset_verifier()


def _client() -> TestClient:
    from index_api.app import app

    return TestClient(app)


def _proof(client: TestClient) -> tuple[dict, str]:
    """Take a challenge and answer it — the flow a real caller performs."""
    nonce = client.get("/tca/human").json()["nonce"]
    return {"HUMAN-PROOF": f"humanid {NULLIFIER}:{nonce}"}, nonce


def _fake_graph(monkeypatch, cluster_payload, days_payload):
    def fake(url, query, variables, key):
        return cluster_payload if "humanCluster" in query else days_payload

    monkeypatch.setattr(tca_mod, "graph_query", fake)


# --- the challenge -----------------------------------------------------------


def test_a_request_without_a_proof_is_challenged_with_a_nonce(monkeypatch):
    _env(monkeypatch)
    try:
        r = _client().get("/tca/human")
        assert r.status_code == 401
        body = r.json()
        assert body["error"] == "human proof required"
        assert len(body["nonce"]) == 32
        # Scoped to this resource, so a proof minted for another route cannot be
        # presented here.
        assert body["resource"] == "/tca/human"
        assert r.headers["WWW-Authenticate"].startswith("HumanProof")
    finally:
        monkeypatch.undo()
        reset_verifier()


def test_a_verified_proof_derives_the_cluster_the_resolver_would_record(monkeypatch):
    """The cluster the API looks up must be the one the resolver wrote. If these
    disagreed the lookup would miss and every human would read as unresolved —
    a silent zero rather than an error."""
    _env(monkeypatch)
    try:
        client = _client()
        _fake_graph(monkeypatch, {"humanCluster": {"wallets": []}}, {})
        headers, _ = _proof(client)
        got = client.get("/tca/human", headers=headers).json()
        expected = "0x" + cluster_id(NULLIFIER, SALT, current_window()).hex()
        assert got["human"]["cluster"] == expected
    finally:
        monkeypatch.undo()
        reset_verifier()


# --- the refusals ------------------------------------------------------------


def test_a_replayed_proof_is_refused(monkeypatch):
    """A nonce spendable twice would turn 'this human authorized this call' into
    'this human authorized one call, and anyone who saw it can repeat it'."""
    _env(monkeypatch)
    try:
        client = _client()
        _fake_graph(monkeypatch, {"humanCluster": {"wallets": []}}, {})
        headers, _ = _proof(client)
        assert client.get("/tca/human", headers=headers).status_code == 200
        assert client.get("/tca/human", headers=headers).status_code == 401
    finally:
        monkeypatch.undo()
        reset_verifier()


def test_an_expired_nonce_is_refused():
    nonces = _Nonces(ttl_s=300.0)
    n = nonces.issue(now=0.0)
    assert nonces.spend(n, now=301.0) is False


def test_a_nonce_is_spendable_exactly_once():
    nonces = _Nonces()
    n = nonces.issue(now=0.0)
    assert nonces.spend(n, now=1.0) is True
    assert nonces.spend(n, now=2.0) is False


def test_an_unknown_nonce_is_refused():
    assert _Nonces().spend("never-issued", now=1.0) is False


@pytest.mark.parametrize(
    "header",
    ["", "garbage", "bearer 0xabc:nonce", "humanid no-colon-here"],
)
def test_a_malformed_or_wrongly_schemed_proof_is_refused(monkeypatch, header):
    _env(monkeypatch)
    try:
        r = _client().get("/tca/human", headers={"HUMAN-PROOF": header})
        assert r.status_code == 401
    finally:
        monkeypatch.undo()
        reset_verifier()


def test_a_salt_that_does_not_match_the_commitment_fails_closed(monkeypatch):
    """A wrong salt derives cluster ids that match nothing, so every human comes
    back empty — indistinguishable from 'this human never traded'. Refuse loudly
    instead of publishing a silent zero."""
    _env(monkeypatch, ACR_HUMANID_SALT_COMMITMENT="0x" + "ff" * 32)
    try:
        client = _client()
        headers, _ = _proof(client)
        r = client.get("/tca/human", headers=headers)
        assert r.status_code == 503
        assert "commitment" in r.json()["detail"]
    finally:
        monkeypatch.undo()
        reset_verifier()


def test_no_salt_configured_fails_closed(monkeypatch):
    _env(monkeypatch, ACR_HUMANID_SALT="")
    try:
        client = _client()
        headers, _ = _proof(client)
        assert client.get("/tca/human", headers=headers).status_code == 503
    finally:
        monkeypatch.undo()
        reset_verifier()


# --- disclosure --------------------------------------------------------------


def test_neither_the_nullifier_nor_the_salt_reaches_a_response(monkeypatch):
    _env(monkeypatch)
    try:
        client = _client()
        _fake_graph(monkeypatch, {"humanCluster": {"wallets": []}}, {})
        headers, _ = _proof(client)
        body = client.get("/tca/human", headers=headers).text
        assert NULLIFIER[2:] not in body
        assert SALT[2:] not in body
        # …nor into the challenge, which is served before anything is verified.
        assert SALT[2:] not in client.get("/tca/human").text
    finally:
        monkeypatch.undo()
        reset_verifier()


def test_the_nullifier_is_masked_in_logs(monkeypatch, caplog):
    _env(monkeypatch)
    try:
        client = _client()
        _fake_graph(monkeypatch, {"humanCluster": {"wallets": []}}, {})
        headers, _ = _proof(client)
        with caplog.at_level(logging.INFO, logger="index_api.humanid"):
            client.get("/tca/human", headers=headers)
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "••••" in text
        assert NULLIFIER[2:] not in text
    finally:
        monkeypatch.undo()
        reset_verifier()


def test_mask_keeps_a_tail_short_enough_to_be_nobody():
    assert mask("0xdeadbeef") == "••••eef"
    assert mask("ab") == "••••?"


def test_the_response_never_enumerates_the_fleet(monkeypatch):
    """The caller knows their own wallets; a response that listed them would hand
    a fleet to anyone who later saw it."""
    _env(monkeypatch)
    try:
        client = _client()
        wallets = ["0x" + "a1" * 20, "0x" + "b2" * 20]
        _fake_graph(
            monkeypatch,
            {"humanCluster": {"wallets": [{"id": w} for w in wallets]}},
            {"payerDays": [], "settlements": []},
        )
        headers, _ = _proof(client)
        body = client.get("/tca/human", headers=headers)
        assert body.json()["human"]["wallet_count"] == 2
        for w in wallets:
            assert w[2:] not in body.text
    finally:
        monkeypatch.undo()
        reset_verifier()


# --- the union ---------------------------------------------------------------


def test_the_fleet_is_aggregated_as_one_book(monkeypatch):
    """Two wallets, one human. The reroute must see a single book: unioning two
    finished cards afterwards could recommend a move the fleet already made."""
    _env(monkeypatch)
    try:
        client = _client()
        _fake_graph(
            monkeypatch,
            {"humanCluster": {"wallets": [{"id": "0x" + "a1" * 20}, {"id": "0x" + "b2" * 20}]}},
            {
                "payerDays": [
                    {"day": 1, "spent": "600000", "bmSpent": "600000",
                     "wSlipTenthBp": "600000000", "overpay": "60000", "n": 30, "nAll": 30},
                ],
                "settlements": [
                    {"seller": {"id": "0xdear"}, "amount": "400000",
                     "slippageTenthBp": "2000", "synthetic": False, "index": "ACR-INF"},
                    {"seller": {"id": "0xcheap"}, "amount": "200000",
                     "slippageTenthBp": "-100", "synthetic": False, "index": "ACR-INF"},
                ],
            },
        )
        headers, _ = _proof(client)
        r = client.get("/tca/human", headers=headers).json()
        assert r["available"] is True
        assert r["human"]["wallet_count"] == 2
        # Worst first, and both wallets' sellers in ONE breakdown.
        assert [b["seller"] for b in r["by_seller"]] == ["0xdear", "0xcheap"]
        assert r["reroute"]["from"] == "0xdear"
        assert r["reroute"]["to"] == "0xcheap"
    finally:
        monkeypatch.undo()
        reset_verifier()


def test_a_human_with_no_wallets_says_so_rather_than_reporting_zero_spend(monkeypatch):
    _env(monkeypatch)
    try:
        client = _client()
        _fake_graph(monkeypatch, {"humanCluster": None}, {})
        headers, _ = _proof(client)
        r = client.get("/tca/human", headers=headers).json()
        assert r["available"] is False
        assert "no wallets are resolved" in r["reason"]
        assert r["human"]["wallet_count"] == 0
    finally:
        monkeypatch.undo()
        reset_verifier()


def test_a_window_longer_than_the_rotation_is_refused(monkeypatch):
    """A cluster describes THIS window's fleet. Reaching further back would union
    today's wallets over a period they may not have been the fleet for."""
    _env(monkeypatch)
    try:
        client = _client()
        headers, _ = _proof(client)
        r = client.get("/tca/human?days=30", headers=headers).json()
        assert r["available"] is False
        assert "rotation" in r["reason"]
    finally:
        monkeypatch.undo()
        reset_verifier()


# --- backend selection + the agentkit path -----------------------------------


def test_mode_dev_wins_over_agentkit_config(monkeypatch):
    _env(monkeypatch, ACR_HUMANID_VERIFIER_URL="https://verifier.example",
         ACR_HUMANID_APP_ID="app_123")
    try:
        assert isinstance(get_verifier(), DevHumanVerifier)
    finally:
        monkeypatch.undo()
        reset_verifier()


def test_auto_mode_stays_on_the_mock_until_something_is_configured(monkeypatch):
    """AgentKit needs no verifier URL — AgentBook's address and RPC are public
    constants — so the old rule (a URL must look real) no longer decides
    anything. What decides is whether the operator configured an app at all."""
    _env(monkeypatch, ACR_HUMANID_MODE="auto", ACR_HUMANID_VERIFIER_URL="",
         ACR_HUMANID_APP_ID="")
    try:
        assert isinstance(get_verifier(), DevHumanVerifier)
    finally:
        monkeypatch.undo()
        reset_verifier()


def test_auto_mode_selects_agentkit_once_an_app_is_configured(monkeypatch):
    _env(monkeypatch, ACR_HUMANID_MODE="auto", ACR_HUMANID_APP_ID="app_123")
    try:
        assert isinstance(get_verifier(), AgentKitVerifier)
    finally:
        monkeypatch.undo()
        reset_verifier()


class _StubBook:
    """An AgentBook that knows exactly one wallet."""

    source = "stub"

    def __init__(self, wallet: str | None = None, nullifier: str = "0x" + "11" * 32):
        self._wallet = (wallet or "").lower()
        self._nullifier = nullifier

    def lookup(self, wallet: str):
        from acr_oracle_client.agentbook import Registration

        if wallet.lower() != self._wallet:
            return None
        return Registration(wallet=wallet, human_id=int(self._nullifier, 16), sandbox=True)

    def roster(self):
        return None


def _agentkit_header(key, *, nonce, uri="/tca/human", issued=None, expires=None,
                     address=None, sigtype="eip191", tamper_msg=None):
    """A signed CAIP-122/SIWE payload, base64-JSON, as World's header carries it."""
    import base64 as b64
    import datetime as dt
    import json as js

    from eth_account import Account
    from eth_account.messages import encode_defunct

    acct = Account.from_key(key)
    issued = issued or dt.datetime.now(dt.UTC).isoformat()
    raw = tamper_msg or (
        f"acr.test wants you to sign in with your account:\n{acct.address}\n\n"
        f"URI: {uri}\nVersion: 1\nChain ID: 480\nNonce: {nonce}\nIssued At: {issued}"
    )
    sig = Account.sign_message(encode_defunct(text=raw), private_key=key).signature.hex()
    payload = {
        "address": address or acct.address, "nonce": nonce, "issuedAt": issued,
        "uri": uri, "chainId": "eip155:480", "signedMessage": raw,
        "signature": sig if sig.startswith("0x") else "0x" + sig, "type": sigtype,
    }
    if expires:
        payload["expirationTime"] = expires
    return b64.b64encode(js.dumps(payload).encode()).decode(), acct.address


def _agentkit(monkeypatch, wallet_known: bool = True):
    """A verifier wired to a stub book, plus a fresh challenge nonce."""
    import types as _t

    from eth_account import Account
    from index_api.humanid import AgentKitVerifier

    key = "0x" + "77" * 32
    address = Account.from_key(key).address
    v = AgentKitVerifier(book=_StubBook(address if wallet_known else None))
    req = _t.SimpleNamespace(url=_t.SimpleNamespace(path="/tca/human"))
    return v, key, address, req


def test_agentkit_accepts_a_signed_message_from_a_registered_wallet(monkeypatch):
    """The real flow: recover the signer, then ask AgentBook whose wallet it is."""
    import asyncio

    _env(monkeypatch)
    try:
        v, key, address, req = _agentkit(monkeypatch)
        header, _ = _agentkit_header(key, nonce=v.nonces.issue())
        nullifier, observed = asyncio.run(v.nullifier_of(req, header))
        assert nullifier == "0x" + "11" * 32
        # AgentBook stores a nullifier and nothing about how the human proved
        # themselves, so provenance is NOT observable on this path.
        assert observed is None
    finally:
        monkeypatch.undo()
        reset_verifier()


def test_agentkit_refuses_a_wallet_nobody_registered(monkeypatch):
    """`lookupHuman` returns 0 for an unregistered wallet. That is the common
    case and an honest answer, not an error on our side."""
    import asyncio

    _env(monkeypatch)
    try:
        v, key, _, req = _agentkit(monkeypatch, wallet_known=False)
        header, _ = _agentkit_header(key, nonce=v.nonces.issue())
        with pytest.raises(HTTPException) as e:
            asyncio.run(v.nullifier_of(req, header))
        assert e.value.status_code == 401
        assert "not registered in AgentBook" in e.value.detail
    finally:
        monkeypatch.undo()
        reset_verifier()


def test_agentkit_refuses_a_signature_from_another_key(monkeypatch):
    """Claiming an address you cannot sign for must not resolve to its human."""
    import asyncio

    _env(monkeypatch)
    try:
        v, _, address, req = _agentkit(monkeypatch)
        # Signed by an impostor, but naming the registered address.
        header, _ = _agentkit_header("0x" + "99" * 32, nonce=v.nonces.issue(), address=address)
        with pytest.raises(HTTPException) as e:
            asyncio.run(v.nullifier_of(req, header))
        assert e.value.status_code == 401
        assert "does not match the address" in e.value.detail
    finally:
        monkeypatch.undo()
        reset_verifier()


def test_agentkit_refuses_a_stale_proof(monkeypatch):
    """Freshness lives inside the signed message, so it cannot be back-dated
    without invalidating the signature — but an OLD one must still be refused."""
    import asyncio
    import datetime as dt

    _env(monkeypatch)
    try:
        v, key, _, req = _agentkit(monkeypatch)
        old = (dt.datetime.now(dt.UTC) - dt.timedelta(hours=2)).isoformat()
        header, _ = _agentkit_header(key, nonce=v.nonces.issue(), issued=old)
        with pytest.raises(HTTPException) as e:
            asyncio.run(v.nullifier_of(req, header))
        assert "stale" in e.value.detail
    finally:
        monkeypatch.undo()
        reset_verifier()


def test_agentkit_refuses_a_proof_bound_to_another_resource(monkeypatch):
    """Context is enforced against OUR view of the request, never against what
    the payload asserts about itself."""
    import asyncio

    _env(monkeypatch)
    try:
        v, key, _, req = _agentkit(monkeypatch)
        header, _ = _agentkit_header(key, nonce=v.nonces.issue(), uri="/some/other/route")
        with pytest.raises(HTTPException) as e:
            asyncio.run(v.nullifier_of(req, header))
        assert "bound to another resource" in e.value.detail
    finally:
        monkeypatch.undo()
        reset_verifier()


def test_agentkit_refuses_a_replayed_nonce(monkeypatch):
    import asyncio

    _env(monkeypatch)
    try:
        v, key, _, req = _agentkit(monkeypatch)
        nonce = v.nonces.issue()
        header, _ = _agentkit_header(key, nonce=nonce)
        asyncio.run(v.nullifier_of(req, header))
        with pytest.raises(HTTPException) as e:
            asyncio.run(v.nullifier_of(req, header))
        assert "nonce is not spendable" in e.value.detail
    finally:
        monkeypatch.undo()
        reset_verifier()


def test_agentkit_refuses_a_smart_wallet_signature_rather_than_waving_it_through(monkeypatch):
    """An EIP-1271 signature cannot be recovered — it has to be asked of the
    wallet on its own chain. Refusing beats accepting it unverified."""
    import asyncio

    _env(monkeypatch)
    try:
        v, key, _, req = _agentkit(monkeypatch)
        header, _ = _agentkit_header(key, nonce=v.nonces.issue(), sigtype="eip1271")
        with pytest.raises(HTTPException) as e:
            asyncio.run(v.nullifier_of(req, header))
        assert "eip1271" in e.value.detail
    finally:
        monkeypatch.undo()
        reset_verifier()


def test_agentkit_refuses_a_tampered_message(monkeypatch):
    """The signature covers the message; changing it must break recovery."""
    import asyncio

    _env(monkeypatch)
    try:
        v, key, _, req = _agentkit(monkeypatch)
        nonce = v.nonces.issue()
        header, _ = _agentkit_header(key, nonce=nonce)
        import base64 as b64
        import json as js

        payload = js.loads(b64.b64decode(header))
        payload["signedMessage"] = payload["signedMessage"] + " (edited)"
        tampered = b64.b64encode(js.dumps(payload).encode()).decode()
        with pytest.raises(HTTPException) as e:
            asyncio.run(v.nullifier_of(req, tampered))
        assert e.value.status_code == 401
    finally:
        monkeypatch.undo()
        reset_verifier()


def test_agentkit_uses_worlds_own_header_name():
    from index_api.humanid import AgentKitVerifier

    assert AgentKitVerifier.HEADER == "agentkit"


def test_the_verifier_is_injectable_for_tests(monkeypatch):
    _env(monkeypatch)
    try:
        mine = DevHumanVerifier()
        set_verifier(mine)
        assert get_verifier() is mine
    finally:
        monkeypatch.undo()
        reset_verifier()


def test_humanid_info_reports_the_backend_and_the_sandbox_limit(monkeypatch):
    """Nobody should have to take the README's word for what 'verified human'
    means in this deployment."""
    _env(monkeypatch)
    try:
        info = _client().get("/humanid/info").json()
        assert info["backend"] == "dev"
        assert info["sandbox"] is True
        assert info["salt_matches_commitment"] is True
        assert info["rotation_window_days"] == 7
    finally:
        monkeypatch.undo()
        reset_verifier()


def test_a_settings_object_never_carries_a_salt_by_default():
    """A default salt would be a shared secret in a public repo."""
    assert ACRSettings(_env_file=None).humanid_salt == ""


# --- the cloud-verify path ----------------------------------------------------


def _cloud(monkeypatch, responses):
    """A WorldIdCloudVerifier whose HTTP calls replay canned (status, body)."""
    import types as _t

    from index_api.humanid import WorldIdCloudVerifier

    v = WorldIdCloudVerifier()
    seen = []

    async def fake_post(url, payload):
        seen.append(url)
        return responses[min(len(seen) - 1, len(responses) - 1)]

    monkeypatch.setattr(v, "_post", fake_post)
    req = _t.SimpleNamespace(url=_t.SimpleNamespace(path="/tca/human"))
    return v, req, seen


def _idkit(payload=None):
    import base64 as b64
    import json as js

    return b64.b64encode(js.dumps(payload or {"proof": "0xabc"}).encode()).decode()


def test_cloud_v4_returns_the_nullifier_and_observes_the_environment(monkeypatch):
    """v4 is the ONE place any of this can observe provenance rather than assert
    it — the response carries `environment`."""
    import asyncio

    _env(monkeypatch, ACR_HUMANID_MODE="worldid", ACR_HUMANID_APP_ID="app_test")
    try:
        v, req, seen = _cloud(monkeypatch, [(200, {
            "success": True, "nullifier": "0x2a", "environment": "staging"})])
        nullifier, observed = asyncio.run(v.nullifier_of(req, _idkit()))
        assert nullifier == "0x" + "00" * 31 + "2a"
        assert observed is True          # staging → sandbox
        assert "/api/v4/verify/app_test" in seen[0]
    finally:
        monkeypatch.undo()
        reset_verifier()


def test_cloud_v4_production_is_not_sandbox(monkeypatch):
    import asyncio

    _env(monkeypatch, ACR_HUMANID_MODE="worldid", ACR_HUMANID_APP_ID="app_test")
    try:
        v, req, _ = _cloud(monkeypatch, [(200, {
            "success": True, "nullifier": "0x1", "environment": "production"})])
        _, observed = asyncio.run(v.nullifier_of(req, _idkit()))
        assert observed is False
    finally:
        monkeypatch.undo()
        reset_verifier()


def test_cloud_falls_back_to_v2_only_on_app_not_migrated(monkeypatch):
    """And v2 answers with `nullifier_hash`, not `nullifier` — a rename that
    would otherwise read as 'no nullifier returned'."""
    import asyncio

    _env(monkeypatch, ACR_HUMANID_MODE="worldid", ACR_HUMANID_APP_ID="app_test")
    try:
        v, req, seen = _cloud(monkeypatch, [
            (400, {"code": "app_not_migrated", "detail": "use v2"}),
            (200, {"success": True, "nullifier_hash": "0x7b"}),
        ])
        nullifier, observed = asyncio.run(v.nullifier_of(req, _idkit()))
        assert nullifier.endswith("7b")
        # v2 carries no environment: unobservable, which is NOT the same as
        # "production" and must not be reported as if it were.
        assert observed is None
        assert "/api/v4/" in seen[0] and "/api/v2/" in seen[1]
    finally:
        monkeypatch.undo()
        reset_verifier()


def test_cloud_reports_the_verifiers_own_reason(monkeypatch):
    import asyncio

    _env(monkeypatch, ACR_HUMANID_MODE="worldid", ACR_HUMANID_APP_ID="app_test")
    try:
        v, req, _ = _cloud(monkeypatch, [(400, {
            "code": "invalid_proof", "detail": "The provided proof is invalid"})])
        with pytest.raises(HTTPException) as e:
            asyncio.run(v.nullifier_of(req, _idkit()))
        assert e.value.status_code == 401
        assert "invalid" in e.value.detail.lower()
    finally:
        monkeypatch.undo()
        reset_verifier()


def test_cloud_normalises_a_decimal_nullifier(monkeypatch):
    """World's own guidance stores nullifiers as numerics; `cluster_id` wants 32
    bytes. Normalising in one place beats each caller guessing the form."""
    import asyncio

    _env(monkeypatch, ACR_HUMANID_MODE="worldid", ACR_HUMANID_APP_ID="app_test")
    try:
        v, req, _ = _cloud(monkeypatch, [(200, {"success": True, "nullifier": "255"})])
        nullifier, _ = asyncio.run(v.nullifier_of(req, _idkit()))
        assert nullifier == "0x" + "00" * 31 + "ff"
    finally:
        monkeypatch.undo()
        reset_verifier()


def test_cloud_without_an_app_id_fails_closed(monkeypatch):
    import asyncio

    _env(monkeypatch, ACR_HUMANID_MODE="worldid", ACR_HUMANID_APP_ID="")
    try:
        v, req, _ = _cloud(monkeypatch, [(200, {"success": True, "nullifier": "0x1"})])
        with pytest.raises(HTTPException) as e:
            asyncio.run(v.nullifier_of(req, _idkit()))
        assert e.value.status_code == 503
    finally:
        monkeypatch.undo()
        reset_verifier()


def test_an_app_id_alone_selects_a_real_gate_not_the_mock(monkeypatch):
    from index_api.humanid import AgentKitVerifier

    _env(monkeypatch, ACR_HUMANID_MODE="auto", ACR_HUMANID_APP_ID="app_test")
    try:
        assert isinstance(get_verifier(), AgentKitVerifier)
    finally:
        monkeypatch.undo()
        reset_verifier()


def test_the_stray_credential_check_reads_the_env_file_not_just_the_environment(tmp_path,
                                                                                monkeypatch):
    """The failure this catches happens in `.env`, and pydantic reads that file
    without exporting it — so an environment-only scan would miss the very case
    the check exists for."""
    from index_api.humanid import stray_world_credentials

    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("WOrld_app_id=secret\nACR_HUMANID_APP_ID=fine\nOTHER=x\n")
    hits = stray_world_credentials()
    assert "WOrld_app_id" in hits
    # An ACR_-prefixed name is read by settings, so it is not stray…
    assert "ACR_HUMANID_APP_ID" not in hits
    # …and an unrelated variable is nobody's business.
    assert "OTHER" not in hits


def test_the_sdk_header_name_is_read_too_and_the_challenge_names_ours(monkeypatch):
    """Production's 401 said `"header": "agentkit"` — World's own header name — and
    `require_human` read only HUMAN-PROOF, so a client doing exactly what the
    challenge said was re-challenged forever. Both names are read now, and the
    challenge names the one this server reads first, with the SDK's as the alternative."""
    _env(monkeypatch)
    try:
        client = _client()
        _fake_graph(monkeypatch, {"humanCluster": {"wallets": []}}, {})
        challenge = client.get("/tca/human").json()
        assert challenge["header"] == "HUMAN-PROOF"
        # The dev verifier's challenge does not mention the SDK header; the AgentKit
        # one does, and the dependency reads it regardless of which verifier runs.
        ok_ours = client.get("/tca/human", headers={"HUMAN-PROOF": f"humanid {NULLIFIER}:{challenge['nonce']}"})
        assert ok_ours.status_code == 200
        nonce2 = client.get("/tca/human").json()["nonce"]
        ok_sdk = client.get("/tca/human", headers={"agentkit": f"humanid {NULLIFIER}:{nonce2}"})
        assert ok_sdk.status_code == 200, ok_sdk.text
    finally:
        monkeypatch.undo()
        reset_verifier()
