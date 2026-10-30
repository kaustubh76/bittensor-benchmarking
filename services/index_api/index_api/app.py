"""x402-gated ACR index API (FastAPI).

Public: /health, / (service card), /onchain/{index_id} (settlement-grade print
read straight from ACROracle when an oracle is configured), /revenue
(Nanopayments dogfood metric), /x402/info, /marketplace/catalog (Bazaar-shaped
agent listings), /marketplace/receipts (settlement ledger), /terminal/data.
x402-gated (HTTP 402 unless a valid PAYMENT-SIGNATURE / X-Payment header):
    GET /prints                 all latest prints + CI + attack cost
    GET /prints/{index_id}      one index
    GET /curve/{index_id}       term structure (A-S mids by tenor)
    GET /vol/{index_id}         realized vol
    GET /seller-scores/{id}     seller reliability

Run: uv run uvicorn index_api.app:app --reload
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path

from acr_core import (
    ALL_INDEX_IDS,
    assert_mainnet_ready,
    get_settings,
    spec_for,
    testnet_surfaces_enabled,
)
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from . import armor, graph_proxy, memory, ratelimit
from .agentgate import (
    CARD_HEADER,
    TIER_ANON,
    VerifiedAgent,
    get_gate,
    optional_agent,
    require_business_read,
    scope_enforced,
)
from .armor import SCREEN_CAP, SCREEN_CAP_REPLY, get_screen, screen_is_live
from .fleet import fleet_summary, listing_for
from .humanid import (
    AgentKitVerifier,
    HumanProof,
    HumanProofRequired,
    HumanVerifier,
    get_verifier,
    require_human,
    stray_world_credentials,
)
from .onchain import OracleUnreachable, get_futures, get_reader
from .payments import require_business_credential
from .payments import router as payments_router
from .poster import OraclePoster
from .statement import build_statement
from .store import PrintStore
from .tca import RATING_WINDOW_DAYS, human_tca, payer_tca, seller_rating
from .x402 import (
    PAY_TO,
    CircleFacilitator,
    Facilitator,
    PaymentReceipt,
    PaymentRequired,
    get_facilitator,
    price_usdc,
    require_payment,
)

log = logging.getLogger("index_api")

#: Wall-clock start of this process, served on /health (see `rss_mib` there).
_PROCESS_STARTED_AT = time.time()
# Surface app INFO logs (webhooks, x402 settles, poster) under `make api`. Without
# a handler, Python's last-resort logger only prints WARNING+, so successful
# webhook events would be invisible. One handler, added once, owns the output.
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s · %(message)s"))
    log.addHandler(_h)
    log.setLevel(logging.INFO)

store = PrintStore()

# Lazy module singleton (mirrors onchain.get_reader) so payload building can
# reach the poster the background loop drives — and so tests can inject one.
_poster: OraclePoster | None = None


def get_poster() -> OraclePoster:
    global _poster
    if _poster is None:
        _poster = OraclePoster(store)
    return _poster


def set_poster(poster: OraclePoster) -> None:
    """Install a poster (the lifespan's background loop; tests inject a fake)."""
    global _poster
    _poster = poster


def reset_poster() -> None:
    """Drop the cached poster (tests / after changing oracle env)."""
    global _poster
    _poster = None


# /terminal/data payload cache. The background loop rebuilds this off the event
# loop after each warm/refresh; the request handler serves this one shared dict
# instead of running the heavy build (sim + attack_snapshot + 3 sequential RPC
# reads) per request. On the 512MB free tier, N concurrent cold requests each
# building would blow past memory and OOM-kill → restart → death-spiral; serving
# a cached dict (with a single-flight lock for the pre-warm window) keeps the
# single free instance alive. See _rebuild_terminal_cache / terminal_data.
_terminal_payload_cache: dict | None = None
_terminal_payload_lock = threading.Lock()


def _stale_indices(
    onchain: dict[str, dict], refresh_seconds: float, now: float
) -> list[str]:
    """The indices whose on-chain print is missing or older than one refresh cycle.

    Per-index, because ``OraclePoster.post_latest`` isolates each index's failure
    so one revert cannot abort the rest — which means a single index can fail on
    its own and be the only thing stale.
    """
    return [
        iid
        for iid, v in onchain.items()
        if (now - (v.get("posted_at", 0) or 0)) > refresh_seconds
    ]


def _overdue_for_startup_post(onchain: dict[str, dict], refresh_seconds: float, now: float) -> bool:
    """True when ANY on-chain print is missing or older than one refresh cycle.

    The STALEST index governs, not the freshest. It used to be the freshest, on
    the reasoning that "the timer cycle posts all indices, so a lagging one is at
    most a refresh away" — and that reasoning is wrong by a margin the contract
    cares about. Post at T, fail for one index at T+60, succeed at T+120: the gap
    is already the whole 120-minute ``MAX_SETTLE_AGE`` before any jitter, and the
    press does not run to the second.

    Measured, not theorised: on 2026-08-03 the 13:31 run posted ACR-GPU and
    ACR-INF but not ACR-DATA, and ACR-DATA went **121.0 minutes** between prints
    — past the window, so its series could not have been settled. Two healthy
    indices hid the third, which is this codebase's recurring shape: the absent
    thing looks exactly like the fine thing. `make print-gaps` reports per-index
    gaps for precisely this reason.
    """
    if not onchain:
        return True
    return bool(_stale_indices(onchain, refresh_seconds, now))


#: How often to re-read the chain into the request-path caches. This is a
#: SEPARATE cadence from ``refresh_seconds`` on purpose: that timer also posts
#: an oracle print, which spends real gas, so production runs it hourly — while
#: the read caches (``READ_ALL_TTL_S`` / ``FUTURES_TTL_S``) live 90 seconds.
#: Tying the warm to the print meant the caches were cold for ~58 minutes of
#: every hour and the next visitor paid a full sequential chain sweep on the
#: request path: a measured 55s for /desk/limits, which is past the terminal
#: proxy's budget, so the desk told a reader it was down while it was up.
#: Reading costs nothing but RPC, so warm it on its own short timer.
CHAIN_WARM_SECONDS = float(os.environ.get("ACR_CHAIN_WARM_SECONDS", "60"))


async def _rehydrate_provenance(poster) -> None:
    """Seed the poster's per-index provenance from the on-chain PricePosted log.

    A no-op once populated (``OraclePoster.rehydrate`` refuses to overwrite live
    state), which is what makes it safe to call on a timer — and it must be on a
    timer. This used to run exactly once, at startup: the least reliable moment
    in the process's life, when the RPC is busiest and a cold box is racing
    everything else it has to warm. One throttled call there and /health reports
    `poster_last_tx: null` — the product telling visitors "awaiting first live
    post" while three real posts sit on chain — until the next hourly post
    repopulates it by accident. That state was live in production.
    """
    if poster.last_posts:
        return
    try:
        n = poster.rehydrate(await asyncio.to_thread(poster.client.recent_posts))
        if n:
            log.info("re-hydrated poster provenance from %d on-chain posts", n)
    except Exception:  # pragma: no cover - defensive; the next tick retries
        log.exception("poster provenance re-hydrate failed")


#: The service's own public URL. When set, the warm loop calls it on every tick
#: so the instance never looks idle.
#:
#: THE PRESS ONLY POSTS WHILE THE PROCESS IS AWAKE. The free instance sleeps
#: after ~15 idle minutes, and the only thing waking it was a GitHub cron that
#: drops ~88% of its ticks (17 of 144 slots in a measured day). Real gaps
#: between on-chain prints came out 80 · 64 · 122 · 62 · 100 · **216** · 82 · 82
#: minutes against a contract that refuses to settle on a print older than 120 —
#: so for a couple of hours the venue could not be settled at all. The posting
#: logic was never at fault: `_background` already posts on boot when the record
#: is overdue. Nothing was waking it.
#:
#: Whether a self-request resets the platform's idle timer is an EMPIRICAL
#: question, not a guarantee — the request leaves the instance, crosses the
#: router and comes back as ordinary inbound traffic, which is the same thing
#: the external pinger provides. The proof is the gap distribution measured
#: afterwards, not this comment.
SELF_URL = os.environ.get("ACR_SELF_URL", "").rstrip("/")

#: Never retry an overdue post more often than this. Without a floor, a press
#: that is failing (a 429, a dry wallet) would be retried every warm tick.
OVERDUE_POST_COOLDOWN_S = float(os.environ.get("ACR_OVERDUE_POST_COOLDOWN_S", "600"))
_last_overdue_attempt = 0.0


async def _touch_self() -> None:
    """Knock on our own front door so the instance does not go idle.

    Fire-and-forget: this must never slow a tick or raise. If the ping fails the
    worst case is the status quo — the box sleeps and the external cron is back
    to being the only alarm clock.
    """
    if not SELF_URL:
        return
    try:
        import httpx

        # Tagged so it is identifiable in the access log. That matters: Render's
        # OWN health check hits /health every ~5s from 10.228.x.x and the
        # service still sleeps, which proves the platform does not count all
        # inbound traffic toward the idle timer. Whether a knock that egresses
        # to the public hostname and returns through the edge is counted is the
        # open question — an untagged ping would be indistinguishable from the
        # platform's own probe, and I would have no way to tell.
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.get(f"{SELF_URL}/health?src=self-heartbeat")
    except Exception:  # pragma: no cover - a failed knock is not an error
        log.debug("self-ping failed", exc_info=True)


def _record_print_skip(poster) -> bool:
    """Put a gas refusal on the keeper's chore log. True when one happened.

    `OracleClient.post` declines quietly when the press is below its print floor
    — what is left is kept for receipt mirroring, which cannot be redone. Quietly
    is not good enough: an unexplained gap in the fixing is exactly the shape of
    the 48.9-hour outage this project has already paid for once, so the reason
    goes where /ops and the keeper's chore log will show it.
    """
    from . import keeper

    reason = getattr(getattr(poster, "client", None), "last_skip_reason", None)
    if not reason:
        return False
    keeper.record("print", reason)
    log.warning("press declined to print: %s", reason)
    return True


async def _post_if_overdue(store, poster, reader, settings) -> None:
    """Post when the ON-CHAIN record is stale, wherever the refresh timer sits.

    The hourly timer is the normal path; this is the recovery one. A single
    failed post used to mean waiting a full hour for the next attempt — and an
    hour is most of the contract's 120-minute settle window. Checking against
    the chain rather than against our own timer also means a process that slept
    through its slot fixes itself on the next tick instead of on the next boot.
    """
    global _last_overdue_attempt
    if not poster.client.can_post():
        return
    now = time.time()
    if now - _last_overdue_attempt < OVERDUE_POST_COOLDOWN_S:
        return
    onchain = await asyncio.to_thread(reader.read_all)  # cached; cheap per tick
    stale = _stale_indices(onchain, settings.refresh_seconds, now) if onchain else None
    if onchain and not stale:
        return
    _last_overdue_attempt = now
    # Re-post ONLY what is stale. Posting all three costs gas for two indices
    # that are already fine, and this path can fire every cooldown while one
    # index keeps failing — `stale=None` (no on-chain record at all) still means
    # post everything, because then nothing is known to be fresh.
    log.info("on-chain print is overdue for %s — posting off-cycle", stale or "every index")
    await asyncio.to_thread(store.refresh)
    await asyncio.to_thread(poster.post_latest, set(stale) if stale else None)
    _record_print_skip(poster)
    await asyncio.to_thread(reader.read_all, use_cache=False)


async def _warm_chain(stop: asyncio.Event) -> None:
    """Keep the on-chain read caches warm so no reader ever pays for a cold one,
    keep the instance awake, and republish if the press has fallen behind.

    Deliberately does NOT use ``use_cache=False`` blindly on a dead chain: every
    call is already exception-tolerant and returns the last good value, so a
    throttled Arc simply leaves the previous warm entry in place.
    """
    settings = get_settings()
    reader, futures = get_reader(), get_futures()
    # The self-ping has to run even with no chain configured — keeping the box
    # awake is not an on-chain concern.
    # The mirror keeps the tape fed and depends on neither the oracle reader nor
    # the venue, so it must not be gated on them. Moving `_run_mirror` out of the
    # `futures.configured` branch below was not enough: this outer guard would
    # still have returned first on a mirror-only deployment.
    # THIS LINE KILLED THE WHOLE LOOP FOR FOUR WEEKS.
    #
    # It read `bool(_gs().receipt_mirror_address)`, where `_gs` was an alias for
    # `testnet_surfaces_enabled` — a function that takes a required `settings`
    # argument and returns a BOOL. So the call raised TypeError on the way in,
    # before `while not stop.is_set()`, and since `_warm_chain` is an
    # `asyncio.create_task` whose exception nobody awaits until shutdown, the
    # task died at every boot in silence.
    #
    # The alias reads like it was meant to be `get_settings`, and `settings` is
    # already in scope two lines above — so the import was never needed at all.
    #
    # What was off for four weeks: the self-ping that keeps a free instance
    # awake, the oracle cache warming, the settlement mirror, the venue keeper's
    # heartbeat and roll checks, and the memory guard that stops a 512 MiB tier
    # from OOM-killing the process. The visible symptom was `checked_at: null`
    # for all three keeper chores on a sixty-second loop — which is what finding
    # this started from. Landed 2026-09-05 in b8649bb.
    mirror_configured = bool(settings.receipt_mirror_address)
    if not (reader.configured or futures.configured or mirror_configured or SELF_URL):
        return
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=CHAIN_WARM_SECONDS)
        except TimeoutError:
            pass
        if stop.is_set():
            break
        try:
            # First, because it is the one that keeps everything else running.
            await _touch_self()
            # EACH CHORE IN ITS OWN try/except, not just the keeper's.
            #
            # These two reads used to sit bare inside the outer handler, ahead of
            # everything after them — so ONE throttled Arc read skipped the
            # mirror, the memory guard and the keeper for that tick, every tick,
            # and the only trace was a line in the server log. `_run_keeper`
            # already carried this reasoning ("the caller's handler would also
            # catch this, but then a keeper failure would skip the rest of that
            # tick") and the reads above it never got it.
            #
            # Not hypothetical: the deployed press reports `checked_at: null` for
            # all three keeper chores after ten minutes of uptime on a
            # sixty-second loop, with `keeper.enabled: true`. The recording code
            # dates from 2026-09-05, so the image is not too old to have it —
            # which leaves a read above it throwing, or the outer guard. Arc
            # answers 429, and this is the shape that turns a throttle into a
            # silently dead chore.
            if reader.configured:
                try:
                    await asyncio.to_thread(reader.read_all, use_cache=False)
                    # Cheap while it matters, free once it doesn't: this returns
                    # immediately as soon as provenance is populated. Guard on
                    # the global rather than get_poster(), which would lazily
                    # build a THROWAWAY poster if this tick beat _background's
                    # set_poster() — we'd hydrate an instance nothing else can
                    # see.
                    if _poster is not None:
                        await _rehydrate_provenance(_poster)
                        await _post_if_overdue(store, _poster, reader, settings)
                except Exception:  # pragma: no cover - a chore must never cost a beat
                    log.warning("warm: the oracle read failed", exc_info=True)
            if futures.configured:
                try:
                    await asyncio.to_thread(futures.read_all, use_cache=False)
                    # The tape pages back several hours over a throttled RPC, so
                    # it is the most expensive read the desk serves — and it sits
                    # on /futures, the endpoint the venue's liveness is judged by.
                    await asyncio.to_thread(futures.recent_trades, use_cache=False)
                except Exception:  # pragma: no cover - a chore must never cost a beat
                    log.warning("warm: the venue read failed", exc_info=True)
                # OUTSIDE that handler: the keeper's chores are cooldown-driven
                # and do not need the reads to have landed. A throttled tape must
                # not stop a settlement.
                await _run_keeper(futures)
            # OUTSIDE the futures guard, deliberately. Mirroring settlements has
            # no venue dependency, and a deployment with no ACR_FUTURES_ADDRESS
            # would otherwise stop feeding the tape without ever saying so.
            await _run_mirror()
            # Past 90 % of the tier's limit: drop caches and hand heap back, and
            # say so on /ops — a stutter instead of an OOM kill (memory.py).
            await asyncio.to_thread(memory.guard)
        except Exception:  # pragma: no cover - keep the loop alive
            log.exception("chain cache warm failed")


async def _operator_loop(stop: asyncio.Event) -> None:
    """The spend operator, on its own timer, when it is armed.

    ITS OWN TASK AND NOT A CHORE ON THE WARM LOOP, deliberately. `_warm_chain`
    runs `reader.read_all` and `futures.read_all` bare inside one `try`, ahead
    of everything after them, so a single throttled Arc read skips the rest of
    that tick — the hazard `_run_keeper` was isolated to avoid and the two reads
    above it still have. A loop that pays bills must not be downstream of
    somebody else's RPC luck.

    Off unless `ACR_OPERATOR_AUTORUN` says otherwise, so a checkout, a laptop
    and CI all do nothing. `operator_keeper` holds the cooldown, the lock and
    the kill switch; this is only the clock.
    """
    from . import operator_keeper

    while not stop.is_set():
        # The interval FIRST, so arming the loop never means a payment during
        # boot — the one moment the process is least able to report what it did.
        try:
            await asyncio.wait_for(stop.wait(), timeout=operator_keeper.sleep_s())
        except TimeoutError:
            pass
        if stop.is_set():
            break
        if not operator_keeper.enabled():
            continue
        try:
            verdict = await asyncio.to_thread(operator_keeper.tick_once)
            if verdict:
                log.info("operator: %s", verdict)
        except Exception:  # pragma: no cover - a chore must never cost a beat
            log.exception("operator tick failed")


async def _humans_tick() -> None:
    """One look by the humans chore, contained like every other chore: a failure
    is recorded and logged, and never reaches the press."""
    from . import keeper

    try:
        verdict = await asyncio.to_thread(keeper.humans_once)
        # A quiet look (cooldown, or the window already done) keeps the last real
        # verdict. Recording its None would turn "not resolving: …" into what /ops
        # renders as "nothing to do" within one tick.
        keeper.record("humans", verdict if verdict is not None else keeper.last_verdict("humans"))
        if verdict:
            log.info("keeper humans: %s", verdict)
    except Exception as exc:  # a chore must never cost a beat
        keeper.record("humans", f"failed: {exc}")
        log.warning("keeper humans failed", exc_info=True)


async def _humans_loop(stop: asyncio.Event) -> None:
    """Resolve each rotation window's humans, on its own clock.

    ITS OWN TASK, like `_operator_loop` and for the same reason: a Circle-signed
    `record` blocks for up to two minutes while Circle confirms it, which the
    60 s warm loop must never wait on. The interval comes first, so a boot is
    never also a burst of identity writes.
    """
    from . import keeper

    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=keeper.HUMANS_TICK_S)
        except TimeoutError:
            pass
        if stop.is_set():
            break
        if keeper.humans_enabled():
            await _humans_tick()


#: The systems-ledger snapshot, recomputed on its own slow timer. None until
#: the first pass lands — served as "pending", never as an empty dashboard.
_ops_cache: dict | None = None


async def _ops_loop(stop: asyncio.Event) -> None:
    """Recompute the systems ledger on a slow timer.

    Its OWN timer, not the 60s warm loop: a full pass reads the venue, the
    prints and two wallet balances, and hanging that off the loop whose job is
    keeping the press's caches hot would make the dashboard compete with the
    product it reports on. Fifteen minutes is far inside the cadence of
    anything it measures (hourly prints, half-hourly roll checks).

    Wrapped like every other loop here: a checker that can take the press down
    is worse than no checker.
    """
    from . import ops

    global _ops_cache
    while not stop.is_set():
        try:
            _ops_cache = await asyncio.to_thread(ops.run_all)
            await asyncio.to_thread(memory.trim)
        except Exception:  # pragma: no cover - the ledger must never cost a beat
            log.exception("ops verify failed")
        try:
            await asyncio.wait_for(stop.wait(), timeout=ops.OPS_VERIFY_S)
        except TimeoutError:
            pass


async def _run_mirror() -> None:
    """The mirror chore, on its own call site.

    Same containment as the venue chores — a failure is recorded and logged, and
    never propagates into the warm loop, because the tape falling behind must
    not be able to stop the press.
    """
    from . import keeper

    if not keeper.enabled():
        return
    try:
        verdict = await asyncio.to_thread(keeper.mirror_once)
        keeper.record("mirror", verdict)
        if verdict:
            log.info("keeper mirror: %s", verdict)
    except Exception as exc:  # pragma: no cover - a chore must never cost a beat
        keeper.record("mirror", f"failed: {exc}")
        log.warning("keeper mirror failed", exc_info=True)


async def _run_keeper(futures) -> None:
    """The venue's chores, on the host that already holds the credentials.

    Isolated in its own try/except INSIDE the warm loop's, deliberately: the
    caller's handler would also catch this, but then a keeper failure would skip
    the rest of that tick. A venue that stops trading is a degraded demo; a
    press that stops printing is a dead product, because the contract will not
    settle against a stale print. The chores are never allowed to cost the press
    a beat.
    """
    from . import keeper

    if not keeper.enabled():
        return
    for name, fn in (("heartbeat", keeper.heartbeat_once), ("roll", keeper.roll_if_needed)):
        try:
            verdict = await asyncio.to_thread(fn, futures)
            # Record the tick even when the chore stood down: a cooldown is the
            # healthy majority case, and a surface that only ever saw verdicts
            # could not tell "nothing to do" from "nobody home".
            keeper.record(name, verdict)
            if verdict:
                log.info("keeper %s: %s", name, verdict)
        except Exception as exc:  # pragma: no cover - a chore must never cost a beat
            keeper.record(name, f"failed: {exc}")
            log.warning("keeper %s failed", name, exc_info=True)


async def _background(stop: asyncio.Event) -> None:
    """Build the store off the event loop, then refresh (and post) on a timer so
    prints evolve, realized vol becomes real, and the oracle stays fresh."""
    settings = get_settings()
    await asyncio.to_thread(store.ensure)  # heavy sim + first estimate, off-loop
    try:
        from .attack import attack_snapshot  # warm the deterministic exhibit once

        await asyncio.to_thread(attack_snapshot)
    except Exception:  # pragma: no cover - defensive
        log.exception("attack snapshot warm-up failed")
    reader = get_reader()  # construct at startup so oracle env is read now
    futures = get_futures()  # on-chain futures desk (inventory-skewed curve)
    poster = OraclePoster(store)
    set_poster(poster)  # reachable for /terminal/data + /health provenance
    # Warm the on-chain read cache off the request path so /terminal/data never
    # blocks on slow remote-RPC reads (each read_all is 3 sequential eth_calls),
    # and seed the print-ts cursor above the on-chain latest so the poster's next
    # post is monotone (avoids 'non-monotone ts' reverts after a restart).
    if reader.configured:
        onchain = await asyncio.to_thread(reader.read_all, use_cache=False)
        latest_ts = max((v.get("timestamp", 0) for v in onchain.values()), default=0)
        if latest_ts:
            store.seed_cursor(latest_ts)
            log.info("seeded print-ts cursor above on-chain latest ts=%s", latest_ts)
        # Post-on-wake: if the on-chain record is overdue (the press slept through
        # its slot), post now instead of waiting out a full refresh cycle. Mirrors
        # the timer body (refresh first so the posted ts clears the seeded cursor).
        # `wake_post_after` (not `refresh_seconds`): on a host that is recycled
        # more often than the product's cadence, the timer never fires and a
        # restart alone must be enough to top the record up. Defaults to one full
        # cycle, so nothing changes unless a deployment sets it.
        if poster.client.can_post() and _overdue_for_startup_post(
            onchain, settings.wake_post_after, time.time()
        ):
            try:
                await asyncio.to_thread(store.refresh)
                await asyncio.to_thread(poster.post_latest)
                await asyncio.to_thread(reader.read_all, use_cache=False)
                if not _record_print_skip(poster):
                    log.info("posted overdue print on wake")
            except Exception:  # pragma: no cover - defensive
                log.exception("post-on-wake failed (timer loop continues)")
    if reader.configured:
        await _rehydrate_provenance(poster)
    # Warm the on-chain attestation summary too (catalog reads it) off-request.
    try:
        from .marketplace import warm_attestation_summary

        await asyncio.to_thread(warm_attestation_summary)
    except Exception:  # pragma: no cover - defensive
        log.exception("attestation summary warm-up failed")
    # Seed the term-structure skew from the live on-chain maker book (if any).
    if futures.configured:
        try:
            inv = await asyncio.to_thread(futures.maker_inventory, use_cache=False)
            store.set_maker_inventory(inv)
        except Exception:  # pragma: no cover - defensive
            log.exception("futures inventory warm-up failed")
    # Prime the /terminal/data cache off-loop so the first visitor is served the
    # shared dict, never a cold synchronous build (the free-tier OOM guard).
    try:
        await asyncio.to_thread(_rebuild_terminal_cache)
    except Exception:  # pragma: no cover - defensive
        log.exception("terminal payload warm-up failed")
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), timeout=settings.refresh_seconds)
        except TimeoutError:
            pass
        if stop.is_set():
            break
        try:
            await asyncio.to_thread(store.refresh)
            if poster.client.can_post():
                await asyncio.to_thread(poster.post_latest)
                _record_print_skip(poster)
            if reader.configured:  # refresh the on-chain caches after any new post
                await asyncio.to_thread(reader.read_all, use_cache=False)
                from .marketplace import warm_attestation_summary

                await asyncio.to_thread(warm_attestation_summary)
            if futures.configured:  # refresh the maker book so the curve skew tracks it
                inv = await asyncio.to_thread(futures.maker_inventory, use_cache=False)
                store.set_maker_inventory(inv)
            # The press is the heaviest job the instance runs; give the transient
            # back to the OS before the next hour lands on top of it (memory.py).
            await asyncio.to_thread(memory.trim)
            # Rebuild the /terminal/data cache off-loop so the served snapshot
            # reflects the fresh prints + on-chain reads (never on the request path).
            await asyncio.to_thread(_rebuild_terminal_cache)
        except Exception:  # pragma: no cover - keep the loop alive
            log.exception("refresh/post cycle failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # On Arc mainnet, refuse to start unless every gate is explicit and configured.
    # Both gates fail OPEN in `auto` mode; a stack trace at deploy time is the
    # only signal an operator reliably reads. Off mainnet this is a no-op.
    assert_mainnet_ready(get_settings())
    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(_background(stop)),
        asyncio.create_task(_warm_chain(stop)),
        asyncio.create_task(_ops_loop(stop)),
        asyncio.create_task(_operator_loop(stop)),
        asyncio.create_task(_humans_loop(stop)),
    ]
    try:
        yield
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


app = FastAPI(title="ACR — The Arc Compute Rate", version="0.1.0", lifespan=lifespan)

app.include_router(payments_router)

# CORS so the public API is queryable cross-origin from browsers (the Terminal
# proxies server-side and doesn't need this, but direct API/`/docs` use does).
# Origins from ACR_CORS_ORIGINS ("*" default for the testnet demo; empty = off).
_cors = [o.strip() for o in (get_settings().cors_origins or "").split(",") if o.strip()]
if _cors:
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_cors,
        allow_credentials=False,  # public read API; "*" origins can't use credentials
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["PAYMENT-REQUIRED", "PAYMENT-RESPONSE", "X-PAYMENT-RESPONSE"],
    )


@app.exception_handler(HumanProofRequired)
async def _human_proof_required_handler(request, exc: HumanProofRequired):
    """Render the 401 challenge: the nonce to sign and what to sign it for.

    401, not 403 — the caller is unauthenticated rather than forbidden, and
    `WWW-Authenticate` is how a client is told what to present.
    """
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=exc.status_code, content=exc.body, headers=exc.headers)


@app.exception_handler(PaymentRequired)
async def _payment_required_handler(request, exc: PaymentRequired):
    """Render the 402 challenge as its Gateway-shaped x402 JSON body
    (``{"x402Version": 2, "resource": {...}, "accepts": [...]}``) — what buyer
    SDKs parse — alongside the legacy challenge headers."""
    from fastapi.responses import JSONResponse

    return JSONResponse(status_code=exc.status_code, content=exc.body, headers=exc.headers)

#: Every route behind `require_payment`. This is what `/x402/info` and the root
#: card serve as `gated_endpoints`, so it is the service's own machine-readable
#: answer to "what costs money" — and for as long as `/compute/{label}` was
#: missing, that answer was false by one route: a fleet seller's metered endpoint
#: charged a real per-unit price while the list said five. The five index routes
#: share the flat `price_usdc()`; the fleet route prices per listing, and the
#: catalog carries each seller's terms.
GATED_ENDPOINTS = [
    "/prints",
    "/prints/{index_id}",
    "/curve/{index_id}",
    "/vol/{index_id}",
    "/seller-scores/{index_id}",
    "/compute/{label}",
]


def require_known_seller(label: str) -> str:
    """404 for an unknown fleet seller, for the same reason as the index guard
    below: a Circle settlement is irrevocable, so a buyer must never be able to
    pay for a seller that does not exist. Without this the 402 fires first, the
    payment settles to the PLATFORM wallet (the fleet lookup having found
    nothing to override it), and the request then 404s — money taken for a
    resource that was never going to answer."""
    if listing_for(label) is None:
        raise HTTPException(status_code=404, detail=f"unknown seller {label}")
    return label


def require_known_index(index_id: str) -> str:
    """404 for an unknown index — declared BEFORE ``require_payment`` in every
    parametrized gated endpoint so a buyer is never charged (verify + settle is
    irrevocable in Circle mode) for a resource that cannot exist."""
    if index_id not in ALL_INDEX_IDS:
        raise HTTPException(status_code=404, detail=f"unknown index {index_id}")
    return index_id


@app.get("/")
def root() -> dict:
    return {
        "name": "ACR — The Arc Compute Rate",
        "tagline": "machine commerce just got its SOFR — and it prints its own attack cost",
        "indices": list(ALL_INDEX_IDS),
        "pricing": {
            "per_query_usdc": price_usdc(),
            "scheme": "x402",
            "network": get_settings().caip2(),
        },
        "gated_endpoints": GATED_ENDPOINTS,
        "marketplace": {"catalog": "/marketplace/catalog", "receipts": "/marketplace/receipts"},
        # Not in `gated_endpoints`: that list means "x402 paid", and this one is
        # gated by a human proof rather than by money.
        "human": {"tca": "/tca/human", "info": "/humanid/info"},
        # Gated by neither money nor a proof. Says WHERE the screen applies, not
        # merely that one exists: for as long as this key claimed the screen "sits
        # on agent-to-agent traffic", it sat on nothing at all.
        "armor": {
            "info": "/armor/info",
            "screens": "POST /graph/query, both directions, for carded callers",
        },
        # DISCOVERABLE, because /agent/challenge exists to tell an agent how to
        # mint a card "without reading our source" — and it was reachable only by
        # reading our source. A discovery endpoint nothing links to is a private
        # endpoint with good intentions.
        "agent": {
            "info": "/agent/info",
            "challenge": "/agent/challenge",
            "whoami": "/agent/whoami",
        },
    }


def _short_addr(addr: str | None) -> str | None:
    """``0x1234…abcd`` — enough to eyeball a wallet without leaking the full
    address into every health poll/log line."""
    return f"{addr[:6]}…{addr[-4:]}" if addr else None


@app.get("/health")
def health() -> dict:
    store.ensure()
    from urllib.parse import urlparse

    from acr_oracle_client.signer import build_signer

    s = get_settings()
    sg = build_signer(s)
    signer = "circle" if type(sg).__name__ == "CircleWalletSigner" else "local" if sg else "none"
    circle = isinstance(get_facilitator(), CircleFacilitator)
    # Most recent on-chain post across the poster's per-index provenance (or None
    # while offline) — the one-curl answer to "is the oracle actually posting?".
    last = [e for e in get_poster().last_posts.values() if e.get("tx")]
    poster_last_tx = max(last, key=lambda e: e.get("at_wall") or 0.0)["tx"] if last else None
    from .memory import rss_mib

    return {
        "status": "ok",
        "indices_live": len(store.latest),
        # Resident memory, so an OOM on the 512 MiB tier is seen approaching
        # rather than discovered in Render's event log after the restart.
        "rss_mib": (lambda m: round(m) if m is not None else None)(rss_mib()),
        # When this process started. A restart is now a KNOWN way for the store
        # to run short of the chain (the memory-only receipts of the last few
        # seconds), so anyone comparing paid_queries against the subgraph can
        # tell a restart from a bug without Render's event log.
        "process_started_at": _PROCESS_STARTED_AT,
        # Active modes — makes "why is my query 402ing / which path am I on" a
        # single curl. gate=dev accepts a mock header; gate=circle needs real x402.
        "gate": "circle" if circle else "dev",
        "signer": signer,
        "tape": s.tape_source,
        "oracle_configured": bool(s.oracle_address),
        # Wiring at a glance — chain, contracts, seller wallet, facilitator.
        "chain_id": s.arc_chain_id,
        "oracle_address": s.oracle_address or None,
        "registry_address": s.registry_address or None,
        # The fourth contract. It was deployed, exercised and then invisible on
        # every surface — a reader could not tell it existed, let alone that a
        # paid query buys a right that lives on-chain.
        "attestor_address": s.attestor_address or None,
        "pay_to": _short_addr((s.x402_pay_to if circle else PAY_TO) or None),
        "facilitator_host": (urlparse(s.x402_facilitator_url).hostname
                             if s.x402_facilitator_url else None),
        "tape_source": s.tape_source,
        "poster_last_tx": poster_last_tx,
        # Is anything still minding the book? Pure module-state read — no chain
        # calls, no credentials — so it cannot slow the cheapest probe we have.
        "keeper": _keeper_status(),
        # The autonomous spender's standing, beside the venue's. An operator
        # asking "is anything paying our bills?" should not have to read a log
        # to find out, and "off" is a legitimate answer that must be visible
        # rather than inferred from silence.
        "operator": _operator_status(),
    }


def _operator_status() -> dict:
    """The spend operator's standing, with the same containment as the keeper's:
    /health is the probe everything leans on, so an import that blew up here
    must not take the liveness check down with it."""
    try:
        from . import operator_keeper

        return operator_keeper.status()
    except Exception:  # pragma: no cover - health must answer regardless
        return {"mode": "unknown"}


def _keeper_status() -> dict:
    """The keeper's standing, or an honest silence.

    /health is the probe everything else leans on. A keeper import that blew up
    here would take down the liveness check for the whole product, so a failure
    reports "unknown" rather than propagating.
    """
    try:
        from . import keeper

        return keeper.status()
    except Exception:  # pragma: no cover - health must answer regardless
        log.warning("keeper status unavailable", exc_info=True)
        return {"enabled": None}


@app.get("/ops/verify")
def ops_verify() -> dict:
    """The systems ledger — every pillar's standing, as the press sees it.

    Serves the background snapshot; it does NOT compute on the request path.
    A dashboard that runs a full venue scan per viewer is a self-inflicted
    outage on a free-tier box, and the numbers move on the hour anyway.

    Ungated and read-only. Before the first pass lands this says "pending" —
    an empty ledger would render as a product with nothing running.
    """
    if _ops_cache is None:
        return {"status": "pending", "sections": [], "verdict": "unread"}
    return {"status": "ok", **_ops_cache}


class OpsActionBody(BaseModel):
    action: str
    params: dict = {}
    #: Dry by default. A missing field can therefore never spend money; only a
    #: present `false` can. This is the single most important default here.
    dry_run: bool = True


def _ops_token(request: Request) -> str | None:
    return request.headers.get("X-ACR-Ops-Token")


@app.get("/ops/actions")
def ops_actions_list(request: Request) -> dict:
    """The action catalogue and the recent audit trail. Token-gated.

    404s when no ACR_OPS_TOKEN is configured — an endpoint that admits it
    exists is one worth guessing at, and most deployments want none of this.
    """
    from . import ops_actions

    try:
        ops_actions.authorize(_ops_token(request))
    except ops_actions.ActionError as e:
        raise HTTPException(e.status, str(e)) from e
    return {
        "actions": [
            {"action": name, "description": desc}
            for name, (_fn, desc) in ops_actions.ACTIONS.items()
        ],
        "caps": {
            "collateralize_usdc": ops_actions.MAX_COLLATERALIZE_USDC,
            "fund_usdc": ops_actions.MAX_FUND_USDC,
        },
        "recent": ops_actions.recent(50),
    }


@app.post("/ops/actions")
def ops_action_run(body: OpsActionBody, request: Request) -> dict:
    """Run one operator action. Token-gated, dry by default, always audited."""
    from . import ops_actions

    try:
        ops_actions.authorize(_ops_token(request))
        return ops_actions.run(body.action, body.params or {}, bool(body.dry_run))
    except ops_actions.ActionError as e:
        raise HTTPException(e.status, str(e)) from e


@app.get("/x402/info")
def x402_info(fac: Facilitator = Depends(get_facilitator)) -> dict:
    """The payment gate, described dynamically (ungated). The Terminal's API
    console reads this to label the gate mode and drive the paid-query demo."""
    s = get_settings()
    circle = isinstance(fac, CircleFacilitator)
    return {
        "facilitator": "circle" if circle else "dev",
        "price_usdc": price_usdc(),
        "scheme": "x402",
        # CAIP-2 for BOTH gates — the catalog, receipts, and this descriptor
        # must name the same network or the Terminal surfaces disagree.
        "network": s.caip2(),
        # The Gateway SDK's chain key and the private-mainnet header flag, so a payer
        # built in the browser or on the terminal's server needs no chain table.
        "gateway_chain": s.gateway_chain or None,
        "private_mainnet": s.private_mainnet,
        "pay_to": (s.x402_pay_to if circle else PAY_TO) or None,
        "payment_header": "PAYMENT-SIGNATURE",
        "gated_endpoints": GATED_ENDPOINTS,
    }


@app.post("/webhooks/circle")
async def circle_webhook(request: Request) -> dict:
    """Inbound Circle webhook (ungated). Verifies the ECDSA P-256 signature over
    the RAW body, records the event, and always ACKs 200 so the subscription-
    confirmation ping activates the webhook even when a signature can't be
    verified. Unverified events are recorded as such and only displayed."""
    from .webhooks import get_webhook_store, summarize, verify_signature

    raw = await request.body()  # must read raw bytes before JSON parse (for the sig)
    verified = verify_signature(
        raw, request.headers.get("X-Circle-Signature"), request.headers.get("X-Circle-Key-Id")
    )
    try:
        payload = json.loads(raw) if raw else {}
        if not isinstance(payload, dict):
            payload = {"value": payload}
    except Exception:
        payload = {}
    etype, sub, summary = summarize(payload)
    from .webhooks import WebhookEvent

    get_webhook_store().record(
        WebhookEvent(
            id=str(payload.get("notificationId") or payload.get("id") or f"evt-{time.time():.0f}"),
            type=etype,
            verified=verified,
            received_at=time.time(),
            subscription_id=sub,
            summary=summary,
            payload=payload,
        )
    )
    return {"received": True}


@app.get("/webhooks/recent")
def webhooks_recent() -> dict:
    """Recent Circle webhook events for the Terminal's activity panel (ungated)."""
    from .webhooks import get_webhook_store

    store_wh = get_webhook_store()
    try:
        import cryptography  # noqa: F401

        verify_available = True
    except Exception:
        verify_available = False
    return {
        "events": store_wh.recent(25),
        "received": store_wh.received,
        "verify_available": verify_available,
        "note": "point a Circle Programmable Wallets webhook at POST /webhooks/circle",
    }


def provenance() -> dict:
    """Where the numbers in a paid response actually come from.

    An agent pays USDC for these endpoints and then acts on the answer, so it is
    entitled to know that the estimator — which is real — currently runs over a
    calibrated SIMULATED tape rather than observed settlement flow. That fact was
    visible in `/health` and in the Terminal's chrome, but not in the machine
    responses themselves, which is precisely where it matters most.

    ``tape`` is the honest tier: "sim" (synthetic flow), "arc" (real Arc USDC
    settlements) or "receipts" (this venue's own x402 ledger). ``estimator`` and
    the on-chain print are real in every tier.
    """
    s = get_settings()
    tape = (s.tape_source or "sim").lower()
    return {
        "tape": tape,
        "simulated_tape": tape == "sim",
        "estimator": "real",
        # The chain the print is on, in the payload's own terms (`eip155:5042`
        # on mainnet); a literal here would tell every paying agent the wrong
        # network the day the addresses move.
        "chain": s.caip2() if s.oracle_address else None,
        "note": (
            "prices are estimated from a calibrated simulated tape; the estimator, "
            "the signature and the on-chain print are real"
            if tape == "sim"
            else f"prices are estimated from the {tape} tape"
        ),
    }


# EVERY PAID ROUTE READS THE CARD, and reads it BEFORE the payment. The buyer agent
# has presented one on every request since fd2e3b9, and for a day these six routes —
# the only ones it actually buys from — never looked: the card was verified on the
# free reads and decorative on the paid ones, so a human claim could not reach the
# human tier where money moved. Dependency order is the safety: a forged card is a
# 401 before require_payment runs, so nobody is charged for a refused request.
@app.get("/prints")
def prints(
    agent: VerifiedAgent | None = Depends(optional_agent),
    _: PaymentReceipt = Depends(require_payment),
) -> dict:
    return {**store.snapshot(), "provenance": provenance()}


@app.get("/prints/{index_id}")
def print_one(
    index_id: str = Depends(require_known_index),
    agent: VerifiedAgent | None = Depends(optional_agent),
    _: PaymentReceipt = Depends(require_payment),
) -> dict:
    store.ensure()
    if index_id not in store.latest:
        raise HTTPException(status_code=404, detail=f"unknown index {index_id}")
    p = store.latest[index_id]
    d = store.diag[index_id]
    return {
        **p.model_dump(),
        "unit": spec_for(index_id).unit,
        "naive_vwap": d.naive_vwap,
        "cost_to_move_1pct": d.bound.cost_to_move_1pct,
        "cleaned_pct": 100 * d.cleaning.removed_fraction,
        "robustness": store.robustness(index_id),
        "provenance": provenance(),
    }


@app.get("/curve/{index_id}")
def curve(
    index_id: str = Depends(require_known_index),
    agent: VerifiedAgent | None = Depends(optional_agent),
    _: PaymentReceipt = Depends(require_payment),
) -> dict:
    return {"index_id": index_id, "curve": store.curve(index_id), "provenance": provenance()}


@app.get("/vol/{index_id}")
def vol(
    index_id: str = Depends(require_known_index),
    agent: VerifiedAgent | None = Depends(optional_agent),
    _: PaymentReceipt = Depends(require_payment),
) -> dict:
    return {"index_id": index_id, "annualized_vol": store.vol(index_id),
            "provenance": provenance()}


@app.get("/seller-scores/{index_id}")
def seller_scores(
    index_id: str = Depends(require_known_index),
    agent: VerifiedAgent | None = Depends(optional_agent),
    _: PaymentReceipt = Depends(require_payment),
) -> dict:
    return {"index_id": index_id, "sellers": store.seller_scores(index_id),
            "provenance": provenance()}


def _meter_agent(request: Request, agent: VerifiedAgent | None) -> None:
    """Charge a carded caller's own bucket; leave an anonymous one exactly as it was.

    THE CONDITION IS THE WHOLE DESIGN, and it is measured rather than cautious.
    `ratelimit.check` applies the HOST bucket even with no ident, so calling it
    unconditionally would put a new global ceiling on nine previously unlimited
    public reads — and these nine carry the Terminal's own polling. `/api/tape`
    refreshes every 30s and fans out one `/rating/{seller}` call PER SELLER, so a
    single reader can drive on the order of a thousand upstream calls an hour, all
    arriving from one Vercel proxy address. Any ceiling I could pick tonight would
    stop the desk before it stopped an abuser.

    So the limiter runs where there is an identity to limit, which is also the
    honest form of the argument: the card is what creates the identity. Metering
    anonymous callers here is a separate decision with its own blast radius and
    deserves its own commit and its own measurement.

    `verified=True` because this ident is an EIP-712 signature recovered to the
    key that signed it — so it is allowed to stand in for the host key, which
    behind one proxy identifies nobody.
    """
    if agent is not None:
        ratelimit.check(request, "read", agent.ident, verified=True)


@app.post("/graph/query")
async def graph_proxy_query(
    body: dict,
    request: Request,
    agent: VerifiedAgent | None = Depends(optional_agent),
) -> dict:
    """Read the tape through ACR's Studio key, over an allowlist of operations.

    Not a passthrough: the query text lives server-side and the caller names an
    operation. A proxy that forwarded arbitrary GraphQL would be the API key
    with extra steps — anyone could spend our quota on nested queries, or relay
    through us to a different subgraph entirely.

    Rate-limited on IDENTITY where one is offered, not only on host: every
    browser reader arrives through the same edge proxy, so an IP-keyed limit is
    a global limit wearing a per-person costume (see ratelimit.py).
    """
    # AN IDENTITY AT LAST, AND WHY THIS ONE IS ALLOWED. The rule in ratelimit.py
    # is that an identity must never be derived from something the caller
    # controls, because a body field rotated per request mints a fresh bucket
    # every time and leaves only the loose host ceiling. A card is caller-supplied
    # too, so what makes it different is COST, not provenance:
    #
    #   anonymous  no card -> the host ceiling, which behind one proxy is global.
    #   carded     a signed card -> per-KEY. Better, and still rotatable by anyone
    #              willing to mint keys, because keys are free.
    #   human      a card whose cluster the CHAIN confirms -> per-PERSON. Ten keys
    #              belonging to one human share one bucket.
    #
    # Only the third tier is genuinely scarce, and it is scarce because
    # HumanIdMirror says so rather than because we asked nicely. The card stays
    # OPTIONAL: this is a public read, and a gate that began refusing anonymous
    # callers would be a different endpoint than the one documented.
    ratelimit.check(request, "graph", agent.ident if agent else None,
                    verified=agent is not None)

    # THE SCREEN RUNS FOR CARDED CALLERS ONLY, and the name of the module is the
    # reason: armor.py screens "agent-to-agent traffic". A browser reader arriving
    # through the Vercel proxy is not that, and there are roughly a thousand of
    # those an hour — putting a fail-closed call to Google in front of them would
    # mean one expired service-account key turns the public tape into a blank
    # page, with /armor/info correctly reporting that the screen did its job.
    #
    # So the card buys two things rather than one: a bucket of your own, AND a
    # screened reply. A caller that declines the card declines the protection.
    # It runs AFTER the limiter on purpose — a caller sending injections should
    # spend budget doing it, and a screen in front of an unmetered caller is a new
    # amplifier rather than a defence.
    if agent is not None:
        await armor.enforce(armor.request_text(body), "request")

    # Off-loop: graph_proxy.run is blocking urllib to Studio and this handler is
    # async now so the screen can be awaited. Same idiom as the background tasks
    # above (`asyncio.to_thread(reader.read_all)`), not a new mechanism.
    result = await asyncio.to_thread(
        graph_proxy.run, str(body.get("operation") or ""), body.get("variables") or {}
    )

    # BOTH DIRECTIONS OR NEITHER — armor.py's opening argument, and the reply is
    # the direction that matters here. The tape indexes strings SELLERS control
    # (addresses, attestation URIs, unbenchmarked reasons), so a reply assembled
    # from our own subgraph can still carry someone else's payload onward to an
    # agent that trusts us. Screened only when there IS third-party data: an
    # `available: False` envelope carries our own strings and nobody else's.
    if agent is not None and result.get("available"):
        await armor.enforce(
            json.dumps(result.get("data"), separators=(",", ":"), sort_keys=True),
            "reply",
        )
    return result


@app.get("/graph/operations")
def graph_operations(
    request: Request,
    agent: VerifiedAgent | None = Depends(optional_agent),
) -> dict:
    """What the proxy will run — the tape's public read API, named.

    `transport` is the press's own ledger of subgraph queries — how many, through
    which host, how fast, how many served from the short cache — because The
    Graph's dashboard counts gateway queries made with an API key and the Studio
    development URL is counted nowhere while being capped at 3,000 a day. The
    same reason `/armor/info` records when Google last answered.
    """
    from acr_tape.graph_client import transport_info

    _meter_agent(request, agent)
    return {
        "operations": sorted(graph_proxy.OPERATIONS),
        "max_first": graph_proxy.MAX_FIRST,
        "transport": transport_info(get_settings().subgraph_url),
    }


@app.get("/humanid/info")
def humanid_info(verifier: HumanVerifier = Depends(get_verifier)) -> dict:
    """The human-proof gate, described dynamically (ungated).

    Reports the backend honestly, including that demo identities are Sandbox
    ones rather than Orb-verified people — the limit of what "verified human"
    means in this deployment, stated where a reader will actually see it.
    """
    s = get_settings()
    return {
        "backend": "agentkit" if isinstance(verifier, AgentKitVerifier) else "dev",
        # WHICH roster the proof is checked against. "agentkit" alone read as a
        # World Chain lookup; with the fixture roster it is the demo wallets only.
        "agentbook": verifier.book().source if isinstance(verifier, AgentKitVerifier) else None,
        "sandbox": s.humanid_sandbox,
        "app_id": s.humanid_app_id or None,
        "proof_header": "HUMAN-PROOF",
        "rotation_window_days": RATING_WINDOW_DAYS,
        # None when there is nothing to compare against; False is a
        # misconfiguration that would otherwise read as "this human never traded".
        "salt_matches_commitment": verifier.salt_ok(),
        "verified_proofs": verifier.verified,
        # Names only, never values. A credential under a name nothing reads is
        # discarded in silence, so the operator sees "unset" while looking at
        # the value in their own .env — worth surfacing where they will look.
        "unrecognised_env": stray_world_credentials(),
    }


@app.get("/agent/info")
def agent_info(request: Request) -> dict:
    """The agent-card gate, described dynamically (ungated).

    Reports `human_binding_verifiable` for the same reason `/humanid/info`
    reports whether its salt matches its commitment: a gate that cannot check a
    human claim and a gate that is granting the tier to anyone who asks look
    identical from outside, and only one of them is a gate.
    """
    ratelimit.check(request, "agent")
    return get_gate().info()


@app.get("/agent/challenge")
def agent_challenge(request: Request) -> dict:
    """Everything needed to mint a card, without reading our source.

    A 200 rather than the 401 a gated route raises: this is documentation an
    agent fetches deliberately, and answering 401 to a request for instructions
    would be answering the wrong question.
    """
    ratelimit.check(request, "agent")
    # The challenge object is built by the gate so this page and a real refusal
    # can never describe two different schemes — the drift that made the footer
    # and /developers disagree about which contracts exist.
    return dict(get_gate().challenge(request).detail)


@app.get("/agent/whoami")
def agent_whoami(
    request: Request,
    agent: VerifiedAgent | None = Depends(optional_agent),
) -> dict:
    """What the gate made of the card you presented.

    The trio is now complete: `/agent/info` says what the gate IS,
    `/agent/challenge` says how to mint a card, and this says what happened to
    YOURS. Without it a card author had to infer the tier from whether a rate
    limit behaved differently, which is not an answer, and the gate had no route
    a test could drive over real HTTP.

    Reports the TIER and never the ident. The ident is a rate-limit key, and
    echoing a key invites clients to depend on its shape; `human_note` is what a
    caller actually needs when a claim did not reach the human tier.

    `scope_enforced` REPORTS THE FLAG, and used to be a hardcoded `False` with a
    paragraph here explaining that `scopeHash` was a signed field checked against
    nothing. That was honest for exactly as long as the answer was always false.
    It is now `ACR_OPERATOR_READ_SCOPE`: when set, `require_business_read` checks
    a card's `scopeHash` against `read:business:<slug>` on the three per-business
    operator routes, and refuses four ways.

    STILL ONLY THOSE THREE ROUTES. There is no per-route scope map, so a caller
    reading `scope_enforced: true` learns that SOME scope is enforced somewhere,
    not that every route checks one. `scopeHash` is one-way, so nothing here can
    enumerate what a card grants — only test a candidate for equality.
    """
    ratelimit.check(request, "agent")
    if agent is None:
        return {
            "tier": TIER_ANON,
            "carded": False,
            "header": CARD_HEADER,
            "challenge": "/agent/challenge",
        }
    return {
        "tier": agent.tier,
        "carded": True,
        "agent": agent.masked,
        "role": agent.card.role,
        "audience": agent.card.audience,
        "issued_at": agent.card.issued_at,
        "expires_at": agent.card.expires_at,
        "ttl_s": agent.card.ttl_s,
        "scope_hash": agent.card.scope_hash,
        "scope_enforced": scope_enforced(),
        # WHAT the limit is keyed on, without saying what the key IS. The ident is
        # still withheld — echoing it invites clients to depend on its shape — but
        # whether your budget is shared with the rest of your fleet or is yours
        # alone is the single most useful thing a carded caller can be told, and it
        # is the whole claim of the human tier.
        "ident_kind": "human-cluster" if agent.cluster else "agent-key",
        "claimed_human": agent.card.claims_human,
        "human_note": agent.human_note,
    }


@app.get("/armor/info")
def armor_info(request: Request) -> dict:
    """The screen on agent-to-agent traffic, described dynamically (ungated).

    Reports WHICH BACKEND ANSWERED, because that is the one thing a reader cannot
    infer. A screen that silently fell back to the offline floor and a screen
    that is inspecting nothing look identical from outside — `/humanid/info`
    reports whether its salt matches its commitment for the same reason.

    Never reports the template's filter thresholds: those live in the Model Armor
    template where an operator can change them without a redeploy, so echoing a
    copy here would be a second source of truth that goes stale silently.
    """
    ratelimit.check(request, "agent")
    screen = get_screen()
    s = get_settings()
    configured = screen_is_live(screen)
    return {
        **screen.info(),
        "mode": s.armor_mode,
        # The project and location, not the credentials. Location is included
        # because it is regional and a wrong one 404s on the template, which
        # reads like "the template does not exist".
        "project_id": s.armor_project_id or None,
        "location": s.armor_location or None,
        "template": s.armor_template or None,
        # Whether a credential FILE is present — never its contents, and never
        # its path, which would name a location on the host.
        "credentials_present": bool(
            s.armor_credentials_file and Path(s.armor_credentials_file).is_file()
        ),
        "live": configured,
        "directions": ["request", "reply"],
        # WHERE the screen is actually applied. Listing the directions alone said
        # what the module can do, not what the service does with it, and those
        # were different facts for as long as nothing called it.
        "applies_to": ["POST /graph/query (carded callers, both directions)"],
        "max_chars": {"request": SCREEN_CAP, "reply": SCREEN_CAP_REPLY},
    }


# REGISTERED BEFORE `/tca/{payer}` ON PURPOSE. FastAPI matches in declaration
# order, so with the parametrized route first this path would bind `payer` to the
# literal string "human" and quietly return a TCA for a wallet that cannot exist.
@app.get("/tca/human")
def tca_human(
    request: Request,
    days: int = 7,
    proof: HumanProof = Depends(require_human),
) -> dict:
    """This human's purchases, unioned across every wallet resolved to them.

    The proof is not what makes this free — `/tca/{payer}` is ungated too. It is
    what makes the aggregation safe to offer: without it this endpoint would take
    a cluster id, and anyone passing one could enumerate a stranger's whole
    wallet fleet. There is no request shape here that returns someone else's.
    """
    # Per-human, not per-IP: a shared proxy makes an IP-keyed limit a global one.
    # The nullifier is hashed rather than used raw — `ratelimit.py` keeps bearer
    # credentials out of its key table, and this is the more sensitive one.
    # `verified`: `require_human` has already checked this proof cryptographically,
    # which is the same fact that excuses a carded agent from the shared host
    # ceiling — a proven person behind a busy proxy should not be throttled
    # because strangers share their egress IP.
    ratelimit.check(request, "humanid", ratelimit.session_ident(proof.nullifier), verified=True)
    return human_tca(proof.cluster, proof.window, days=days)


_EVM_ADDRESS = re.compile(r"^0x[0-9a-fA-F]{40}$")


def _require_address(value: str, what: str) -> str:
    """422 on anything that is not an address. `GET /tca/undefined` used to answer
    200 `"the subgraph did not answer"` — a caller's typo reported as our outage,
    which is the one confusion a provenance surface must never produce."""
    if not _EVM_ADDRESS.match(value):
        raise HTTPException(status_code=422, detail=f"{what} must be a 0x-prefixed 20-byte address, got {value!r}")
    return value


@app.get("/tca/{payer}")
def tca(
    payer: str,
    request: Request,
    days: int = 7,
    agent: VerifiedAgent | None = Depends(optional_agent),
) -> dict:
    """What this payer's purchases cost against the benchmark they could see.

    Ungated like the rest of the marketplace surfaces: a benchmark nobody can
    check for free is a benchmark nobody checks.
    """
    _meter_agent(request, agent)
    return payer_tca(_require_address(payer, "payer"), days=days)


#: The units a bill can be priced in — the three the index registry declares,
#: and nothing else. A typo must not come back as "no market for that", because
#: `_require_address` records the same lesson one route up: a caller's mistake
#: reported as our outage is the one confusion a provenance surface must never
#: produce.
_PRICEABLE_UNITS = tuple(spec_for(i).unit for i in ALL_INDEX_IDS)

#: Stands in for a payee when a caller is only asking about a price. The ladder
#: refuses at check 2 without one ("a payee, and an amount"), which is correct
#: for a payment and useless for a price check — so a price check supplies a
#: placeholder and the response says `vendor_supplied: false`. Deliberately not
#: a fleet address, so the bill is priced against the market like any other
#: stranger's invoice.
_NO_VENDOR = "0x" + "11" * 20


class ParBasketOut(BaseModel):
    """The comparison set `/par` answered from, and how old it is.

    READ `status` BEFORE THE VERDICT. `ok` and `aging` both price against real
    third-party list prices; the SCREAMING values mean the basket was unusable
    and the comparison fell back to sellers THIS DEPLOYMENT OPERATES, at a scale
    `anchors/GAP.md` records as 10x to 1250x off market. `benchmarked_against`
    names which happened, and a verdict read without it is not a verdict.
    """

    index_id: str
    #: `ok` · `aging` · `STALE` · `ABSENT` · `NO_ROWS`. The two lowercase words
    #: still price; the rest are refusals. `aging` is the band before the cliff:
    #: old enough to re-anchor, still measuring against the market.
    status: str
    #: Unix SECONDS the basket was fetched.
    fetched_at: float
    #: How old it is, in seconds. **`null` when unknowable**, never 0 — a zero
    #: would read as "fetched this instant", the freshest possible answer for
    #: the least evidence.
    age_s: float | None
    #: Past this the press refuses the basket and reprices against the fleet.
    max_age_s: float
    #: Where `aging` begins. Always below `max_age_s`.
    warn_after_s: float
    #: Prices the basket HAS and prices it ASKED FOR. A basket that shrinks
    #: silently re-medians a different population, so both travel.
    rows: int
    requested: int


class ParCheckOut(BaseModel):
    """What `GET /par` answers.

    DOCUMENTATION, NOT A FILTER — attached through `responses=` rather than
    `response_model=` on purpose. A `response_model` makes FastAPI serialize
    through the model, so any field this class failed to declare would be
    silently dropped from a live response: a doc improvement that quietly
    breaks the terminal and every integrator. This way the route keeps
    returning exactly what it returns, and the schema is a description that
    `test_par_schema.py` holds to the truth in both directions.

    It is the only route here with a documented response, because it is the
    only one a stranger can exercise with no key, no card and no payment — and
    the one third parties were given no way to learn the shape of.
    """

    unit: str
    billed_usdc: float
    quantity: float
    vendor: str | None
    vendor_supplied: bool
    #: `market` or `fleet` — WHOSE prices answered.
    benchmarked_against: str
    basket: ParBasketOut
    #: How far over the going rate, in basis points. THIS is the number to
    #: show; `verdict.verdict` answers a different question and disagrees on
    #: purpose, reading `over_par` whenever material money sits at the cheapest
    #: row even for a bill at exactly the market median.
    over_rate_bp: float | None
    par: dict
    verdict: dict
    #: What the spend operator WOULD do with this bill, without doing it.
    would: dict


@app.get(
    "/par",
    responses={200: {"model": ParCheckOut, "description": "The bill, priced, with its basis."}},
)
def price_check(
    request: Request,
    unit: str,
    billed_usdc: float,
    quantity: float,
    vendor: str = "",
    agent: VerifiedAgent | None = Depends(optional_agent),
) -> dict:
    """Price one bill against what the market is actually paying.

    THE FIRST PUBLIC SURFACE FOR `par.py`, which until now was reachable only by
    being in `businesses.json`. A benchmark nobody can check for free is a
    benchmark nobody checks — the same sentence `/tca/{payer}` is ungated for,
    and this is the pre-trade half of that post-trade answer.

    Ungated, and there is no batch sibling. This docstring used to promise a
    paid `POST /par/batch` as "where a ledger goes"; nothing ever built it, and
    a route advertised in prose and absent from the router is a worse answer
    than no route. The ledger went to the CALLER instead — `mcp/src/spendLog.ts`
    records each priced bill on the developer's own machine and `spend_report`
    totals them, which needs no paywall decision and keeps a business's vendor
    list out of this service entirely.

    WHICH MARKET depends on the vendor, and the rule is `operator.py`'s, not a
    second one invented here: a seller this deployment operates is judged
    like-for-like against fleet quotes, both pinned to the same scale, and
    anybody else is a real invoice that only the market basket can price.
    `anchors/GAP.md` is why — the reference levels sit 10x to 1250x off real
    prices, so a real bill measured against them reads as a ~9,990 bp discount.

    What this does NOT check is named rather than implied. There is no meter, no
    counterparty screen, no agreement, no budget and no balance for a caller who
    has onboarded nothing, and `decide` leaves a note for each.
    """
    from .operator import Obligation, decide
    from .par import (
        AGE_WARN_FRACTION,
        ANCHOR_MAX_AGE_S,
        is_first_party,
        market_basket,
        par_from_quotes,
        quotes_by_unit,
    )
    from .par import assess as par_assess

    _meter_agent(request, agent)
    if unit not in _PRICEABLE_UNITS:
        raise HTTPException(
            status_code=422,
            detail=f"unit must be one of {list(_PRICEABLE_UNITS)}, got {unit!r}",
        )
    if not (billed_usdc > 0) or not (quantity > 0):
        raise HTTPException(
            status_code=422,
            detail="billed_usdc and quantity must both be positive",
        )
    payee = _require_address(vendor, "vendor") if vendor else _NO_VENDOR

    basket = market_basket(unit)
    if is_first_party(payee) or not basket.usable:
        from .marketplace import build_receipts

        quotes = quotes_by_unit(build_receipts(get_facilitator()).get("receipts") or [], unit)
        against = "fleet"
    else:
        quotes = list(basket.quotes)
        against = "market"
    par = par_from_quotes(unit, quotes, exclude_seller=payee, denomination="unit")
    verdict = par_assess(billed_usdc, par, quantity=quantity)

    ob = Obligation(
        obligation_id="price-check", vendor=payee, billed_usdc=billed_usdc,
        category="infra", unit=unit, vendor_quantity=quantity,
    )
    d = decide(ob, par=par, metered_quantity=quantity, commitment=None)

    return {
        "unit": unit,
        "billed_usdc": billed_usdc,
        "quantity": quantity,
        "vendor": vendor or None,
        "vendor_supplied": bool(vendor),
        # WHOSE prices answered. `graph/schema.graphql` states the standard:
        # "a benchmark that counted one silently would be claiming security it
        # does not have."
        "benchmarked_against": against,
        # A DATE IS NOT AN AGE. This shipped `fetched_at` alone — a bare unix
        # float — so no reader could tell a one-day-old basket from a
        # twenty-nine-day-old one, and the surface rendered it as a calendar
        # date in a teal chip either way. Meanwhile `AGE_WARN_FRACTION` had
        # already decided that past 24 days this is worth saying out loud, and
        # said it only to `make ci`.
        #
        # So the answer now carries the clock AND the two marks on it, because
        # an age is only readable against the threshold that governs it and
        # that threshold is an env var a reader cannot see. `age_s` is `None`
        # when unknowable rather than 0.0 — see `Basket.age_s`.
        "basket": {
            "index_id": basket.index_id,
            "status": basket.wire_status,
            "fetched_at": basket.fetched_at,
            "age_s": basket.age_s,
            "max_age_s": ANCHOR_MAX_AGE_S,
            "warn_after_s": AGE_WARN_FRACTION * ANCHOR_MAX_AGE_S,
            "rows": basket.rows,
            "requested": basket.requested,
        },
        # HOW FAR OVER THE GOING RATE, which is the question a caller is asking
        # and is NOT what `verdict.verdict` answers. That field reads "over_par"
        # whenever material money is available at the CHEAPEST row — the right
        # test when the cheaper seller is an address we can pay, and misleading
        # here, where a bill priced at exactly the market median would come back
        # labelled over par because one mid-class model is cheaper than another.
        # The engine keeps its semantics; the public surface states the rate.
        "over_rate_bp": verdict.get("over_par_bp"),
        "par": par.as_dict(),
        "verdict": verdict,
        "would": {"intent": d.intent, "rule": d.rule, "notes": d.notes,
                  "recommended_intent": d.recommended_intent},
    }


def _policy_for(business):
    """A ``PolicyClient`` pointed at ONE business's wallet.

    Built per request rather than cached: the wallet address comes from the
    registry, and a cached client would keep serving a business whose wallet was
    rotated. Returns None when the client cannot be built at all, so the
    statement reports "measured, not spent for" instead of failing the page.
    """
    if not business.policy_wallet:
        return None
    try:
        from acr_oracle_client.policy import PolicyClient

        return PolicyClient(wallet_address=business.policy_wallet)
    except Exception as exc:  # pragma: no cover - env dependent
        log.warning("statement: no policy client for %s (%s)", business.slug, exc)
        return None


@app.get("/operator/businesses")
def operator_businesses(
    request: Request,
    agent: VerifiedAgent | None = Depends(optional_agent),
) -> dict:
    """Who the operator runs for, and the traction numbers derived from it.

    Derived, never maintained: the counts come from the registry itself, so the
    number on this page cannot drift from the list beside it. That matters more
    than usual here, because these are the figures a reviewer checks.

    Mainnet and testnet are reported separately and there is no field that adds
    them. An unconsented business appears under a pseudonym and still counts:
    dropping it would understate real usage, naming it would use somebody's
    identity without asking.
    """
    _meter_agent(request, agent)
    from .businesses import counts, load

    registry = load()
    return {
        "businesses": [b.as_public_dict() for b in registry],
        "counts": counts(registry),
    }


@app.get("/operator/traction")
def operator_traction(
    request: Request,
    agent: VerifiedAgent | None = Depends(optional_agent),
) -> dict:
    """The traction numbers, computed from the rows that justify them.

    Canteen's form asks how many businesses are onboarded, how much the agent
    moved, and what problems it solves; their FAQ adds that a synthetic dataset
    does not count. So nothing here is maintained by hand — the figures are
    derived at request time from the registry and the decision log, which is the
    only arrangement where a number cannot drift from the rows beside it.

    `moved_usdc` is what the operator actually paid out and is kept apart from
    `priced_usdc`, what it assessed. Mainnet and testnet are never summed. Every
    business carries links to its own ledger and statement so the arithmetic is
    checkable rather than asserted.
    """
    _meter_agent(request, agent)
    from .ledger_audit import load_tape
    from .traction import build_traction

    # The tape is the other side of the money: a treasury appearing as the
    # SELLER on a settlement is a treasury that was paid, which is the half of
    # "total USDC received and paid out" that did not exist until now.
    return build_traction(tape=load_tape())


@app.get("/operator/ledger/{business}", response_class=PlainTextResponse)
def operator_ledger(
    business: str,
    request: Request,
    days: int = 90,
    agent: VerifiedAgent | None = Depends(optional_agent),
) -> str:
    """The business's decisions as a beancount file, served as plain text.

    Prior Art #01 names beancount as "a ledger an agent can write to and a human
    can read", and says it has never been connected to money that actually
    moves. This is that connection: a real double-entry file whose every
    transaction sums to zero, annotated with the rule that produced it, what we
    independently metered, the par it was checked against and the transaction
    hash — the three things *Agents and Ledgers* says a balanced ledger cannot
    check on its own.

    `text/plain` rather than JSON because the consumer is beancount, or a person
    reading it. Ungated, like the statement: an accountant should not need a key
    to check our arithmetic.
    """
    _meter_agent(request, agent)
    from .businesses import resolve
    from .ledger_export import to_beancount
    from .statement import read_decisions

    b = resolve(business)
    if b is None:
        raise HTTPException(status_code=404, detail=f"no business registered as {business!r}")
    # AFTER the 404, deliberately. A gate that fired first would answer 401 for a
    # business that does not exist, which hides nothing — `/operator/businesses`
    # lists every slug publicly — and would break `verify_operator.py`, whose
    # unknown-business check accepts 404/422/503 and not 401. Off by default; see
    # `require_business_read`.
    require_business_credential(request, b.slug)
    require_business_read(agent, b)
    since = time.time() - max(1, min(365, int(days))) * 86_400
    rows = read_decisions(business=b.slug, since=since)
    return to_beancount(rows, b.slug)


def _confirm_tx(business):
    """Does the chain corroborate a transaction we recorded as a payment?

    Returns a callable answering True / False / None. `None` is load-bearing:
    an unreachable node, a pruned history or a wallet we cannot even locate are
    all "could not tell", and reporting those as phantoms would cry wolf on
    every blip until nobody read the audit at all.

    Corroboration is deliberately narrow — the receipt exists, it succeeded, and
    it was sent TO this business's wallet. A receipt alone is not enough: the
    whole failure being checked for is a transaction that succeeded and did
    nothing, and the one thing it cannot do is have gone to a contract that is
    not there.
    """
    if not business.policy_wallet:
        return None
    try:
        from acr_oracle_client.policy import PolicyClient
        from web3.exceptions import TransactionNotFound

        client = PolicyClient(wallet_address=business.policy_wallet)
        if client.wallet_status() != "ok":
            # The wallet itself is absent on this chain, so nothing sent to it
            # can be corroborated and nothing can be denied either.
            return lambda _tx: None
        w3 = client.web3()
        if w3 is None:
            return None
    except Exception as exc:  # pragma: no cover - env dependent
        log.warning("audit: no chain to corroborate against (%s)", exc)
        return None

    wallet = str(business.policy_wallet).lower()

    def confirm(tx: str):
        try:
            rcpt = w3.eth.get_transaction_receipt(tx)
        except TransactionNotFound:
            # THE NODE ANSWERED, AND ITS ANSWER WAS NO. Different from a node
            # that could not be reached, and collapsing the two was the bug in
            # the first version of this: a fabricated hash read as "could not
            # tell", so the audit reported clean against a planted phantom.
            #
            # Safe on Arc specifically: finality is deterministic and there are
            # no reorgs, so a settled transaction is permanent and a node that
            # does not have it never saw it. The caveat is history pruning —
            # against a pruned node an old payment would read as denied. The
            # press reads a full node; if that changes, this goes back to None.
            return False
        except Exception:
            return None
        if rcpt is None:
            return None
        if int(getattr(rcpt, "status", 0)) != 1:
            return False
        to = str(getattr(rcpt, "to", "") or "").lower()
        return to == wallet

    return confirm


@app.get("/operator/audit/{business}")
def operator_audit(
    business: str,
    request: Request,
    days: int = 90,
    agent: VerifiedAgent | None = Depends(optional_agent),
) -> dict:
    """The six errors a trial balance cannot see, searched for by name.

    *Agents and Ledgers* — the analysis this hackathon hands every team — argues
    that double entry's one built-in check is nearly worthless against an agent:
    "nearly every mistake an LLM can make with money passes it." It names six,
    and says the controls that catch them live OUTSIDE the ledger.

    `/operator/ledger/{business}` serves the balanced file. This serves the part
    the file cannot do for itself, and none of these checks reads the ledger
    alone: omission compares our decisions against the sellers' own settlement
    tape, commission asks whether the payee ever served this business, principle
    asks the registry rather than the exporter.

    Ungated for the same reason as the statement and the ledger: an accountant
    should not need a key to check our arithmetic, and a reviewer should not
    need one to check whether we checked.
    """
    _meter_agent(request, agent)
    from .businesses import resolve
    from .ledger_audit import audit, load_tape
    from .statement import read_decisions

    b = resolve(business)
    if b is None:
        raise HTTPException(status_code=404, detail=f"no business registered as {business!r}")
    # AFTER the 404, deliberately. A gate that fired first would answer 401 for a
    # business that does not exist, which hides nothing — `/operator/businesses`
    # lists every slug publicly — and would break `verify_operator.py`, whose
    # unknown-business check accepts 404/422/503 and not 401. Off by default; see
    # `require_business_read`.
    require_business_credential(request, b.slug)
    require_business_read(agent, b)
    window = max(1, min(365, int(days)))
    since = time.time() - window * 86_400
    out = audit(
        read_decisions(business=b.slug, since=since),
        load_tape(),
        treasury=b.treasury,
        slug=b.slug,
        categories=b.categories,
        # The chain's opinion of the payments we claim. `None` for "could not
        # tell" so a sleeping node is never reported as a phantom.
        confirm=_confirm_tx(b),
    )
    out["business"] = b.slug
    out["period_days"] = window
    return out


@app.get("/operator/statement/{business}")
def operator_statement(
    business: str,
    request: Request,
    days: int = 7,
    limit: int | None = None,
    agent: VerifiedAgent | None = Depends(optional_agent),
) -> dict:
    """What the owner reads: what the agent decided, and what is waiting on them.

    Ungated like the other per-payer surfaces — a business should not have to
    pay to read its own statement, and a reviewer should not have to pay to
    check ours.

    ``business`` is a registry slug or a treasury address; a URL will carry
    either. 404 rather than an empty statement for an unknown one: an empty
    statement reads as "this business has spent nothing", which is a different
    and much more flattering claim than "we have never heard of them".

    USDC savings here come from observed quotes. The index appears only as
    `market_context`, in basis points — see `statement.py` for why that
    separation is load-bearing.
    """
    from .businesses import resolve

    _meter_agent(request, agent)
    # Resolved here as well as inside `build_statement`, so the 404 for an unknown
    # business comes before the gate and the gate has a `Business` to key on. Two
    # lookups of a two-row registry; the alternative is a gate that cannot name
    # which business it is protecting.
    b = resolve(business)
    if b is None:
        raise HTTPException(
            status_code=404,
            detail=f"no business registered as {business!r}",
        )
    # AFTER the 404, deliberately. A gate that fired first would answer 401 for a
    # business that does not exist, which hides nothing — `/operator/businesses`
    # lists every slug publicly — and would break `verify_operator.py`, whose
    # unknown-business check accepts 404/422/503 and not 401. Off by default; see
    # `require_business_read`.
    require_business_credential(request, b.slug)
    require_business_read(agent, b)
    st = build_statement(
        business, days=days, limit=limit, tca_fn=payer_tca, policy_for=_policy_for
    )
    if st is None:
        raise HTTPException(
            status_code=404,
            detail=f"no business registered as {business!r}",
        )
    return st


@app.get("/rating/{seller}")
def rating(
    seller: str,
    request: Request,
    days: int = 7,
    agent: VerifiedAgent | None = Depends(optional_agent),
) -> dict:
    """Grade a seller from the same tape, with `n` and the synthetic share on
    every card so a reader can discount it without being told to."""
    _meter_agent(request, agent)
    return seller_rating(_require_address(seller, "seller"), days=days)


@app.get("/fleet")
def fleet_listings(
    request: Request,
    agent: VerifiedAgent | None = Depends(optional_agent),
) -> dict:
    """The seller fleet and its price spread — ungated, like the catalog.

    The spread IS the point. Before the fleet, every settlement in the system
    paid one wallet one flat price, so no payer could have paid more than
    another and transaction-cost analysis had nothing to measure. Published
    openly so anyone can see what a buyer could have chosen between, and check a
    reroute suggestion against it themselves.
    """
    # Grouped by index AND model class, because that is the only grouping a
    # reroute suggestion may be built on: across classes a price gap is quality,
    # which the hedonic stage adjusts away. Publishing the comparable sets makes
    # the like-for-like pairs checkable instead of asserted.
    _meter_agent(request, agent)
    comparable: dict[str, dict[str, list[str]]] = {}
    for listing in fleet_summary():
        comparable.setdefault(listing["index_id"], {}).setdefault(
            listing["model_class"], []
        ).append(listing["label"])
    return {
        "listings": fleet_summary(),
        "comparable_sets": comparable,
        "note": (
            "Unit prices differ by seller. Compare only within a model_class — "
            "across classes the difference is quality, which the hedonic stage "
            "adjusts away, not overcharging."
        ),
        "provenance": provenance(),
    }


@app.get("/compute/{label}")
def compute(
    label: str = Depends(require_known_seller),
    agent: VerifiedAgent | None = Depends(optional_agent),
    receipt: PaymentReceipt = Depends(require_payment),
) -> dict:
    """A fleet seller's metered endpoint — the thing that produces a priced tape.

    The response is deliberately thin: this exists to make a REAL Gateway
    settlement happen at a REAL per-seller unit price, which is the input TCA
    needs. It returns the terms it just charged so a buyer can reconcile its own
    receipt without trusting ours.
    """
    listing = listing_for(label)
    assert listing is not None  # require_known_seller ran first
    return {
        "seller": listing.seller,
        "label": listing.label,
        "index_id": listing.index_id,
        "unit": listing.unit,
        "unit_price_usdc": listing.unit_price_usdc,
        "quantity": listing.quantity,
        "amount_usdc": listing.amount_usdc,
        "model_class": listing.model_class.value,
        "settlement": {"tx_ref": receipt.tx_ref, "payer": receipt.payer},
        "provenance": provenance(),
    }


@app.get("/marketplace/catalog")
def marketplace_catalog(
    request: Request,
    agent: VerifiedAgent | None = Depends(optional_agent),
) -> dict:
    """Machine-readable listings of every paid ACR resource (ungated — discovery
    is free, the data costs). Bazaar-shaped items; a buyer agent reads this,
    picks a resource, and pays via x402."""
    from .marketplace import build_catalog

    _meter_agent(request, agent)
    return build_catalog(str(request.base_url))


@app.get("/marketplace/receipts")
def marketplace_receipts(
    request: Request,
    agent: VerifiedAgent | None = Depends(optional_agent),
) -> dict:
    """The public settlement ledger — recent x402 receipts, newest first
    (ungated; it is the marketplace's proof-of-commerce tape)."""
    from .marketplace import build_receipts

    _meter_agent(request, agent)
    return build_receipts(get_facilitator())


@app.get("/onchain/{index_id}")
def onchain_print(
    index_id: str,
    request: Request,
    agent: VerifiedAgent | None = Depends(optional_agent),
) -> dict:
    """The settlement-grade print read straight from ``ACROracle`` on-chain.

    Ungated (a public read of chain state). 503 if no oracle is configured or
    the chain did not answer (ask again); 404 only if the oracle has no print
    for this index yet. Served from the warm cache inside its TTL
    (``OracleReader.latest``).
    """
    reader = get_reader()
    if not reader.configured:
        raise HTTPException(status_code=503, detail="no oracle configured (set ACR_ORACLE_ADDRESS)")
    try:
        r = reader.latest(index_id)
    except OracleUnreachable:
        raise HTTPException(
            status_code=503, detail=f"the chain did not answer for {index_id}; ask again shortly"
        ) from None
    if r is None:
        raise HTTPException(status_code=404, detail=f"no on-chain print for {index_id}")
    _meter_agent(request, agent)
    return {"source": "onchain", "oracle": reader.oracle_address, **r}


@app.get("/hedger")
def hedger_state(fac: Facilitator = Depends(get_facilitator)) -> dict:
    """The autonomous hedger's standing — mandate, position, fills, and spend.

    Ungated, and derived only from public data: the agent's position on the
    venue, the ``Traded`` events whose taker is the agent, and the settlement
    ledger rows whose payer is it. Nothing here depends on a log file from
    wherever the agent happened to run, so every number is one a reader can
    reproduce from the chain.
    """
    from . import hedger
    from .marketplace import build_receipts

    return hedger.build_hedger_state(get_futures(), build_receipts(fac))


@app.get("/futures")
def futures_roster(
    request: Request,
    agent: VerifiedAgent | None = Depends(optional_agent),
) -> dict:
    """The whole on-chain futures venue for the Terminal's live desk + trade tape:
    the venue address, per-index desks, and recent fills (newest-first). Ungated;
    empty (venue null) when no ACRFutures is configured. This is the fast endpoint
    the desk polls — the heavy /terminal/data carries only the aggregate desks."""
    _meter_agent(request, agent)
    return get_futures().roster()


@app.get("/futures/{index_id}")
def futures_desk(
    request: Request,
    index_id: str = Depends(require_known_index),
    agent: VerifiedAgent | None = Depends(optional_agent),
) -> dict:
    """The live on-chain futures desk for an index — the maker's inventory,
    mark-to-oracle PnL, and settlement status, read from ``ACRFutures``.

    Ungated (a public read of chain state). 503 if no futures venue is
    configured; 404 if no series exists for this index yet.
    """
    futures = get_futures()
    if not futures.configured:
        raise HTTPException(status_code=503, detail="no futures venue configured (set ACR_FUTURES_ADDRESS)")
    desk = futures.read_desk(index_id)
    if desk is None:
        raise HTTPException(status_code=404, detail=f"no futures series for {index_id}")
    _meter_agent(request, agent)
    return {"source": "onchain", "futures": futures.futures_address, **desk}


def build_terminal_payload(store: PrintStore, reader, poster=None, fac=None) -> dict:
    """The full /terminal/data payload. ``scripts/gen_snapshot.py`` builds the
    Terminal's bundled offline snapshot from this same function, so the two
    can never drift."""
    from .attack import attack_snapshot

    settings = get_settings()
    fac = fac or get_facilitator()
    # The poster's signer address is lazy (a Circle-custody signer fetches it
    # over the network) — never let a provenance nicety fail the payload.
    signer_addr = None
    if poster is not None and getattr(poster.client, "signer", None) is not None:
        try:
            signer_addr = poster.client.signer.address
        except Exception:  # pragma: no cover - live Circle hiccup → honest null
            signer_addr = None
    store.ensure()
    onchain = reader.read_all()
    futures = get_futures()
    futures_desks = futures.read_all() if futures.configured else {}
    prints = {
        iid: {
            **store.latest[iid].model_dump(),
            "unit": spec_for(iid).unit,
            "naive_vwap": store.diag[iid].naive_vwap,
            "cost_to_move_1pct": store.diag[iid].bound.cost_to_move_1pct,
            "cleaned_pct": 100 * store.diag[iid].cleaning.removed_fraction,
            "vol": store.vol(iid),
            "curve": store.curve(iid),
            "robustness": store.robustness(iid),
            "onchain": onchain.get(iid),
        }
        for iid in store.latest
    }
    return {
        "prints": prints,
        # Recent print history (the deque already exists) — sparklines and the
        # index-detail history chart. Capped at 96 points per index.
        "history": {
            iid: [
                {"ts": p.ts, "value": p.value, "ci_lo": p.ci_lo, "ci_hi": p.ci_hi}
                for p in list(store.history.get(iid, []))[-96:]
            ]
            for iid in store.latest
        },
        # Seller reliability for the human Registry view; the machine endpoint
        # /seller-scores/{id} stays x402-gated.
        "sellers": {iid: store.seller_scores(iid) for iid in store.latest},
        # The on-chain futures desk per index (maker inventory, mark-to-oracle
        # PnL, settlement) — empty {} when no ACRFutures venue is configured.
        "futures": futures_desks,
        "attack": attack_snapshot(),
        "oracle": reader.oracle_address if reader.configured else None,
        # Network identity card — the frontend contract for every chain-aware
        # surface (explorer links, gate badge, poster provenance). Shape is fixed.
        "chain": {
            "name": settings.chain_name,
            "chain_id": settings.arc_chain_id,
            # Circle's identifiers for this chain, so a browser payer and the Desk
            # can be built without a testnet literal anywhere in the frontend.
            "circle_blockchain": settings.circle_blockchain or None,
            "gateway_chain": settings.gateway_chain or None,
            "private_mainnet": settings.private_mainnet,
            "caip2": settings.caip2(),
            # The payload is public, and the server's RPC may carry a provider key:
            # both fields name the chain's PUBLIC endpoint (EIP-3085 for a visitor's
            # wallet; the terminal's server-side fallback). Local anvil has no
            # public endpoint, so there the server's own is the only one.
            "rpc_url": settings.public_rpc_url or settings.arc_rpc_url,
            "public_rpc_url": settings.public_rpc_url,
            "explorer_base": settings.explorer_base,
            "usdc_address": settings.usdc_address,
            "gateway_wallet": settings.x402_gateway_wallet,
            "oracle_address": settings.oracle_address or None,
            "registry_address": settings.registry_address or None,
            "futures_address": settings.futures_address or None,
            # The fourth contract. Deployed and exercised, then invisible on
            # every surface — so a reader could not tell that paying for data
            # buys a right that lives on chain, not a row in our own files.
            "attestor_address": settings.attestor_address or None,
            # The fifth contract, and the same story a second time. HumanIdMirror
            # is deployed on Arc and publishes the window-rotated cluster ids the
            # human-denominated bound rests on — and until now no surface named
            # it, so a reader could not tell the identity layer was on chain at
            # all rather than a claim in our own files.
            "humanid_address": settings.humanid_mirror_address or None,
            "gate": "circle" if isinstance(fac, CircleFacilitator) else "dev",
            "tape_source": settings.tape_source,
            "signer": signer_addr,
            "poster": {"posts": poster.posts, "last": poster.last_posts} if poster else None,
        },
    }


def _build_payload_locked() -> dict:
    """Build the /terminal/data payload + store it in the module cache. Heavy
    (sim estimate + attack exhibit + on-chain reads). The caller MUST hold
    ``_terminal_payload_lock`` so two heavy builds never run at once — the whole
    point of the cache on a 512MB box."""
    global _terminal_payload_cache
    _terminal_payload_cache = build_terminal_payload(
        store, get_reader(), poster=get_poster(), fac=get_facilitator()
    )
    return _terminal_payload_cache


def _rebuild_terminal_cache() -> dict:
    """Force-rebuild the cache (the background loop, after each warm/refresh) —
    lock-guarded so it can never run concurrently with a cold request's build."""
    with _terminal_payload_lock:
        return _build_payload_locked()


@app.get("/terminal/data")
def terminal_data() -> dict:
    """Ungated snapshot for the human-facing ACR Terminal (prints + curve + vol).

    The Terminal is the human view; the x402 gate applies to the machine API.
    Includes a live attack comparison so the 'Attack the Index' panel renders,
    and — when an oracle is configured — the on-chain print for provenance.

    Served from a background-maintained cache: the request path never runs the
    heavy build concurrently, so N simultaneous visitors on the 512MB free tier
    can't OOM-spiral the box (a single shared dict, ~13KB, is returned instead).
    """
    cached = _terminal_payload_cache
    if cached is not None:
        return cached
    # Pre-warm window (before the background loop has built the cache): build once
    # under the lock — concurrent cold requests AND the background loop all
    # serialize here, so the box never runs parallel heavy builds. First waiter
    # builds + caches; the rest return that dict.
    with _terminal_payload_lock:
        if _terminal_payload_cache is not None:
            return _terminal_payload_cache
        return _build_payload_locked()


class AttackStartRequest(BaseModel):
    budget_usdc: float = 8000.0
    target_multiplier: float = 2.5
    seed: int | None = None


# Strong refs to in-flight demo tasks — an un-referenced asyncio task may be
# garbage-collected mid-run.
_demo_tasks: set[asyncio.Task] = set()


def _testnet_surface(name: str):
    """A route-level dependency: the faucet, the demo buyer and the attack lab spend
    OUR money on a stranger's request, so they exist only off mainnet —
    `testnet_surfaces_enabled` is False on chain 5042 no matter what the environment
    says. As a dependency it runs BEFORE body validation, so the answer is 404 for any
    request shape — and 404 rather than 403, because a route that admits it exists
    invites the next attempt."""

    def _check() -> None:
        if not testnet_surfaces_enabled(get_settings()):
            raise HTTPException(404, f"{name} is not available on this network")

    return _check


@app.post("/demo/attack/start", dependencies=[Depends(_testnet_surface('the attack lab'))])
async def demo_attack_start(req: AttackStartRequest | None = None) -> dict:
    """Kick a live wash-flow attack run for the Terminal's Attack Lab (ungated —
    it drives the human demo). Single-flight: 409 while a run is in progress."""
    from . import demo

    r = req or AttackStartRequest()
    budget = min(50_000.0, max(500.0, r.budget_usdc))
    mult = min(5.0, max(1.1, r.target_multiplier))
    seed = r.seed if r.seed is not None else int(time.time()) % 1_000_000
    if not demo.try_start(budget, mult, seed):
        raise HTTPException(status_code=409, detail="an attack run is already in flight")
    task = asyncio.create_task(asyncio.to_thread(demo.execute, budget, mult, seed))
    _demo_tasks.add(task)
    task.add_done_callback(_demo_tasks.discard)
    return {
        "state": "running",
        "params": {"budget_usdc": budget, "target_multiplier": mult, "seed": seed},
    }


@app.get("/demo/attack/status")
def demo_attack_status() -> dict:
    """The run's state, PLUS whether a run can be started here at all.

    Ungated on purpose — a reader should be able to see a finished run's chart on
    any network. But `/demo/attack/start` is gated to testnet, and the two
    together used to lie: this route answered 200 on mainnet, the Terminal read
    that as "the lab is live", rendered a teal `press live` chip and armed the
    button, and a visitor who pressed it got a 404. Measured on production
    2026-10-07: status 200, start 404.

    `available` is the server answering the question the page was guessing at.
    The gate is the authority on what the gate will accept, and a client that
    infers it from a chain id would be a second copy of the rule.
    """
    from . import demo

    return {**demo.status(), "available": testnet_surfaces_enabled(get_settings())}


class BuyerStartRequest(BaseModel):
    count: int = 20
    delay_ms: int = 400


@app.post("/demo/buyer/start", dependencies=[Depends(_testnet_surface('the demo buyer'))])
async def demo_buyer_start(req: BuyerStartRequest | None = None) -> dict:
    """Release the Exchange's floor buyer (ungated — it drives the human demo):
    N real x402 two-act exchanges through this app's own gate, receipts landing
    on the public ledger/tape. Dev gate only — the mock header fails closed on
    the Circle gate by design (use apps/agent with a funded wallet there)."""
    from . import buyer_demo

    if isinstance(get_facilitator(), CircleFacilitator):
        raise HTTPException(
            status_code=409,
            detail="live Circle gate: the in-page buyer pays the dev gate only; "
            "run apps/agent with a funded wallet instead",
        )
    r = req or BuyerStartRequest()
    count = min(50, max(1, r.count))
    delay_s = min(2.0, max(0.0, r.delay_ms / 1000))
    payer = buyer_demo.new_payer()
    if not buyer_demo.try_start(count, payer):
        raise HTTPException(status_code=409, detail="a buyer run is already on the floor")
    task = asyncio.create_task(buyer_demo.execute(app, count, delay_s, payer))
    _demo_tasks.add(task)
    task.add_done_callback(_demo_tasks.discard)
    return {"state": "running", "count": count, "payer": payer}


# --- the Public Desk: user-controlled wallets trading ACRFutures -----------


class DeskSessionRequest(BaseModel):
    user_id: str


class DeskFaucetRequest(BaseModel):
    # No address: the destination is derived from the session token, so a
    # caller cannot choose where the faucet sends money (see desk.drip_stake).
    user_token: str


class DeskPassStatusRequest(BaseModel):
    user_token: str


class DeskPassClaimRequest(BaseModel):
    user_token: str
    #: The transfer to claim. Omitted → the newest completed transfer this wallet
    #: made to the seller, found through Circle's own record and then verified on
    #: chain like any other — the browser never has to learn a hash.
    tx_hash: str = ""


@app.post("/desk/pass/status")
def desk_pass_status(req: DeskPassStatusRequest, request: Request) -> dict:
    """What a pass costs, and whether this session's wallet holds one — read from
    the chain, never from our own memory. POST so the token stays out of logs."""
    ratelimit.check(request, "wallet", ratelimit.session_ident(req.user_token))
    from . import desk, feedpass

    q = feedpass.quote()
    w = _desk_call(desk.wallet_of, req.user_token)
    has, until = (False, 0)
    if w:
        has, until = feedpass.access(_pass_w3(), w["address"])
    return {
        "price_usdc": q.price_usdc,
        "window_s": q.window_s,
        "pay_to": q.pay_to or None,
        "attestor": q.attestor,
        "available": bool(q.pay_to and q.attestor),
        "wallet": (w or {}).get("address"),
        "has_access": has,
        "paid_until": until or None,
    }


@app.post("/desk/pass/claim")
def desk_pass_claim(req: DeskPassClaimRequest, request: Request) -> dict:
    """Turn a mined USDC transfer into a pass: verify it on chain, sign the
    attestation with the press, relay it, answer with what the chain now says.
    Every check runs before the signature exists (index_api.feedpass)."""
    ratelimit.check(request, "challenge", ratelimit.session_ident(req.user_token))
    from . import desk, feedpass

    w = _desk_call(desk.wallet_of, req.user_token)
    if not w:
        raise HTTPException(409, "this session has no wallet yet — finish the PIN ceremony first")
    signer = _pass_signer()
    if signer is None:
        raise HTTPException(503, "no press signer configured: a pass cannot be attested")
    tx_hash = req.tx_hash.strip()
    if not tx_hash:
        q = feedpass.quote()
        found = _desk_call(desk.recent_transfers_from, w["address"], q.pay_to) if q.pay_to else []
        if not found:
            raise HTTPException(404, "no completed payment from this wallet to the seller yet — "
                                     "give the transfer a moment to confirm, then claim again")
        tx_hash = found[0]["tx_hash"]
    try:
        out = feedpass.mint(_pass_w3(), signer, wallet=w["address"], tx_hash=tx_hash)
    except feedpass.PassError as e:
        raise HTTPException(e.status, e.detail) from e
    feedpass.forget(w["address"])
    return out


def _pass_w3():
    from web3 import Web3

    s = get_settings()
    return Web3(Web3.HTTPProvider(s.arc_rpc_url, request_kwargs={"timeout": 25}))


def _pass_signer():
    from acr_oracle_client import build_role_signer

    return build_role_signer("poster", get_settings())


class DeskChallengeRequest(BaseModel):
    user_token: str
    wallet_id: str
    action: str  # approve | collateral | trade | withdraw | settle | pass
    index_id: str = "ACR-GPU"
    qty: float = 0.0
    address: str = ""  # the SCA — lets the server size the action to live margin
    series_id: int | None = None  # withdraw: which series to empty


class DeskLimitsRequest(BaseModel):
    address: str
    index_id: str = "ACR-GPU"


class DeskWithdrawableRequest(BaseModel):
    address: str


def _desk_call(fn, *args):
    from .desk import DeskError

    try:
        return fn(*args)
    except DeskError as e:
        raise HTTPException(status_code=e.status, detail=e.detail) from e


@app.post("/desk/session")
def desk_session(req: DeskSessionRequest, request: Request) -> dict:
    """Open (or resume) a Public Desk session: Circle user + 60-min token, a
    PIN-setup challenge on first contact, the existing SCA wallet afterwards.
    Ungated — the desk IS the demo; guardrails live in desk.py."""
    ratelimit.check(request, "session", req.user_id)
    from . import desk

    return _desk_call(desk.open_session, req.user_id)


class DeskWalletRequest(BaseModel):
    user_token: str


@app.post("/desk/wallet")
def desk_wallet(req: DeskWalletRequest, request: Request) -> dict:
    """The session's ARC-TESTNET wallet + its USDC stake (null pre-PIN).
    POST so the session token stays out of URLs and access logs."""
    ratelimit.check(request, "wallet", ratelimit.session_ident(req.user_token))
    from . import desk

    w = _desk_call(desk.wallet_of, req.user_token)
    if w is None:
        return {"wallet": None, "usdc": None}
    # USDC *is* Arc's native token — the 0x3600… predeploy is its ERC-20 view of
    # the same balance, so the native read (18-dec) is the 6-dec ERC-20 amount.
    # A throttled read degrades to null rather than a failed desk step.
    return {
        "wallet": w,
        "usdc": desk._wallet_usdc(w["address"]),
        "faucet": desk.get_ledger().status(w["address"]),
    }


@app.post("/desk/faucet", dependencies=[Depends(_testnet_surface('the faucet'))])
def desk_faucet(req: DeskFaucetRequest, request: Request) -> dict:
    """Claim + start the one-per-wallet 0.5 USDC stake from the custody wallet.
    The destination is THIS SESSION'S wallet — never a caller-supplied address.
    Returns as soon as the slot is reserved: Circle's confirm poll outlives any
    sane HTTP timeout, so the drip lands on a background thread and the client
    watches its wallet balance."""
    # Per SESSION, not per source IP: every reader reaches this through the same
    # Vercel proxy, so an IP-keyed faucet limit is a global one. The one-drip-
    # per-address rule that actually protects the custody wallet lives in
    # desk.FaucetLedger.claim, which this cannot weaken.
    ratelimit.check(request, "faucet", ratelimit.session_ident(req.user_token))
    from . import desk

    return _desk_call(desk.drip_stake, req.user_token)


@app.post("/desk/limits")
def desk_limits(req: DeskLimitsRequest, request: Request) -> dict:
    """The live per-direction size caps for this wallet — what the desk may
    offer without minting a challenge the contract would revert."""
    ratelimit.check(request, "limits", req.address)
    from . import desk

    return _desk_call(desk.desk_limits, req.address, req.index_id)


@app.post("/desk/withdrawable")
def desk_withdrawable(req: DeskWithdrawableRequest, request: Request) -> dict:
    """What this wallet can take back out, across every series it holds
    collateral in — including expired and settled ones, which is exactly where
    a reader needs an exit and where the tradable-series gate refuses to look."""
    ratelimit.check(request, "withdrawable", req.address)
    from . import desk

    return _desk_call(desk.withdrawable, req.address)


@app.post("/desk/challenge")
def desk_challenge(req: DeskChallengeRequest, request: Request) -> dict:
    """Mint the contractExecution challenge for one desk action; the browser
    SDK executes it under the user's PIN."""
    ratelimit.check(request, "challenge", ratelimit.session_ident(req.user_token))
    from . import desk

    return _desk_call(
        desk.build_challenge,
        req.user_token,
        req.wallet_id,
        req.action,
        req.index_id,
        req.qty,
        req.address,
        req.series_id,
    )


@app.get("/demo/buyer/status")
def demo_buyer_status() -> dict:
    from . import buyer_demo

    return buyer_demo.status()


@app.get("/revenue")
def revenue(fac: Facilitator = Depends(get_facilitator)) -> dict:
    return {
        "paid_queries": fac.paid_queries,
        "revenue_usdc": fac.revenue_usdc,
        "price_usdc": price_usdc(),
        "recent": [
            {"payer": r.payer, "amount_usdc": r.amount_usdc, "tx_ref": r.tx_ref}
            for r in list(fac.recent)[-10:]
        ],
        "note": "the index about machine commerce, bought by machines",
    }
