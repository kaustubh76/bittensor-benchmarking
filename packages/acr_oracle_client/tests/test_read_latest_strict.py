"""`read_latest(strict=True)` tells "the contract has no print" from "the chain
did not answer". Without it a 429 reached every reader as `404 no on-chain
print`, and the MCP plugin and the Aomi App each grew a re-ask to survive it."""

from __future__ import annotations

import pytest
from acr_oracle_client import OracleClient
from acr_oracle_client import futures as futures_mod
from web3.exceptions import ContractLogicError


class _Call:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def __call__(self):
        self.calls += 1
        raise self.outcomes.pop(0)


def _client(monkeypatch, outcomes) -> tuple[OracleClient, _Call]:
    c = OracleClient(rpc_url="http://unused.invalid", oracle_address="0x" + "22" * 20)
    call = _Call(outcomes)
    fn = type("Fn", (), {"call": call})()
    contract = type("C", (), {"functions": type("F", (), {"latestPrint": staticmethod(lambda idx: fn)})()})()
    monkeypatch.setattr(c, "_connect", lambda: object())
    monkeypatch.setattr(c, "_contract", lambda: contract)
    monkeypatch.setattr(futures_mod.time, "sleep", lambda s: None)
    return c, call


def test_a_revert_is_no_print_in_both_modes(monkeypatch):
    for strict in (False, True):
        c, _ = _client(monkeypatch, [ContractLogicError("execution reverted: no print")])
        assert c.read_latest("ACR-INF", strict=strict) is None


def test_a_dead_rpc_raises_under_strict_and_is_none_by_default(monkeypatch):
    c, _ = _client(monkeypatch, [ConnectionError("connection refused")])
    with pytest.raises(ConnectionError):
        c.read_latest("ACR-INF", strict=True)
    c, _ = _client(monkeypatch, [ConnectionError("connection refused")])
    assert c.read_latest("ACR-INF") is None


def test_strict_waits_out_a_429_before_answering(monkeypatch):
    c, call = _client(monkeypatch, [RuntimeError("429 Too Many Requests"), ContractLogicError("no print")])
    assert c.read_latest("ACR-INF", strict=True) is None
    assert call.calls == 2
