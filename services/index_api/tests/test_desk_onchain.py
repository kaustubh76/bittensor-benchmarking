"""The desk's exit path, proven against the REAL ACRFutures contract.

Everything here runs against a genuine deployment on a local anvil node — the
same contract bytecode that is live on Arc, driven through the same
``FuturesClient`` and the same ``desk.withdrawable()`` the API serves. Nothing
about the venue is stood in for.

That distinction matters for this feature specifically. ``withdrawable()``'s job
is to predict what ``withdrawCollateral`` will accept, and a stubbed client can
only ever confirm that the prediction matches *itself*. The assertions below
compare the prediction against what the contract actually does — withdrawing the
quoted amount must move real tokens, and one unit past the contract's own limit
must revert. A fake cannot fail those, and this suite already has: the first
version asserted that a unit past the *quote* would revert, which is false,
because the safety haircut deliberately leaves the quote below the limit.

Skipped (not failed) when anvil isn't up or the contracts aren't built, so the
default `make test` stays hermetic and credential-free.

    make anvil                     # or: anvil &
    uv run pytest services/index_api/tests/test_desk_onchain.py -q
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from acr_oracle_client import FuturesClient, OracleClient

#: Honours ACR_TEST_RPC like the other on-chain suites: this machine runs
#: more than one project, and a hardcoded 8545 SKIPPED these nine tests
#: against a private anvil while the rest of the suite ran (2026-10-13).
RPC = os.environ.get("ACR_TEST_RPC", "http://127.0.0.1:8545")
# anvil's well-known dev accounts — local-only, never hold real value.
OWNER_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
MAKER_KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"
TAKER_KEY = "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a"
ROOT = Path(__file__).resolve().parents[3]
ARTIFACT = ROOT / "contracts/out/ACRFutures.sol/ACRFutures.json"
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
    {"type": "function", "name": "balanceOf", "stateMutability": "view",
     "inputs": [{"name": "", "type": "address"}], "outputs": [{"name": "", "type": "uint256"}]},
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
    """A real ACRFutures + oracle + USDC on anvil, with a live series and a
    taker holding an open position. Module-scoped: one deployment, several
    assertions against it."""
    try:
        from eth_account import Account
        from web3 import Web3

        w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={"timeout": 3}))
        if not w3.is_connected():
            pytest.skip(f"anvil not reachable at {RPC}")
    except Exception:
        pytest.skip("web3/eth-account unavailable")

    from acr_core import ACRPrint

    owner, maker, taker = (Account.from_key(k) for k in (OWNER_KEY, MAKER_KEY, TAKER_KEY))
    oracle_addr = _deploy(w3, owner, "ACROracle")
    usdc_addr = _deploy(w3, owner, "MockUSDC")
    futures_addr = _deploy(w3, owner, "ACRFutures", oracle_addr, usdc_addr, MARGIN_BPS)

    # A print, so the series has a mark to open and margin against.
    oc = OracleClient(rpc_url=RPC, oracle_address=oracle_addr, private_key=OWNER_KEY)
    now = int(w3.eth.get_block("latest").timestamp)
    oc.post(ACRPrint(index_id=INDEX, ts=float(now), value=MARK, ci_lo=MARK * 0.98,
                     ci_hi=MARK * 1.02, n_obs=100, attack_cost_per_bp=1000.0))

    usdc = w3.eth.contract(address=w3.to_checksum_address(usdc_addr), abi=ERC20_ABI)
    for acct in (maker, taker):
        _send(w3, acct, usdc.functions.mint(acct.address, 10_000 * 10**6))
        _send(w3, acct, usdc.functions.approve(futures_addr, 2**256 - 1))

    owner_fc = FuturesClient(rpc_url=RPC, futures_address=futures_addr, private_key=OWNER_KEY)
    maker_fc = FuturesClient(rpc_url=RPC, futures_address=futures_addr, private_key=MAKER_KEY)
    taker_fc = FuturesClient(rpc_url=RPC, futures_address=futures_addr, private_key=TAKER_KEY)

    # Chain time, not wall clock: a node reused across runs carries any
    # evm_increaseTime a previous settle test applied, and openSeries rejects
    # an expiry that is already in its past.
    expiry = now + 3600
    owner_fc.open_series(INDEX, expiry, MULT, maker.address)
    maker_fc.post_collateral(0, 100.0)
    taker_fc.post_collateral(0, 10.0)
    taker_fc.trade(0, 2.0)  # 2 contracts × 0.5 × 10 × 20% = 2.0 USDC pinned

    return {
        "w3": w3, "usdc": usdc, "futures": futures_addr, "oracle": oracle_addr,
        "owner": owner, "maker": maker, "taker": taker,
        "owner_fc": owner_fc, "taker_fc": taker_fc, "maker_fc": maker_fc,
        "expiry": expiry,
    }


def _desk_against(monkeypatch, venue, settled_override=None):
    """Point index_api.desk at the anvil venue, so withdrawable() runs its real
    orchestration over a real contract.

    A REAL ``FuturesReader``, not a hand-rolled stand-in. This used to inject a
    two-attribute shim exposing ``_client``, and when ``withdrawable()`` grew a
    call to the reader's memoized ``all_series()`` the shim no longer had the
    method — so every test here failed with ``'_Fut' object has no attribute
    'all_series'``. CI could not see it: these tests skip without anvil, so a
    break in them is silent by construction, and faking one of our OWN objects
    is precisely the trap this suite's docstring warns about. Using the real
    reader means it cannot drift from production again.

    Built fresh per test on purpose: the reader memoizes the series list for 90
    seconds, and the settlement test changes that list mid-module.
    """
    from index_api import desk
    from index_api.onchain import FuturesReader

    reader = FuturesReader(rpc_url=RPC, futures_address=venue["futures"])
    monkeypatch.setattr("index_api.onchain.get_futures", lambda: reader)
    monkeypatch.setattr(desk, "_live_mark", lambda iid: MARK)
    monkeypatch.setattr(desk, "_margin_bps", lambda: MARGIN_BPS)
    desk._withdrawable_memo.drop(venue["taker"].address.lower())
    return desk


def test_withdrawable_never_quotes_more_than_the_contract_allows(monkeypatch, venue):
    """The quote has to be SAFE, not merely close: the contract's own boundary
    must reject one unit past it, and the quote must sit at or below that.

    The gap between the two is the ``WITHDRAW_SAFETY`` haircut, and it exists
    because ``_requiredMargin`` is re-evaluated at signing time — a mark that
    ticks up between quote and PIN would otherwise revert a withdrawal the
    reader had already authorized.
    """
    desk = _desk_against(monkeypatch, venue)
    taker, taker_fc = venue["taker"], venue["taker_fc"]

    row = desk.withdrawable(taker.address)["series"][0]
    assert row["contracts"] == pytest.approx(2.0)

    # The contract's real boundary: collateral − requiredMargin.
    # 2 contracts × 0.5 mark × 10 multiplier × 20% = 2.0 USDC pinned of the 10 posted.
    posted = venue["owner_fc"].collateral_units_of(0, taker.address)
    true_max = posted - 2_000_000
    assert row["free_units"] <= true_max, "a quote above the contract's limit would revert"

    # One unit past the contract's own limit really is refused. The revert
    # surfaces from gas estimation as ContractLogicError; a node that estimates
    # anyway would surface it as _send's RuntimeError on a status-0 receipt.
    from web3.exceptions import ContractLogicError

    with pytest.raises((ContractLogicError, RuntimeError), match="below margin|reverted"):
        taker_fc.withdraw_collateral(0, true_max + 1)

    before = venue["usdc"].functions.balanceOf(taker.address).call()
    taker_fc.withdraw_collateral(0, row["free_units"])
    after = venue["usdc"].functions.balanceOf(taker.address).call()
    assert after - before == row["free_units"]  # the money actually moved

    desk._withdrawable_memo.drop(taker.address.lower())
    left = desk.withdrawable(taker.address)["series"][0]
    assert left["free_units"] < row["free_units"]  # the stake really shrank


def test_settlement_frees_the_whole_cleared_balance(monkeypatch, venue):
    """After settle() the position is flat, so the margin check is skipped and
    the entire cleared balance becomes withdrawable — including PnL."""
    w3, taker, taker_fc = venue["w3"], venue["taker"], venue["taker_fc"]
    desk = _desk_against(monkeypatch, venue)

    w3.provider.make_request("evm_increaseTime", [4000])
    w3.provider.make_request("evm_mine", [])
    from acr_core import ACRPrint

    OracleClient(rpc_url=RPC, oracle_address=venue["oracle"], private_key=OWNER_KEY).post(
        ACRPrint(index_id=INDEX, ts=venue["expiry"] + 100.0, value=MARK, ci_lo=MARK * 0.98,
                 ci_hi=MARK * 1.02, n_obs=100, attack_cost_per_bp=1000.0)
    )
    venue["owner_fc"].settle(0)

    desk._withdrawable_memo.drop(taker.address.lower())
    row = desk.withdrawable(taker.address)["series"][0]
    assert row["settled"] is True
    on_chain = venue["owner_fc"].collateral_units_of(0, taker.address)
    assert row["free_units"] == on_chain  # the WHOLE cleared balance, no haircut

    before = venue["usdc"].functions.balanceOf(taker.address).call()
    taker_fc.withdraw_collateral(0, row["free_units"])
    after = venue["usdc"].functions.balanceOf(taker.address).call()
    assert after - before == on_chain
    assert venue["owner_fc"].collateral_units_of(0, taker.address) == 0  # drained exactly


def test_withdrawable_reports_nothing_for_a_stranger(monkeypatch, venue):
    desk = _desk_against(monkeypatch, venue)
    out = desk.withdrawable("0x" + "9" * 40)
    assert out["series"] == [] and out["free_units"] == 0
