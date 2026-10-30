"""Circle deploy-script tests — request bodies + setSigner calldata, all mocked."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "scripts"))

from deploy_circle import (  # noqa: E402
    import_request,
    run_deploy,
    set_signer_calldata,
)

_ARTIFACT = _ROOT / "contracts/out/ACROracle.sol/ACROracle.json"
pytestmark = pytest.mark.skipif(not _ARTIFACT.exists(), reason="contracts not built (forge build)")

BLOCKCHAIN = "eip155:5042002"
WALLET = "wallet-1"
ADDR = "0x5FbDB2315678afecb367f032d93F642f64180aa3"


class _FakeDeployer:
    def __init__(self) -> None:
        self.deployed: list[dict] = []
        self.imported: list[dict] = []
        self.execs: list[tuple] = []

    def deploy(self, request: dict) -> str:
        self.deployed.append(request)
        return "0x" + "11" * 20

    def import_contract(self, request: dict) -> str:
        self.imported.append(request)
        return "contract-id"

    def contract_execution(self, wallet_id, contract_address, call_data) -> str:
        self.execs.append((wallet_id, contract_address, call_data))
        return "0xtx"


def test_deploy_requests_carry_abi_bytecode_and_uuid():
    plan = run_deploy(None, blockchain=BLOCKCHAIN, wallet_id=WALLET, dry_run=True)
    assert len(plan["requests"]) == 2
    for req, name in zip(plan["requests"], ["ACROracle", "AttestationRegistry"], strict=True):
        assert req["name"] == name
        assert req["blockchain"] == BLOCKCHAIN
        assert req["walletId"] == WALLET
        assert req["bytecode"].startswith("0x") or len(req["bytecode"]) > 0
        assert "abiJson" in req and req["abiJson"].startswith("[")
        assert req["feeLevel"] == "MEDIUM"
        uuid.UUID(req["idempotencyKey"])  # raises if invalid


def test_import_by_address_builds_requests():
    plan = run_deploy(
        None, blockchain=BLOCKCHAIN, wallet_id=WALLET,
        import_addrs=(ADDR, "0xReg"), dry_run=True,
    )
    assert plan["oracle_address"] == ADDR and plan["registry_address"] == "0xReg"
    req = import_request("ACROracle", ADDR, BLOCKCHAIN)
    assert req == {"name": "ACROracle", "address": ADDR, "blockchain": BLOCKCHAIN}


def test_setsigner_calldata_encodes_address():
    calldata = set_signer_calldata(ADDR, True)
    assert calldata.startswith("0x")
    # setSigner(address,bool): selector + padded address + padded bool.
    assert ADDR[2:].lower() in calldata.lower()
    assert calldata.endswith("1")  # allowed = true


def test_run_deploy_executes_with_fake_deployer():
    dep = _FakeDeployer()
    plan = run_deploy(
        dep, blockchain=BLOCKCHAIN, wallet_id=WALLET,
        poster_address="0x70997970C51812dc3A010C7d01b50e0d17dc79C8", dry_run=False,
    )
    assert len(dep.deployed) == 2
    assert plan["oracle_address"] == "0x" + "11" * 20
    # Poster differs from the deployer wallet → setSigner contract-execution ran.
    assert len(dep.execs) == 1
    assert dep.execs[0][1] == plan["oracle_address"]
