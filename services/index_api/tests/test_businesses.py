"""The business registry — and the three ways it refuses to flatter the numbers.

A registry is a place traction gets exaggerated, so the tests are mostly about
that: consent gates the NAME and never the count, mainnet and testnet are never
summed, and two businesses cannot share one treasury.
"""

from __future__ import annotations

import json

from index_api.businesses import (
    Business,
    by_treasury,
    counts,
    load,
    resolve,
)

A = "0x" + "aa" * 20
B = "0x" + "bb" * 20


def _write(tmp_path, rows, key="businesses"):
    p = tmp_path / "businesses.json"
    p.write_text(json.dumps({key: rows} if key else rows))
    return str(p)


# --- an empty or broken registry is a working state ------------------------

def test_a_missing_registry_is_no_businesses_not_an_error():
    """Every host starts here, and it must start, not crash."""
    assert load("/no/such/registry.json") == ()


def test_an_unreadable_registry_is_no_businesses(tmp_path):
    p = tmp_path / "businesses.json"
    p.write_text("{ this is not json")
    assert load(str(p)) == ()


def test_a_registry_that_is_not_a_list_is_rejected_whole(tmp_path):
    p = tmp_path / "businesses.json"
    p.write_text(json.dumps({"businesses": {"slug": "nope"}}))
    assert load(str(p)) == ()


def test_a_bare_list_is_accepted_too(tmp_path):
    path = _write(tmp_path, [{"slug": "acme", "treasury": A}], key=None)
    assert [b.slug for b in load(path)] == ["acme"]


# --- rows that are not businesses -----------------------------------------

def test_a_row_with_no_treasury_is_not_a_business(tmp_path):
    """The treasury IS the identity; without it nothing downstream can key."""
    path = _write(tmp_path, [{"slug": "ghost"}, {"slug": "real", "treasury": A}])
    assert [b.slug for b in load(path)] == ["real"]


def test_a_row_with_no_slug_is_dropped(tmp_path):
    path = _write(tmp_path, [{"treasury": A}, {"slug": "real", "treasury": B}])
    assert [b.slug for b in load(path)] == ["real"]


def test_one_bad_row_does_not_take_the_registry_down(tmp_path):
    path = _write(tmp_path, ["not-a-dict", {"slug": "real", "treasury": A}])
    assert [b.slug for b in load(path)] == ["real"]


def test_two_businesses_cannot_share_one_treasury(tmp_path):
    """They would merge their ledgers and their budgets. Dropped rather than
    quietly last-wins, because last-wins depends on file order."""
    path = _write(tmp_path, [
        {"slug": "first", "treasury": A},
        {"slug": "second", "treasury": A.upper()},
    ])
    reg = load(path)
    assert [b.slug for b in reg] == ["first"]


def test_an_unknown_tier_or_chain_falls_back_rather_than_inventing_one(tmp_path):
    path = _write(tmp_path, [
        {"slug": "x", "treasury": A, "tier": "enterprise", "chain": "solana"}
    ])
    b = load(path)[0]
    assert b.tier == "own"
    assert b.chain == "testnet", "never defaults to the chain that counts for more"


# --- consent gates the name, not the count --------------------------------

def test_an_unconsented_business_is_counted_and_not_named(tmp_path):
    path = _write(tmp_path, [
        {"slug": "acme", "treasury": A, "name": "Acme Ltd", "consented": False}
    ])
    b = load(path)[0]
    assert b.public_name == "business acme"
    assert counts(load(path))["businesses"] == 1, "still real usage"
    assert counts(load(path))["consented"] == 0


def test_a_consented_business_is_named(tmp_path):
    path = _write(tmp_path, [
        {"slug": "acme", "treasury": A, "name": "Acme Ltd", "consented": True}
    ])
    assert load(path)[0].public_name == "Acme Ltd"


def test_the_public_shape_omits_the_name_rather_than_blanking_it():
    """A key present and empty invites a UI to render an unnamed row as though
    the name were merely missing."""
    withheld = Business(slug="acme", treasury=A, name="Acme Ltd", consented=False)
    given = Business(slug="acme", treasury=A, name="Acme Ltd", consented=True)

    assert "name" not in withheld.as_public_dict()
    assert withheld.as_public_dict()["label"] == "business acme"
    assert given.as_public_dict()["name"] == "Acme Ltd"


def test_a_pseudonym_is_stable_so_the_same_business_reads_the_same_way():
    b1 = Business(slug="acme", treasury=A)
    b2 = Business(slug="acme", treasury=A, name="Acme Ltd")
    assert b1.public_name == b2.public_name


# --- what the registry says about spending authority ----------------------

def test_a_business_with_no_policy_wallet_can_be_measured_but_not_spent_for():
    """That is what an evaluation looks like, and it is a real state rather than
    a half-configured one."""
    measured = Business(slug="eval", treasury=A)
    spending = Business(slug="live", treasury=B, policy_wallet="0x" + "cc" * 20)
    assert measured.as_public_dict()["spends"] is False
    assert spending.as_public_dict()["spends"] is True


# --- lookups ---------------------------------------------------------------

def test_a_business_resolves_by_slug_or_by_address(tmp_path):
    """A URL will carry either, and a reader should not have to know which."""
    path = _write(tmp_path, [{"slug": "acme", "treasury": A}])
    reg = load(path)
    assert resolve("acme", reg).slug == "acme"
    assert resolve(A, reg).slug == "acme"
    assert resolve(A.upper(), reg).slug == "acme", "addresses arrive in both cases"
    assert resolve("nobody", reg) is None
    assert resolve("", reg) is None


def test_lookup_by_treasury_ignores_surrounding_whitespace(tmp_path):
    path = _write(tmp_path, [{"slug": "acme", "treasury": A}])
    assert by_treasury(f"  {A}  ", load(path)).slug == "acme"


# --- the traction numbers --------------------------------------------------

def test_mainnet_and_testnet_are_counted_separately_and_never_summed(tmp_path):
    path = _write(tmp_path, [
        {"slug": "a", "treasury": A, "chain": "mainnet"},
        {"slug": "b", "treasury": B, "chain": "testnet"},
        {"slug": "c", "treasury": "0x" + "cc" * 20, "chain": "testnet"},
    ])
    c = counts(load(path))
    assert c["businesses"] == 3
    assert c["mainnet"] == 1 and c["testnet"] == 2
    assert "total_chain" not in c, "there is no number that adds the two"


def test_tiers_are_reported_apart_because_they_are_different_evidence(tmp_path):
    """'Our own company' and 'a studio we have never met' are not the same
    traction, and one number would hide which is which."""
    path = _write(tmp_path, [
        {"slug": "a", "treasury": A, "tier": "own"},
        {"slug": "b", "treasury": B, "tier": "network"},
        {"slug": "c", "treasury": "0x" + "cc" * 20, "tier": "network"},
    ])
    c = counts(load(path))
    assert c["by_tier"]["own"] == 1
    assert c["by_tier"]["network"] == 2
    assert c["by_tier"]["cohort"] == 0


def test_an_empty_registry_reports_zero_rather_than_nothing(tmp_path):
    """A traction page that renders nothing for zero looks broken; zero is the
    honest answer and it has to be sayable."""
    c = counts(())
    assert c["businesses"] == 0
    assert c["by_tier"] == {"own": 0, "network": 0, "cohort": 0, "oss": 0}


def test_the_committed_registry_is_valid():
    """The shipped file must parse, whatever is in it. A registry that fails to
    load in production looks exactly like a product with no users."""
    load()  # the real REGISTRY_PATH; must not raise
