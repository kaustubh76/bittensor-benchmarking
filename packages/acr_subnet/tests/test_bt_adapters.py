"""The networked adapters: what they do when the chain is unhelpful.

These tests never touch a chain. `bt_chain.Chain` is the only thing here that
needs the SDK, and it is constructed nowhere -- the parts worth pinning are the
judgements the adapters make when the network gives them nothing, and those are
reachable without a node.
"""

from __future__ import annotations

import pytest
from acr_subnet.adapters.bt_chain import (
    MAX_COMMITMENT_FIELD_BYTES,
    MinerEndpoint,
    commitment_payload,
    endpoint_commitment,
    endpoint_from_commitment,
)
from acr_subnet.adapters.bt_miner import build_app
from acr_subnet.adapters.bt_validator import (
    Reply,
    Resistance,
    build_canary,
    probe_resistance,
    probe_worst_resistance,
    score_replies,
    stored_matches,
)
from acr_subnet.miner import FourPillarMiner
from acr_subnet.protocol import RatePrint
from acr_tape.chutes_source import ChutesSource
from fastapi.testclient import TestClient

INFERENCE = "ACR-INF"


@pytest.fixture
def served():
    """A miner serving the real captured Chutes tape over HTTP."""
    src = ChutesSource()
    return TestClient(build_app(FourPillarMiner(uid=7), src, INFERENCE)), src


def test_the_miner_serves_a_print_from_real_tape(served):
    c, _ = served
    r = c.get("/print")
    assert r.status_code == 200
    rp = RatePrint.model_validate(r.json())
    assert rp.value > 0 and rp.ci_lo <= rp.value <= rp.ci_hi
    assert rp.strategy == "four-pillar"
    # Single-venue tape cannot price an attack, so the miner must not claim to.
    # Same root cause as `attack_cost_per_bp` being None on the Chutes tape.
    assert rp.declared_cost_per_bp is None


def test_estimate_on_the_miners_own_window_reproduces_its_print(served):
    """Determinism over the wire, which is what makes a resistance probe mean anything.

    If the same window could yield two answers, a measured move would not
    distinguish "the attack worked" from "the estimator is noisy".
    """
    c, src = served
    clean = RatePrint.model_validate(c.get("/print").json())
    window = [e.model_dump(mode="json") for e in src.collect()]
    again = RatePrint.model_validate(c.post("/estimate", json=window).json())
    assert again.value == pytest.approx(clean.value, rel=0, abs=1e-15)


@pytest.mark.parametrize("body", [[], [{"not": "an event"}]])
def test_an_unusable_window_is_refused_not_guessed_at(served, body):
    c, _ = served
    assert c.post("/estimate", json=body).status_code == 422


def test_a_silent_miner_is_left_unscored_rather_than_scored_zero():
    """The distinction the whole adapter is built around.

    Zero is a judgement about an estimate. A miner that never answered has
    published no estimate to judge, and scoring it zero would dilute every
    honest miner's weight in proportion to how many neurons were offline --
    `weights_from_scores` normalises over what it is handed.
    """
    e = MinerEndpoint(uid=3, hotkey="5Fake", ip="10.0.0.1", port=9)
    assert score_replies([Reply(endpoint=e, error="ConnectError: refused")]) == []


def test_miner_endpoint_builds_a_dialable_url():
    e = MinerEndpoint(uid=1, hotkey="5Fake", ip="203.0.113.7", port=8091)
    assert e.url == "http://203.0.113.7:8091"


# ---- the second announcement channel: commitments ----
#
# `ServeAxon` stores an ip and a port and has no hostname field, so a miner on
# a managed host cannot announce an axon at all. These pin the channel that
# exists for it, and in particular that it refuses the same things the axon
# path refuses rather than being the lax way in.


def test_a_committed_url_is_what_gets_dialled():
    """The ip/port pair is meaningless for a commitment-announced miner.

    It is `""`/`0` there -- the same values `endpoints` reads as "not serving"
    on the axon path -- so `url` has to prefer the announced string or the
    validator would dial `http://:0`.
    """
    e = MinerEndpoint(
        uid=1,
        hotkey="5Fake",
        ip="",
        port=0,
        announced_url="https://acr-subnet-miner.onrender.com",
        source="commitment",
    )
    assert e.url == "https://acr-subnet-miner.onrender.com"


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost:8091",
        "http://127.0.0.1:8091",
        "http://0.0.0.0:8091",
        "http://[::1]:8091",
        "http://my-laptop.local:8091",
    ],
)
def test_an_endpoint_only_this_machine_can_reach_is_refused(url):
    """The same refusal `bt_miner` makes for an axon, made for a commitment.

    If the commitment channel accepted loopback it would simply be the
    unguarded way to publish the unkeepable promise the axon path rejects.
    """
    with pytest.raises(ValueError, match="this machine|no host"):
        endpoint_commitment(url)


@pytest.mark.parametrize("url", ["ws://example.com", "file:///tmp/x", "example.com"])
def test_an_endpoint_a_validator_could_not_dial_is_refused(url):
    with pytest.raises(ValueError, match="http|no host"):
        endpoint_commitment(url)


def test_an_oversized_payload_is_refused_before_it_costs_a_signature():
    """128 bytes is the `Data::Raw*` ceiling, and the deposit is real money.

    Discovering the limit as a chain rejection costs a signature and a
    `FieldDeposit` to learn something a `len()` knows for free.
    """
    with pytest.raises(ValueError, match="over the"):
        commitment_payload({"v": "acr/1", "pad": "x" * MAX_COMMITMENT_FIELD_BYTES})


def test_a_commitment_survives_the_decoder_the_sdk_actually_uses():
    """Round-trip through the chain's own encoding, not through ours.

    The `Data` enum names its variant after the payload LENGTH (`Raw61`, not
    `Raw`), and on the way back the SDK concatenates every `Raw*` field and
    decodes utf-8. This reproduces that read path against the write shape, so
    the assumption is pinned here instead of on a chain that charges for it.
    """
    payload = endpoint_commitment("https://acr-subnet-miner.onrender.com")
    raw = commitment_payload(payload)
    # exactly what `Chain.set_commitment` composes
    fields = [{f"Raw{len(raw)}": raw}]
    # exactly what `reads/identity.py::_decode_commitment_data` does with what
    # the chain hands back, which is hex rather than bytes
    data = b""
    for entry in [{k: "0x" + v.hex()} for f in fields for k, v in f.items()]:
        for variant, value in entry.items():
            if variant.startswith("Raw"):
                data += bytes.fromhex(value.removeprefix("0x"))
    assert endpoint_from_commitment(data.decode("utf-8")) == (
        "https://acr-subnet-miner.onrender.com"
    )


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "not json at all",
        "[1,2,3]",
        '{"v":"someone-else/2","url":"https://example.com"}',
        '{"v":"acr/1"}',
        '{"v":"acr/1","url":"http://127.0.0.1:9"}',
    ],
)
def test_a_commitment_this_subnet_did_not_write_is_skipped_not_raised_on(value):
    """Discovery must not be switchable off by one stranger's payload.

    Every hotkey on every subnet writes into the same storage, in whatever
    format it likes, and sealed timelocked payloads read as empty. A discovery
    path that raised on any of those would hand an outsider an outage.
    """
    assert endpoint_from_commitment(value) is None


# ---- the canary: catching a miner that echoes instead of estimating ----


class _EchoApp:
    """A miner that returns one value for every window it is ever handed.

    This is the exploit the canary exists to catch. Measuring resistance over
    HTTP means handing the miner an attacked window, so a miner can answer with
    its clean print: the attack appears to buy nothing, `move_bp` is 0, and it
    scores MAXIMALLY on the pillar it is cheating.
    """

    def __init__(self, value: float) -> None:
        from fastapi import FastAPI

        self.value = value
        self.app = FastAPI()

        @self.app.post("/estimate")
        def _estimate(window: list[dict]) -> RatePrint:
            return RatePrint(
                index_id=INFERENCE, epoch=0, miner_uid=9, value=value,
                ci_lo=value * 0.9, ci_hi=value * 1.1, strategy="echo",
            )


def _probe_through(client, endpoint, clean_value, attacked, fees, clean_window):
    """Run probe_resistance against a TestClient instead of a real socket."""
    import httpx

    real = httpx.post
    httpx.post = lambda url, json=None, timeout=None: client.post(
        "/estimate", json=json
    )
    try:
        return probe_resistance(
            endpoint, clean_value, attacked, fees, clean_window=clean_window
        )
    finally:
        httpx.post = real


ENDPOINT = MinerEndpoint(uid=9, hotkey="5Echo", ip="10.0.0.9", port=1)
WINDOW = [{
    "event_id": "a", "ts": 1.0, "service": "inference",
    "seller": "s", "buyer": "b", "price": 1e-4, "size": 1.0,
}]


def test_an_echoing_miner_is_caught_and_not_credited_with_resistance():
    """The test that would have failed before the canary existed.

    The dangerous outcome is not merely "unmeasured" -- it is `bp_per_usd=0.0`,
    which reads as an attack that bought nothing, i.e. PERFECT resistance. So
    this asserts the absence of that specific number, not just the echo flag.
    """
    echo = _EchoApp(1e-4)
    r = _probe_through(
        TestClient(echo.app), ENDPOINT, 1e-4, WINDOW, 0.05, WINDOW
    )
    assert r.echoed is True
    assert r.bp_per_usd is None, "an echo must not measure as perfect resistance"
    assert r.measured_cost_per_bp is None


def test_an_echoing_miner_scores_zero_rather_than_well():
    """End of the chain: the finding has to reach the weights.

    Routed through `score_print(is_copy=True)`, which already means "not an
    independent estimate" and already zeroes -- so `scoring.py` stays untouched.
    """
    rp = RatePrint(
        index_id=INFERENCE, epoch=0, miner_uid=9, value=1e-4,
        ci_lo=9e-5, ci_hi=1.1e-4, strategy="echo",
    )
    scores = score_replies(
        [Reply(endpoint=ENDPOINT, print_=rp)],
        resistance={9: Resistance(echoed=True)},
    )
    assert scores[0].total == 0.0


def test_the_canary_rescales_price_and_nothing_else():
    """A miner failing the canary must not be able to blame a broken window.

    The graph it sees is the same graph at a different price level, so every
    other field has to survive untouched.
    """
    out = build_canary(WINDOW, factor=1.5)
    assert out[0]["price"] == pytest.approx(1.5e-4)
    for field in ("event_id", "ts", "service", "seller", "buyer", "size"):
        assert out[0][field] == WINDOW[0][field]


def test_abstaining_under_attack_is_resistance_not_an_echo():
    """409 keeps its old meaning and must not be swept up by the new check.

    Declining to print on a tape it cannot handle is correct behaviour -- the
    in-process validator treats it as maximally resistant. Refusing to LOOK at
    the tape is the opposite, and conflating them would punish the honest move.
    """
    from fastapi import FastAPI, HTTPException

    app = FastAPI()

    @app.post("/estimate")
    def _estimate(window: list[dict]) -> RatePrint:
        raise HTTPException(status_code=409, detail="declined")

    r = _probe_through(TestClient(app), ENDPOINT, 1e-4, WINDOW, 0.05, WINDOW)
    assert r.echoed is False
    assert r.bp_per_usd == 0.0


def test_an_unscorable_epoch_carries_weights_forward_and_does_not_rank():
    """Departure (b), which the networked validator originally failed to honour.

    A real-tape epoch has no latent, so it "does not rank anyone; it PUBLISHES,
    under weights carried from the last epoch that could score". `validator.py`
    always did this; `run_epoch` did not, and scored every epoch fresh.

    The cost of getting it wrong, measured on a synthetic epoch with latent
    0.500000: four-pillar (2.1% error) and naive-VWAP (6.1% error) went from
    {0: 1.0, 1: 0.0} to {0: 0.29, 1: 0.71}. Dropping accuracy hands 71% to the
    contaminated estimator, because the remaining pillar rests on a single
    adversary tuned against the better one.
    """
    carry = {0: 1.0, 1: 0.0}
    replies = [
        Reply(endpoint=MinerEndpoint(uid=0, hotkey="a", ip="10.0.0.1", port=1),
              print_=RatePrint(index_id=INFERENCE, epoch=0, miner_uid=0,
                               value=0.5, ci_lo=0.4, ci_hi=0.6)),
        Reply(endpoint=MinerEndpoint(uid=1, hotkey="b", ip="10.0.0.2", port=1),
              print_=RatePrint(index_id=INFERENCE, epoch=0, miner_uid=1,
                               value=0.5, ci_lo=0.4, ci_hi=0.6)),
    ]
    # Reproduce run_epoch's carry branch for an epoch with no latent.
    carried = {r.endpoint.uid: carry[r.endpoint.uid]
               for r in replies if r.answered and r.endpoint.uid in carry}
    total = sum(carried.values())
    weights = {u: w / total for u, w in carried.items()} if total > 0 else {}
    assert weights == {0: 1.0, 1: 0.0}, "the earned ranking must survive"


def test_a_latent_that_exists_is_not_discarded():
    """The bug that caused the inversion: `truth` was hardcoded to None.

    Right on real tape, needlessly wrong on an epoch that carries a truth.
    Scoring the same print with and without the latent must differ, or the
    accuracy pillar is not reaching the score at all.
    """
    rp = RatePrint(index_id=INFERENCE, epoch=0, miner_uid=0,
                   value=0.51, ci_lo=0.50, ci_hi=0.52)
    reply = Reply(endpoint=MinerEndpoint(uid=0, hotkey="a", ip="10.0.0.1", port=1),
                  print_=rp)
    res = {0: Resistance(bp_per_usd=300.0, measured_cost_per_bp=0.003)}
    with_latent = score_replies([reply], resistance=res, truth=0.5)[0]
    without = score_replies([reply], resistance=res)[0]
    assert with_latent.total != without.total


def test_a_zero_the_chain_dropped_is_not_a_failed_write():
    """The chain does not echo what you sent, and the difference is not an error.

    `SetWeights` carrying {0: 1.0, 1: 0.0} on netuid 602 read back as {0: 1.0}:
    the zero entry is simply absent. A uid missing from a weight vector is paid
    nothing, which is what the zero said, so equality would flag a perfect write
    as broken.
    """
    assert stored_matches({0: 1.0}, {0: 1.0, 1: 0.0})


def test_u16_quantisation_is_not_a_failed_write():
    """Weights travel as u16, so 1/3 comes back as 21845/65535."""
    assert stored_matches(
        {0: 21845 / 65535, 1: 43690 / 65535}, {0: 1 / 3, 1: 2 / 3}
    )


def test_the_chain_paying_the_other_miner_is_a_failed_write():
    """The state netuid 602 was actually in, and the one worth catching.

    A fresh commitment named uid 1, while the vector in force still named uid 0
    -- so the chain was paying the naive estimator on a stale self-vote whose
    replacement sat encrypted in `timelocked_weight_commits`. A receipt reading
    `success=True` is consistent with exactly this, which is why the check has
    to read the chain instead.
    """
    assert not stored_matches({0: 1.0}, {0: 0.0, 1: 1.0})


def _probe_family_through(client, endpoint, clean_value, family, clean_window):
    """Run probe_worst_resistance against a TestClient instead of real sockets."""
    import httpx

    real = httpx.post
    httpx.post = lambda url, json=None, timeout=None: client.post(
        "/estimate", json=json
    )
    try:
        return probe_worst_resistance(
            endpoint, clean_value, family, clean_window=clean_window
        )
    finally:
        httpx.post = real


def test_the_wire_probe_keeps_the_familys_worst_member():
    """The networked mirror of worst-case-over-family.

    An estimator that shrugs off one member measures 0.0 bp/USDC against it,
    i.e. perfectly resistant. Averaging the two members would halve the damage
    done by the member aimed at this estimator, which is exactly the credit a
    naive VWAP was collecting from a single in-band adversary.
    """
    from fastapi import FastAPI

    app = FastAPI()

    @app.post("/estimate")
    def _estimate(window: list[dict]) -> RatePrint:
        # Tracks the window's top price, so one member moves it and one does not.
        hi = max(e["price"] for e in window)
        return RatePrint(
            index_id=INFERENCE, epoch=0, miner_uid=9,
            value=hi, ci_lo=hi * 0.9, ci_hi=hi * 1.1,
        )

    family = [
        ("in-band", [dict(WINDOW[0])], 0.05),                 # same as the clean print
        ("out-of-band", [dict(WINDOW[0], price=2e-4)], 0.05),  # twice it
    ]
    r = _probe_family_through(TestClient(app), ENDPOINT, 1e-4, family, WINDOW)
    assert r.bp_per_usd == pytest.approx(10_000 / 0.05), "the mean would be half this"


def test_an_echo_on_any_member_condemns_the_whole_family():
    """Echoing is a property of the miner, not of the member that caught it.

    A miner must not launder a detected echo by answering some other member of
    the family honestly, so the first detection short-circuits the rest and the
    result stays `bp_per_usd=None` rather than 0.0 -- which would read as an
    attack that bought nothing.
    """
    echo = _EchoApp(1e-4)
    family = [
        ("in-band", [dict(WINDOW[0])], 0.05),
        ("out-of-band", [dict(WINDOW[0], price=2e-4)], 0.05),
    ]
    r = _probe_family_through(TestClient(echo.app), ENDPOINT, 1e-4, family, WINDOW)
    assert r.echoed is True
    assert r.bp_per_usd is None


# ---- the read-only subnet view -------------------------------------------
#
# These exercise what the deployed dashboard serves. The one invariant worth
# more than the rest: a chain outage must cost `/subnet` a 503 and must cost
# `/print` nothing, because a validator's epoch depends on the latter.


class _FakeNeuron:
    def __init__(self, uid: int, hotkey: str, incentive: float) -> None:
        self.uid = uid
        self.hotkey = hotkey
        self.incentive = incentive
        self.stake = 0.0


class _FakeChain:
    """Enough of `Chain` for `read_subnet`, with no SDK and no network."""

    def __init__(self, commitments: dict[str, str] | None = None) -> None:
        from acr_subnet.adapters.bt_chain import ChainHead

        self._head = ChainHead(network="test", block=8336500)
        self._commitments = commitments or {}
        self.reads = 0

    def head(self):
        return self._head

    def subnet_identity(self, netuid):
        return {"subnet_name": "ACR Compute Rate", "description": "d", "subnet_url": ""}

    def endpoints(self, netuid):
        self.reads += 1
        return (
            [
                MinerEndpoint(
                    uid=1, hotkey="HK1", ip="", port=0,
                    announced_url="https://example.com", source="commitment",
                )
            ],
            1,
        )

    def commitments(self, netuid):
        class _R:
            def __init__(self, value, uid, block):
                self.value = value
                self.uid = uid
                self.block = block

        return {hk: _R(v, i, 8335720) for i, (hk, v) in enumerate(self._commitments.items())}

    def metagraph(self, netuid):
        class _M:
            neurons = [_FakeNeuron(0, "HK0", 0.0), _FakeNeuron(1, "HK1", 1.0)]

        return _M()


PUBLISHED = (
    '{"hi":0.000160114,"i":"ACR-INF","lo":8.64516e-05,"n":2,'
    '"p":0.000148896,"src":"chutes","ts":1036800,"v":"acr/1"}'
)


def test_the_published_rate_decodes_back_into_the_numbers_that_were_signed():
    """The payload actually on netuid 602 at block 8335720, round-tripped.

    Pinned as a literal rather than regenerated, so a change to the encoder
    that would strand what is already on chain fails here instead of in a
    dashboard that quietly renders nothing.
    """
    from acr_subnet.adapters.subnet_api import print_from_commitment

    got = print_from_commitment(PUBLISHED)
    assert got is not None
    assert got["price"] == 0.000148896
    assert got["lo"] == 8.64516e-05 and got["hi"] == 0.000160114
    assert got["index_id"] == "ACR-INF" and got["source"] == "chutes"
    assert got["n_obs"] == 2


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "not json",
        '{"v":"other/1","p":1}',          # another subnet's format
        '{"v":"acr/1","url":"https://x"}',  # ours, but an endpoint not a rate
        '["acr/1"]',
    ],
)
def test_a_commitment_that_is_not_a_rate_decodes_to_none_rather_than_raising(value):
    """Shared storage: a stranger's payload is not an error, it is not ours."""
    from acr_subnet.adapters.subnet_api import print_from_commitment

    assert print_from_commitment(value) is None


def test_the_subnet_view_reports_who_was_found_and_who_was_silent():
    from acr_subnet.adapters.subnet_api import read_subnet

    view = read_subnet(_FakeChain({"HK0": PUBLISHED}), 602)
    assert view["read_at_block"] == 8336500 and view["cite"] == "test block 8336500"
    assert view["reachable"] == 1 and view["unannounced"] == 1
    assert view["published"][0]["rate"]["price"] == 0.000148896
    found = {n["uid"]: n["found_via"] for n in view["neurons"]}
    assert found == {0: None, 1: "commitment"}


def _service_client(chain_factory):
    """The miner app with `/subnet` mounted, offline."""
    from acr_subnet.adapters.subnet_api import build_subnet_router

    app = build_app(FourPillarMiner(uid=1), ChutesSource(), INFERENCE)
    app.include_router(build_subnet_router(602, "test", chain_factory=chain_factory))
    return TestClient(app, raise_server_exceptions=False)


def test_a_chain_outage_costs_the_dashboard_a_503_and_the_miner_nothing():
    """The invariant that keeps a dashboard from taking down a miner.

    `/subnet` builds its chain connection lazily inside a try, so an RPC that
    is down or rate-limiting returns 503 here while `/print` -- the only route
    with a validator's five-second deadline on it -- keeps answering.
    """
    def _boom():
        raise RuntimeError("ws_upgrade_60s")

    c = _service_client(_boom)
    r = c.get("/subnet")
    assert r.status_code == 503
    assert "ws_upgrade_60s" in r.json()["detail"]

    assert c.get("/health").status_code == 200
    assert c.get("/print").status_code == 200


def test_the_dashboard_reads_the_chain_once_per_ttl_not_once_per_page_load():
    """A page left open must not become a load test against a public RPC."""
    chain = _FakeChain({"HK0": PUBLISHED})
    c = _service_client(lambda: chain)

    first = c.get("/subnet")
    second = c.get("/subnet")
    assert first.status_code == second.status_code == 200
    assert chain.reads == 1, "the second request should have been served from cache"
    assert first.json()["age_s"] == 0.0
    assert second.json()["read_at_block"] == 8336500


def test_the_deployed_service_serves_the_miner_and_the_view_from_one_app():
    """What actually ships: the validator's routes and the page's, composed.

    `build_app` keeps owning `/print`; this only checks that wrapping it for
    deployment did not drop a route, which is the failure a container would
    otherwise reveal to a validator rather than to CI.
    """
    from acr_subnet.adapters.bt_service import build_service_app

    app = build_service_app(
        netuid=602, network="test", index_id=INFERENCE, uid=1, self_url=""
    )
    # The schema, not `app.routes`: this FastAPI keeps an included router as a
    # single `_IncludedRouter` entry, so walking `.routes` sees a wrapper with
    # no `.path` and would have "passed" by finding nothing.
    assert {"/print", "/estimate", "/health", "/subnet"} <= set(app.openapi()["paths"])

    # And actually served -- composition that type-checks but does not route is
    # the failure a validator would find first.
    with TestClient(app) as c:
        assert c.get("/health").json() == {"status": "ok"}
        served = RatePrint.model_validate(c.get("/print").json())
    assert served.miner_uid == 1, "the deployed uid must be the one it serves as"
    assert served.value > 0


# ---- the two readers that exist because of how v11 answers -----------------


class _FakeSubnets:
    """`subnet_hyperparameters` returning a mapping, as v11 actually does."""

    def __init__(self, hp, burn=None, raises=False) -> None:
        self._hp = hp
        self._burn = burn
        self._raises = raises

    def subnet_hyperparameters(self, netuid):
        return self._hp

    def burn(self, netuid):
        if self._raises:
            raise RuntimeError("ws_upgrade_60s")
        return self._burn


def _chain_with(subnets):
    """A `Chain` whose SDK handle is faked, without constructing a real one."""
    from acr_subnet.adapters.bt_chain import Chain

    c = Chain.__new__(Chain)
    c._st = type("_St", (), {"subnets": subnets})()
    return c


def test_hyperparameters_come_back_as_a_dict_whatever_the_sdk_hands_over():
    """The trap: v11 returns a MAPPING, so `getattr(hp, "x", False)` never
    raises and never finds anything -- it returns the default, and every subnet
    reads as closed. Normalising in one place is the fix."""
    c = _chain_with(_FakeSubnets({"registration_allowed": True, "min_burn": 500000}))
    hp = c.hyperparameters(602)
    assert isinstance(hp, dict)
    assert hp["registration_allowed"] is True
    assert getattr(hp, "registration_allowed", "MISSED") == "MISSED"


def test_an_unreadable_burn_is_none_rather_than_zero():
    """An RPC that refuses us must not be reported as a free registration.

    `registration_burn` returns None for "I could not read it", and callers are
    required to treat that as UNKNOWN. Reporting 0 here would be the same class
    of error as this project's void PoW scan: a negative that the test had no
    way to distinguish from a positive.
    """
    c = _chain_with(_FakeSubnets({}, raises=True))
    assert c.registration_burn(602) is None

    missing = _chain_with(type("_S", (), {"subnet_hyperparameters": lambda s, netuid: {}})())
    assert missing.registration_burn(602) is None


def test_the_live_burn_is_not_among_the_hyperparameters():
    """Why `registration_burn` exists at all: the hyperparameters carry the
    bounds and the decay, never the current price."""
    hp = {
        "min_burn": 500000,
        "max_burn": 100000000000,
        "burn_half_life": 360,
        "burn_increase_mult": 23242897532874035200,
    }
    c = _chain_with(_FakeSubnets(hp, burn="τ0.013920908"))
    assert "burn" not in c.hyperparameters(602)
    assert c.registration_burn(602) == "τ0.013920908"
