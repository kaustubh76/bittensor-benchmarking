"""The venue's roll, proven against a REAL ACRFutures deployment.

``futures_roll.py`` is what keeps the venue alive across an expiry: it opens the
successor series and collateralizes the maker. If it fails, the current series
expires with nothing behind it, ``_live_series`` starts raising 409 "no open
series", and the entire trading surface — desk, tape, curve skew — goes dark.

It had never been exercised. Every scheduled run so far reported "nothing to
roll", because the live series always had life left, so the branch that
*actually does the work* had run exactly once, by hand, months of chain-time
ago. The first real test of that path was scheduled to be production.

So this runs **the script itself**, as a subprocess, against a genuine
deployment on anvil — not a reimplementation of its logic, because CI runs the
script and the script is what must work. Skipped (not failed) without anvil, so
the default `make test` stays hermetic.

    make anvil                     # or: anvil &
    uv run pytest services/index_api/tests/test_futures_roll_onchain.py -q
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from acr_oracle_client import FuturesClient, OracleClient

#: Honours ACR_TEST_RPC like the other on-chain suites: this machine runs
#: more than one project, and a hardcoded 8545 SKIPPED these nine tests
#: against a private anvil while the rest of the suite ran (2026-10-13).
RPC = os.environ.get("ACR_TEST_RPC", "http://127.0.0.1:8545")
CHAIN_ID = 31337
# anvil's well-known dev accounts — local-only, never hold real value.
OWNER_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
ROOT = Path(__file__).resolve().parents[3]
ARTIFACT = ROOT / "contracts/out/ACRFutures.sol/ACRFutures.json"
ROLL = ROOT / "scripts" / "futures_roll.py"
INDEX = "ACR-INF"
MULT = 10
MARGIN_BPS = 2000
MARK = 0.5

ERC20_ABI = [
    {"type": "function", "name": "mint", "stateMutability": "nonpayable",
     "inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": []},
    {"type": "function", "name": "approve", "stateMutability": "nonpayable",
     "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
    {"type": "function", "name": "allowance", "stateMutability": "view",
     "inputs": [{"name": "", "type": "address"}, {"name": "", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
]

pytestmark = pytest.mark.skipif(
    not ARTIFACT.exists(), reason="contracts not built (run forge build)"
)


def _artifact(name):
    art = json.loads((ROOT / f"contracts/out/{name}.sol/{name}.json").read_text())
    return art["abi"], art["bytecode"]["object"]


def _send(w3, acct, fn):
    tx = fn.build_transaction(
        {"from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
         "gas": 3_000_000, "chainId": w3.eth.chain_id}
    )
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    return w3.eth.wait_for_transaction_receipt(w3.eth.send_raw_transaction(raw), timeout=30)


def _deploy(w3, acct, name, *args):
    abi, bytecode = _artifact(name)
    c = w3.eth.contract(abi=abi, bytecode=bytecode)
    tx = c.constructor(*args).build_transaction(
        {"from": acct.address, "nonce": w3.eth.get_transaction_count(acct.address),
         "gas": 5_000_000, "chainId": w3.eth.chain_id}
    )
    signed = acct.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    rcpt = w3.eth.wait_for_transaction_receipt(w3.eth.send_raw_transaction(raw), timeout=30)
    return rcpt.contractAddress


@pytest.fixture(scope="module")
def venue():
    """A real venue whose only series is about to expire — the exact state that
    makes the roll do its job rather than no-op."""
    try:
        from eth_account import Account
        from web3 import Web3

        w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 3}))
        if not w3.is_connected():
            pytest.skip(f"anvil not reachable at {RPC}")
        # A node on the WRONG CHAIN is an environment problem, not a regression,
        # and this suite is the only one that notices: it drives
        # scripts/futures_roll.py as a subprocess, and the script refuses to run
        # unless the RPC's chain id matches ACR_ARC_CHAIN_ID. Found 2026-10-29 on
        # a machine where another project's `anvil --chain-id 133` was already
        # bound to 8545 — these two tests then FAILED with "chain id 133 ==
        # 31337" while every sibling suite skipped cleanly, which reads as a
        # broken roll rather than a borrowed port. Skip, name the cause, and say
        # what to do: the same contract as the unreachable-node skip above.
        actual = w3.eth.chain_id
        if actual != CHAIN_ID:
            pytest.skip(
                f"the node at {RPC} is chain {actual}, and scripts/futures_roll.py "
                f"requires {CHAIN_ID} — point ACR_TEST_RPC at a "
                f"`anvil --chain-id {CHAIN_ID}` node, or free port 8545"
            )
    except Exception:
        pytest.skip("web3/eth-account unavailable")

    from acr_core import ACRPrint

    owner = Account.from_key(OWNER_KEY)
    oracle_addr = _deploy(w3, owner, "ACROracle")
    usdc_addr = _deploy(w3, owner, "MockUSDC")
    futures_addr = _deploy(w3, owner, "ACRFutures", oracle_addr, usdc_addr, MARGIN_BPS)

    # openSeries requires a live oracle value, so post one first.
    oc = OracleClient(rpc_url=RPC, oracle_address=oracle_addr, private_key=OWNER_KEY)
    now = int(w3.eth.get_block("latest").timestamp)
    oc.post(ACRPrint(index_id=INDEX, ts=float(now), value=MARK, ci_lo=MARK * 0.98,
                     ci_hi=MARK * 1.02, n_obs=100, attack_cost_per_bp=1000.0))

    usdc = w3.eth.contract(address=w3.to_checksum_address(usdc_addr), abi=ERC20_ABI)
    _send(w3, owner, usdc.functions.mint(owner.address, 10_000 * 10**6))

    # The incumbent: alive, but with only an hour left — under any sane
    # ROLL_MIN_LIFE_H, so the script must decide to open a successor.
    fc = FuturesClient(rpc_url=RPC, futures_address=futures_addr, private_key=OWNER_KEY)
    fc.open_series(INDEX, now + 3600, MULT, owner.address)

    return {"w3": w3, "owner": owner, "futures": futures_addr, "oracle": oracle_addr,
            "usdc": usdc_addr, "fc": fc}


def _roll(venue, **overrides) -> subprocess.CompletedProcess:
    """Run the real script against the anvil venue."""
    env = {
        **os.environ,
        "ACR_ARC_RPC_URL": RPC,
        "ACR_ARC_CHAIN_ID": str(CHAIN_ID),
        "ACR_FUTURES_ADDRESS": venue["futures"],
        "ACR_ORACLE_ADDRESS": venue["oracle"],
        "MAKER_PRIVATE_KEY": OWNER_KEY,
        "ROLL_USDC_ADDRESS": venue["usdc"],
        "ROLL_INDEX": INDEX,
        "ROLL_MULT": str(MULT),
        "ROLL_COLLATERAL": "5",
        "ROLL_EXPIRY_DAYS": "7",
        "ROLL_MIN_LIFE_H": "24",
        "ROLL_GAS_FLOOR": "0.75",
        **overrides,
    }
    return subprocess.run(
        [sys.executable, str(ROLL)], env=env, capture_output=True, text=True, timeout=180
    )


def test_the_roll_opens_a_collateralized_successor(venue):
    """The whole point. An open-but-uncollateralized series is worse than none:
    the desk advertises a venue that reverts on first contact."""
    before = len(venue["fc"].read_all_series())
    p = _roll(venue)
    assert p.returncode == 0, f"roll failed\n{p.stdout}\n{p.stderr}"

    series = venue["fc"].read_all_series()
    assert len(series) == before + 1, f"no successor opened\n{p.stdout}"

    now = int(venue["w3"].eth.get_block("latest").timestamp)
    live = [s for s in series if s["index_id"] == INDEX and not s["settled"]
            and s["expiry_ts"] > now + 24 * 3600]
    assert live, f"successor is not comfortably unexpired\n{p.stdout}"

    newest = max(live, key=lambda s: s["series_id"])
    posted = venue["fc"].collateral_of(newest["series_id"], venue["owner"].address) or 0.0
    # This is the assertion futures_seed.py does NOT make, which is why the
    # runbook forbids rolling with it: it exits 0 having left the series bare.
    assert posted > 0, f"successor {newest['series_id']} has no maker collateral\n{p.stdout}"


def test_rolling_again_is_a_no_op(venue):
    """Idempotence matters because the lifecycle cron fires hourly. A roll that
    opened a series every run would shred the maker's balance and scatter
    collateral across a dozen dead series."""
    before = len(venue["fc"].read_all_series())
    p = _roll(venue)
    assert p.returncode == 0, f"{p.stdout}\n{p.stderr}"
    assert "nothing to roll" in p.stdout, p.stdout
    assert len(venue["fc"].read_all_series()) == before, "a healthy venue was rolled anyway"


def test_it_refuses_rather_than_opening_a_series_it_cannot_fund(venue):
    """The budget guard, which has to run BEFORE the write.

    Collateral and gas come out of one balance, so a roll that opens first and
    discovers it cannot fund leaves exactly the state the first test forbids —
    a live series the desk will advertise and the contract will reject.
    """
    before = len(venue["fc"].read_all_series())
    # Demand more collateral than the maker could ever post.
    p = _roll(venue, ROLL_COLLATERAL="10000000", ROLL_MIN_LIFE_H="999999")
    assert p.returncode == 1, f"expected a refusal, got {p.returncode}\n{p.stdout}"
    assert len(venue["fc"].read_all_series()) == before, (
        f"it opened a series it could not fund\n{p.stdout}"
    )


def test_a_throttled_read_does_not_trigger_a_roll(venue, monkeypatch):
    """The bug that cost a redundant series in production.

    The roll asked "is the maker funded?" as ``collateral_of(...) or 0``, which
    turns a REFUSED READ into "unfunded" — and the response to an unfunded maker
    is to open a whole new series and post collateral to it. On 2026-08-02 that
    fired against a series with **147.9 hours** of life left and a funded maker:
    a 429 cost 1.50 USDC and left the venue carrying two live series.

    Simulated here by pointing the roll at an RPC that refuses, which is what a
    throttled node looks like from the script's side. It must decline to act on
    a reading it does not have, and exit 0 — the next run will see the truth.
    """
    before = len(venue["fc"].read_all_series())
    p = _roll(venue, ACR_ARC_RPC_URL="http://127.0.0.1:1")  # nothing listening
    assert len(venue["fc"].read_all_series()) == before, (
        f"it rolled without being able to read the maker's collateral\n{p.stdout}"
    )
