"""`make operator-run` judges a business only against ITS chain's receipts.

Found 2026-10-16 wiring the mainnet PolicyWallet. `scripts/operator_run.py` had
no chain check and read the committed receipts archive unfiltered — 120 rows,
every one `eip155:5042002`. A mainnet business whose treasury had ever paid on
testnet would have been billed from testnet activity and paid with mainnet USDC.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from acr_core import reset_settings

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import operator_run  # noqa: E402


@pytest.fixture()
def chain(monkeypatch):
    def use(chain_id: int):
        monkeypatch.setenv("ACR_ARC_CHAIN_ID", str(chain_id))
        monkeypatch.delenv("ACR_ARC_NETWORK_CAIP2", raising=False)
        reset_settings()
        from acr_core import get_settings

        return get_settings()
    yield use
    monkeypatch.undo()
    reset_settings()


def test_mainnet_reads_no_testnet_receipt(chain):
    s = chain(5042)
    rows = operator_run._receipts(s)
    assert rows == [], "every archived settlement is testnet; none may bill a mainnet business"


def test_testnet_still_reads_its_own(chain):
    s = chain(5042002)
    rows = operator_run._receipts(s)
    assert rows and all(r["network"] == "eip155:5042002" for r in rows)


def test_a_row_without_a_network_is_not_billed(chain, tmp_path, monkeypatch):
    # Paying is irreversible, so "network unknown" is not good enough here.
    s = chain(5042)
    archive = tmp_path / "r.jsonl"
    archive.write_text('{"tx_ref":"a","payer":"0x1","resource":"/prints","amount_usdc":0.01}\n'
                       '{"tx_ref":"b","payer":"0x1","resource":"/prints","amount_usdc":0.01,"network":"eip155:5042"}\n')
    monkeypatch.setattr(operator_run, "RECEIPTS_ARCHIVE", archive)
    assert [r["tx_ref"] for r in operator_run._receipts(s)] == ["b"]


def test_a_business_on_the_other_chain_is_refused(chain, monkeypatch, capsys):
    chain(5042002)  # a testnet run…
    monkeypatch.setattr(operator_run, "resolve",
                        lambda slug: SimpleNamespace(slug="acr-mainnet", chain="mainnet", policy_wallet="0x" + "3a" * 20))
    monkeypatch.setattr(sys, "argv", ["operator_run.py", "--business", "acr-mainnet"])
    assert operator_run.main() == 2  # …of a mainnet business
    err = capsys.readouterr().err
    assert "mainnet business" in err and "5042002" in err
