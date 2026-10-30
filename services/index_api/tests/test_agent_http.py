"""The card over real HTTP — the proof that was missing.

`test_agentgate.py` drives `AgentGate.verify` through a hand-rolled request stub,
which is the right way to reach every refusal but proves nothing about the wiring:
before this file, **no test anywhere sent an `AGENT-CARD` header over HTTP**, and
that is exactly why the gate sat on one route of forty-two without anyone noticing.

Three things only a TestClient can establish:

  * a dependency that raises becomes a 401 RESPONSE rather than a 500,
  * the rate-limit ident the route actually passes is the cluster, not the key,
  * an anonymous caller on the newly carded routes is charged nothing at all.

Named `test_agent_http` deliberately — it sorts BEFORE `test_agentgate.py`, which
pins `index_api.agentgate.current_window` globally and never restores it. The
mirror stub here ignores the window for the same reason, so neither file can
decide the other's outcome.
"""

from __future__ import annotations

import hashlib

import pytest
from acr_core.config import ACRSettings
from acr_oracle_client.agentcard import encode_header, mint, sign_card
from acr_oracle_client.signer import LocalKeySigner
from fastapi.testclient import TestClient
from index_api import graph_proxy, ratelimit
from index_api.agentgate import AgentGate, reset_gate, set_gate
from index_api.app import app
from index_api.armor import NullScreen, Screen, ScreenError, Verdict, reset_screen, set_screen
from index_api.ratelimit import RateLimiter

CHAIN = 5042002
FLEET = "0x" + "aa" * 32   # one human, several wallets
SOLO = "0x" + "bb" * 32    # a different human

#: The nine reads that gained an optional card. Anonymous behaviour on these is
#: the thing most at risk from this change, so it is asserted rather than assumed.
CARDED_READS = (
    "/graph/operations",
    "/tca/0x00000000000000000000000000000000000000a1",
    "/rating/0x00000000000000000000000000000000000000a1",
    "/fleet",
    "/marketplace/catalog",
    "/marketplace/receipts",
    "/onchain/ACR-INF",
    "/futures",
    "/futures/ACR-INF",
)

client = TestClient(app)


class _Mirror:
    """Enough `HumanIdMirrorClient` to drive the gate over HTTP.

    WINDOW-AGNOSTIC ON PURPOSE. `test_agentgate.py` replaces
    `index_api.agentgate.current_window` at import time and never puts it back, so
    a stub that keyed on the window would pass or fail depending on which file ran
    first — a test whose result depends on collection order is not a test.
    """

    def __init__(self, by_wallet=None, configured=True):
        self._by_wallet = {k.lower(): v for k, v in (by_wallet or {}).items()}
        self._configured = configured

    def configured(self) -> bool:
        return self._configured

    def readable(self) -> bool:
        """What the gate actually asks: `cluster_of` is a view, so a read
        credential is the requirement and a write credential is not."""
        return self._configured

    def cluster_of(self, wallet: str, window: int):  # noqa: ARG002 - see the docstring
        hexed = self._by_wallet.get(wallet.lower())
        return bytes.fromhex(hexed[2:]) if hexed else None


def _signer(label: str) -> LocalKeySigner:
    return LocalKeySigner("0x" + hashlib.sha256(f"acr-http::{label}".encode()).hexdigest())


def _settings() -> ACRSettings:
    return ACRSettings(_env_file=None, arc_chain_id=CHAIN, agent_audience="acr-index-api")


def _header(label: str, **kw) -> str:
    """mint → sign → base64, the exact three calls an agent author copies."""
    opts = {"name": "demo", "role": "reader", "audience": "acr-index-api", "ttl_s": 300}
    opts.update(kw)
    signer = _signer(label)
    card = mint(signer.address, **opts)
    return encode_header(card, sign_card(card, signer, CHAIN))


def _fleet_mirror() -> _Mirror:
    return _Mirror(
        {_signer("w1").address: FLEET, _signer("w2").address: FLEET,
         _signer("stranger").address: SOLO}
    )


@pytest.fixture(autouse=True)
def _fresh_limiter(monkeypatch):
    """A limiter per test. Budgets are process-global, so without this the sybil
    test below would be decided by whatever ran before it."""
    monkeypatch.setattr(ratelimit, "_limiter", RateLimiter())


@pytest.fixture
def human_gate():
    """A gate whose mirror confirms the fleet — the human tier, reachable."""
    set_gate(AgentGate(settings=_settings(), mirror=_fleet_mirror()))
    yield
    reset_gate()


@pytest.fixture
def unverifiable_gate():
    """A gate that cannot check claims at all: the read-only deployment."""
    set_gate(AgentGate(settings=_settings(), mirror=_Mirror(configured=False)))
    yield
    reset_gate()


# --- the tiers, read back over HTTP ------------------------------------------


def test_no_card_is_anonymous_and_is_told_how_to_stop_being_anonymous():
    r = client.get("/agent/whoami")
    assert r.status_code == 200
    body = r.json()
    assert body["tier"] == "anonymous"
    assert body["carded"] is False
    # An endpoint that says "you are nobody" without saying how to become somebody
    # has described the problem and withheld the answer.
    assert body["header"] == "AGENT-CARD"
    assert body["challenge"] == "/agent/challenge"


def test_a_confirmed_cluster_reaches_the_human_tier(human_gate):
    r = client.get("/agent/whoami", headers={"AGENT-CARD": _header("w1", human_cluster=FLEET)})
    assert r.status_code == 200
    body = r.json()
    assert body["tier"] == "human"
    assert body["claimed_human"] is True
    assert body["human_note"] == ""
    # The ident is the rate-limit key and is deliberately NOT echoed.
    assert "ident" not in body and "cluster" not in body


def test_an_unverifiable_mirror_declines_the_tier_and_says_so(unverifiable_gate):
    """The third state. A gate that cannot check a claim must not grant it, and
    must not report the refusal as though the claim were false."""
    r = client.get("/agent/whoami", headers={"AGENT-CARD": _header("w1", human_cluster=FLEET)})
    assert r.status_code == 200
    body = r.json()
    assert body["tier"] == "carded"
    assert body["claimed_human"] is True
    assert "configured" in body["human_note"].lower()


def test_the_signed_scope_is_reported_as_unenforced_by_default(human_gate):
    """The DEFAULT-PATH tripwire, and it keeps its original value on purpose.

    This assertion was placed when `scope_enforced` was a hardcoded `False`, so
    that enforcing scopes could not happen silently. It still asserts `False`,
    but for a different reason: `ACR_OPERATOR_READ_SCOPE` is unset here, which is
    the state a reviewer and `scripts/verify_operator.py` meet. The other state
    is asserted in `test_operator_scope.py`, which drives both.

    So the tripwire did its job — it was changed deliberately, by a commit that
    had to come here and say why — rather than being deleted."""
    r = client.get("/agent/whoami", headers={"AGENT-CARD": _header("w1")})
    assert r.json()["scope_enforced"] is False


# --- the refusals, through the real stack ------------------------------------


def test_a_card_for_another_service_is_401_not_500(human_gate):
    """Raising inside a FastAPI dependency must become a response. This is the
    assertion the stub-driven tests structurally cannot make."""
    r = client.get("/agent/whoami", headers={"AGENT-CARD": _header("w1", audience="someone-else")})
    assert r.status_code == 401
    # The refusal names BOTH audiences. A card rejected with "wrong audience" and
    # nothing else sends an agent author guessing at which string we wanted.
    detail = str(r.json()["detail"]).lower()
    assert "someone-else" in detail and "acr-index-api" in detail


def test_a_header_that_is_not_a_card_is_401_with_something_readable(human_gate):
    r = client.get("/agent/whoami", headers={"AGENT-CARD": "this is not base64 json"})
    assert r.status_code == 401
    assert "card" in str(r.json()["detail"]).lower()


# --- what the card is FOR ----------------------------------------------------


def test_the_route_keys_the_limit_on_the_human_not_the_key(human_gate, monkeypatch):
    """Two different keys, one human, one ident — observed at the call site the
    route actually makes rather than inferred from `verify()`."""
    seen: list[tuple[str, str | None]] = []
    real = ratelimit.check

    def _spy(request, endpoint, ident=None, **kw):
        seen.append((endpoint, ident))
        return real(request, endpoint, ident, **kw)

    monkeypatch.setattr(ratelimit, "check", _spy)
    for label in ("w1", "w2"):
        r = client.post(
            "/graph/query",
            json={"operation": "meta", "variables": {}},
            headers={"AGENT-CARD": _header(label, human_cluster=FLEET)},
        )
        assert r.status_code == 200
    idents = {ident for endpoint, ident in seen if endpoint == "graph"}
    assert idents == {FLEET.lower()}, "two keys of one human must share one ident"


def test_ten_keys_of_one_human_buy_one_budget_over_http(human_gate, monkeypatch):
    """THE point, end to end. A budget of two, spent by one wallet, is exhausted
    for the OTHER wallet of the same person — while a stranger is unaffected."""
    monkeypatch.setitem(ratelimit.DESK_BUDGETS, "graph", (2, 3600.0))
    body = {"operation": "meta", "variables": {}}

    for _ in range(2):
        r = client.post("/graph/query", json=body,
                        headers={"AGENT-CARD": _header("w1", human_cluster=FLEET)})
        assert r.status_code == 200

    spent = client.post("/graph/query", json=body,
                        headers={"AGENT-CARD": _header("w2", human_cluster=FLEET)})
    assert spent.status_code == 429, "a second key of the same human must not buy a second budget"

    # The control. Without it this test would also pass if the limiter had simply
    # stopped letting anyone through.
    other = client.post("/graph/query", json=body,
                        headers={"AGENT-CARD": _header("stranger", human_cluster=SOLO)})
    assert other.status_code == 200


def test_a_carded_caller_escapes_the_shared_host_ceiling(human_gate, monkeypatch):
    """What a card BUYS. The host bucket is global behind one proxy, so a verified
    identity that still queued behind strangers would make presenting a card
    strictly worse than presenting nothing."""
    monkeypatch.setitem(ratelimit.HOST_BUDGETS, "graph", (1, 3600.0))
    body = {"operation": "meta", "variables": {}}
    card = {"AGENT-CARD": _header("w1", human_cluster=FLEET)}

    # One anonymous call spends the entire shared ceiling...
    assert client.post("/graph/query", json=body).status_code == 200
    # ...so the next anonymous caller is refused for someone else's spending. This
    # is the global-limit-behind-a-proxy problem, reproduced in three lines.
    assert client.post("/graph/query", json=body).status_code == 429
    # A verified identity is held to its OWN budget and is served anyway. That is
    # what the card buys, and before `verified=` it bought the opposite: the same
    # shared ceiling PLUS a tighter personal one.
    assert client.post("/graph/query", json=body, headers=card).status_code == 200
    assert client.post("/graph/query", json=body, headers=card).status_code == 200


# --- the regression guard for the whole scoping decision ---------------------


@pytest.mark.parametrize("path", CARDED_READS)
def test_an_anonymous_reader_is_metered_exactly_as_before(path, monkeypatch):
    """The nine reads carry the Terminal's own polling — `/api/tape` alone fans
    out one `/rating/{seller}` call per seller every 30 seconds. Adding a host
    ceiling there would stop the desk long before it stopped an abuser, so an
    anonymous caller must reach no limiter at all. Asserted, because "I was
    careful" is not a guard."""
    calls: list[str] = []
    monkeypatch.setattr(ratelimit, "check", lambda *a, **k: calls.append(a[1] if len(a) > 1 else ""))
    client.get(path)
    assert calls == [], f"{path} metered an anonymous caller"


# --- the screen, through the route that calls it -----------------------------
#
# Until these existed, `armor.py` was 352 lines with zero production call sites and
# a counter that could only ever read 0. A test of the module alone cannot tell the
# difference between "the screen works" and "nothing calls it", which is exactly
# the gap that let it ship. These drive the real route.


class _Blocks(Screen):
    """Refuses one direction and allows the other, so ordering is observable."""

    backend = "stub"

    def __init__(self, direction: str):
        super().__init__()
        self.where = direction
        self.calls: list[str] = []

    async def _verdict(self, text: str, direction: str) -> Verdict:  # noqa: ARG002
        self.calls.append(direction)
        allowed = direction != self.where
        return Verdict(allowed=allowed, backend=self.backend, direction=direction,
                       matched=() if allowed else ("prompt_injection",),
                       reason="" if allowed else "stub refusal")


class _Breaks(Screen):
    """Cannot reach a verdict. The fail-closed path."""

    backend = "stub"

    async def _verdict(self, text: str, direction: str) -> Verdict:
        raise ScreenError("stub could not reach a verdict")


@pytest.fixture
def _screen():
    made: list[Screen] = []

    def _install(screen: Screen) -> Screen:
        set_screen(screen)
        made.append(screen)
        return screen

    yield _install
    reset_screen()


def _carded_post():
    """One carded tape read — the only route the screen is wired to."""
    return client.post(
        "/graph/query",
        json={"operation": "meta", "variables": {}},
        headers={"AGENT-CARD": _header("w1", human_cluster=FLEET)},
    )


def test_a_refused_request_is_403_and_names_the_filter(human_gate, _screen):
    screen = _screen(_Blocks("request"))
    r = _carded_post()
    assert r.status_code == 403, "a caller's own refused input is 403, not 400 or 500"
    detail = r.json()["detail"]
    assert detail["matched"] == ["prompt_injection"]
    # The screened TEXT must never come back. A screen that echoes what it refused
    # is the leak it was installed to prevent.
    assert "variables" not in str(detail) and "meta" not in str(detail["reason"])
    assert screen.screened == 1 and screen.blocked == 1


def test_a_refused_reply_is_502_because_the_caller_did_nothing_wrong(
    human_gate, _screen, monkeypatch
):
    """The direction that matters. The tape indexes seller-controlled strings, so a
    reply assembled from our own subgraph can still carry someone else's payload."""
    monkeypatch.setattr(
        graph_proxy, "run", lambda op, var: {"available": True, "data": {"x": 1}}
    )
    screen = _screen(_Blocks("reply"))
    r = _carded_post()
    assert r.status_code == 502, "a bad upstream reply is not the caller's fault"
    assert screen.calls == ["request", "reply"], "the request must be screened first"


def test_a_screen_that_cannot_answer_refuses_with_503(human_gate, _screen):
    """Fail closed. A screen that allows traffic it could not inspect has stopped
    being a screen while continuing to report that it is one."""
    _screen(_Breaks())
    r = _carded_post()
    assert r.status_code == 503
    assert "verdict" in str(r.json()["detail"]).lower()


def test_an_anonymous_caller_is_never_screened(_screen):
    """The scoping decision, guarded. `armor.py` screens agent-to-agent traffic;
    a browser reader behind the Vercel proxy is not that, and roughly a thousand of
    those reach this route every hour. If this ever fails, one expired
    service-account key takes the public tape down."""
    screen = _screen(_Blocks("request"))
    r = client.post("/graph/query", json={"operation": "meta", "variables": {}})
    assert r.status_code == 200
    assert screen.calls == [], "an anonymous read must not reach the screen at all"


def test_mode_off_is_not_counted_as_an_inspection(human_gate, _screen):
    """`NullScreen` returns `screened=False`. Counting it would make a switched-off
    screen report traffic nothing looked at — the counter and the verdict field
    disagreeing about the same fact."""
    screen = _screen(NullScreen())
    assert _carded_post().status_code == 200
    assert screen.screened == 0


# --- the paid routes read the card, and read it before the payment -------------


PAID = ("/prints", "/prints/ACR-INF", "/curve/ACR-INF", "/vol/ACR-INF", "/seller-scores/ACR-INF")


@pytest.mark.parametrize("path", PAID)
def test_a_forged_card_is_refused_before_any_payment_is_taken(path, human_gate, monkeypatch):
    """For a day the buyer agent sent a card on every purchase and the six paid routes
    never read it — the only routes it actually buys from. Now they do, and the ORDER
    is the safety: the card dependency runs before `require_payment`, so a bad card is
    a 401 before money can move. Pinned by asserting the facilitator is never asked."""
    from index_api import x402

    asked: list[str] = []
    real = x402.require_payment

    async def _spy(*a, **k):
        asked.append(path)
        return await real(*a, **k)

    monkeypatch.setattr(x402, "require_payment", _spy)
    r = client.get(path, headers={"AGENT-CARD": _header("w1", audience="someone-else")})
    assert r.status_code == 401
    assert asked == [], "the payment gate must not run for a card the agent gate refused"


def test_a_good_card_on_a_paid_route_is_counted(human_gate):
    """The counter `human_tier_granted` read 0 in production after a human-claiming
    buyer bought twice, because the paid routes ignored the card. With the card read,
    a 402 challenge is still a 402 — but the gate has seen and counted the caller."""
    from index_api.agentgate import get_gate

    gate = get_gate()
    before = gate.human_verified
    r = client.get("/prints", headers={"AGENT-CARD": _header("w1", human_cluster=FLEET)})
    assert r.status_code == 402, "no payment was sent, so the gate must still ask for one"
    assert gate.human_verified == before + 1, "the human claim must be verified on a paid route"


def test_the_receipt_records_the_tier_the_card_earned(human_gate):
    """For a day the paid routes verified the card and then dropped it: the only
    trace a human-attributed purchase left was a counter on /agent/info. The receipt
    is the row the ticker, the ledger and the mirror all read, so the tier belongs
    ON it — three purchases, three tiers, each one named on its own line."""
    from index_api.x402 import DevFacilitator, reset_facilitator, set_facilitator

    set_facilitator(DevFacilitator())
    try:
        pay = {"X-Payment": "x402 0xtiered-buyer:0.0001"}
        assert client.get("/vol/ACR-INF", headers=pay).status_code == 200
        assert client.get("/vol/ACR-INF", headers={**pay, "AGENT-CARD": _header("w1")}).status_code == 200
        r = client.get("/vol/ACR-INF", headers={**pay, "AGENT-CARD": _header("w2", human_cluster=FLEET)})
        assert r.status_code == 200
        rows = client.get("/marketplace/receipts").json()["receipts"]
        # Newest first on the ledger.
        assert [row["tier"] for row in rows[:3]] == ["human", "carded", "anonymous"]
    finally:
        reset_facilitator()


@pytest.mark.parametrize("path", ["/tca/undefined", "/rating/notanaddress", "/tca/0x1234"])
def test_a_typo_is_a_422_not_a_subgraph_outage(path):
    """`GET /tca/undefined` answered 200 "the subgraph did not answer" — a caller's
    typo reported as our outage. The proxy already refused bad input by name; the
    two TCA routes now do the same, before any query is sent."""
    r = client.get(path)
    assert r.status_code == 422
    assert "address" in r.json()["detail"]
