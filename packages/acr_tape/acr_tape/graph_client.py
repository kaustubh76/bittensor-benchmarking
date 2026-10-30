"""The GraphQL transport — one place, so nothing can drift.

Both the estimator's tape source and the API's TCA surfaces read the same
subgraph. Two transports would eventually differ in a timeout, an auth header or
an error convention, and the symptom would be two ACR surfaces disagreeing about
the same public data — which is the one thing a benchmark cannot afford.

``urllib`` rather than httpx deliberately: nothing else in ``packages/`` carries
an HTTP dependency, and a tape adapter is not the place to add one to the
estimator's install.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque

log = logging.getLogger("acr_tape.graph")

#: The Graph caps `first` at 1000 and refuses `skip` past 5000 on one query.
PAGE = 1000
SKIP_CEILING = 5000

#: Studio's DEVELOPMENT query URL is capped at 3,000 queries a day (docs: "The
#: development query URL is limited to 3,000 queries per day"). The gateway,
#: with an API key, is the production path and the one Studio's dashboard counts.
STUDIO_DEV_DAILY_CAP = 3_000

#: How long one answer is reused for the same (url, query, variables). The
#: subgraph itself runs ~3 s behind the chain, so twenty seconds changes nothing a
#: reader could see — and it collapses a dozen viewers polling the same twelve
#: ratings into one query per seller per window. Hits are counted, not hidden.
CACHE_TTL_S = 20.0

#: A pace needs a window, not a boot average: the first minute after a deploy is
#: a burst (every page's first paint), and boot-average × 86 400 turned that
#: burst into an 11,000/day alarm that cried wolf on every restart. The last hour
#: × 24 is what a day at THIS load would cost, and nothing is claimed before ten
#: minutes of uptime have been seen.
PACE_WINDOW_S = 3_600.0
PACE_MIN_UPTIME_S = 600.0
_recent: deque[float] = deque(maxlen=20_000)

_lock = threading.Lock()
_cache: dict[str, tuple[float, dict, int]] = {}
#: The process-wide ledger — every query, through every caller, on one transport.
#: Served on /graph/operations so "how many subgraph queries has this press
#: made" is a number a reader can check, the way /armor/info answers the same
#: question for Google. A dashboard can read zero while the calls land.
_ledger: dict = {
    "queries": 0,
    "errors": 0,
    "cache_hits": 0,
    "last_at": None,
    "last_latency_ms": None,
    "host": None,
    "started_at": time.time(),
}


def via_of(url: str) -> str:
    """Which path a URL is: Studio's development endpoint, the network gateway, or
    something else. Named on /ops because the two are billed and counted apart."""
    host = urllib.parse.urlsplit(url).hostname or ""
    if host == "api.studio.thegraph.com":
        return "studio-dev"
    if host.endswith("gateway.thegraph.com") or host.endswith("gateway-arbitrum.network.thegraph.com"):
        return "gateway"
    return "custom" if host else "unset"


#: How far AHEAD of the chain's head a subgraph may legitimately report. Only a
#: race between two reads: we ask the subgraph and the node at slightly different
#: moments, and at ~0.510 s a block a few hundred is minutes, not a mistake.
AHEAD_MARGIN_BLOCKS = 500


#: What the last identity check saw, for transport_info. Written by whoever ran
#: the check (the API knows the chain head; this layer does not).
_chain_seen: dict[str, object] = {}


def record_chain_check(subgraph_head: int | None, chain_head: int | None, verdict: str | None) -> None:
    """Keep the last identity verdict where /ops can show it."""
    _chain_seen.update(
        subgraph_head=subgraph_head, chain_head=chain_head, verdict=verdict, at=time.time()
    )


def wrong_chain_reason(subgraph_head: int | None, chain_head: int | None) -> str | None:
    """Why this subgraph cannot be indexing this chain, or None.

    `via_of` above answers "what KIND of path is this". This answers the harder
    question nothing was asking: "is it even our chain?" `graph_proxy` checked
    only that ACR_SUBGRAPH_URL was SET, and `tca.py` does not go through that
    gate at all — so /tca would answer from whatever subgraph it was pointed at,
    including another network's, and report the result as this deployment's TCA.
    Measured 2026-10-01: the URL in `.env` reports head 64,922,872 while Arc
    mainnet is at 23,685,519.

    ONE DIRECTION ONLY, on purpose. A subgraph cannot have indexed blocks the
    chain has not produced, so "ahead" is a certainty and needs no tuning — and
    it is the drift that actually happens here, a mainnet deployment still
    reading the testnet subgraph. "Behind" is reported elsewhere and never
    refused: a subgraph re-indexing from its startBlock is legitimately far
    behind and catching up, and a tunable threshold guessing between that and a
    foreign chain would be worse than the hole it closed.

    Absence of evidence is not evidence — an unknown head on either side allows
    the subgraph, the same way an unreadable balance never stops a print.
    """
    if subgraph_head is None or chain_head is None:
        return None
    if subgraph_head <= 0 or chain_head <= 0:
        return None
    ahead = subgraph_head - chain_head
    if ahead <= AHEAD_MARGIN_BLOCKS:
        return None
    return (
        f"the subgraph is at block {subgraph_head:,} but this chain's head is "
        f"{chain_head:,} ({ahead:,} ahead) — it is indexing a different network"
    )


def transport_info(url: str = "") -> dict:
    """The ledger plus the path's identity, for /graph/operations and /ops."""
    with _lock:
        snap = dict(_ledger)
    host = snap.get("host") or (urllib.parse.urlsplit(url).hostname if url else None)
    via = via_of(url) if url else ("studio-dev" if host == "api.studio.thegraph.com" else "unset")
    now = time.time()
    uptime_s = max(1.0, now - float(snap.pop("started_at")))
    with _lock:
        while _recent and now - _recent[0] > PACE_WINDOW_S:
            _recent.popleft()
        last_hour = len(_recent)
    measuring = uptime_s < PACE_MIN_UPTIME_S
    return {
        **snap,
        "host": host,
        "via": via,
        "uptime_s": round(uptime_s),
        "last_hour": last_hour,
        "measuring": measuring,
        "pace_per_day": None if measuring else last_hour * 24,
        "daily_cap": STUDIO_DEV_DAILY_CAP if via == "studio-dev" else None,
        "cache_ttl_s": CACHE_TTL_S,
        "cache_bytes": cache_bytes(),
        "cache_entries": len(_cache),
        # Whose chain, when something has asked. `wrong_chain_reason` is the
        # judgement; this is how /ops and /graph/operations learn it was made, so
        # a refused /tca is diagnosable instead of a mysteriously empty answer.
        "chain_check": dict(_chain_seen) or None,
    }


def _reset_for_tests() -> None:
    _chain_seen.clear()
    with _lock:
        _cache.clear()
        _recent.clear()
        _ledger.update(queries=0, errors=0, cache_hits=0, last_at=None, last_latency_ms=None,
                       host=None, started_at=time.time())


def graph_query(
    url: str, query: str, variables: dict, api_key: str = "", timeout: float = 20.0
) -> dict:
    """One GraphQL POST. Returns ``{}`` on any failure — never raises.

    A GraphQL endpoint answers 200 with an ``errors`` array, so a malformed
    query looks exactly like a good one to the transport layer. Both are treated
    as no data, and both are logged: an empty tape reported as empty is honest,
    an empty tape reported as zero is not.
    """
    if not url:
        return {}
    body = json.dumps({"query": query, "variables": variables}).encode()
    key = url + "\x00" + body.decode()
    now = time.time()
    with _lock:
        hit = _cache.get(key)
        if hit is not None and now - hit[0] < CACHE_TTL_S:
            _ledger["cache_hits"] += 1
            return hit[1]
    headers = {"Content-Type": "application/json", "User-Agent": "acr-graph"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(url, data=body, headers=headers)
    started = time.monotonic()
    with _lock:
        _ledger["queries"] += 1
        _ledger["host"] = urllib.parse.urlsplit(url).hostname
        _recent.append(now)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            payload = json.loads(r.read() or b"{}")
    except (urllib.error.URLError, TimeoutError, ValueError, OSError) as exc:
        log.warning("graph: query failed (%s)", exc)
        with _lock:
            _ledger["errors"] += 1
        return {}
    with _lock:
        _ledger["last_at"] = time.time()
        _ledger["last_latency_ms"] = int((time.monotonic() - started) * 1000)
    if payload.get("errors"):
        log.warning("graph: GraphQL errors: %s", payload["errors"][:2])
        with _lock:
            _ledger["errors"] += 1
        return {}
    data = payload.get("data") or {}
    with _lock:
        _cache[key] = (now, data, len(payload_bytes) if (payload_bytes := _approx_bytes(data)) else 0)
        _sweep(now)
    return data


#: The cache's memory budget. The production press runs in 512 MiB, and on
#: 2026-09-13 it was OOM-killed three minutes after this cache landed: expired
#: 1,000-row settlement pages lingered until the entry COUNT crossed 512, which a
#: tape with many payers and sellers reaches with hundreds of stale pages in hand.
#: Now expired entries go on every insert and the live set is capped in bytes.
CACHE_MAX_BYTES = 8 * 1024 * 1024
CACHE_MAX_ENTRIES = 128


def _approx_bytes(data: dict) -> bytes:
    try:
        return json.dumps(data, separators=(",", ":")).encode()
    except (TypeError, ValueError):
        return b""


def _sweep(now: float) -> None:
    """Drop expired entries, then the oldest until the byte and count caps hold.
    Called under `_lock`."""
    for k in [k for k, v in _cache.items() if now - v[0] >= CACHE_TTL_S]:
        _cache.pop(k, None)
    total = sum(v[2] for v in _cache.values())
    if total <= CACHE_MAX_BYTES and len(_cache) <= CACHE_MAX_ENTRIES:
        return
    for k in sorted(_cache, key=lambda k: _cache[k][0]):
        v = _cache.pop(k, None)
        if v is None:
            continue
        total -= v[2]
        if total <= CACHE_MAX_BYTES and len(_cache) <= CACHE_MAX_ENTRIES:
            break


def cache_bytes() -> int:
    with _lock:
        return sum(v[2] for v in _cache.values())


def drop_cache() -> None:
    """Forget every cached page. The memory guard's lever; the next reads
    simply query again."""
    with _lock:
        _cache.clear()
