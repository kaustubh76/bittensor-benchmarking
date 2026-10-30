"""The consensus print reaching a contract — what makes this a benchmark.

A reference rate is a number something settles against. Until a consensus print
reaches a contract that enforces the benchmark invariants on chain, the whole
mechanism is a well-tested opinion.

Two layers here, and only one of them needs a node:

* The **invariant checks** are pure arithmetic over the print, and they run
  everywhere. They exist because finding out from a revert costs a deploy and
  tells you less than a named assertion does.
* The **chain leg** skips cleanly without `forge build` output or a reachable
  node, following the repo's existing `skipif(not ARTIFACT.exists())` idiom. A
  missing local toolchain is not a failure of the subnet.

Note the node this uses: port 8545 on the development machine was occupied by a
foreign `anvil --chain-id 133`, which is why `scripts/futures_roll.py` fails
there with "chain id 133 == 31337". `ACR_TEST_RPC` is the override the root
conftest deliberately lets through the env scrub, so point it at your own node.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from acr_core import ACRPrint

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "scripts"))
sys.path.insert(0, str(_ROOT / "redteam"))

from subnet_oracle import (  # noqa: E402
    ANVIL_KEY,
    MAX_TS_SKEW,
    check_invariants,
    connect,
    deploy_oracle,
)

ARTIFACT = _ROOT / "contracts" / "out" / "ACROracle.sol" / "ACROracle.json"
RPC = os.environ.get("ACR_TEST_RPC", "http://127.0.0.1:8545")

#: Chains this suite is willing to deploy to and spend a hardcoded dev key on.
#: Matches `scripts/gen_snapshot.py`'s `LOCAL_CHAIN_IDS`, which draws the same
#: line for the same reason: anything else is somebody else's chain.
LOCAL_CHAIN_IDS = frozenset({31337, 1337})


def pr(ts: float, value: float = 0.5, lo: float = 0.49, hi: float = 0.51,
       bound: float | None = 0.02) -> ACRPrint:
    return ACRPrint(index_id="ACR-INF", ts=ts, value=value, ci_lo=lo, ci_hi=hi,
                    attack_cost_per_bp=bound, n_obs=3, trim_alpha=0.10)


# --- the pre-flight checks (no node needed) -------------------------------


def test_ascending_prints_pass():
    assert check_invariants([pr(3600), pr(7200), pr(10800)], block_ts=1_800_000_000) == []


def test_a_non_monotone_timestamp_is_caught_before_the_transaction():
    """`ACROracle` requires `timestamp > prev.timestamp` per index."""
    problems = check_invariants([pr(7200), pr(3600)], block_ts=1_800_000_000)
    assert problems
    assert "monotone" in problems[0]


def test_a_repeated_timestamp_is_also_refused():
    problems = check_invariants([pr(3600), pr(3600)], block_ts=1_800_000_000)
    assert problems and "monotone" in problems[0]


def test_a_value_outside_its_own_interval_is_caught():
    """ACRPrint refuses this at construction, so the check is belt and braces —
    but the contract's `require(ciLo <= value && value <= ciHi)` is the one that
    costs gas, and this asserts the property rather than trusting two layers."""
    with pytest.raises(ValueError):
        pr(3600, value=0.6, lo=0.49, hi=0.51)


def test_a_future_timestamp_is_caught():
    """The skew guard is one-sided: `ts <= block.timestamp + MAX_TS_SKEW`."""
    problems = check_invariants([pr(2_000_000_000)], block_ts=1_000)
    assert problems and "future" in problems[0]


def test_a_fixing_clock_timestamp_is_not_in_the_future():
    """The important consequence of that one-sidedness.

    A consensus print carries a Fixing-clock `ts` (3600, 7200, or 86400-multiples
    on real tape), not unix time. Against a real block timestamp (~1.8e9) those
    are vastly in the past, so the skew guard passes trivially and there is no
    lower bound to trip. Ordering is the only timestamp risk.
    """
    assert check_invariants([pr(3600)], block_ts=1_800_000_000) == []
    assert MAX_TS_SKEW == 900


def test_no_prints_is_reported_rather_than_silently_succeeding():
    assert check_invariants([], block_ts=1_000) == ["no consensus prints to post"]


def test_the_block_timestamp_may_be_unknown():
    """When no node is reachable the skew check is skipped, not failed."""
    assert check_invariants([pr(3600), pr(7200)], block_ts=None) == []


# --- the chain leg --------------------------------------------------------


@pytest.mark.skipif(not ARTIFACT.exists(), reason="contracts not built (run forge build)")
class TestOnChain:
    @pytest.fixture
    def oracle(self):
        conn = connect(RPC)
        if conn is None:
            pytest.skip(f"no node reachable at {RPC}")
        w3, acct = conn
        # A node we can REACH is not a node we can USE, and these tests were
        # flaky until that distinction was made. `connect` only proves something
        # answers on the port. This suite then deploys a contract and sends
        # transactions from a hardcoded anvil dev key, which needs a clean local
        # dev chain — and on this machine port 8545 is held by another project's
        # `anvil --chain-id 133`. Against that, `connect` succeeds and the deploy
        # fails on balance or nonce state we do not own, so the suite reported a
        # broken oracle loop when what was broken was the borrowed port.
        #
        # Same remedy as `test_futures_roll_onchain.py`: verify the chain, then
        # skip with the cause named rather than failing on someone else's node.
        if w3.eth.chain_id not in LOCAL_CHAIN_IDS:
            pytest.skip(
                f"the node at {RPC} is chain {w3.eth.chain_id}, not a local dev chain "
                f"{sorted(LOCAL_CHAIN_IDS)} — point ACR_TEST_RPC at "
                f"`anvil --chain-id 31337`, or run `make subnet-oracle`, which starts "
                f"its own on SUBNET_ANVIL_PORT"
            )
        if w3.eth.get_balance(acct.address) <= 0:
            pytest.skip(
                f"the anvil dev key {acct.address} has no balance at {RPC}; this suite "
                f"needs a chain that funds the standard dev accounts"
            )
        # A fresh oracle per test: `_latest` starts empty, so the monotonicity
        # rule cannot trip over a print left by an earlier run.
        address = deploy_oracle(w3, acct)
        from acr_oracle_client import OracleClient

        return OracleClient(rpc_url=RPC, oracle_address=address, private_key=ANVIL_KEY)

    def test_a_consensus_print_posts_and_reads_back(self, oracle):
        p = pr(3600, value=0.498_680_07)
        assert oracle.post(p) is not None
        back = oracle.read_latest("ACR-INF")
        assert back is not None
        assert back["value"] == pytest.approx(p.value, rel=1e-9)
        assert back["timestamp"] == int(p.ts)

    def test_ascending_prints_all_land_and_the_latest_wins(self, oracle):
        for ts, v in ((3600, 0.50), (7200, 0.51), (10800, 0.52)):
            assert oracle.post(pr(ts, value=v, lo=v - 0.01, hi=v + 0.01)) is not None
        back = oracle.read_latest("ACR-INF")
        assert back["timestamp"] == 10800
        assert back["value"] == pytest.approx(0.52, rel=1e-9)

    def test_an_absent_bound_posts_under_a_wire_floor_not_as_zero(self, oracle):
        """The real-tape case, and the one most open to being misread.

        On single-venue flow the print carries `attack_cost_per_bp=None` — there
        is no funding graph to price an attack against. `ACROracle` refuses a
        zero bound (`require(attackCostPerBp > 0, "bound=0")`), so
        `PostPayload.from_print` floors it to one unit, 1e-6 USDC.

        That floor is a wire-format artefact and **not a measurement**. Anyone
        reading 1e-06 off the chain and treating it as "this feed costs a
        millionth of a dollar to move" has it exactly backwards: it means no
        bound was computed at all. The venue-concentration diagnostic is what
        carries real information there.
        """
        assert oracle.post(pr(3600, bound=None)) is not None
        back = oracle.read_latest("ACR-INF")
        assert back["attack_cost_per_bp"] == pytest.approx(1e-6)
        assert back["attack_cost_per_bp"] > 0  # never equal to None on chain

    def test_the_deployer_is_already_an_authorized_signer(self, oracle):
        """`ACROracle`'s constructor sets `isSigner[msg.sender] = true`, so the
        loop needs no `setSigner` call. If that ever changed, `post` would revert
        with "bad signer" and this test says where to look."""
        assert oracle.post(pr(3600)) is not None
