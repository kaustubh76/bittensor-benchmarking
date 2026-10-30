"""Dual-post tests — the migration's safety property, as a test not a paragraph.

Two oracles run side by side: v1 because `ACRFutures` settles against it and its
oracle pointer is immutable, v2 because it carries the policy hash and window
that make a print reproducible. The asymmetry between them is the whole design:

  * v1 stale  → `ACRFutures.settle()` refuses a print older than two hours, so
    every expired open series becomes unsettleable and its collateral sits
    stranded. A v1 failure must abort the cycle, exactly as before.
  * v2 stale  → the subgraph's arrival ring goes sparse, which it already
    reports honestly as `benchmarked: false`. A v2 failure must be recorded and
    stepped over.

A tape problem must never be able to stop the press.
"""

from __future__ import annotations

from acr_core import ACRPrint
from index_api.poster import OraclePoster


class _Store:
    """Just enough PrintStore for the poster: a `latest` dict and a refresh."""

    def __init__(self, prints: dict[str, ACRPrint]):
        self.latest = prints
        self.refreshed = 0

    def refresh(self, ts=None):
        self.refreshed += 1


class _Client:
    def __init__(self, fail: bool = False, tx: str = "0xv1"):
        self.fail = fail
        self.tx = tx
        self.posted: list[str] = []
        self.last_receipt = {"tx": tx, "block": 1, "gas_used": 1}

    def post(self, p, wait: bool = True):
        if self.fail:
            raise RuntimeError("chain said no")
        self.posted.append(p.index_id)
        return self.tx


def _print(index_id: str = "ACR-INF") -> ACRPrint:
    return ACRPrint(
        index_id=index_id, ts=3600.0, value=0.5, ci_lo=0.49, ci_hi=0.51,
        attack_cost_per_bp=1000.0, n_obs=10,
        policy_hash="0x" + "ab" * 32, window_start=0.0, window_end=3600.0,
    )


def _poster(v1: _Client, v2: _Client | None) -> OraclePoster:
    store = _Store({"ACR-INF": _print()})
    return OraclePoster(store, client=v1, v2_client=v2)


def test_a_healthy_cycle_posts_to_both():
    v1, v2 = _Client(), _Client(tx="0xv2")
    poster = _poster(v1, v2)
    refs = poster.post_latest()
    assert refs == ["0xv1"]
    assert v1.posted == ["ACR-INF"]
    assert v2.posted == ["ACR-INF"]
    assert poster.last_posts["ACR-INF"]["v2"] == "0xv2"


def test_a_v2_failure_does_not_stop_the_press():
    """The property the whole asymmetry exists for. v1 still posted, and the
    cycle still returned its tx — a tape outage must not stale the feed the
    venue settles against."""
    v1, v2 = _Client(), _Client(fail=True)
    poster = _poster(v1, v2)
    refs = poster.post_latest()

    assert refs == ["0xv1"], "the v1 post must still have happened"
    assert v1.posted == ["ACR-INF"]
    assert poster.posts == 1
    # …and the failure is recorded, not swallowed: an operator has to be able
    # to see that the tape fell behind.
    assert poster.last_posts["ACR-INF"]["v2"].startswith("error:")


def test_a_v1_failure_still_aborts_that_index():
    """Unchanged from before v2 existed — a stale venue feed is the failure
    that costs money, so it stays a hard one."""
    v1, v2 = _Client(fail=True), _Client()
    poster = _poster(v1, v2)
    refs = poster.post_latest()

    assert refs == ["error:ACR-INF"]
    assert v2.posted == [], "v2 must not be posted when v1 failed"
    # The REASON is on the record, not only in the log: a bare "error" sat on
    # every surface for ~49 hours of mainnet outage while the cause (a gas
    # reservation the wallet could not cover) was in a Render log nobody could read.
    assert poster.last_posts["ACR-INF"]["note"] == "error: chain said no"


class _RpcRefusal(_Client):
    """A post refused the way web3 actually refuses one: a JSON-RPC error dict."""

    def post(self, p, wait: bool = True):
        raise ValueError({
            "code": -32000,
            "message": "insufficient funds for gas * price + value: balance 5272576941191814, "
                       "tx cost 7272000000000000, overshot 1999423058808186",
        })


def test_a_refused_post_records_the_nodes_own_sentence():
    """The case that actually happened (mainnet, 2026-10-13). `str()` of web3's
    dict spends the truncation on `{'code': -32000, ...`; the record must carry
    the node's sentence, because that sentence IS the diagnosis."""
    poster = _poster(_RpcRefusal(), None)
    poster.post_latest()
    note = poster.last_posts["ACR-INF"]["note"]
    assert note.startswith("error: insufficient funds for gas * price + value"), note
    assert "{" not in note and "code" not in note, "the repr leaked into the record"


def test_no_v2_configured_behaves_exactly_as_before():
    v1 = _Client()
    poster = _poster(v1, None)
    refs = poster.post_latest()
    assert refs == ["0xv1"]
    assert "v2" not in poster.last_posts["ACR-INF"]


def test_an_offline_v2_is_recorded_as_offline_not_as_an_error():
    """`post` returning None is the honest no-op the client uses when it has no
    key or no chain — a different fact from a revert, and recorded as one."""
    v1, v2 = _Client(), _Client(tx=None)  # type: ignore[arg-type]
    v2.tx = None  # post() returns None
    poster = _poster(v1, v2)
    poster.post_latest()
    assert poster.last_posts["ACR-INF"]["v2"] == "offline"


def test_the_v2_client_is_absent_until_an_address_is_configured():
    """Built from settings inside the poster, so the service loop and
    scripts/post_once.py cannot disagree about whether they dual-post."""
    from index_api.poster import _build_v2_client

    assert _build_v2_client() is None
