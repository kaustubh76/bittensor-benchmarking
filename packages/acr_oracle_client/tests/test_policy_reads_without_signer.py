"""A PolicyWallet's budget and balance are public reads, and need no signer.

Found 2026-10-16 wiring the mainnet PolicyWallet (0x3a5F…4e04, cap 1.00/day,
per-tx 0.10). The host that serves the mainnet `/operator/*` pages
(acr-probe-free) holds no key, and `budget()`/`balance_usdc()` returned None
whenever there was no agent signer, so the statement said "no budget set on
chain" and "the chain could not be reached" about a wallet with both.
"""
from __future__ import annotations

from types import SimpleNamespace

from acr_oracle_client.policy import USDC, PolicyClient, category_id

WALLET = "0x3a5F444B0e4Ddc7FA6dcddA07A71d0710eea4e04"
TOKEN = "0x3600000000000000000000000000000000000000"


class _Call:
    def __init__(self, value):
        self.value = value

    def call(self):
        return self.value


def _client_without_a_signer(monkeypatch, budget_row, held_units):
    client = PolicyClient(wallet_address=WALLET, agent_signer=None, owner_signer=None)
    client.agent_signer = None  # the role fallback must not lend it one either
    asked = []

    def budget_of(cat):
        asked.append(cat)
        return _Call(budget_row)

    wallet_fns = SimpleNamespace(budgetOf=budget_of, usdc=lambda: _Call(TOKEN))
    token_fns = SimpleNamespace(balanceOf=lambda who: _Call(held_units))

    class _W3:
        class eth:
            @staticmethod
            def contract(address, abi):
                return SimpleNamespace(functions=token_fns)

        @staticmethod
        def to_checksum_address(a):
            return a

    monkeypatch.setattr(client, "_connect", lambda: _W3())
    monkeypatch.setattr(client, "_contract", lambda: SimpleNamespace(functions=wallet_fns))
    return client, asked


def test_the_budget_reads_without_a_signer(monkeypatch):
    client, asked = _client_without_a_signer(monkeypatch, (True, 1 * USDC, 0, 100_000, 0, 86_400), 0)
    assert not client.configured(), "this host can read but must not be able to pay"
    b = client.budget("machine-services")
    assert b is not None
    assert b["cap_usdc"] == 1.0 and b["per_tx_limit_usdc"] == 0.1 and b["period_length"] == 86_400
    assert asked == [category_id("machine-services")]


def test_the_balance_reads_without_a_signer(monkeypatch):
    client, _ = _client_without_a_signer(monkeypatch, (True, 1 * USDC, 0, 100_000, 0, 86_400), 0)
    assert client.balance_usdc() == 0.0, "an unfunded wallet is 0.0, not 'unreachable'"


def test_no_address_still_reads_nothing():
    client = PolicyClient(wallet_address="", agent_signer=None, owner_signer=None)
    assert client.budget("machine-services") is None and client.balance_usdc() is None
