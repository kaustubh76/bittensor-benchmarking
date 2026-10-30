"""In-memory print store — runs the estimator and derives the terminal's views.

Holds the latest ACR prints and their recent history, and computes the derived
surfaces the ACR Terminal shows: the term-structure curve (from the instrument
layer), realized vol, and seller reliability scores (from the cleaning stack's
verdicts). Backed by any ``TapeSource``. The published deployment runs ``SimSource``:
real Arc USDC transfers carry no compute-price signal, so the honest options
are a labelled simulator or no index at all (``scripts/tape_audit.py`` measures
this against live Arc rather than asserting it). ``ArcSource`` and
``ReceiptSource`` are built and selectable via ``ACR_TAPE_SOURCE``.

Prints *evolve*: each ``refresh`` advances a window cursor over the fixed tape
(wrapping through the day) while the print timestamp ``k·step_s`` increases
forever, so realized vol becomes real and the on-chain feed stays timestamp-
monotone across wraps. Writes swap in fresh ``latest``/``diag`` dicts under a lock
(copy-on-write), so concurrent reads from the threadpool always see a coherent
snapshot without holding the lock.
"""

from __future__ import annotations

import logging
import math
import threading
from collections import defaultdict, deque

import numpy as np
from acr_core import ALL_INDEX_IDS, ACRPrint, get_settings, spec_for
from acr_estimator import estimate_index
from acr_estimator.cleaning import policy_hash
from acr_estimator.pipeline import PrintDiagnostics
from acr_instrument import AvellanedaStoikovMM, Position
from acr_sim import SimConfig
from acr_tape import SimSource, TapeSource

WEEK = 7 * 24 * 3600.0
log = logging.getLogger("index_api.store")

#: Consecutive failed refreshes on a SERVED index before the log goes from
#: warning to error. One empty window is a thin window and recovers on its own;
#: three in a row means the previous print has been served unchanged for three
#: cycles, which is indistinguishable from a healthy feed to anyone reading the
#: API. Small on purpose -- the point is to be loud early, not to be certain.
STALL_MISSES = 3


def default_source() -> TapeSource:
    """Pick the tape source from config: ``ACR_TAPE_SOURCE=arc`` scans live Arc
    testnet USDC flow; ``=receipts`` reads the authoritative x402 settlement
    ledger (real paid queries — an audit tape; flat single-seller query flow is
    correctly cleaned out by the estimator, so it doesn't drive the published
    indices); ``=graph`` reads the indexed settlement tape, the only source that
    carries a per-seller unit price and so the only one transaction-cost
    analysis can be computed from; ``=chutes`` reads realized inference prices
    from Bittensor subnet 64; anything else uses the calibrated simulator
    (a richer default horizon so hourly windows aren't thin) — the honest
    default.

    **Three things a ``chutes`` caller has to honour.** This function returns a
    source, not a windowing policy, so the source ADVERTISES all three and
    `PrintStore.__init__` reads them off it. They were documented here as "the
    caller's job" and the only caller in the product did not do the job, so they
    are now defaults rather than instructions:

    1. ``ChutesSource.requires_single_venue`` is True. Every Chutes sale is one
       aggregate buyer paying many sellers, so the funding graph is a star and
       `acr_estimator.cleaning.clean` either zeroes the window or caps every
       weight to ``cluster_volume_cap``. Windows from this source must reach
       ``estimate_index(..., single_venue=True)``, which publishes a price and
       refuses an attack cost.
    2. ``native_step_s`` is 86,400. The tape is DAILY, and windowing it hourly
       yields one populated window per 24 and 23 empty ones. Empty windows raise
       inside `refresh`'s per-index `except`, get logged, and leave the previous
       print in place, so the visible result is a feed that looks alive and never
       moves. `PrintStore` now defaults ``window_s``/``step_s`` to this.
    3. ``covers_services`` is ``(INFERENCE,)``. ACR-GPU and ACR-DATA have no
       Chutes tape and never will, so `refresh` skips them instead of logging a
       failure that is expected and permanent — which is what made the one real
       failure mode above hard to see.

    See `docs/BITTENSOR.md`."""
    mode = get_settings().tape_source.strip().lower()
    if mode == "arc":
        from acr_tape import ArcSource

        return ArcSource()
    if mode == "chutes":
        from acr_tape import ChutesSource

        return ChutesSource()
    if mode == "receipts":
        from acr_tape import ReceiptSource

        return ReceiptSource()
    if mode == "graph":
        from acr_tape import GraphSource

        return GraphSource()
    # Sim size + horizon are configurable so memory-constrained cloud instances
    # (e.g. a 512MB free tier) can shrink the store build while keeping window
    # density high enough to estimate (ACR_SIM_EVENTS_PER_SERVICE + ACR_SIM_HORIZON_SECONDS).
    s = get_settings()
    return SimSource(
        config=SimConfig(events_per_service=s.sim_events_per_service, horizon=s.sim_horizon_seconds)
    )


class PrintStore:
    def __init__(
        self,
        source: TapeSource | None = None,
        history: int = 168,
        window_s: float | None = None,
        step_s: float | None = None,
    ) -> None:
        # Sim by default; ACR_TAPE_SOURCE=arc selects the live Arc testnet tape.
        self.source = source or default_source()
        self.latest: dict[str, ACRPrint] = {}
        self.diag: dict[str, PrintDiagnostics] = {}
        self.history: dict[str, deque[ACRPrint]] = defaultdict(lambda: deque(maxlen=history))
        self.settings = get_settings()
        # Both windowing knobs and the single-venue flag come OFF THE SOURCE
        # unless a caller overrides them. `default_source` above called this the
        # caller's job, and the only caller in the product -- `app.py`'s bare
        # `PrintStore()` -- did not do it: a daily tape windowed hourly leaves 23
        # empty windows in 24, which `refresh` logs and swallows, so the feed
        # looks alive and never moves. Same `getattr` contract as
        # `acr_subnet.harness`, so a source declaring neither behaves as before.
        native = float(getattr(self.source, "native_step_s", 3600.0))
        self.window_s = native if window_s is None else float(window_s)
        self.step_s = native if step_s is None else float(step_s)
        self.single_venue = bool(getattr(self.source, "requires_single_venue", False))
        # Which indices this source could ever print. A source that declares no
        # `covers_services` is assumed to serve all three, which is every source
        # but `ChutesSource` and preserves their behaviour exactly.
        covers = getattr(self.source, "covers_services", None)
        self.served = (
            frozenset(ALL_INDEX_IDS)
            if covers is None
            else frozenset(i for i in ALL_INDEX_IDS if spec_for(i).service in set(covers))
        )
        #: Consecutive `refresh` calls in which a SERVED index failed to print.
        #: An unserved index is never counted, so a stall is visible instead of
        #: being buried under warnings that are expected and permanent.
        self._misses: dict[str, int] = dict.fromkeys(ALL_INDEX_IDS, 0)
        self._lock = threading.Lock()
        self._cursor = 0
        self._events: list | None = None
        self._ts = np.empty(0)
        self._attest: list | None = None
        self._horizon = 0.0
        #: Per-index maker inventory (signed contracts) driving the A-S quote
        #: skew. Fed from the live on-chain ``ACRFutures`` maker position when a
        #: futures address is configured; empty (→ 0) otherwise, so the term
        #: structure is unchanged from the flat-inventory default.
        self.maker_inventory: dict[str, float] = {}

    def set_maker_inventory(self, inventory: dict[str, float]) -> None:
        """Update the maker inventory the term structure skews around (called by
        the API's background loop from the on-chain futures desk)."""
        with self._lock:
            self.maker_inventory = dict(inventory)

    def _merge_onchain_attestations(self, attests: list) -> list:
        """Union the tape's attestations with the live ``AttestationRegistry``
        (deduped by seller, on-chain wins) when ``ACR_REGISTRY_ADDRESS`` is set,
        so the hedonic stage constant-quality-adjusts using *real* on-chain seller
        metadata regardless of tape source. No registry / offline → unchanged."""
        if not self.settings.registry_address:
            return attests
        try:
            from acr_oracle_client import RegistryClient

            onchain = RegistryClient().all_attestations()
        except Exception as exc:  # pragma: no cover - env dependent
            log.warning("on-chain attestation merge skipped (%s)", exc)
            return attests
        if not onchain:
            return attests
        by_seller = {a.seller: a for a in attests}
        for a in onchain:  # on-chain records override tape ones for the same seller
            by_seller[a.seller] = a
        log.info("merged %d on-chain attestation(s) into the estimator set", len(onchain))
        return list(by_seller.values())

    def _load(self) -> None:
        """Cache the sorted tape + attestations + horizon (once)."""
        if self._events is not None:
            return
        evs = sorted(self.source.collect(), key=lambda e: e.ts)
        ts = np.array([e.ts for e in evs]) if evs else np.empty(0)
        horizon = 0.0
        result = getattr(self.source, "result", None)
        cfg = getattr(result, "config", None)
        if cfg is not None:
            horizon = float(getattr(cfg, "horizon", 0.0))
        if horizon <= 0.0 and ts.size:
            horizon = float(ts.max())
        attests = self._merge_onchain_attestations(list(self.source.attestations()))
        with self._lock:
            self._events, self._ts, self._attest, self._horizon = evs, ts, attests, horizon

    def refresh(self, ts: float | None = None) -> None:
        """Estimate every index over the next window; advance the cursor.

        ``ts`` pins the window to ``floor(ts/step_s)`` (used by the demo/snapshot);
        otherwise the cursor advances by one. The print timestamp ``k·step_s``
        increases monotonically forever, even as the window wraps the day.
        """
        self._load()
        with self._lock:
            if ts is not None:
                self._cursor = max(1, int(ts // self.step_s))
            else:
                self._cursor += 1
            k = self._cursor

        n_windows = max(1, int(self._horizon // self.step_s)) if self._horizon else 1
        lo = ((k - 1) % n_windows) * self.step_s
        hi = lo + self.window_s
        print_ts = k * self.step_s
        if self._ts.size:
            i0 = int(np.searchsorted(self._ts, lo, "left"))
            i1 = int(np.searchsorted(self._ts, hi, "left"))
            window = self._events[i0:i1]
        else:
            window = []

        new_latest: dict[str, ACRPrint] = {}
        new_diag: dict[str, PrintDiagnostics] = {}
        for iid in ALL_INDEX_IDS:
            if iid not in self.served:
                # Not a failure: this source carries no tape for this service and
                # never will. Logged once per refresh at debug so it stays
                # greppable without drowning a real stall in noise.
                log.debug(
                    "%s: %s carries no %s tape",
                    iid,
                    type(self.source).__name__,
                    spec_for(iid).service.value,
                )
                continue
            try:
                p, d = estimate_index(
                    iid,
                    window,
                    self._attest,
                    ts=print_ts,
                    settings=self.settings,
                    single_venue=self.single_venue,
                )
            except Exception as exc:  # isolate one index's failure from the rest
                self._misses[iid] += 1
                n = self._misses[iid]
                # A served index that cannot print leaves the PREVIOUS print in
                # place, so the feed looks alive and never moves -- the worst
                # failure mode a reference rate has. One miss is a thin window;
                # `STALL_MISSES` in a row is a broken feed, and says so at ERROR.
                if n >= STALL_MISSES:
                    log.error(
                        "estimate failed for %s on %d consecutive refreshes (%s) - "
                        "the published print is stale and not moving",
                        iid,
                        n,
                        exc,
                    )
                else:
                    log.warning("estimate failed for %s (%d/%d): %s", iid, n, STALL_MISSES, exc)
                continue
            self._misses[iid] = 0
            # The four v2 fields, attached HERE because the store is the only
            # component that knows both the settings the estimator ran under and
            # the window it ran over — and doing it here leaves estimate_index's
            # signature (and the tests pinning it) untouched.
            #
            # The window is the NOMINAL one: `window_s` ending at the print's
            # economic timestamp. The store's actual slice bounds are
            # tape-relative and wrap the day, while the print timestamp is
            # epoch-aligned, so publishing the raw slice as epoch bounds would
            # be meaningless. This is a true statement about what the print
            # claims to describe, not a claim about which events were used.
            new_latest[iid] = p.model_copy(
                update={
                    "policy_hash": policy_hash(self.settings),
                    "window_start": print_ts - self.window_s,
                    "window_end": print_ts,
                }
            )
            new_diag[iid] = d

        with self._lock:  # copy-on-write swap; readers see old-or-new, never partial
            self.latest = {**self.latest, **new_latest}
            self.diag = {**self.diag, **new_diag}
            for iid, p in new_latest.items():
                self.history[iid].append(p)

    def ensure(self) -> None:
        if not self.latest:
            self.refresh()

    def seed_cursor(self, min_ts: float) -> None:
        """Advance the print-ts cursor so the next ``refresh`` produces a ts that
        strictly exceeds ``min_ts`` (the on-chain latest). ``ACROracle`` enforces
        strictly-monotone print timestamps, so without this the poster reverts
        with 'non-monotone ts' after a restart (which resets the cursor to 0)
        until it slowly catches back up — wasting gas each cycle."""
        with self._lock:
            self._cursor = max(self._cursor, int(min_ts // self.step_s))

    # --- derived views ---
    def curve(self, index_id: str, tenors_weeks: tuple[int, ...] = (1, 2, 4, 8)) -> list[dict]:
        """Term structure: A-S mid quotes at successive weekly tenors.

        Two things give the curve its shape, and for a long time only one of
        them worked.

        The lean is the maker's *live inventory* (from the on-chain futures desk
        when configured): a long book skews the reservation price down and a
        short book up, exactly as ``AvellanedaStoikovMM`` prescribes.

        The slope is the horizon, and it used to be absent. This loop built a
        different ``expiry`` per tenor and then called ``quote(now=p.ts,
        start=p.ts)``, which makes ``_time_left`` compute ``(T-t)/(T-start)`` =
        1.0 for EVERY tenor — the week count cancelled itself, and all four rows
        came out bit-identical while carrying four different ``expiry_ts``
        labels. Passing ``tau`` explicitly is the fix; in the model's units that
        makes ``sigma`` a per-week volatility, and the 1-week point is unchanged
        (tau=1 either way), so the ~50 bp base calibration still holds at the
        front and the corridor widens from there."""
        self.ensure()
        p = self.latest.get(index_id)
        if p is None:
            return []
        inventory = self.maker_inventory.get(index_id, 0.0)
        pts = []
        for w in tenors_weeks:
            expiry = p.ts + w * WEEK
            mm = AvellanedaStoikovMM(index_id, expiry_ts=expiry)
            # avg_price is irrelevant to the quote (which skews on `contracts`);
            # seed it at the mark so the position is internally consistent.
            pos = Position(contracts=inventory, avg_price=p.value)
            # tau = the tenor itself, in weeks. NOT now/start: those are equal
            # here, and the ratio would be 1.0 for every w (see the docstring).
            q = mm.quote(p.value, pos, now=p.ts, start=p.ts, tau=float(w))
            pts.append(
                {
                    "tenor_weeks": w,
                    "expiry_ts": expiry,
                    "mid": q.mid,
                    "bid": q.bid,
                    "ask": q.ask,
                    "spread_bp": q.spread_bp,
                }
            )
        return pts

    def vol(self, index_id: str) -> float:
        """Annualized realized vol from the print history (log returns)."""
        h = list(self.history.get(index_id, []))
        if len(h) < 3:
            return 0.0
        rets = [
            math.log(h[i].value / h[i - 1].value) for i in range(1, len(h)) if h[i - 1].value > 0
        ]
        if not rets:
            return 0.0
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / len(rets)
        hourly = math.sqrt(var)
        return hourly * math.sqrt(24 * 365)  # annualize hourly prints

    def seller_scores(self, index_id: str, top: int = 20) -> list[dict]:
        """Reliability score per seller: attested + share of clean volume."""
        self.ensure()
        d = self.diag.get(index_id)
        if d is None:
            return []
        flagged = d.cleaning.all_flagged
        vol_clean: dict[str, float] = defaultdict(float)
        vol_total: dict[str, float] = defaultdict(float)
        # Reflect the REAL on-chain registry: self._attest is the source tape's
        # attestations unioned with the live AttestationRegistry (on-chain wins,
        # via _merge_onchain_attestations). Falls back to the source set when no
        # registry is configured (hermetic tests / offline) — behavior preserved.
        attested = {a.seller for a in (self._attest or self.source.attestations())}
        for e, w in zip(d.cleaning.events, d.cleaning.weights, strict=True):
            vol_total[e.seller] += e.notional
            if e.event_id not in flagged and w > 0:
                vol_clean[e.seller] += e.notional
        scores = []
        for s, tot in vol_total.items():
            clean_share = vol_clean.get(s, 0.0) / tot if tot > 0 else 0.0
            score = 0.5 * clean_share + 0.5 * (1.0 if s in attested else 0.0)
            scores.append(
                {
                    "seller": s,
                    "score": round(score, 3),
                    "clean_share": round(clean_share, 3),
                    "attested": s in attested,
                    "volume_usdc": round(tot, 2),
                }
            )
        scores.sort(key=lambda x: x["score"], reverse=True)
        return scores[:top]

    def robustness(self, index_id: str) -> dict | None:
        """Per-print robustness diagnostics (methodology §4), for the API/Terminal."""
        d = self.diag.get(index_id)
        if d is None or d.robustness is None:
            return None
        r = d.robustness
        return {
            "single_cluster_flip_fraction": r.single_cluster_flip_fraction,
            "max_cluster_share": r.max_cluster_share,
            "max_cluster_influence_bp": r.max_cluster_influence_bp,
            "sybil_clusters_required": r.sybil_clusters_required,
            "min_identities": r.min_identities,
        }

    def snapshot(self) -> dict:
        self.ensure()
        latest = self.latest  # single reference grab (copy-on-write safe)
        diag = self.diag
        return {
            "prints": {
                iid: {
                    **latest[iid].model_dump(),
                    "unit": spec_for(iid).unit,
                    "naive_vwap": diag[iid].naive_vwap,
                    "cleaned_pct": 100 * diag[iid].cleaning.removed_fraction,
                    "cost_to_move_1pct": diag[iid].bound.cost_to_move_1pct,
                    "vol": self.vol(iid),
                    "robustness": self.robustness(iid),
                }
                for iid in latest
            }
        }
