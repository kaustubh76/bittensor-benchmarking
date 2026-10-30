"""The venue keeper, running where the credentials already are.

The heartbeat trade and the series roll used to run in GitHub Actions from raw
private keys. Now that the venue signs through Circle custody, the same jobs in
CI would need ``ACR_CIRCLE_ENTITY_SECRET`` — a credential that controls **every**
developer-controlled wallet, including the one that signs oracle prints. That is
a strictly larger blast radius than the scoped key it would replace, so moving
the jobs to CI's credential is the wrong direction. Move the jobs instead.

This service already holds those credentials, already runs a 60-second loop, and
already posts hourly prints with them. Adding the venue's two chores here means
**no signing credential in CI at all** — see docs/WALLETS.md.

Three rules this module lives by, because it shares a process with the press:

1. **It can never take the press down.** Every entry point is wrapped by the
   caller and every failure is swallowed after logging. A venue that stops
   trading is a degraded demo; a press that stops printing is a dead product,
   and the contract will not settle against a stale print.
2. **It does nothing without custody.** If the maker/taker roles do not resolve
   to Circle wallets it returns immediately rather than reaching for an ambient
   key — the exact fallback that kept the venue on a raw EOA for weeks.
3. **Cooldowns bound every write.** A failing chore retried each 60s tick would
   burn gas and RPC budget to keep failing.

``ACR_KEEPER=0`` disables the whole thing without a deploy.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time

from acr_core import get_settings

log = logging.getLogger("index_api.keeper")

#: One tick an hour, matching the cron this replaces. Not tied to the warm
#: loop's cadence: the warm loop is a read, and this spends money.
HEARTBEAT_EVERY_S = float(os.environ.get("ACR_KEEPER_HEARTBEAT_S", "3600"))
#: The roll only needs to be *considered* often; futures_roll no-ops while a
#: collateralized series has life left, so this is cheap when there is nothing
#: to do — one read, no write.
ROLL_CHECK_EVERY_S = float(os.environ.get("ACR_KEEPER_ROLL_S", "1800"))

#: How often to mirror settled receipts onto ``ReceiptMirror``. Minutes, not
#: hours: the contract only accepts an ordinary mirror within one hour of the
#: settlement, and the arrival print a settlement is measured against is chosen
#: from its own ``settledAt`` — so lateness costs freshness, and past the hour
#: it costs an operator's intervention.
MIRROR_EVERY_S = float(os.environ.get("ACR_KEEPER_MIRROR_S", "120"))
#: Below this the keeper stands down rather than half-completing a chore. On Arc
#: USDC is gas, so a wallet spent to the floor cannot even withdraw.
GAS_FLOOR_USDC = float(os.environ.get("ACR_KEEPER_GAS_FLOOR", "1.0"))
#: How much a heartbeat trade moves. Small on purpose: the point is a live tape,
#: not a position.
TRADE_QTY = float(os.environ.get("ACR_KEEPER_QTY", "0.25"))
#: An ALLOWLIST and an ordering, never a selection. Which indices the keeper
#: works is DERIVED from which ones actually have a live series (see
#: `live_indices`) — a fixed list is precisely what was removed from
#: .github/workflows/futures-heartbeat.yml, where rotating over all three meant
#: "two hours in three picked an index with no series at all and the workflow
#: failed — noise that would have masked a real outage". Empty means "any".
INDEX_ALLOW = [
    x.strip()
    for x in os.environ.get("ACR_KEEPER_INDEX", os.environ.get("SEED_INDEX", "")).split(",")
    if x.strip()
]
#: What a roll posts as maker collateral. Mirrors scripts/futures_roll.py so the
#: two paths cannot disagree about what a healthy successor looks like.
ROLL_COLLATERAL = float(os.environ.get("ROLL_COLLATERAL", "1.5"))
#: The collateral token — Arc's native USDC predeploy, also a standard ERC-20.
USDC_ADDRESS = os.environ.get(
    "ACR_USDC_ADDRESS", "0x3600000000000000000000000000000000000000"
)

_OWNER_ABI = [
    {"type": "function", "name": "owner", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "address"}]},
]

_last_heartbeat = 0.0
#: Which index the next heartbeat trades, modulo the live roster.
_hb_cursor = 0
_last_roll_check = 0.0
#: Last mirror sweep.
_last_mirror = 0.0
#: One mirror pass at a time. The tick and a settlement-triggered pass can
#: coincide; the contract's replay guards make a double pass safe but not free.
_mirror_lock = threading.Lock()

#: HUMAN RESOLUTION, once per 7-day rotation window, on the host that already
#: holds the salt, the mirror address and the mirror's one authorized signer.
#: Until 2026-10-16 it was a laptop chore (`make resolve-humans ARGS=--commit`),
#: and it had silently stopped: the last resolved window was 2958, five weeks
#: back, so every surface read "0 humans" and new settlements were stamped
#: human:false. Opt-in per service (`ACR_KEEPER_HUMANS=1`), so mainnet does not
#: start writing identity to its chain because an image changed.
HUMANS_EVERY_S = float(os.environ.get("ACR_KEEPER_HUMANS_S", "3600"))
#: How often the loop looks. Cheap: while the host clock says the resolved
#: window still runs, a look costs no chain read at all.
HUMANS_TICK_S = float(os.environ.get("ACR_KEEPER_HUMANS_TICK_S", "300"))
_last_humans = 0.0
_humans_done_window: int | None = None
_humans_lock = threading.Lock()
#: The pending settlement-triggered pass, if any. Kept so a burst of purchases
#: coalesces into ONE pass a few seconds after the last of them.
_mirror_kick: asyncio.Task | None = None
#: How long after a settlement the mirror runs. Long enough for a buyer loop's
#: next few receipts to join the same pass, short enough that a restart in the
#: gap is a narrow accident rather than a 120 s window.
MIRROR_KICK_DELAY_S = float(os.environ.get("ACR_KEEPER_MIRROR_KICK_S", "3"))

#: Last observed outcome per chore, for the Terminal. The keeper's verdicts
#: went only to the server log, so the one question a reader of a live venue
#: actually has — "is anything still minding this book?" — had no answer on
#: any surface. Shape per chore: {"at": epoch, "verdict": str|None}.
_last_run: dict[str, dict[str, object]] = {}


def enabled() -> bool:
    return os.environ.get("ACR_KEEPER", "1") not in ("0", "false", "no")


def humans_enabled() -> bool:
    """The humans chore runs only where a service asked for it, under the kill switch."""
    return enabled() and os.environ.get("ACR_KEEPER_HUMANS", "0").strip().lower() in ("1", "true", "yes", "on")


def record(chore: str, verdict: str | None) -> None:
    """Note that ``chore`` ran, whatever it decided.

    Recording the tick and not just the verdict is the point. Both chores
    return None while on cooldown, which is the healthy majority of ticks; if
    only verdicts were kept, a keeper doing exactly its job would look
    indistinguishable from one that had died an hour ago.
    """
    _last_run[chore] = {"at": time.time(), "verdict": verdict}


def last_verdict(chore: str) -> str | None:
    """The most recent verdict recorded for ``chore``, or None."""
    seen = _last_run.get(chore)
    return seen["verdict"] if seen else None  # type: ignore[return-value]


def _chore_status(chore: str, every_s: float, last_fire: float) -> dict[str, object]:
    now = time.time()
    seen = _last_run.get(chore)
    #: `last_fire` is when the chore last did work; `seen["at"]` is when it was
    #: last CHECKED. They differ by design and a reader is owed both.
    return {
        "checked_at": seen["at"] if seen else None,
        "checked_age_s": round(now - float(seen["at"]), 1) if seen else None,  # type: ignore[arg-type]
        "verdict": seen["verdict"] if seen else None,
        "last_fire_at": last_fire or None,
        "last_fire_age_s": round(now - last_fire, 1) if last_fire else None,
        "every_s": every_s,
        "next_due_s": max(0.0, round(every_s - (now - last_fire), 1)) if last_fire else 0.0,
    }


def status() -> dict[str, object]:
    """What the keeper has been doing, for /health.

    Read-only over module state — no chain calls, no credentials touched. A
    disabled keeper says so rather than reporting zeros, because "off" and
    "stalled" are different facts and the Terminal must not render one as the
    other.
    """
    if not enabled():
        return {"enabled": False}
    return {
        "enabled": True,
        "heartbeat": _chore_status("heartbeat", HEARTBEAT_EVERY_S, _last_heartbeat),
        "roll": _chore_status("roll", ROLL_CHECK_EVERY_S, _last_roll_check),
        "mirror": _chore_status("mirror", MIRROR_EVERY_S, _last_mirror),
        # Always present, with `on`, so "not asked for" never reads as "stalled".
        "humans": {**_chore_status("humans", HUMANS_EVERY_S, _last_humans), "on": humans_enabled()},
    }



def may_open_series(owner: str, me: str) -> bool:
    """Whether ``me`` is allowed to call ``openSeries``.

    Trivial, and extracted anyway: it is the difference between a roll and a
    transaction that reverts on every cooldown forever. Until 2026-08-03 the
    venue's owner was the retiring EOA and this was always False, so the keeper
    could only shout for a human; the handover to the maker's own Circle wallet
    is what made an unattended roll possible. The guard has to survive that
    changing back — a fork, a redeploy pointed elsewhere, another transfer.
    """
    return bool(owner) and bool(me) and str(owner).lower() == str(me).lower()


def roll_collateral_for(fc, index_id: str) -> float:
    """What a fresh book on ``index_id`` should be funded with.

    Asks the desk's own inverse of ``feasible_qty`` rather than posting the flat
    ROLL_COLLATERAL, which is sized for ACR-INF and would be ~30x ACR-GPU's
    requirement. Falls back to the constant when there is no mark — never a
    guess dressed as a measurement, and never zero, which would open a series
    nobody can trade into.
    """
    from .desk import collateral_for_full_book
    from .onchain import get_reader

    mark = (get_reader().read(index_id) or {}).get("value")
    if not mark:
        return ROLL_COLLATERAL
    mult = int(os.environ.get("ROLL_MULT", "10"))
    want = collateral_for_full_book(float(mark), mult, 2000)
    # Never below a floor that pays for its own gas, never above the ACR-INF
    # sized constant — this decides real money on a cron with no human watching.
    return max(0.05, min(want, ROLL_COLLATERAL))


def roll_budget_ok(gas_usdc: float, collateral: float | None = None) -> bool:
    """Whether one wallet can afford BOTH the collateral and the gas.

    On Arc they come out of the same balance, so checking them separately is
    how you open a series you then cannot collateralize — and an
    uncollateralized series is a desk that looks live and reverts on first
    contact, which is worse than no roll at all.
    """
    return gas_usdc >= GAS_FLOOR_USDC + (ROLL_COLLATERAL if collateral is None else collateral)


def live_indices(
    all_series: list[dict], now_chain: int, allow: list[str] | None = None
) -> list[str]:
    """Indices with an unsettled, unexpired series — DERIVED, never a list.

    The venue's shape is a fact about the chain, not configuration. Hard-coding
    it is what broke the old rotation: two hours in three the cron picked an
    index that had no series, failed, and buried a real outage in the noise.
    Here an index without a series is simply absent from the roster, so that
    failure mode is unreachable rather than merely unlikely.

    Ordered by ``allow`` when given (so an operator can pin the rotation without
    a deploy), then by index id, so the cursor advances deterministically under
    test rather than however the chain happened to return the series.
    """
    live = {
        s["index_id"]
        for s in all_series
        if not s.get("settled") and int(s.get("expiry_ts") or 0) > now_chain
    }
    if allow:
        return [i for i in allow if i in live]
    return sorted(live)


def maker_inventory_or_none(desk: dict | None) -> float | None:
    """The maker's position from a DESK read, or None when the field is absent.

    Exists because `.get("maker_inventory", 0.0)` cost the venue eleven hours of
    trading. `descale_series` carries no such field, so sizing off
    `read_all_series()` silently believed the maker was FLAT — and `feasible_qty`
    clamps against the auto-mirrored maker's margin as well as the taker's. Once
    the maker had gone short 2.31 contracts, every trade exceeded the maker-side
    cap and reverted, while the default made the wrong number look like a read
    one. "The maker is flat" and "we did not read the maker" size to very
    different trades; only one of them is safe to guess.
    """
    if not desk:
        return None
    v = desk.get("maker_inventory")
    return None if v is None else float(v)


def _custody_signer(role: str):
    """The role's signer, but ONLY if it is Circle custody.

    Returning a local-key signer here would quietly reintroduce the thing this
    module exists to remove, and it would do it on a host that has an ambient
    ``ACR_POSTER_PRIVATE_KEY`` lying around in some deployments.
    """
    from acr_oracle_client import build_role_signer

    sg = build_role_signer(role, get_settings())
    return sg if sg is not None and type(sg).__name__ == "CircleWalletSigner" else None


def heartbeat_once(futures) -> str | None:
    """One mean-reverting taker fill, so the public tape keeps moving.

    Returns a short verdict for the log, or None when it did nothing. Sized
    against the desk's own ``feasible_qty`` so it can never ask for a fill the
    contract would revert — the same clamp a reader gets.
    """
    global _last_heartbeat
    if not enabled():
        return None
    now = time.time()
    if now - _last_heartbeat < HEARTBEAT_EVERY_S:
        return None

    signer = _custody_signer("taker")
    if signer is None:
        return None
    _last_heartbeat = now

    from acr_oracle_client import FuturesClient
    from acr_oracle_client.futures import _rpc_retry, collateral_or_none

    from .desk import feasible_qty

    s = get_settings()
    fc = FuturesClient(
        rpc_url=s.arc_rpc_url, futures_address=s.futures_address, signer=signer
    )
    me = signer.address
    # read_desk, NOT read_all_series. `descale_series` carries no
    # `maker_inventory` field, so sizing off a raw series silently believes the
    # maker is FLAT — and `feasible_qty` clamps against the auto-mirrored
    # maker's margin too. This shipped: the keeper traded twice while both
    # sides were near flat, then every trade reverted once the maker had gone
    # short 2.31 contracts, because the cap it computed was for a maker holding
    # nothing. A missing dict key defaulted to 0.0 and read as a fact.
    # WHICH index this tick trades is derived from the chain, then advanced by
    # a CURSOR rather than the wall-clock hour: Render restarts and dropped
    # ticks make hour-based rotation skip indices unevenly, and a cursor is
    # deterministic under test.
    global _hb_cursor
    w3 = fc._connect()
    if w3 is None:
        return "no chain — skipped"
    now_chain = int(_rpc_retry(lambda: w3.eth.get_block("latest"))["timestamp"])
    roster = live_indices(fc.read_all_series(), now_chain, INDEX_ALLOW)
    if not roster:
        return "no live series on any index"
    index_id = roster[_hb_cursor % len(roster)]
    _hb_cursor += 1

    series = futures.read_desk(index_id)
    if not series or series.get("settled"):
        return f"no live series on {index_id}"

    sid, mult = series["series_id"], series["multiplier"]
    mine = collateral_or_none(fc, sid, me)
    maker_coll = collateral_or_none(fc, sid, series["maker"])
    # A throttled read is not an empty account, and trading on the assumption
    # that it is has cost this project real money twice.
    if mine is None or maker_coll is None:
        return "chain would not say — skipped"
    if mine <= 0:
        # The taker must hold collateral on EVERY series it rotates onto, or
        # this silently no-ops on that index while every dashboard stays green.
        return f"no taker collateral on {index_id} series {sid} — operator must provision"

    from .onchain import get_reader

    mark = (get_reader().read(index_id) or {}).get("value")
    if not mark:
        return f"no mark for {index_id}"
    pos = (fc.position_of(sid, me) or {}).get("contracts", 0.0)
    maker_inv = maker_inventory_or_none(series)
    if maker_inv is None:
        return "maker inventory unreadable — skipped"
    max_buy, max_sell = feasible_qty(mark, mult, 2000, mine, pos, maker_coll, maker_inv)
    # Mean-revert around flat: sell when long, buy when short. Keeps the tape
    # alive without accumulating a position nobody asked for.
    want = -TRADE_QTY if pos > 0 else TRADE_QTY
    room = max_buy if want > 0 else max_sell
    if room <= 0:
        return "no room on the book"
    qty = round(min(abs(want), room) * (1 if want > 0 else -1), 2)
    if abs(qty) < 0.01:
        return "clamped below the minimum"
    try:
        fc.trade(sid, qty)
    except Exception as exc:  # noqa: BLE001 — the reason matters more than the trace
        return f"trade {qty:+.2f} REVERTED on {index_id} series {sid}: {str(exc)[:100]}"
    return f"traded {qty:+.2f} on {index_id} series {sid}"


def roll_if_needed(futures) -> str | None:
    """Open a successor series before the live one expires.

    Deliberately thin: it defers to the SAME arithmetic ``scripts/futures_roll``
    uses rather than re-deriving when a roll is due, so the cron path and this
    one can never disagree about whether the venue needs rolling.
    """
    global _last_roll_check
    if not enabled():
        return None
    now = time.time()
    if now - _last_roll_check < ROLL_CHECK_EVERY_S:
        return None
    _last_roll_check = now

    signer = _custody_signer("maker")
    if signer is None:
        return None

    from acr_oracle_client import FuturesClient
    from acr_oracle_client.futures import _rpc_retry, collateral_or_none

    s = get_settings()
    fc = FuturesClient(
        rpc_url=s.arc_rpc_url, futures_address=s.futures_address, signer=signer
    )
    min_life_h = float(os.environ.get("ROLL_MIN_LIFE_H", "72"))
    w3 = fc._connect()
    if w3 is None:
        return None
    now_chain = int(_rpc_retry(lambda: w3.eth.get_block("latest"))["timestamp"])
    all_series = fc.read_all_series()

    # Per-index ANY(healthy), not "the first healthy series wins".
    #
    # The old loop returned None on the first healthy series it met, whatever
    # index it belonged to — so the answer depended on the order the chain
    # returned them. Series 1 and 2 expire 8 and 9 Aug, INSIDE judging week, and
    # the venue only stayed quiet because series 3 happened to come last. With
    # three indices that ordering accident becomes a wrong answer for two of
    # them.
    want = INDEX_ALLOW or sorted({s["index_id"] for s in all_series})
    due: list[str] = []
    for idx in want:
        healthy = False
        for existing in all_series:
            if existing["index_id"] != idx or existing["settled"]:
                continue
            hours_left = (existing["expiry_ts"] - now_chain) / 3600
            funded = collateral_or_none(fc, existing["series_id"], signer.address)
            if funded is None:
                return "collateral unreadable — refusing to roll on a guess"
            if hours_left > min_life_h and funded > 0:
                healthy = True
                break
        if not healthy:
            due.append(idx)
    if not due:
        return None  # every index healthy; nothing to do and nothing to log
    # At most ONE roll per check, so a single tick can never spend three rolls'
    # worth of collateral. The next cooldown picks up the next one.
    index_id = due[0]

    # Something needs rolling.
    #
    # `openSeries` is onlyOwner, and until 2026-08-03 the owner was the retiring
    # EOA — a key this process does not hold and should not — so this could only
    # shout for a human. Ownership now sits with the maker's own Circle wallet
    # (verified on-chain: owner 0x9D44A7Dd…, pendingOwner zero), which is what
    # makes an unattended roll possible at all.
    gas = _rpc_retry(w3.eth.get_balance, w3.to_checksum_address(signer.address)) / 1e18
    # Collateral AND gas come out of one balance on Arc, so check for both up
    # front rather than opening a series we then cannot collateralize — an
    # uncollateralized series is a desk that looks live and reverts on first
    # contact, which is worse than no roll at all.
    # Size the stake to the index being rolled. Margin scales with the mark, so
    # a flat 1.50 is roughly right for ACR-INF (mark ~0.49) and ~30x more than
    # ACR-GPU needs (mark ~0.011) — over-funding one book starves another.
    stake = roll_collateral_for(fc, index_id)
    if not roll_budget_ok(gas, stake):
        return (f"maker holds {gas:.2f} USDC; rolling {index_id} needs {stake:.2f} "
                f"collateral + {GAS_FLOOR_USDC:.2f} gas floor — standing down")

    owner = _rpc_retry(
        w3.eth.contract(
            address=w3.to_checksum_address(s.futures_address), abi=_OWNER_ABI
        ).functions.owner().call
    )
    if not may_open_series(owner, signer.address):
        # Not ours to open. Refuse loudly instead of sending a transaction that
        # reverts and burns gas every cooldown.
        return f"ROLL DUE but the venue is owned by {owner} — run `make futures-roll`"

    expiry = now_chain + int(float(os.environ.get("ROLL_EXPIRY_DAYS", "14")) * 86400)
    fc.open_series(index_id, expiry, int(os.environ.get("ROLL_MULT", "10")), signer.address)
    series = fc.read_all_series()
    sid = len(series) - 1

    # Post collateral, or the successor is a series nobody can trade into.
    allowance = fc.allowance_units(USDC_ADDRESS, signer.address)
    if allowance is not None and allowance < int(stake * 1_000_000):
        fc.approve_venue(USDC_ADDRESS)
        time.sleep(2)
    fc.post_collateral(sid, stake)

    # Assert it landed. `futures_seed` exits 0 on an uncollateralized series and
    # that is exactly the failure worth refusing to repeat here.
    posted = collateral_or_none(fc, sid, signer.address)
    if not posted:
        return f"opened series {sid} but collateral did NOT land — the desk needs a human"
    return (f"rolled {index_id} to series {sid} "
            f"(+{os.environ.get('ROLL_EXPIRY_DAYS', '14')}d), {posted:.2f} USDC posted")


def mirror_once(_futures=None, *, force: bool = False) -> str | None:
    """Put freshly settled receipts on chain, so the subgraph has a tape.

    Takes an unused ``_futures`` argument to match the other chores' signature —
    it has no venue dependency at all, which is exactly why it must NOT be
    called from inside the warm loop's ``if futures.configured:`` branch. A
    deployment with no futures address would otherwise mirror nothing and say
    nothing about it.

    Cooldown is stamped before the work, like the other chores: a keeper that
    retried a failing mirror every tick would spend the press's gas on it.
    ``force`` is the settlement-triggered pass (`kick_mirror`), which skips the
    cooldown — a receipt that exists only in memory until the next tick is lost
    to any restart inside that window, and Circle has already settled it.
    """
    global _last_mirror
    if not enabled():
        return None
    now = time.time()
    if not force and now - _last_mirror < MIRROR_EVERY_S:
        return None
    if not _mirror_lock.acquire(blocking=False):
        return None  # a pass is already running; it will see the same receipts

    try:
        from acr_oracle_client import MirrorClient

        client = MirrorClient()
        if not client.configured():
            return None
        _last_mirror = now

        from .mirror import mirror_once as _mirror
        from .x402 import get_facilitator

        return _mirror(list(get_facilitator().recent), client=client)
    finally:
        _mirror_lock.release()


def humans_once(_futures=None, *, force: bool = False) -> str | None:
    """Resolve this rotation window's humans, once, with the press's own signer.

    The same loop the script runs (`acr_oracle_client.humanid.resolve_window`),
    so a wallet already on chain is skipped and a rerun after a partial failure
    finishes the job. Signs only through Circle custody (`_custody_signer`), like
    every chore here: a raw key on the host is refused, not borrowed.

    Cost: while the host clock says the resolved window still runs, nothing is
    read at all. Near a boundary the CHAIN's window decides (`record` requires
    `window == currentWindow()`), so a host clock running ahead only means
    asking again next time.
    """
    global _last_humans, _humans_done_window
    if not humans_enabled():
        return None
    from acr_oracle_client import humanid

    if _humans_done_window is not None and humanid.current_window() == _humans_done_window:
        return None
    now = time.time()
    if not force and now - _last_humans < HUMANS_EVERY_S:
        return None
    if not _humans_lock.acquire(blocking=False):
        return None
    try:
        s = get_settings()
        _last_humans = now  # stamped before the work, so a failing chore cannot spin
        if not (s.humanid_mirror_address or "").strip() or not (s.humanid_salt or "").strip():
            # This service opted in, so silence would hide the gap for weeks.
            return "not resolving: ACR_HUMANID_MIRROR_ADDRESS or ACR_HUMANID_SALT is not set on this service"
        signer = _custody_signer("poster")
        if signer is None:
            return "not resolving: the poster is not a Circle custody wallet (the keeper never signs with a raw key)"
        from acr_oracle_client import agentbook

        client = humanid.HumanIdMirrorClient(settings=s, signer=signer)
        window = client.chain_window()
        if window is None:
            return "not resolving: the mirror could not be read"
        if window == _humans_done_window:
            return None
        if client.signer_authorized() is False:
            return f"not resolving: {signer.address} is not in the mirror's signer set"
        if client.salt_matches(s.humanid_salt) is False:
            return "not resolving: the salt (ACR_HUMANID_SALT) does not hash to the mirror's SALT_COMMITMENT"
        pairs = humanid.roster_pairs(agentbook.build_agentbook(s))
        if pairs is None:
            return "not resolving: this AgentBook cannot list its humans"
        results = list(humanid.resolve_window(client, pairs, s.humanid_salt, window, dry_run=False))
        recorded = sum(r.status == "recorded" for r in results)
        already = len(results) - recorded
        _humans_done_window = window
        if recorded == 0:
            return f"window {window}: all {already} wallet(s) already resolved"
        return f"resolved {recorded} wallet(s) for window {window}" + (
            f" ({already} already on chain)" if already else ""
        )
    finally:
        _humans_lock.release()


def kick_mirror(delay_s: float | None = None) -> bool:
    """Schedule a mirror pass a few seconds from now, coalescing a burst into one.

    Called by the facilitator the moment a REAL settlement is recorded. Until
    this existed a receipt waited for the keeper's next tick (every 120 s, eight
    per tick); a restart inside that window — a Render rollover, a free-tier
    nap — dropped it from the tape permanently while Circle had settled the
    money. Measured on 2026-09-13: twelve real settlements, gone in five minutes.

    Returns False when there is no running event loop (a sync test, a script),
    which is the right answer there: nothing is lost, the tick still runs.
    Never raises: the payment has already succeeded and must not be told
    otherwise by a scheduling detail.
    """
    global _mirror_kick
    if not enabled():
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False
    if _mirror_kick is not None and not _mirror_kick.done():
        return True  # already pending — this settlement rides in the same pass

    async def _later() -> None:
        await asyncio.sleep(MIRROR_KICK_DELAY_S if delay_s is None else delay_s)
        try:
            verdict = await asyncio.to_thread(mirror_once, None, force=True)
            record("mirror", verdict)
            if verdict:
                log.info("keeper mirror (settlement-triggered): %s", verdict)
        except Exception as exc:  # noqa: BLE001 — a chore must never cost a beat
            record("mirror", f"failed: {exc}")
            log.warning("settlement-triggered mirror failed", exc_info=True)

    _mirror_kick = loop.create_task(_later())
    return True
