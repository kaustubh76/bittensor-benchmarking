"""The desk's rate limiter — the guard on an unauthenticated public endpoint.

Two properties matter here and they pull in opposite directions:

* the guard must actually stop a runaway caller, and it must not be possible to
  step around it by rewriting a header;
* it must not mistake *a crowd behind one proxy* for one abusive caller. The
  desk is reached through a server-side Next.js proxy, so IP alone says nothing
  about who is asking — an earlier version keyed everything on it and rationed
  the whole world to five sessions an hour.

The bound on key count is as load-bearing as the limit itself: these keys come
from caller-supplied identity, so a limiter that grows forever is the denial of
service it was added to prevent.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from index_api import ratelimit
from index_api.ratelimit import (
    DESK_BUDGETS,
    HOST_BUDGETS,
    RateLimiter,
    client_key,
    session_ident,
)


@pytest.fixture(autouse=True)
def fresh_limiter(monkeypatch):
    """``check`` uses a module-global counter; give each test its own."""
    monkeypatch.setattr(ratelimit, "_limiter", RateLimiter())


def _req(xff: str = "", peer: str = "10.0.0.1"):
    return type(
        "_Req",
        (),
        {
            "headers": {"x-forwarded-for": xff} if xff else {},
            "client": type("C", (), {"host": peer})(),
        },
    )()


# --- the counter itself -----------------------------------------------------


def test_allows_up_to_the_limit_then_refuses():
    lim = RateLimiter()
    assert all(lim.allow("k", 3, 60.0, now=0.0) for _ in range(3))
    assert not lim.allow("k", 3, 60.0, now=0.0)


def test_keys_are_independent():
    lim = RateLimiter()
    assert lim.allow("a", 1, 60.0, now=0.0)
    assert not lim.allow("a", 1, 60.0, now=0.0)
    assert lim.allow("b", 1, 60.0, now=0.0)  # one caller can't exhaust another


def test_the_window_expires():
    lim = RateLimiter()
    assert lim.allow("k", 1, 60.0, now=0.0)
    assert not lim.allow("k", 1, 60.0, now=59.0)
    assert lim.allow("k", 1, 60.0, now=61.0)


def test_key_table_is_bounded_and_evicts_least_recently_used():
    lim = RateLimiter(max_keys=4)
    for i in range(50):
        lim.allow(f"k{i}", 1, 60.0, now=0.0)
    assert len(lim._hits) <= 4


# --- which hop is the source host -------------------------------------------


def test_client_key_takes_the_hop_our_own_edge_SAW():
    """Each proxy APPENDS the peer it saw, so the right-most entry is the only
    one we observed; the left-most is whatever the caller chose to send. This
    host is public, so trusting the left-most would let an abuser mint a fresh
    bucket per request — the exact thing this guard exists to prevent."""
    assert client_key(_req("45.33.32.156, 151.101.65.140")) == "151.101.65.140"


def test_a_spoofed_forwarded_for_cannot_mint_a_new_bucket():
    a = client_key(_req("45.33.32.1, 151.101.65.140"))
    b = client_key(_req("45.33.32.2, 151.101.65.140"))
    assert a == b == "151.101.65.140"


def test_client_key_walks_past_our_own_plumbing():
    """A platform load balancer may append its own internal address. Keying on
    that would put every source in the world into one bucket."""
    assert client_key(_req("45.33.32.156, 151.101.65.140, 10.0.0.9")) == "151.101.65.140"


def test_client_key_falls_back_to_the_peer():
    assert client_key(_req(peer="151.101.65.140")) == "151.101.65.140"


# --- identity vs host -------------------------------------------------------


def test_a_crowd_behind_one_proxy_is_not_one_caller():
    """The demo-day case: several readers, one Vercel edge IP. Each must get
    their own faucet budget — this is what the old IP-only limiter broke."""
    proxy = _req(peer="151.101.65.140")
    faucet_per_session, _ = DESK_BUDGETS["faucet"]
    for reader in range(6):
        ident = session_ident(f"token-for-reader-{reader}")
        for _ in range(faucet_per_session):
            ratelimit.check(proxy, "faucet", ident)  # must not raise


def test_one_greedy_session_cannot_lock_out_another():
    proxy = _req(peer="151.101.65.140")
    greedy, other = session_ident("greedy"), session_ident("other")
    for _ in range(DESK_BUDGETS["faucet"][0]):
        ratelimit.check(proxy, "faucet", greedy)
    with pytest.raises(HTTPException) as exc:
        ratelimit.check(proxy, "faucet", greedy)
    assert exc.value.status_code == 429
    ratelimit.check(proxy, "faucet", other)  # unaffected


def test_the_host_ceiling_still_trips():
    """Per-identity fairness must not become 'no limit at all' — a caller who
    rotates identities is still held by the source-host ceiling."""
    proxy = _req(peer="151.101.65.140")
    limit, _ = HOST_BUDGETS["session"]
    for i in range(limit):
        ratelimit.check(proxy, "session", f"user-{i}")
    with pytest.raises(HTTPException) as exc:
        ratelimit.check(proxy, "session", "user-one-too-many")
    assert exc.value.status_code == 429


def test_hosts_are_independent_of_each_other():
    for i in range(HOST_BUDGETS["session"][0]):
        ratelimit.check(_req(peer="151.101.65.140"), "session", f"user-{i}")
    ratelimit.check(_req(peer="8.8.8.8"), "session", "someone-else")


def test_an_unidentified_caller_still_meets_the_ceiling():
    """No identity in hand (a malformed or direct call) means the host ceiling
    is the only thing holding — it must still hold."""
    peer = _req(peer="151.101.65.140")
    for _ in range(HOST_BUDGETS["faucet"][0]):
        ratelimit.check(peer, "faucet")
    with pytest.raises(HTTPException):
        ratelimit.check(peer, "faucet")


def test_identity_is_case_insensitive():
    """Wallet addresses arrive from Circle lowercase and from the chain
    checksummed; the same wallet must not get two budgets."""
    proxy = _req(peer="151.101.65.140")
    addr = "0x95DE70736E21e70DF921Fb3ab91dD56750965b59"
    for _ in range(DESK_BUDGETS["limits"][0]):
        ratelimit.check(proxy, "limits", addr)
    with pytest.raises(HTTPException):
        ratelimit.check(proxy, "limits", addr.lower())


# --- budget shape -----------------------------------------------------------


def test_session_ident_does_not_leak_the_bearer_token():
    tok = "eyJhbGciOiJIUzI1NiJ9.super-secret-user-token"
    ident = session_ident(tok)
    assert tok not in ident and len(ident) == 32
    assert ident == session_ident(tok)  # stable across calls


def test_the_money_endpoints_are_the_strictest():
    """A budget regression here is a funding leak, not a nuisance."""
    faucet, _ = DESK_BUDGETS["faucet"]
    session, _ = DESK_BUDGETS["session"]
    reads, _ = DESK_BUDGETS["limits"]
    assert faucet <= session < reads


def test_every_endpoint_has_both_budgets_and_the_host_one_is_looser():
    """A host ceiling BELOW the per-person budget would silently re-create the
    global limit this split exists to remove."""
    assert set(DESK_BUDGETS) == set(HOST_BUDGETS)
    for endpoint, (per_identity, _) in DESK_BUDGETS.items():
        assert HOST_BUDGETS[endpoint][0] >= per_identity, endpoint


def test_the_faucet_ceiling_leaves_room_for_the_ledgers_own_cap():
    """The ledger's global cap should be what says no — it says so honestly
    ('the faucet is out of funds') rather than 'try again later'."""
    from index_api.desk import FAUCET_GLOBAL_CAP

    assert HOST_BUDGETS["faucet"][0] > FAUCET_GLOBAL_CAP
