"""Who may read one business's detail, and the four different ways in which they may not.

`/spend` is publicly readable by design — right for review, wrong for a customer
with a real treasury and a real vendor list. This is the gate for the second
case, and it is OFF unless `ACR_OPERATOR_READ_SCOPE` says otherwise.

THE FLAG IS THE POINT, not a convenience. The default path is the one a reviewer
meets, so every existing assertion in `test_operator_api.py` and every check in
`scripts/verify_operator.py` has to keep passing untouched — and does, because
with the flag unset `require_business_read` returns before it looks at anything.
Both states are tested here; a gate whose off-state nobody asserts is a gate that
will be switched on by accident.

FOUR CAUSES, FOUR ANSWERS, and that is most of what this file is for. "Denied"
is useless to the holder of a good card scoped to the wrong business, and equally
useless to someone holding no card at all. Each refusal is asserted on its own
status AND on the sentence it gives, because the sentence is the part a person
acts on.

ORDERING: an unknown business is 404 BEFORE the gate is consulted. That is not a
leak — `/operator/businesses` publishes every slug — and `verify_operator.py`'s
unknown-business check accepts 404/422/503 and not 401, so a gate that fired
first would break the standing audit for no security gain.
"""

from __future__ import annotations

import json

import pytest
from acr_oracle_client.agentcard import ZERO32, encode_header, mint, scope_hash, sign_card
from acr_oracle_client.signer import LocalKeySigner
from fastapi.testclient import TestClient
from index_api import businesses as biz
from index_api.agentgate import business_read_scope, scope_enforced
from index_api.app import app

client = TestClient(app)

CHAIN = 5042002
TREASURY = LocalKeySigner("0x" + "99" * 32)
READER = LocalKeySigner("0x" + "77" * 32)
STRANGER = LocalKeySigner("0x" + "88" * 32)
ROUTES = ("statement", "ledger", "audit")


@pytest.fixture
def registry(tmp_path, monkeypatch):
    """One business whose treasury we hold a key for, with one nominated reader.

    The treasury has to be a key the test can sign with, which a real registry
    row never is — so the row is synthetic and the addresses are derived.
    """
    path = tmp_path / "businesses.json"
    path.write_text(
        json.dumps(
            {
                "businesses": [
                    {
                        "slug": "acme",
                        "treasury": TREASURY.address,
                        "name": "Acme",
                        "consented": True,
                        "tier": "own",
                        "chain": "testnet",
                        # Upper-cased on purpose: addresses are compared
                        # lower-cased on both sides, never raw.
                        "readers": [READER.address.upper()],
                    }
                ]
            }
        )
    )
    monkeypatch.setattr(biz, "REGISTRY_PATH", path)
    return path


@pytest.fixture
def enforced(monkeypatch):
    monkeypatch.setenv("ACR_OPERATOR_READ_SCOPE", "1")


def header(signer: LocalKeySigner, *, slug: str | None = "acme", **kw) -> dict[str, str]:
    card = mint(
        signer.address,
        name="probe",
        role="reader",
        audience=kw.pop("audience", "acr-index-api"),
        scopes=[business_read_scope(slug)] if slug else None,
        ttl_s=kw.pop("ttl_s", 300),
    )
    return {"AGENT-CARD": encode_header(card, sign_card(card, signer, CHAIN))}


# ─────────────────────────────────────────────────── the flag, off

def test_off_by_default_so_nothing_changes_for_a_reviewer(registry):
    """The acceptance criterion for the default path, stated as a test.

    A reviewer arriving with no card reads everything, exactly as before. If this
    fails, the flag has stopped being a flag.
    """
    assert scope_enforced() is False
    for route in ROUTES:
        r = client.get(f"/operator/{route}/acme")
        assert r.status_code == 200, f"{route} refused a reader with the flag off"


def test_off_by_default_ignores_a_card_that_would_be_refused(registry):
    """Not merely "no card is fine" — a WRONG card is also irrelevant when the
    gate is off. Otherwise the off-state would still be enforcing something, and
    the two states would not be the clean pair the rollout depends on."""
    for route in ROUTES:
        r = client.get(f"/operator/{route}/acme", headers=header(STRANGER, slug="somewhere-else"))
        assert r.status_code == 200, route


# ─────────────────────────────────────────────────── the flag, on · admitted

def test_the_treasury_reads_its_own_business(registry, enforced):
    for route in ROUTES:
        r = client.get(f"/operator/{route}/acme", headers=header(TREASURY))
        assert r.status_code == 200, f"{route}: {r.text[:160]}"


def test_a_nominated_reader_reads_it_too(registry, enforced):
    """The reason `readers` exists. Without it, looking at a dashboard means
    bringing the key that SPENDS online — which is the thing a real customer
    would refuse, and rightly."""
    for route in ROUTES:
        r = client.get(f"/operator/{route}/acme", headers=header(READER))
        assert r.status_code == 200, f"{route}: {r.text[:160]}"


def test_the_nomination_is_case_insensitive(registry, enforced):
    """The row lists the reader upper-cased and the card carries the checksum
    form. An address comparison that failed on case would present as "my card is
    wrong" to someone holding a perfectly good card."""
    row = biz.resolve("acme")
    assert row is not None
    assert row.readers == (READER.address.lower(),), "normalised on the way in"
    assert row.may_read(READER.address.upper())
    assert row.may_read(TREASURY.address.lower())


# ─────────────────────────────────────────────────── the flag, on · the four refusals

def test_no_card_is_a_401_that_says_how_to_get_one(registry, enforced):
    """A 401 naming neither the scope nor the header is a dead end. This one
    carries both, in the same base64 challenge envelope the x402 and human-proof
    gates use, so a client decodes one shape everywhere."""
    for route in ROUTES:
        r = client.get(f"/operator/{route}/acme")
        assert r.status_code == 401, route
        assert r.headers.get("WWW-Authenticate") == "AgentCard"
        body = r.json()["detail"]
        assert body["scope"] == "read:business:acme"
        assert body["header"] == "AGENT-CARD"
        assert body["challenge"] == "/agent/challenge"


def test_a_card_for_another_business_is_a_403_that_names_the_scope_wanted(registry, enforced):
    for route in ROUTES:
        r = client.get(f"/operator/{route}/acme", headers=header(READER, slug="somewhere-else"))
        assert r.status_code == 403, route
        detail = r.json()["detail"]
        assert "read:business:acme" in detail
        assert "another scope" in detail


def test_a_card_claiming_nothing_is_told_it_claimed_nothing(registry, enforced):
    """`scopeHash` is the zero word for "claimed no scopes", which
    `scope_hash([])` keeps distinct from a hash of the empty string. A reader who
    minted a card the normal way has exactly this, and "claims nothing" tells
    them what to add; "another scope" would send them looking for a scope they
    never set."""
    r = client.get("/operator/statement/acme", headers=header(READER, slug=None))
    assert r.status_code == 403
    assert "claims nothing" in r.json()["detail"]


def test_a_correctly_scoped_card_from_a_stranger_is_still_refused(registry, enforced):
    """THE ONE THAT MATTERS. Cards are self-minted and keys are free, so a scope
    string alone authorises anybody who can read the slug off the public
    registry. The signer has to be someone the business named."""
    for route in ROUTES:
        r = client.get(f"/operator/{route}/acme", headers=header(STRANGER))
        assert r.status_code == 403, route
        assert "not nominated" in r.json()["detail"] or "has not nominated" in r.json()["detail"]


def test_the_four_refusals_do_not_say_the_same_thing(registry, enforced):
    """A gate that answers one sentence to four causes is a gate nobody can
    configure. Asserted as a set, so a future edit cannot quietly collapse two."""
    said = {
        client.get("/operator/statement/acme").json()["detail"]["reason"],
        client.get("/operator/statement/acme", headers=header(READER, slug="elsewhere")).json()["detail"],
        client.get("/operator/statement/acme", headers=header(READER, slug=None)).json()["detail"],
        client.get("/operator/statement/acme", headers=header(STRANGER)).json()["detail"],
    }
    assert len(said) == 4, said


# ─────────────────────────────────────────────────── what the gate must not touch

def test_an_unknown_business_is_still_404_and_not_401(registry, enforced):
    """Ordering, asserted. The gate runs AFTER the lookup: `/operator/businesses`
    publishes every slug, so 401-before-404 hides nothing, and
    `verify_operator.py` accepts 404/422/503 for an unknown business and not 401.
    A gate that fired first would break the standing audit for no gain."""
    for route in ROUTES:
        assert client.get(f"/operator/{route}/nobody").status_code == 404, route


def test_the_aggregates_stay_public_in_both_states(registry, monkeypatch):
    """Counts are not a vendor list. These two are what `/traction` renders, and
    they are the reason a stranger can still verify the product's claims."""
    for flag in ("", "1"):
        monkeypatch.setenv("ACR_OPERATOR_READ_SCOPE", flag)
        for path in ("/operator/businesses", "/operator/traction"):
            assert client.get(path).status_code == 200, f"{path} at flag={flag!r}"


def test_whoami_reports_whether_the_scope_is_enforced(registry, monkeypatch):
    """This replaced a hardcoded `False`, which was honest for as long as the
    answer was always false. Two things assert it — this and
    `scripts/demo_agent.py` — so it cannot drift back into a constant."""
    for flag, expected in (("", False), ("1", True)):
        monkeypatch.setenv("ACR_OPERATOR_READ_SCOPE", flag)
        r = client.get("/agent/whoami", headers=header(READER))
        assert r.json()["scope_enforced"] is expected


# ─────────────────────────────────────────────────── the scope itself

def test_the_scope_string_is_stable_and_hashes_the_agreed_way():
    """The credential IS the hash, so it is checked against a second computation
    rather than against itself. If this changes, every card already minted stops
    working — which is the point of pinning it."""
    assert business_read_scope("acme") == "read:business:acme"
    assert scope_hash([business_read_scope("acme")]) != ZERO32
    # Reusing the existing encoder, not a new namespace: a card minted by any of
    # the four existing encoders has to verify without changing them.
    assert scope_hash(["read:business:acme"]) == scope_hash([" read:business:acme "])
    assert scope_hash([business_read_scope("a")]) != scope_hash([business_read_scope("b")])
