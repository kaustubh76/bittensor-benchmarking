"""On-chain oracle reader — the API serving the *settlement-grade* print.

When the API is configured with a deployed ``ACROracle`` address
(``ACR_ORACLE_ADDRESS`` + ``ACR_ARC_RPC_URL``), it can serve the value other
contracts actually settle against — read straight from chain — alongside the
freshly-computed estimate. Fully optional: with no chain configured every method
returns ``None``/``{}`` and the API keeps serving in-process prints.
"""

from __future__ import annotations

import logging
import threading
import time

from acr_core import ALL_INDEX_IDS, get_settings
from acr_oracle_client import FuturesClient, OracleClient

log = logging.getLogger(__name__)

#: On-chain prints only change when the poster posts (~every refresh, 30s), but
#: each read is a slow remote-RPC call. Cache read_all() so /terminal/data stays
#: fast (the human feed is polled every few seconds by many clients) instead of
#: blocking on 3 sequential eth_calls per request. TTL is set above the refresh
#: interval so the background loop's warm (read_all(use_cache=False) after each
#: post) keeps the request-path cache from ever expiring; the value stays ~fresh
#: to the last post cycle regardless.
READ_ALL_TTL_S = 90.0


class OracleUnreachable(RuntimeError):
    """The chain did not answer and no print for this index was ever read."""


class OracleReader:
    def __init__(self, rpc_url: str | None = None, oracle_address: str | None = None) -> None:
        settings = get_settings()
        self.oracle_address = oracle_address or (settings.oracle_address or None)
        self.rpc_url = rpc_url or settings.arc_rpc_url
        self._client = OracleClient(rpc_url=self.rpc_url, oracle_address=self.oracle_address)
        self._lock = threading.Lock()
        self._cache: dict[str, dict] = {}
        self._cache_at = 0.0

    @property
    def configured(self) -> bool:
        return bool(self.oracle_address)

    def read(self, index_id: str) -> dict | None:
        """The latest print, read from chain now; on a failed read, the last good
        one. None means the oracle has no print for this index, or the chain is
        down and nothing was ever read (callers here use ``or {}``)."""
        if not self.configured:
            return None
        try:
            return self._client.read_latest(index_id, strict=True)
        except Exception:
            with self._lock:
                kept = self._cache.get(index_id)
            return dict(kept) if kept else None

    def latest(self, index_id: str) -> dict | None:
        """What ``/onchain/{index_id}`` serves: the warm cache when it is inside
        its TTL (the warm loop re-reads it every tick, and prints are hourly),
        else a read from chain, else the last good print.

        That route is what the MCP plugin's ``get_rate``/``check_spend`` and the
        Aomi App call for every agent question, and it used to go to the chain
        uncached, where a 429 came back as ``404 no on-chain print``: both
        integrations grew a re-ask to survive it. Raises
        :class:`OracleUnreachable` only when the chain did not answer AND no
        print was ever read, so a caller can say "ask again" (503), not "none".
        """
        if not self.configured:
            return None
        with self._lock:
            kept = self._cache.get(index_id)
            fresh = kept is not None and time.monotonic() - self._cache_at < READ_ALL_TTL_S
        if fresh:
            return dict(kept)
        try:
            r = self._client.read_latest(index_id, strict=True)
        except Exception as exc:
            if kept is not None:
                return dict(kept)
            raise OracleUnreachable(index_id) from exc
        if r is not None:
            with self._lock:
                self._cache[index_id] = r
        return r

    def read_all(self, *, use_cache: bool = True) -> dict[str, dict]:
        """Every index's latest print, cached for ``READ_ALL_TTL_S``. A throttled
        read keeps that index's last good print rather than dropping it: a print
        carries its own ``posted_at``, so a kept one still states its age."""
        if not self.configured:
            return {}
        if use_cache:
            with self._lock:
                if self._cache and time.monotonic() - self._cache_at < READ_ALL_TTL_S:
                    return dict(self._cache)
        with self._lock:
            before = dict(self._cache)
        out: dict[str, dict] = {}
        for iid in ALL_INDEX_IDS:
            try:
                r = self._client.read_latest(iid, strict=True)
            except Exception:
                r = before.get(iid)
            if r is not None:
                out[iid] = r
        with self._lock:
            self._cache = out
            self._cache_at = time.monotonic()
        return out


# Lazy module singleton (not lru_cache) so the env is read when the reader is
# first built at startup — and so tests can reset it.
_reader: OracleReader | None = None


def get_reader() -> OracleReader:
    global _reader
    if _reader is None:
        _reader = OracleReader()
    return _reader


def reset_reader() -> None:
    """Drop the cached reader (tests / after changing oracle env)."""
    global _reader
    _reader = None


#: TTL for the futures desk cache — same rationale as READ_ALL_TTL_S (the human
#: feed is polled often; each desk read is several eth_calls).
FUTURES_TTL_S = 90.0


class FuturesReader:
    """Reads the on-chain ``ACRFutures`` desk (one series per index) for the
    Terminal. Mirrors :class:`OracleReader`: cached, and a graceful no-op
    (``configured is False`` → empty) when ``ACR_FUTURES_ADDRESS`` is unset.

    A FAILED READ NEVER REPLACES A GOOD ONE. The client swallows every RPC error
    into ``[]``/``None``, so one throttled scan used to cache an EMPTY desk and
    an EMPTY tape, and ``/futures`` then answered live with no desks: the testnet
    terminal showed an empty venue between two full ones (desks per poll
    ``[3, 0]``, 2026-10-14). The reader now reads in the client's ``strict`` mode
    and keeps the last good value when a read raises. Series are append-only on
    chain, so an index that had a desk always has one, and a missing desk after
    a good read can only be a failed read.

    A STALE CACHE IS SERVED, NOT WAITED ON. The warm loop refreshes these every
    ``ACR_CHAIN_WARM_SECONDS`` plus however long its chores take over a
    throttled RPC, which can outlast the TTL; the next visitor then paid the full
    scan on the request path (``/futures`` measured 2-26 s while ``/health``
    answered in 0.4 s), and concurrent visitors each paid it again. Past the TTL
    a request now gets the last read at once and starts ONE background refresh.
    Only a reader with nothing cached at all waits for the chain.
    """

    def __init__(self, rpc_url: str | None = None, futures_address: str | None = None) -> None:
        settings = get_settings()
        self.futures_address = futures_address or (settings.futures_address or None)
        self.rpc_url = rpc_url or settings.arc_rpc_url
        self._client = FuturesClient(rpc_url=self.rpc_url, futures_address=self.futures_address)
        self._lock = threading.Lock()
        self._cache: dict[str, dict] = {}
        self._cache_at = 0.0
        #: True once a desk reading exists to serve, even a partly kept one.
        #: Distinct from ``_cache_at``, which only a clean read advances.
        self._cache_held = False
        #: The venue's whole series list, shared by the desk's exit path.
        self._series: list[dict] = []
        self._series_at = 0.0
        #: Short-TTL trade-tape cache + first-seen wall-clock per tx (so the UI can
        #: show an honest "seen Ns ago" without a per-block RPC round trip).
        self._trades: list[dict] = []
        self._trades_at = 0.0
        self._seen: dict[str, float] = {}
        #: Background refreshes in flight, by cache name: one at a time each.
        self._inflight: set[str] = set()
        #: When each cache was last ATTEMPTED (monotonic), success or not. Past
        #: its first attempt a cache never makes a request wait on the chain.
        self._tried: dict[str, float] = {}
        #: One cold read per cache at a time; concurrent cold readers wait for it
        #: and then find it filled, instead of each paying for the same scan.
        self._cold = {name: threading.Lock() for name in ("series", "desks", "trades")}

    @property
    def configured(self) -> bool:
        return bool(self.futures_address)

    #: The floor between two attempts at a cache whose reads keep failing. Under
    #: the TTL, so a healthy cache refreshes exactly as before; a throttled one
    #: is retried every half minute rather than on every poll.
    _RETRY_S = 30.0

    def _refresh_behind(self, name: str, fn) -> None:
        """Run ``fn`` on a daemon thread unless a refresh of ``name`` is running
        or was attempted within ``_RETRY_S``."""
        with self._lock:
            if name in self._inflight:
                return
            if time.monotonic() - self._tried.get(name, -1e9) < self._RETRY_S:
                return
            self._inflight.add(name)

        def run() -> None:
            try:
                fn()
            except Exception:  # pragma: no cover - fn keeps the last good value
                log.warning("futures: background %s refresh failed", name, exc_info=True)
            finally:
                with self._lock:
                    self._inflight.discard(name)

        threading.Thread(target=run, name=f"futures-{name}", daemon=True).start()

    def all_series(self, *, use_cache: bool = True) -> list[dict]:
        """Every series on the venue, settled ones included — memoized.

        The desk's exit path needs exactly the series ``read_all`` filters out
        (a reader must be able to leave a series that has expired), so it used
        to call the client directly and paid a full uncached scan every time:
        measured at 9.5s on the request path against a warm host. Sharing one
        memo means the background warm covers that path too.
        """
        if not self.configured:
            return []
        if use_cache:
            with self._lock:
                known = self._series_at > 0
                cached, age = list(self._series), time.monotonic() - self._series_at
            if known:
                if age >= FUTURES_TTL_S:
                    # The desk refresh re-reads the series first: one scan, not two.
                    self._refresh_behind("desks", lambda: self.read_all(use_cache=False))
                return [dict(s) for s in cached]
            with self._cold["series"]:
                with self._lock:
                    if self._series_at > 0:
                        return [dict(s) for s in self._series]
                return self._read_series()
        return self._read_series()

    def _read_series(self) -> list[dict]:
        try:
            out = self._client.read_all_series(strict=True)
        except Exception:
            with self._lock:
                self._tried["series"] = time.monotonic()
                return [dict(s) for s in self._series]
        with self._lock:
            self._tried["series"] = time.monotonic()
            # Append-only on chain: a shorter list than the last one is a short read.
            if len(out) >= len(self._series):
                self._series = out
                self._series_at = time.monotonic()
            return [dict(s) for s in self._series]

    def read_desk(self, index_id: str, *, all_series: list[dict] | None = None) -> dict | None:
        if not self.configured:
            return None
        try:
            return self._client.read_desk(index_id, all_series=all_series)
        except Exception:
            return None

    def read_all(self, *, use_cache: bool = True) -> dict[str, dict]:
        """Every index's live desk.

        Scans the venue's series ONCE and hands the list to each index, rather
        than letting each ``read_desk`` re-scan. That scan is the expensive part
        and only one of the demo's three indices has a series, so the old shape
        paid for the whole venue three times to produce one desk — the single
        biggest contributor to a cold desk read.
        """
        if not self.configured:
            return {}
        if use_cache:
            with self._lock:
                held = self._cache_held
                cached, age = dict(self._cache), time.monotonic() - self._cache_at
            if held:
                if age >= FUTURES_TTL_S:
                    self._refresh_behind("desks", lambda: self.read_all(use_cache=False))
                return cached
            with self._cold["desks"]:
                with self._lock:
                    if self._cache_held:
                        return dict(self._cache)
                return self._read_desks(use_cache=True)
        return self._read_desks(use_cache=False)

    def _read_desks(self, *, use_cache: bool) -> dict[str, dict]:
        with self._lock:
            self._tried["desks"] = time.monotonic()
        series = self.all_series(use_cache=use_cache)
        with self._lock:
            series_known = self._series_at > 0
            before = dict(self._cache)
        if not series_known:
            return before  # the venue has never been read: nothing to build a desk from
        out: dict[str, dict] = {}
        failed = 0
        for iid in ALL_INDEX_IDS:
            try:
                d = self._client.read_desk(iid, all_series=series, strict=True)
            except Exception:
                failed += 1
                d = before.get(iid)  # append-only series: a vanished desk is a failed read
            if d is not None:
                out[iid] = d
        with self._lock:
            self._cache = out
            self._cache_held = True
            # Only a read with no failures is FRESH; a merged one is served, and
            # its old age makes the next request refresh it behind.
            if failed == 0:
                self._cache_at = time.monotonic()
            return dict(out)

    def maker_inventory(self, *, use_cache: bool = True) -> dict[str, float]:
        """Per-index maker inventory (signed contracts) — what ``store.curve()``
        skews the term structure around."""
        return {
            iid: float(d.get("maker_inventory", 0.0))
            for iid, d in self.read_all(use_cache=use_cache).items()
        }

    #: The trade tape wants fresher data than the aggregate desk, but every miss
    #: costs an eth_getLogs against a throttled RPC — and that read sits on the
    #: request path of the endpoint the trading desk's liveness is judged by, so
    #: a too-eager TTL makes /futures intermittently slow enough for the
    #: terminal to fall back a tier and hide the desk. The heartbeat trades
    #: hourly; half a minute of tape staleness is invisible next to that.
    #: Raised from 30s once the tape started PAGING back several hours: that walk
    #: costs a few seconds when it misses, and /futures is the endpoint the
    #: desk's liveness is judged by. The background warm refreshes this every
    #: ACR_CHAIN_WARM_SECONDS (60), so the TTL only has to outlive that gap —
    #: and the fills themselves are at most hourly, so a 90s-old tape is not
    #: meaningfully staler than a fresh one. Ages shown in the UI come from a
    #: server-side first-seen stamp, not from this read.
    _TRADES_TTL_S = 90.0

    def recent_trades(self, *, use_cache: bool = True) -> list[dict]:
        """Recent on-chain fills (newest-first), each stamped with the wall-clock
        the server first observed its tx (``seen_at``)."""
        if not self.configured:
            return []
        if use_cache:
            with self._lock:
                # TRIED, not succeeded: on the press, Arc's RPC can throttle the
                # tape's first page on every attempt (Render's shared egress), and
                # keying on success sent EVERY /futures request into the walk and
                # its 429 backoff, 2-42 s each, while a local run of the same
                # reader answered in 0.0 s. An unread tape is served as held and
                # retried behind, no more than once per _RETRY_S.
                tried = "trades" in self._tried
                cached, age = list(self._trades), time.monotonic() - self._trades_at
            if tried:
                if age >= self._TRADES_TTL_S:
                    self._refresh_behind("trades", lambda: self.recent_trades(use_cache=False))
                return [dict(t) for t in cached]
            with self._cold["trades"]:
                with self._lock:
                    if "trades" in self._tried:
                        return [dict(t) for t in self._trades]
                return self._read_trades()
        return self._read_trades()

    def _read_trades(self) -> list[dict]:
        try:
            raw = self._client.recent_trades(strict=True)
        except Exception:
            with self._lock:
                self._tried["trades"] = time.monotonic()
                return [dict(t) for t in self._trades]
        now = time.time()
        stamped: list[dict] = []
        for t in raw:
            seen = self._seen.setdefault(t["tx"], now)
            stamped.append({**t, "seen_at": seen})
        with self._lock:
            self._trades = stamped
            self._trades_at = self._tried["trades"] = time.monotonic()
            # Bound the first-seen map to the txs still in the window.
            live_txs = {t["tx"] for t in stamped}
            self._seen = {tx: s for tx, s in self._seen.items() if tx in live_txs}
        return [dict(t) for t in stamped]

    def settled_rounds(self, *, use_cache: bool = True) -> list[dict]:
        """Every series that has completed its life, newest first.

        ``select_series_for_index`` deliberately hides a settled series the
        moment a live one exists on the same index — right for the tradable
        desk, and exactly wrong for proving the machinery works: the venue's
        first completed settlement would leave no visible trace anywhere but
        the explorer. This is the ledger of finished rounds, built from the
        same cached ``all_series`` scan the desks already paid for, so it
        costs no extra RPC.
        """
        if not self.configured:
            return []
        try:
            series = self.all_series(use_cache=use_cache)
        except Exception:
            return []
        done = [s for s in series if s.get("exists") and s.get("settled")]
        done.sort(key=lambda s: s.get("series_id", 0), reverse=True)
        # Exactly the fields `descale_series` decodes, and no more. Open
        # interest and trader count belong to `read_desk`'s enrichment, not to
        # this scan, and after settlement the contract has deleted every
        # position anyway — a zero OI here would describe the clearing, not
        # the round that was traded.
        return [
            {
                "series_id": s.get("series_id"),
                "index_id": s.get("index_id"),
                "settlement_price": s.get("settlement_price"),
                "expiry_ts": s.get("expiry_ts"),
                "multiplier": s.get("multiplier"),
                "maker": s.get("maker"),
            }
            for s in done
        ]

    def roster(self, *, use_cache: bool = True) -> dict:
        """The whole futures venue for the Terminal desk + tape: address, per-index
        desks, the recent trade tape, and the settled-rounds ledger."""
        if not self.configured:
            return {"venue": None, "desks": {}, "trades": [], "settled": []}
        roster = {
            "venue": self.futures_address,
            "desks": self.read_all(use_cache=use_cache),
            "trades": self.recent_trades(use_cache=use_cache),
            "settled": self.settled_rounds(use_cache=use_cache),
        }
        roster["freshness"] = self.freshness()
        return roster

    def freshness(self) -> dict:
        """Seconds since each part of the roster was last READ from chain, None
        if it never has been since boot. An empty tape and an unread one look
        identical in ``trades``; this is what tells them apart
        (scripts/verify_live.py reads it)."""
        now = time.monotonic()
        with self._lock:
            ages = {
                "desks_age_s": now - self._cache_at if self._cache_at > 0 else None,
                "tape_age_s": now - self._trades_at if self._trades_at > 0 else None,
                "series_age_s": now - self._series_at if self._series_at > 0 else None,
            }
        return {k: (round(v, 1) if v is not None else None) for k, v in ages.items()}


_futures: FuturesReader | None = None


def get_futures() -> FuturesReader:
    global _futures
    if _futures is None:
        _futures = FuturesReader()
    return _futures


def reset_futures() -> None:
    """Drop the cached futures reader (tests / after changing futures env)."""
    global _futures
    _futures = None
