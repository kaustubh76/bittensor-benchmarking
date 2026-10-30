"""Read/write the on-chain ``ACRFutures`` contract.

The Terminal desk surfaces the live futures market — the maker's inventory,
mark-to-oracle PnL, and settlement — read straight from chain. This client
mirrors :class:`OracleClient`: lazy web3, graceful offline (every read returns
``None``/``[]`` with no chain configured), and build-sign-send writes for the
demo maker/taker loop. Prices are WAD (1e18); collateral/PnL are USDC-6 (1e6).
"""

from __future__ import annotations

import logging
import os
import time

from acr_core import get_settings

from .client import USDC, WAD, index_id_to_bytes32
from .signer import Signer, build_signer

log = logging.getLogger("acr_oracle_client")


def _rpc_retry(fn, *args, tries: int = 5, base: float = 1.5, **kwargs):
    """Call an RPC-backed function, retrying the public Arc RPC's 429s with
    linear backoff. The free-tier Arc endpoint throttles hard; a transient 429
    must not silently degrade a desk read to 'empty' (which would show a flat
    book when there is really an open one)."""
    last: Exception | None = None
    for i in range(tries):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — inspect the message, then re-raise
            msg = str(exc)
            if ("429" in msg or "Too Many Requests" in msg) and i < tries - 1:
                last = exc
                time.sleep(base * (i + 1))
                continue
            raise
    if last:  # pragma: no cover - loop always returns or raises above
        raise last


#: How many independent eth_calls to have in flight at once. **Default 1 —
#: serial — and that is a measured choice, not caution.**
#:
#: Against Arc's public RPC, interleaved A/B over the desk's series scan:
#:
#:     fanout=1   median 3.0s   p90 12.2s
#:     fanout=4   median 1.3-4.1s (unstable)   p90 30.8s
#:
#: The median is a coin flip at this fan-out width; the TAIL is not. Concurrency
#: raises the odds of a 429, and every 429 costs a `_rpc_retry` backoff measured
#: in seconds — which is how fanout=4 produced a 30s read. These calls sit on
#: the request path behind a 28s proxy budget, so the tail is the number that
#: decides whether a reader sees the desk or an error, and serial wins it.
#:
#: The real latency fix was doing FEWER calls (scan the venue once per sweep
#: instead of once per index), not doing them at once. Raise this only against
#: a private RPC that does not throttle, where concurrency is a clean win.
RPC_FANOUT = int(os.environ.get("ACR_RPC_FANOUT", "1"))

#: Blocks per ``eth_getLogs`` page for the trade tape.
#:
#: **Arc hard-caps the range at ~15000 blocks.** Measured against the live RPC
#: by binary search: 14843 blocks answers, 15000 returns HTTP 413 Payload Too
#: Large, and it does so regardless of how few logs actually match — this is a
#: range limit, not a response-size limit. 20000 / 40000 / 100000 are all
#: refused identically. So the tape's reach CANNOT be extended by raising a
#: window: a bigger number just fails on every call and silently degrades to
#: whatever the fallback rung is. It has to be paged. 14000 leaves headroom
#: under the cap.
TAPE_PAGE_BLOCKS = int(os.environ.get("ACR_TAPE_PAGE_BLOCKS", "14000"))

#: How many pages back the tape will walk when it hasn't filled its limit.
#:
#: Arc's measured block time is 0.510s, so one page is ~1.98h and four is
#: ~7.9h. That number is chosen from the heartbeat's REAL cadence, not its
#: nominal one: GitHub free-tier drops scheduled ticks, and the observed gaps
#: were 59m, 63m, 150m and 209m. Against a single 10000-block window (1.42h)
#: those gaps left the public tape empty 39% of the time — a "live market" that
#: is blank two hours in five. Four pages covers the worst observed gap twice
#: over.
TAPE_PAGES = int(os.environ.get("ACR_TAPE_PAGES", "4"))


def _rpc_gather(calls: list) -> list:
    """Run independent read-only RPC thunks in order, or concurrently when
    ``RPC_FANOUT`` allows it.

    Exists mainly to mark these calls as genuinely independent — the desk's
    reads are a fan-out, not a chain — so the width is one tunable constant
    rather than a rewrite. Exceptions propagate as they would serially, so a
    caller's existing try/except keeps its meaning.
    """
    if RPC_FANOUT <= 1 or len(calls) <= 1:
        return [fn() for fn in calls]
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=min(RPC_FANOUT, len(calls))) as pool:
        return list(pool.map(lambda fn: fn(), calls))


def collateral_or_none(client, series_id: int, trader: str, tries: int = 4) -> float | None:
    """A trader's collateral, or **None if the chain would not say**.

    Exists because ``collateral_of(...) or 0`` — the obvious spelling — flattens
    a *failed read* into *an empty account*, and those call for opposite
    actions: wait, versus spend. Arc's public RPC 429s routinely, so this is not
    hypothetical. Both callers have been bitten:

    * the heartbeat announced "no collateral on series 1 — posting 3.00 USDC"
      over an account holding exactly 3.00; only the write also failing kept the
      money still;
    * the roll read "maker collateral NONE" on a series with **147.9 hours** of
      life left and opened a redundant successor, committing 1.50 USDC to it.
      That write succeeded.

    Retries with backoff, then reports the uncertainty instead of guessing.
    """
    for attempt in range(1, tries + 1):
        try:
            v = client.collateral_of(series_id, trader)
        except Exception:  # noqa: BLE001 — a throttle is not an answer
            v = None
        if v is not None:
            return float(v)
        if attempt < tries:
            time.sleep(3.0 * attempt)
    return None


def _is_range_error(exc: Exception) -> bool:
    """True when the node refused the *size of the range*, as opposed to
    refusing *us*.

    The distinction is load-bearing and was worth real debugging. Arc answers a
    too-wide ``eth_getLogs`` with HTTP 413, and other nodes with JSON-RPC
    -32602 naming a result cap; the cure for both is a narrower range. It
    answers *throttling* with 429, where a narrower range is no cure at all —
    the request was never too big, there were merely too many of them. Treating
    the two alike makes a throttled tape silently shrink its own reach: every
    rung fails for a reason narrowing cannot fix, the cursor crawls, and the
    caller pays a full retry backoff per rung to go nowhere. Measured, that
    turned a four-page 7.9h walk into 1264 blocks in 16.6s.

    Arc MAINNET (measured 2026-09-28) refuses in two ways, not one, and only the
    first was recognised here:

    * ``-32602`` — "request exceeded max allowed range: query exceeds max results
      2000, retry with the range A-B". A RESULT cap, and it names the range to
      retry with.
    * ``-32012`` — "requested range too large". A BLOCK cap, and a different code:
      address-filtered, 5 000 blocks answer and 10 000 do not.

    Missing ``-32012`` made an over-wide window look transient, so the caller
    re-asked the SAME width twice before giving up, which is the throttle
    behaviour applied to a problem narrowing would have fixed.
    """
    msg = str(exc)
    if "429" in msg or "Too Many Requests" in msg:
        return False
    low = msg.lower()
    return (
        "413" in msg
        or "Payload Too Large" in msg
        or "-32602" in msg
        or "-32012" in msg
        or "exceeds max results" in msg
        or "range too large" in low
        or "limit exceeded" in low
    )


def _get_traded_logs(w3, contract, from_block: int, to_block: int):  # pragma: no cover - live chain
    """Raw topic-filtered ``eth_getLogs`` with EXPLICIT numeric bounds — the Arc
    RPC answers 413 Payload Too Large when ``toBlock`` is the string "latest"
    on a wide range (which is what web3's ``event.get_logs`` sends), but
    accepts the same range with a number. Decoded through the contract event
    so the args come back typed."""
    sig = w3.keccak(text="Traded(uint256,address,int256,uint256)").hex()
    topic0 = sig if sig.startswith("0x") else "0x" + sig
    raw = w3.eth.get_logs({
        "address": contract.address,
        "fromBlock": from_block,
        "toBlock": to_block,
        "topics": [topic0],
    })
    return [contract.events.Traded().process_log(log) for log in raw]


def bytes32_to_index_id(raw: bytes) -> str:
    """Decode a right-null-padded ``bytes32`` index id back to its string."""
    if isinstance(raw, str):  # some providers hand back hex
        raw = bytes.fromhex(raw[2:] if raw.startswith("0x") else raw)
    return raw.rstrip(b"\x00").decode("utf-8", "ignore")


def descale_series(series_id: int, t: tuple) -> dict:
    """Decode a ``getSeries`` tuple to a plain dict (human units)."""
    index_id, expiry_ts, multiplier, maker, exists, settled, settlement_price = t
    return {
        "series_id": series_id,
        "index_id": bytes32_to_index_id(index_id),
        "expiry_ts": int(expiry_ts),
        "multiplier": int(multiplier),
        "maker": maker,
        "exists": bool(exists),
        "settled": bool(settled),
        "settlement_price": settlement_price / WAD if settlement_price else 0.0,
    }


def descale_position(t: tuple, multiplier: int) -> dict:
    """Decode a ``positionOf`` tuple. ``realized_pnl_usdc`` applies the series
    multiplier to the WAD value·contracts the contract stores."""
    contracts, avg_price, realized_pnl = t
    return {
        "contracts": contracts / WAD,
        "avg_price": avg_price / WAD,
        "realized_pnl_usdc": (realized_pnl / WAD) * multiplier,
    }


def select_series_for_index(series: list[dict], index_id: str) -> dict | None:
    """Pick the series to show for an index: the latest un-settled one, else the
    latest settled one (so a just-expired series still renders until replaced)."""
    matching = [s for s in series if s["index_id"] == index_id and s["exists"]]
    if not matching:
        return None
    live = [s for s in matching if not s["settled"]]
    pool = live or matching
    return max(pool, key=lambda s: s["series_id"])


# ABI fragment: the reads the desk needs + the writes the demo loop uses.
_SERIES_TUPLE = {
    "type": "tuple",
    "name": "",
    "components": [
        {"name": "indexId", "type": "bytes32"},
        {"name": "expiryTs", "type": "uint64"},
        {"name": "multiplier", "type": "uint256"},
        {"name": "maker", "type": "address"},
        {"name": "exists", "type": "bool"},
        {"name": "settled", "type": "bool"},
        {"name": "settlementPrice", "type": "uint256"},
    ],
}
_POSITION_TUPLE = {
    "type": "tuple",
    "name": "",
    "components": [
        {"name": "contracts", "type": "int256"},
        {"name": "avgPrice", "type": "int256"},
        {"name": "realizedPnl", "type": "int256"},
    ],
}
#: Just enough ERC-20 to grant and read the venue's allowance. On Arc the token
#: is the native USDC predeploy, which is a standard ERC-20 (measured, not
#: assumed — balanceOf/decimals()==6/approve/transferFrom all work).
_ERC20_ALLOWANCE_ABI = [
    {"type": "function", "name": "approve", "stateMutability": "nonpayable",
     "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"name": "", "type": "bool"}]},
    {"type": "function", "name": "allowance", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
]

FUTURES_ABI = [
    {"type": "function", "name": "seriesCount", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"type": "function", "name": "getSeries", "stateMutability": "view",
     "inputs": [{"name": "seriesId", "type": "uint256"}], "outputs": [_SERIES_TUPLE]},
    {"type": "function", "name": "positionOf", "stateMutability": "view",
     "inputs": [{"name": "seriesId", "type": "uint256"}, {"name": "trader", "type": "address"}],
     "outputs": [_POSITION_TUPLE]},
    {"type": "function", "name": "unrealizedPnl", "stateMutability": "view",
     "inputs": [{"name": "seriesId", "type": "uint256"}, {"name": "trader", "type": "address"}],
     "outputs": [{"name": "", "type": "int256"}]},
    {"type": "function", "name": "collateral", "stateMutability": "view",
     "inputs": [{"name": "", "type": "uint256"}, {"name": "", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"type": "function", "name": "traderCount", "stateMutability": "view",
     "inputs": [{"name": "seriesId", "type": "uint256"}], "outputs": [{"name": "", "type": "uint256"}]},
    {"type": "function", "name": "traderAt", "stateMutability": "view",
     "inputs": [{"name": "seriesId", "type": "uint256"}, {"name": "i", "type": "uint256"}],
     "outputs": [{"name": "", "type": "address"}]},
    {"type": "function", "name": "openSeries", "stateMutability": "nonpayable",
     "inputs": [{"name": "indexId", "type": "bytes32"}, {"name": "expiryTs", "type": "uint64"},
                {"name": "multiplier", "type": "uint256"}, {"name": "maker", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"type": "function", "name": "postCollateral", "stateMutability": "nonpayable",
     "inputs": [{"name": "seriesId", "type": "uint256"}, {"name": "amount", "type": "uint256"}], "outputs": []},
    {"type": "function", "name": "trade", "stateMutability": "nonpayable",
     "inputs": [{"name": "seriesId", "type": "uint256"}, {"name": "qty", "type": "int256"}], "outputs": []},
    {"type": "function", "name": "withdrawCollateral", "stateMutability": "nonpayable",
     "inputs": [{"name": "seriesId", "type": "uint256"}, {"name": "amount", "type": "uint256"}], "outputs": []},
    {"type": "function", "name": "settle", "stateMutability": "nonpayable",
     "inputs": [{"name": "seriesId", "type": "uint256"}], "outputs": []},
    # The escape hatch (docs/SECURITY-AUDIT.md C3): after expiry + SETTLE_GRACE with
    # the series still open, anyone clears it at the best print that exists. Absent
    # from the venue deployed before it existed — `settle_grace()` is the probe.
    {"type": "function", "name": "settleStale", "stateMutability": "nonpayable",
     "inputs": [{"name": "seriesId", "type": "uint256"}], "outputs": []},
    {"type": "function", "name": "SETTLE_GRACE", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint64"}]},
    {"type": "event", "name": "SettledStale", "anonymous": False, "inputs": [
        {"name": "seriesId", "type": "uint256", "indexed": True},
        {"name": "settlementPrice", "type": "uint256", "indexed": False},
        {"name": "printPostedAt", "type": "uint64", "indexed": False},
        {"name": "participants", "type": "uint256", "indexed": False}]},
    {"type": "event", "name": "Traded", "anonymous": False, "inputs": [
        {"name": "seriesId", "type": "uint256", "indexed": True},
        {"name": "taker", "type": "address", "indexed": True},
        {"name": "qty", "type": "int256", "indexed": False},
        {"name": "mark", "type": "uint256", "indexed": False}]},
    # The collateral round trip and the settlement itself — evidence tooling
    # reads these, and without them every caller has to re-declare the fragment.
    {"type": "event", "name": "CollateralPosted", "anonymous": False, "inputs": [
        {"name": "seriesId", "type": "uint256", "indexed": True},
        {"name": "trader", "type": "address", "indexed": True},
        {"name": "amount", "type": "uint256", "indexed": False}]},
    {"type": "event", "name": "CollateralWithdrawn", "anonymous": False, "inputs": [
        {"name": "seriesId", "type": "uint256", "indexed": True},
        {"name": "trader", "type": "address", "indexed": True},
        {"name": "amount", "type": "uint256", "indexed": False}]},
    {"type": "event", "name": "Settled", "anonymous": False, "inputs": [
        {"name": "seriesId", "type": "uint256", "indexed": True},
        {"name": "settlementPrice", "type": "uint256", "indexed": False},
        {"name": "participants", "type": "uint256", "indexed": False}]},
]


class FuturesClient:
    def __init__(
        self,
        rpc_url: str | None = None,
        futures_address: str | None = None,
        private_key: str | None = None,
        signer: Signer | None = None,
    ) -> None:
        settings = get_settings()
        self.rpc_url = rpc_url or settings.arc_rpc_url
        self.futures_address = futures_address or (settings.futures_address or None)
        self.private_key = private_key or (settings.poster_private_key or None)
        self.signer = signer or build_signer(settings, private_key=self.private_key)
        self._w3 = None

    @property
    def configured(self) -> bool:
        return bool(self.futures_address)

    def _connect(self):
        if self._w3 is not None:
            return self._w3
        try:
            from web3 import Web3

            self._w3 = Web3(Web3.HTTPProvider(self.rpc_url, request_kwargs={"timeout": 5}))
        except Exception as exc:  # pragma: no cover - env dependent
            log.warning("FuturesClient: web3 unavailable (%s)", exc)
            self._w3 = None
        return self._w3

    def _contract(self):  # pragma: no cover - requires live chain
        w3 = self._connect()
        return w3.eth.contract(
            address=w3.to_checksum_address(self.futures_address), abi=FUTURES_ABI
        )

    def read_all_series(self, *, strict: bool = False) -> list[dict]:  # pragma: no cover - live chain
        """All series as dicts (small N — one per index in the demo).

        The per-series ``getSeries`` calls are independent, so they go out
        together: N grows by one on every roll, and a serial scan meant the
        venue got permanently slower to read the longer it stayed open.

        ``strict`` raises instead of answering ``[]`` when the read FAILED, so a
        caching caller can tell a throttled RPC from an empty venue. The default
        keeps every existing caller's meaning.
        """
        if not self.configured:
            return []
        if self._connect() is None:
            if strict:
                raise ConnectionError("futures RPC unreachable")
            return []
        try:
            c = self._contract()
            n = int(_rpc_retry(c.functions.seriesCount().call))
            raw = _rpc_gather(
                [(lambda i=i: _rpc_retry(c.functions.getSeries(i).call)) for i in range(n)]
            )
            return [descale_series(i, tuple(r)) for i, r in enumerate(raw)]
        except Exception:
            if strict:
                raise
            return []

    def read_desk(
        self, index_id: str, *, all_series: list[dict] | None = None, strict: bool = False
    ) -> dict | None:  # pragma: no cover - live chain
        """The live desk for one index: series + maker inventory + PnL, or None
        when nothing is configured / no series exists for the index yet.

        ``all_series`` lets a caller reading several indices scan the venue ONCE
        instead of once per index. That scan is the dominant cost, and two of
        the three demo indices have no series at all — so without it the reader
        paid for a full scan twice over to be told "nothing here".

        ``strict``: None then means only "no series for this index"; a failed
        read raises (see :meth:`read_all_series`).
        """
        if not self.configured:
            return None
        if self._connect() is None:
            if strict:
                raise ConnectionError("futures RPC unreachable")
            return None
        try:
            series = select_series_for_index(
                self.read_all_series(strict=strict) if all_series is None else all_series, index_id
            )
            if series is None:
                return None
            c = self._contract()
            sid, maker, mult = series["series_id"], series["maker"], series["multiplier"]
            raw_pos, raw_upnl, raw_traders = _rpc_gather(
                [
                    lambda: _rpc_retry(c.functions.positionOf(sid, maker).call),
                    lambda: _rpc_retry(c.functions.unrealizedPnl(sid, maker).call),
                    lambda: _rpc_retry(c.functions.traderCount(sid).call),
                ]
            )
            pos = descale_position(tuple(raw_pos), mult)
            upnl = int(raw_upnl) / USDC
            traders = int(raw_traders)
            return {
                **series,
                "maker_inventory": pos["contracts"],
                "maker_avg_price": pos["avg_price"],
                "maker_realized_usdc": pos["realized_pnl_usdc"],
                "maker_unrealized_usdc": upnl,
                "open_interest": abs(pos["contracts"]),
                "trader_count": traders,
            }
        except Exception:
            if strict:
                raise
            return None

    def position_of(self, series_id: int, trader: str) -> dict | None:  # pragma: no cover - live chain
        """A specific trader's position: ``{contracts, avg_price}`` (WAD-descaled,
        multiplier-independent). None if unconfigured/offline. Used by the
        maker/taker loop to read its own inventory (not the maker mirror)."""
        if self._connect() is None or not self.configured:
            return None
        try:
            c = self._contract()
            raw = tuple(_rpc_retry(c.functions.positionOf(int(series_id), trader).call))
            contracts, avg_price, _realized = raw
            return {"contracts": contracts / WAD, "avg_price": avg_price / WAD}
        except Exception:
            return None

    def collateral_of(self, series_id: int, trader: str) -> float | None:  # pragma: no cover - live chain
        """A trader's posted collateral in USDC. None if unconfigured/offline."""
        if self._connect() is None or not self.configured:
            return None
        try:
            c = self._contract()
            return int(_rpc_retry(c.functions.collateral(int(series_id), trader).call)) / USDC
        except Exception:
            return None

    def collateral_units_of(self, series_id: int, trader: str) -> int | None:  # pragma: no cover - live chain
        """The same balance in RAW USDC-6 units. A withdrawal has to name an
        exact integer to drain an account to zero — going through the float in
        :meth:`collateral_of` leaves a micro-USDC of dust behind."""
        if self._connect() is None or not self.configured:
            return None
        try:
            c = self._contract()
            return int(_rpc_retry(c.functions.collateral(int(series_id), trader).call))
        except Exception:
            return None

    def recent_trades(  # pragma: no cover - live chain
        self,
        lookback_blocks: int = TAPE_PAGE_BLOCKS,
        limit: int = 25,
        pages: int = TAPE_PAGES,
        *,
        strict: bool = False,
    ) -> list[dict]:
        """Recent on-chain fills from the ``Traded`` event, newest-first — the
        live trade tape.

        Walks BACKWARDS a page at a time rather than asking for one wide range,
        because Arc refuses a wide one outright (see ``TAPE_PAGE_BLOCKS``).
        Stops as soon as it has ``limit`` fills, so a busy book still costs a
        single request; only a quiet one pays for the full reach.

        ``strict`` raises when the FIRST page could not be read (that is no tape
        at all, not a quiet one); a later page failing still returns the fills
        already in hand.
        """
        if not self.configured:
            return []
        if self._connect() is None:
            if strict:
                raise ConnectionError("futures RPC unreachable")
            return []
        try:
            w3 = self._connect()
            c = self._contract()
            latest = int(_rpc_retry(lambda: w3.eth.block_number))
            logs: list = []
            end = latest
            for page_no in range(max(1, pages)):
                if end <= 0:
                    break
                # The shrink ladder is per PAGE and solves a different problem
                # from paging: paging extends reach, shrinking survives a node
                # stricter than the one this was measured against. The old code
                # had only the ladder, which is why a short tape never got
                # longer — it could narrow, never reach.
                #
                # The first page is the tape; the rest are depth. So the first
                # is worth waiting out a throttle for, and the others are not —
                # better a shorter tape now than a complete one in a minute.
                page = None
                start = max(0, end - lookback_blocks)
                for span in (lookback_blocks, 2500, 1000, 300):
                    start = max(0, end - span)
                    try:
                        page = _rpc_retry(
                            lambda s=start, e=end: _get_traded_logs(w3, c, s, e),
                            tries=5 if page_no == 0 else 2,
                        )
                        break
                    except Exception as exc:
                        if not _is_range_error(exc):
                            break  # throttled or down — a narrower range is no cure
                        page = None
                if page is None:
                    if strict and page_no == 0:
                        raise ConnectionError("the tape's first page could not be read")
                    break  # keep what we have rather than spend the budget going nowhere
                logs = list(page) + logs  # older page goes in front
                if len(logs) >= limit or start <= 0:
                    break
                end = start - 1
            out: list[dict] = []
            for ev in list(logs)[-limit:][::-1]:  # newest first
                a = ev["args"]
                qty = int(a["qty"]) / WAD
                out.append({
                    "series_id": int(a["seriesId"]),
                    "taker": a["taker"],
                    "qty": qty,
                    "side": "buy" if qty > 0 else "sell",
                    "mark": int(a["mark"]) / WAD,
                    "block": int(ev["blockNumber"]),
                    "tx": w3.to_hex(ev["transactionHash"]),
                })
            return out
        except Exception:
            if strict:
                raise
            return []

    # --- writes (demo maker/taker loop; operator-gated on a funded key) ---

    def can_write(self) -> bool:
        w3 = self._connect()
        if w3 is None or not self.configured or self.signer is None:
            return False
        try:
            return bool(w3.is_connected())
        except Exception:  # pragma: no cover - env dependent
            return False

    def _send(self, fn):  # pragma: no cover - requires live chain
        w3 = self._connect()
        tx = _rpc_retry(
            fn.build_transaction,
            {
                "from": self.signer.address,
                "nonce": w3.eth.get_transaction_count(self.signer.address),
                "chainId": w3.eth.chain_id,
            },
        )
        tx_hash = self.signer.send_transaction(w3, tx)
        # poll_latency=2s (not the 0.1s default) so the receipt wait doesn't
        # hammer the throttled Arc RPC into 429s.
        rcpt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=90, poll_latency=2.0)
        if rcpt.status != 1:
            raise RuntimeError(f"tx reverted ({tx_hash})")
        return str(tx_hash)

    def allowance_units(self, token: str, owner: str | None = None) -> int | None:  # pragma: no cover - live chain
        """The venue's USDC allowance from ``owner``, in raw 1e6 units."""
        w3 = self._connect()
        if w3 is None or not self.configured:
            return None
        who = owner or (self.signer.address if self.signer else None)
        if not who:
            return None
        try:
            erc20 = w3.eth.contract(address=w3.to_checksum_address(token), abi=_ERC20_ALLOWANCE_ABI)
            return int(
                _rpc_retry(
                    erc20.functions.allowance(
                        w3.to_checksum_address(who), w3.to_checksum_address(self.futures_address)
                    ).call
                )
            )
        except Exception:
            return None

    def approve_venue(self, token: str, amount_units: int | None = None) -> str | None:  # pragma: no cover - live chain
        """Approve the venue to pull ``token`` from this signer's wallet.

        Exists because the approve step used to be the ONE write that bypassed
        the ``Signer`` seam: every script hand-built it and signed with
        ``eth_account``, which silently made a raw key mandatory even on a host
        with full Circle credentials. Posting collateral is impossible without
        an allowance, so that single omission pinned the whole venue to an EOA.

        Goes through ``_send`` like every other write, so it signs with whatever
        the client was given — a local key on anvil, a Circle custody wallet in
        production.
        """
        if not self.can_write():
            return None
        w3 = self._connect()
        erc20 = w3.eth.contract(address=w3.to_checksum_address(token), abi=_ERC20_ALLOWANCE_ABI)
        amount = (2**256 - 1) if amount_units is None else int(amount_units)
        return self._send(
            erc20.functions.approve(w3.to_checksum_address(self.futures_address), amount)
        )

    def open_series(self, index_id: str, expiry_ts: int, multiplier: int, maker: str) -> str | None:  # pragma: no cover - live chain
        if not self.can_write():
            return None
        c = self._contract()
        return self._send(
            c.functions.openSeries(index_id_to_bytes32(index_id), int(expiry_ts), int(multiplier), maker)
        )

    def post_collateral(self, series_id: int, amount_usdc: float) -> str | None:  # pragma: no cover - live chain
        if not self.can_write():
            return None
        c = self._contract()
        return self._send(c.functions.postCollateral(int(series_id), int(round(amount_usdc * USDC))))

    def withdraw_collateral(self, series_id: int, amount_units: int) -> str | None:  # pragma: no cover - live chain
        """Take collateral back out, in RAW USDC-6 units (see
        :meth:`collateral_units_of`). The contract lets an unsettled account
        withdraw only down to its initial margin; a settled one is flat, so the
        whole cleared balance is free."""
        if not self.can_write():
            return None
        c = self._contract()
        return self._send(c.functions.withdrawCollateral(int(series_id), int(amount_units)))

    def trade(self, series_id: int, qty_contracts: float) -> str | None:  # pragma: no cover - live chain
        if not self.can_write():
            return None
        c = self._contract()
        return self._send(c.functions.trade(int(series_id), int(round(qty_contracts * WAD))))

    def settle(self, series_id: int) -> str | None:  # pragma: no cover - live chain
        if not self.can_write():
            return None
        return self._send(self._contract().functions.settle(int(series_id)))

    def settle_grace(self) -> int | None:  # pragma: no cover - live chain
        """`SETTLE_GRACE` in seconds, or None on a venue deployed before the escape
        hatch existed — the one probe that tells the two generations apart."""
        if self._connect() is None:
            return None
        try:
            return int(self._contract().functions.SETTLE_GRACE().call())
        except Exception:
            return None

    def settle_stale(self, series_id: int) -> str | None:  # pragma: no cover - live chain
        """The escape hatch: valid only once expiry + SETTLE_GRACE has passed."""
        if not self.can_write():
            return None
        return self._send(self._contract().functions.settleStale(int(series_id)))
