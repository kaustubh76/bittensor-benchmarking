"""AgentBook tests — the roster's shape, and what a lookup refuses to claim."""

from __future__ import annotations

import types

from acr_core.config import ACRSettings
from acr_oracle_client.agentbook import (
    AGENTBOOK_ADDRESS,
    WORLD_CHAIN_RPC,
    FixtureAgentBook,
    Registration,
    WorldChainAgentBook,
    build_agentbook,
)
from acr_oracle_client.demo_humans import DEMO_HUMANS, DemoBuyer, DemoHuman


def _offline() -> ACRSettings:
    return ACRSettings(_env_file=None)


# --- the fixture roster -------------------------------------------------------


def test_the_roster_is_lopsided_on_purpose():
    """One human with several wallets and one with a single wallet.

    With one wallet per human, `distinctHumans` always equals `distinctPayers`,
    the ratio is always 1.0, and nothing shows why grouping wallets by human
    matters — which is the only thing the human-depth component measures.
    """
    roster = FixtureAgentBook().roster()
    assert len(roster) == 2
    assert sorted(len(h.wallets) for h in roster) == [1, 3]


def test_every_fixture_human_is_flagged_sandbox():
    """Derived identities, not people. The flag reaches the chain and the tape,
    so a rating that counts them says how much of its human depth is demo."""
    assert all(h.sandbox for h in FixtureAgentBook().roster())


def test_a_wallet_never_reveals_the_human_behind_it():
    """Wallet keys and nullifiers come from different namespaces on purpose. If
    one were derivable from the other, a public payer address would give up the
    human and the rotation in HumanIdMirror would be protecting nothing."""
    for human in DEMO_HUMANS:
        for label in human.buyers:
            assert DemoBuyer(label).private_key != human.nullifier


def test_the_roster_is_deterministic():
    assert FixtureAgentBook().roster() == FixtureAgentBook().roster()


def test_wallets_are_distinct_across_humans():
    seen = [w for h in FixtureAgentBook().roster() for w in h.wallets]
    assert len(seen) == len(set(seen))


def test_a_human_with_no_buyers_has_no_wallets():
    assert DemoHuman("nobody", ()).wallets == []


# --- lookup -------------------------------------------------------------------


def test_a_known_wallet_resolves_to_its_human():
    book = FixtureAgentBook()
    human = FixtureAgentBook().roster()[0]
    reg = book.lookup(human.wallets[0])
    assert reg is not None
    assert reg.nullifier == human.nullifier
    assert reg.sandbox is True


def test_every_wallet_of_a_fleet_resolves_to_the_same_human():
    """The property the whole human-depth component rests on."""
    book = FixtureAgentBook()
    fleet = next(h for h in book.roster() if len(h.wallets) > 1)
    ids = {book.lookup(w).nullifier for w in fleet.wallets}
    assert len(ids) == 1


def test_an_unknown_wallet_is_None_not_a_zero_human():
    """Unregistered and 'human zero' must never be one keystroke apart: a zero
    nullifier would derive a cluster id that looked perfectly valid."""
    assert FixtureAgentBook().lookup("0x" + "de" * 20) is None


def test_lookup_does_not_care_about_address_casing():
    book = FixtureAgentBook()
    wallet = book.roster()[0].wallets[0]
    assert book.lookup(wallet.lower()) is not None
    assert book.lookup(wallet.upper().replace("0X", "0x")) is not None


def test_a_registration_renders_its_nullifier_as_bytes32():
    """`cluster_id()` takes 32 bytes; AgentBook stores a uint256."""
    reg = Registration(wallet="0x" + "ab" * 20, human_id=1, sandbox=True)
    assert reg.nullifier == "0x" + "00" * 31 + "01"
    assert len(reg.nullifier) == 66


# --- the live book ------------------------------------------------------------


def test_the_live_book_needs_no_configuration():
    """Address and RPC are published constants, not credentials. An operator
    should not have to discover a value that is the same for everyone."""
    book = WorldChainAgentBook(settings=_offline())
    assert book.address == AGENTBOOK_ADDRESS
    assert book.rpc_url == WORLD_CHAIN_RPC
    assert book.configured() is True


def test_the_live_book_cannot_be_enumerated():
    """`lookupHuman` answers about one wallet. Rebuilding a roster would mean
    scanning every registration of every app on World Chain — and the public RPC
    caps eth_getLogs at ~100 blocks, so it is not merely expensive."""
    assert WorldChainAgentBook(settings=_offline()).roster() is None


def test_an_unregistered_wallet_reads_as_None(monkeypatch):
    """Zero is the contract's own 'not registered'."""
    book = WorldChainAgentBook(settings=_offline())
    monkeypatch.setattr(book, "_connect", lambda: object())
    monkeypatch.setattr(
        book, "_contract",
        lambda: types.SimpleNamespace(
            functions=types.SimpleNamespace(
                lookupHuman=lambda a: types.SimpleNamespace(call=lambda: 0)
            )
        ),
    )
    assert book.lookup("0x" + "ab" * 20) is None


def test_a_registered_wallet_decodes_to_its_nullifier(monkeypatch):
    book = WorldChainAgentBook(settings=_offline())
    monkeypatch.setattr(book, "_connect", lambda: object())
    monkeypatch.setattr(
        book, "_contract",
        lambda: types.SimpleNamespace(
            functions=types.SimpleNamespace(
                lookupHuman=lambda a: types.SimpleNamespace(call=lambda: 42)
            )
        ),
    )
    reg = book.lookup("0x" + "ab" * 20)
    assert reg is not None and reg.human_id == 42
    assert reg.nullifier.endswith("2a")


def test_an_unreachable_book_resolves_nobody_rather_than_guessing(monkeypatch):
    """Offline-tolerant on the READ side. Declining to invent data when a source
    is down is the same discipline as refusing to admit an unverified human."""
    book = WorldChainAgentBook(
        rpc_url="http://127.0.0.1:1", address=AGENTBOOK_ADDRESS, settings=_offline()
    )
    assert book.lookup("0x" + "ab" * 20) is None


# --- selection ----------------------------------------------------------------


def test_the_demo_stays_on_fixtures_unless_asked(monkeypatch):
    """A default that silently read mainnet would make a demo's numbers depend
    on whether a stranger had registered a wallet we happen to pay."""
    assert isinstance(build_agentbook(_offline()), FixtureAgentBook)


def test_naming_an_endpoint_or_address_selects_the_live_book():
    named_rpc = ACRSettings(_env_file=None, world_rpc_url="https://world.invalid")
    assert isinstance(build_agentbook(named_rpc), WorldChainAgentBook)

    named_addr = ACRSettings(_env_file=None, agentbook_address=AGENTBOOK_ADDRESS)
    assert isinstance(build_agentbook(named_addr), WorldChainAgentBook)


def test_the_mode_switch_overrides_both_ways():
    forced = ACRSettings(_env_file=None, agentbook_mode="worldchain")
    assert isinstance(build_agentbook(forced), WorldChainAgentBook)

    pinned = ACRSettings(
        _env_file=None, agentbook_mode="fixture", world_rpc_url="https://world.invalid"
    )
    assert isinstance(build_agentbook(pinned), FixtureAgentBook)
