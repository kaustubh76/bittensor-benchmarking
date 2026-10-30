"""The systems ledger — what the operator knows, served instead of typed.

``scripts/verify_live.py`` answers "is the thing on the internet working right
now?" by probing the deployment from OUTSIDE, over HTTP, from a laptop. That is
the right shape for a gate and it stays exactly as it is: unchanged, still the
CI/``make verify-live`` path, still the only thing that can catch a Vercel route
serving a 500 or a cron GitHub silently dropped.

This module answers a different question — "what does the press itself
currently believe?" — from INSIDE the process, over state it already holds.
The distinction matters and is not a technicality:

  * An in-process checker must not HTTP-probe its own API. A check that curls
    ``/prints`` from the process serving ``/prints`` proves the socket is open
    and nothing else; on a free-tier host it also competes with the requests it
    is meant to be reassuring you about.
  * The expensive reads (venue scan, oracle prints) are ALREADY memoized by the
    warm loop. Reporting off those memos costs approximately nothing, where a
    fresh probe of each would be the most expensive thing the service does.
  * verify_live must fail loudly on a laptop. This must never fail at all — it
    is a dashboard, and a dashboard that can take the press down with it is
    worse than no dashboard.

So: no writes, no chain calls of its own beyond the shared readers, every
section wrapped, and an honest ``unknown`` wherever a read did not land. Unread
is never rendered as zero — that is the same rule the Terminal lives by, and it
is exactly as load-bearing here, where a zeroed panel reads as an outage.
"""

from __future__ import annotations

import logging
import os
import statistics
import time

log = logging.getLogger("acr.ops")

#: A print older than this cannot settle the venue at all — ACRFutures'
#: MAX_SETTLE_AGE. Mirrors scripts/verify_live.py's PRINT_MAX_AGE_S so the
#: dashboard and the gate cannot disagree about what "broken" means.
PRINT_MAX_AGE_S = float(os.environ.get("VERIFY_PRINT_MAX_AGE_S", "7200"))
#: The press posts hourly, so 90 minutes means a slot was already missed and
#: the settle window is in sight. Warn, don't fail.
PRINT_WARN_AGE_S = float(os.environ.get("VERIFY_PRINT_WARN_AGE_S", "5400"))
#: Below this a roll fails on its own budget guard — a silent expiry.
VENUE_WALLET_FLOOR_USDC = float(os.environ.get("VERIFY_VENUE_FLOOR_USDC", "2.5"))
TAKER_WALLET_FLOOR_USDC = float(os.environ.get("VERIFY_TAKER_FLOOR_USDC", "1.0"))
#: How long an obligation may wait on a person before the queue is reported as
#: a problem rather than as the product working. An escalation IS the design —
#: the agent reaching its authority and stopping — so a fresh queue is a pass.
#: A day old means nobody is reading it, which is an operator fact and the only
#: thing this check can honestly claim.
OPERATOR_QUEUE_STALE_H = float(os.environ.get("ACR_OPS_QUEUE_STALE_H", "24"))

#: How often the background loop recomputes. A full pass is cheap off the warm
#: memos, but it is still not something to do per request.
OPS_VERIFY_S = float(os.environ.get("ACR_OPS_VERIFY_S", "900"))


class Recorder:
    """Collects one section's checks.

    ``ok=None`` is a first-class verdict and the reason this is not a bool:
    "I could not read this" is different from "this is wrong", and collapsing
    the two is how a dashboard starts lying. It renders as its own tier.
    """

    def __init__(self) -> None:
        self.sections: list[dict] = []
        self._current: dict | None = None

    def section(self, name: str, title: str) -> None:
        self._current = {"name": name, "title": title, "checks": []}
        self.sections.append(self._current)

    def check(self, ok: bool | None, label: str, *, warn_only: bool = False,
              detail: str | None = None) -> None:
        if self._current is None:
            self.section("misc", "Other")
        cur = self._current
        if cur is None:  # pragma: no cover - section() always assigns
            return
        # Coerce, because `tally()` discriminates with `is None` / `is False`.
        # A falsy non-bool — 0, "", [] — would satisfy neither branch and be
        # counted as neither pass, warning nor unknown: it would vanish from
        # the verdict while still rendering as a failure in the UI. Miscounting
        # is precisely how a dashboard starts lying.
        cur["checks"].append(
            {
                "ok": None if ok is None else bool(ok),
                "label": label,
                "detail": detail,
                # A warning is something that depends on somebody else's
                # scheduler or budget rather than on our code being correct.
                "warn": bool(warn_only),
            }
        )

    def unknown(self, label: str, why: str) -> None:
        """A read that did not land. Named, so it can never be mistaken for a
        passing check that happens to have no number attached."""
        self.check(None, label, warn_only=True, detail=why)

    def tally(self) -> tuple[int, int, int]:
        failures = warnings = unknowns = 0
        for s in self.sections:
            for c in s["checks"]:
                if c["ok"] is None:
                    unknowns += 1
                elif c["ok"] is False:
                    if c["warn"]:
                        warnings += 1
                    else:
                        failures += 1
        return failures, warnings, unknowns


def _guard(rec: Recorder, name: str, title: str, fn) -> None:
    """Run one section, converting a crash into a recorded unknown.

    Every read here can meet Arc's 429. An unguarded one would take the whole
    dashboard down with a traceback — the single worst outcome, because a page
    that 500s reads as "everything is broken" when the truth was "one RPC was
    throttled for a second".
    """
    rec.section(name, title)
    try:
        fn(rec)
    except Exception as exc:  # noqa: BLE001 — a verdict beats a traceback
        log.warning("ops section %s failed", name, exc_info=True)
        rec.unknown(f"{title.lower()} could not be read", str(exc)[:120])


# --- sections ---------------------------------------------------------------


def _oracle(rec: Recorder) -> None:
    from .onchain import get_reader

    reader = get_reader()
    if not reader.configured:
        rec.unknown("oracle configured", "no ACR_ORACLE_ADDRESS set")
        return
    prints = reader.read_all()
    rec.check(bool(prints), f"prints readable on chain ({len(prints)} index(es))")
    now = time.time()
    for iid, p in sorted(prints.items()):
        posted = float(p.get("posted_at") or p.get("timestamp") or 0)
        if not posted:
            rec.unknown(f"{iid}: print age", "no timestamp on the latest print")
            continue
        age = now - posted
        mins = age / 60
        # Two thresholds, because they mean genuinely different things: past
        # the warn line the press has missed a slot; past the hard line the
        # contract will refuse to settle against this print at all.
        rec.check(
            age <= PRINT_WARN_AGE_S if age <= PRINT_MAX_AGE_S else False,
            f"{iid}: last print {mins:.0f} min ago",
            warn_only=age <= PRINT_MAX_AGE_S,
            detail=(
                "past the settle window — the venue cannot settle against this"
                if age > PRINT_MAX_AGE_S
                else "the press posts hourly" if age > PRINT_WARN_AGE_S
                else None
            ),
        )


MEMORY_FLOOR_FRAC = 0.8


def rss_mib() -> float | None:
    """The instance's resident memory, via the memory module (kept as a name
    here because /health and the tests read it from ops)."""
    from .memory import rss_mib as _rss

    return _rss()


def _memory(rec: Recorder) -> None:
    from acr_tape.graph_client import cache_bytes

    from . import memory

    mib = rss_mib()
    if mib is None:
        rec.check(None, "memory: unreadable on this platform")
        return
    limit = memory.MEMORY_LIMIT_MIB
    frac = mib / limit
    rec.check(
        frac < MEMORY_FLOOR_FRAC,
        f"memory: {mib:.0f} MiB of {limit:.0f} ({frac:.0%}) · graph cache {cache_bytes() / 1_048_576:.1f} MiB",
        detail=None if frac < MEMORY_FLOOR_FRAC
        else "the instance is OOM-killed at the limit and every memory-only receipt with it; "
             "the settlement-triggered mirror is the backstop, not a reason to ignore this",
    )
    a = memory.last_action
    if a:
        rec.check(True, f"memory guard last acted {(time.time() - a['at']) / 60:.0f} min ago: "
                        f"{a['before_mib']} → {a['after_mib']} MiB, freed {a['freed_mib']} MiB",
                  warn_only=True, detail="past 90% the warm tick drops the graph cache and returns freed heap to the OS")


def _press(rec: Recorder) -> None:
    from .app import get_poster

    _memory(rec)
    poster = get_poster()
    posts = getattr(poster, "last_posts", {}) or {}
    landed = [e for e in posts.values() if e.get("tx")]
    if not posts:
        rec.unknown("poster provenance", "the press has not posted this boot")
        return
    rec.check(bool(landed), f"posts landed on chain this boot: {len(landed)}")
    newest = max(landed, key=lambda e: e.get("at_wall") or 0.0) if landed else None
    if newest:
        rec.check(True, f"last tx {str(newest.get('tx'))[:18]}…",
                  detail=f"{(time.time() - float(newest.get('at_wall') or 0)) / 60:.0f} min ago")


#: How far back the cadence check reaches. 16 pages × 14000 blocks ≈ 32h at
#: Arc's measured 0.510s block time — matching scripts/print_gaps.py's own
#: default. Deliberately larger than ACR_TAPE_PAGES (4): the tape wants the
#: last few hours cheaply, this wants the worst hole in a day, and one hole is
#: the whole point. Measured cost at 24 pages was ~115s of the sweep; 16 keeps
#: a full day of reach without making the first pass after boot a two-minute
#: wait.
GAP_PAGES = int(os.environ.get("ACR_OPS_GAP_PAGES", "16"))
GAP_LIMIT = int(os.environ.get("ACR_OPS_GAP_LIMIT", "400"))


def _cadence(rec: Recorder) -> None:
    """The gap between press runs — the TAIL, not the average.

    ``make print-gaps`` measures this from a laptop and its result is a claim in
    hackathon/arc-circle-2026/SUBMISSION.md §5; this is the same measurement standing up on the page.

    It reports what that script reports — n / median / max / over-window —
    rather than a histogram, because ``summarize()`` has no buckets and any bin
    edges invented here would be invented evidence. The only two thresholds
    that exist in this codebase are the ones above: 90 minutes means a slot was
    missed, 120 means the venue cannot settle against the print at all.

    The average is deliberately not the check. One 216-minute hole left the
    venue unsettleable for an hour, and a healthy-looking 70-minute mean hid it
    completely — which is why the count over the window is what fails.
    """
    from acr_oracle_client.cadence import index_gaps_min, press_runs

    from .onchain import get_reader

    reader = get_reader()
    if not reader.configured:
        rec.unknown("press cadence", "no ACR_ORACLE_ADDRESS set")
        return
    # recent_posts() returns [] for BOTH "nothing on chain" and "the RPC
    # refused us". Those are different verdicts and collapsing them would let a
    # throttled read render as a clean cadence — the exact failure this module
    # exists to refuse.
    posts = reader._client.recent_posts(limit=GAP_LIMIT, pages=GAP_PAGES)
    if len(posts) < 2:
        rec.unknown("press cadence", f"fewer than two prints in reach ({len(posts)})")
        return

    runs = press_runs(posts)
    if len(runs) < 2:
        rec.unknown("press cadence", f"only {len(runs)} press run in reach")
        return
    gaps = [(runs[i][0] - runs[i - 1][0]) / 60 for i in range(1, len(runs))]
    span_h = (runs[-1][0] - runs[0][0]) / 3600
    over = [g for g in gaps if g > PRINT_MAX_AGE_S / 60]
    worst = max(gaps)

    # warn_only, deliberately. A breach that has already recovered is HISTORY,
    # and this page reports what is true now; `_oracle` above is what fails
    # when the CURRENT print is too old to settle against. Conflating them
    # would leave the headline stuck on "failed" for a day and a half after a
    # single hole, which trains an operator to ignore it. `make print-gaps` is
    # the gate and still exits 1 — a gate should be louder than a dashboard.
    rec.check(
        not over,
        f"press runs: {len(runs)} over {span_h:.1f}h, worst gap {worst:.0f} min",
        warn_only=True,
        detail=(
            f"{len(over)} gap(s) past the {PRINT_MAX_AGE_S / 60:.0f}-min settle window "
            f"— the venue could not have settled then"
            if over
            else f"median {statistics.median(gaps):.0f} min"
        ),
    )
    # Per index, because a series settles against ITS index: one pressed every
    # third run is staler than the run cadence implies.
    for iid in sorted({str(p["index_id"]) for p in posts}):
        g = index_gaps_min(posts, iid)
        if not g:
            continue
        iid_over = [x for x in g if x > PRINT_MAX_AGE_S / 60]
        rec.check(
            not iid_over,
            f"{iid}: worst gap {max(g):.0f} min",
            warn_only=True,
            detail=f"{len(iid_over)} past the settle window" if iid_over else None,
        )


def _keeper(rec: Recorder) -> None:
    from . import keeper

    st = keeper.status()
    if not st.get("enabled"):
        # Deliberately off is a configuration, not a fault. Saying "disabled"
        # rather than failing is the difference between a dashboard people
        # trust and one they learn to ignore.
        rec.check(True, "keeper disabled by configuration", detail="ACR_KEEPER=0")
        return
    for chore in ("heartbeat", "roll"):
        c = st.get(chore) or {}
        age = c.get("checked_age_s")
        if age is None:
            rec.unknown(f"{chore}: last checked", "not seen since boot")
            continue
        # The chore runs on the 60s warm loop, so anything past a few minutes
        # means the loop itself stopped — a much bigger deal than a cooldown.
        rec.check(
            age < 300,
            f"{chore}: checked {age / 60:.0f} min ago",
            warn_only=True,
            detail=str(c.get("verdict") or "on cooldown, nothing to do"),
        )

    # The humans chore, only where a service turned it on: absent elsewhere,
    # because "not asked for" is a configuration and must not read as a fault.
    h = st.get("humans") or {}
    if h.get("on"):
        age = h.get("checked_age_s")
        verdict = str(h.get("verdict") or "")
        if age is None:
            rec.unknown("humans: last checked", "not seen since boot (the first look comes a few minutes in)")
        elif verdict.startswith(("not resolving", "failed")):
            # A refusal repeats every window until someone acts, so it is said in full.
            rec.check(False, f"humans: checked {age / 60:.0f} min ago and NOT resolving", warn_only=True,
                      detail=verdict)
        else:
            rec.check(age < 3 * keeper.HUMANS_TICK_S, f"humans: checked {age / 60:.0f} min ago", warn_only=True,
                      detail=verdict or "no verdict yet (the first look comes a few minutes after boot)")


def _venue(rec: Recorder) -> None:
    from .onchain import get_futures

    fut = get_futures()
    if not fut.configured:
        rec.unknown("venue configured", "no ACR_FUTURES_ADDRESS set")
        return
    series = fut.all_series()
    if not series:
        rec.unknown("series readable", "the venue scan returned nothing")
        return
    now = time.time()
    live = [s for s in series if not s["settled"] and s["expiry_ts"] > now]
    expired_unsettled = [s for s in series if not s["settled"] and s["expiry_ts"] <= now]
    rec.check(bool(live), f"live series: {len(live)} of {len(series)}")
    for s in live:
        hrs = (s["expiry_ts"] - now) / 3600
        rec.check(
            hrs > 2,
            f"#{s['series_id']} {s['index_id']}: {hrs:.1f}h to expiry",
            warn_only=True,
            detail="a roll is due" if hrs <= 2 else None,
        )
    # Expired-and-unsettled is the one venue state a visitor actually feels:
    # the book stops and the exit stays shut until somebody rings the bell.
    for s in expired_unsettled:
        rec.check(
            False,
            f"#{s['series_id']} {s['index_id']}: expired, not settled",
            warn_only=True,
            detail="anyone may settle it: see the desk",
        )


def _tape(rec: Recorder) -> None:
    from acr_core import get_settings

    s = get_settings()
    src = s.tape_source
    # The one disclosure that has to survive a fully-live state: every other
    # badge reports CONNECTION tier, so a simulated flow under a real print
    # would otherwise read as entirely real.
    rec.check(
        src != "sim",
        f"tape source: {src}",
        warn_only=True,
        detail="estimator, signature and print are real; the flow underneath is simulated"
        if src == "sim"
        else None,
    )
    _subgraph_transport(rec, s.subgraph_url)


def _subgraph_transport(rec: Recorder, url: str) -> None:
    """Every subgraph query this press made, and which path carried it.

    Studio's development URL is capped at 3,000 queries a day and its dashboard
    does not count them; the gateway (with an API key) is counted and billed. So
    the console says which one is in use and, on the dev URL, whether the pace
    would cross the cap — the fact that would otherwise arrive as a tape that
    stops answering at 4 pm.
    """
    from acr_tape.graph_client import transport_info

    t = transport_info(url)
    if t["via"] == "unset":
        rec.check(None, "subgraph queries: no ACR_SUBGRAPH_URL")
        return
    pace = t.get("pace_per_day")
    head = f"subgraph queries this boot: {t['queries']} ({t['cache_hits']} cached, {t['errors']} errors) · via {t['via']}"
    dev_detail = (f"{t['host']} · Studio's development URL: {t['daily_cap'] or 0:,}/day, counted on no dashboard. "
                  "Publish and switch to the gateway with an API key (docs/GRAPH-RUNBOOK.md step 6)")
    if t.get("measuring"):
        # Under ten minutes of uptime a deploy's first-paint burst is all there is
        # to measure, and a number from it is a wrong number. Unknown, not a pass.
        rec.check(None, f"{head} · pace: measuring (under 10 min of uptime)",
                  detail=dev_detail if t["via"] == "studio-dev" else t["host"])
        return
    label = f"{head} · {t['last_hour']} in the last hour, ~{pace:,}/day at that pace"
    if t["via"] == "studio-dev":
        over = pace > 0.7 * t["daily_cap"]
        rec.check(not over, label, warn_only=True, detail=dev_detail)
    else:
        rec.check(True, label, detail=f"{t['host']} · counted on the API key's usage page in Subgraph Studio")


def _gate(rec: Recorder) -> None:
    from .app import get_facilitator
    from .x402 import CircleFacilitator

    circle = isinstance(get_facilitator(), CircleFacilitator)
    rec.check(True, f"payment gate: {'circle' if circle else 'dev'}",
              detail=None if circle else "the dev gate accepts a mock header")


def _hedger(rec: Recorder) -> None:
    from .app import get_facilitator
    from .hedger import build_hedger_state
    from .marketplace import build_receipts
    from .onchain import get_futures

    # The ledger was never passed, so the label "chain + ledger" was half true
    # and the spend leg of the loop was invisible to the operator. Same
    # facilitator `_gate` already reaches for, resolved the same way.
    st = build_hedger_state(get_futures(), build_receipts(get_facilitator()))
    # `if not st` could never fire — build_hedger_state always returns a
    # populated dict — so the honest question is whether an agent is CONFIGURED.
    if not st.get("configured"):
        rec.unknown("hedger standing", "no agent address (ACR_HEDGER_ADDRESS or .env)")
        return
    rec.check(True, "hedger standing derived from chain + ledger")

    # This read was `st["position"]`. The payload has always said
    # `position_contracts`, so the branch was dead and /ops never once reported
    # the one number the agent exists to move. A throttled read is None and
    # stays an unknown — a zeroed position reads as a liquidated agent.
    pos, gap = st.get("position_contracts"), st.get("gap_contracts")
    if pos is None:
        rec.unknown("hedger position", "the venue would not say")
    else:
        on_target = gap is not None and abs(gap) < 0.25
        rec.check(
            on_target,
            f"position: {pos:+.3f} of {st['target_contracts']:+.3f} contracts",
            # An operator-run agent sitting behind its mandate is a schedule
            # fact, not an outage.
            warn_only=True,
            detail=None if on_target
            else (f"gap {gap:+.3f}" if gap is not None else "gap unread"),
        )

    paid = st.get("paid_queries")
    if paid is None:
        rec.unknown("hedger spend", "the receipt ledger was not read")
    else:
        rec.check(paid > 0, f"{paid} paid read(s) for {st['spent_usdc']} USDC",
                  warn_only=True)


def _funding(rec: Recorder) -> None:
    """Wallet runway. The venue's silent-outage class: a roll that fails its
    budget guard looks identical to a venue nobody rolled."""
    from acr_core import get_settings

    from .onchain import get_futures

    s = get_settings()
    try:
        from web3 import Web3

        w3 = Web3(Web3.HTTPProvider(s.arc_rpc_url, request_kwargs={"timeout": 15}))
    except Exception as exc:  # noqa: BLE001
        rec.unknown("wallet balances", f"no RPC: {str(exc)[:60]}")
        return
    from acr_oracle_client import build_role_signer

    from .desk import (
        PRESS_BURN_USDC_PER_DAY,
        PRESS_CRITICAL_FLOOR_USDC,
        post_reservation_usdc,
    )

    # THE PRESS WALLET FIRST, AND BEFORE THE VENUE GATE. It signs every write the
    # service makes — both oracle generations' prints, receipt mirroring, the
    # keeper's heartbeat — so it matters on a deployment with no venue at all, and
    # this section used to not look at it. On 2026-09-12 it ran 0.897 -> 0.006
    # USDC in seven hours; mirroring failed, the next hourly print would have, and
    # the one surface an operator opens said nothing because the wallet was absent
    # from it. The floor is a HARD check, unlike the venue roles below: those
    # failing means a roll is late, this failing means everything stops.
    try:
        sg = build_role_signer("poster", s)
        paddr = getattr(sg, "address", "") if sg else ""
    except Exception as exc:  # noqa: BLE001
        paddr = ""
        rec.unknown("press wallet", f"signer unavailable: {str(exc)[:60]}")
    if paddr:
        try:
            pbal = float(w3.from_wei(w3.eth.get_balance(w3.to_checksum_address(paddr)), "ether"))
        except Exception as exc:  # noqa: BLE001
            rec.unknown("press wallet balance", str(exc)[:60])
        else:
            days = pbal / PRESS_BURN_USDC_PER_DAY if PRESS_BURN_USDC_PER_DAY > 0 else float("inf")
            # The stop line is a post's gas RESERVATION, read off the live base
            # fee — not zero, which is what this sentence used to claim while
            # mainnet sat at 0.005 USDC refusing every post (see desk.py).
            try:
                base_fee = int(w3.eth.get_block("latest")["baseFeePerGas"])
            except Exception:  # noqa: BLE001 - a ledger must still say something
                base_fee = None
            reserve = post_reservation_usdc(base_fee)
            if pbal < reserve:
                detail = (
                    f"posts are being REFUSED NOW — each one must reserve ~{reserve:.4f} USDC up "
                    f"front (gas x 2x base fee) and the wallet holds {pbal:.4f}; far below the "
                    f"{PRESS_CRITICAL_FLOOR_USDC:.1f} USDC critical floor. Prints, mirroring and "
                    "the heartbeat are all stopped until it is funded"
                )
            elif pbal < PRESS_CRITICAL_FLOOR_USDC:
                detail = (
                    f"BELOW the {PRESS_CRITICAL_FLOOR_USDC:.1f} USDC critical floor — about "
                    f"{int(pbal / reserve)} post reservations left. Every write stops at "
                    f"~{reserve:.4f} USDC, not at zero, because a post reserves twice its cost"
                )
            else:
                detail = (
                    f"~{days:.0f} days at the assumed {PRESS_BURN_USDC_PER_DAY}/day; real "
                    "burn scales with mirrored receipts (2026-09-12 ran ~3/day)"
                )
            rec.check(pbal >= PRESS_CRITICAL_FLOOR_USDC, f"press: {pbal:.3f} USDC", detail=detail)

    fut = get_futures()
    if not fut.configured:
        rec.unknown("venue wallet balances", "no venue configured")
        return

    # Resolve each role through the SAME plumbing the keeper signs with, not a
    # separately-configured address. A second source of truth for "which wallet
    # is the maker" is a dashboard that reports a healthy balance for a wallet
    # nothing actually spends from.
    for role, floor in (("maker", VENUE_WALLET_FLOOR_USDC), ("taker", TAKER_WALLET_FLOOR_USDC)):
        try:
            sg = build_role_signer(role, s)
            addr = getattr(sg, "address", "") if sg else ""
        except Exception as exc:  # noqa: BLE001
            rec.unknown(f"{role} wallet", f"signer unavailable: {str(exc)[:60]}")
            continue
        if not addr:
            rec.unknown(f"{role} wallet", "no signer configured for this role")
            continue
        try:
            # USDC is Arc's NATIVE gas token, so the wallet balance IS the
            # native balance — no ERC-20 call needed.
            bal = float(w3.from_wei(w3.eth.get_balance(w3.to_checksum_address(addr)), "ether"))
        except Exception as exc:  # noqa: BLE001
            rec.unknown(f"{role} wallet balance", str(exc)[:60])
            continue
        rec.check(
            bal >= floor,
            f"{role}: {bal:.2f} USDC",
            warn_only=True,
            detail=f"below the {floor:.2f} floor: the next roll may fail its budget guard"
            if bal < floor
            else None,
        )


def _agent(rec: Recorder) -> None:
    """The agent gate and the screen in front of it.

    SEPARATE FROM `_gate` ON PURPOSE. That one answers "who takes the money" and
    says nothing about who is CALLING or what inspects what they send — two
    different gates that an operator has to be able to tell apart. For a while the
    console reported the payment gate and left the agent gate and the screen
    entirely invisible, which is how a screen with no call sites went unnoticed.

    The screen's backend is the load-bearing line. `armor.py`'s own docstring says
    a screen that fell back to its offline floor and a screen inspecting nothing
    look identical from outside, so `local` is reported as a floor rather than
    passed over in silence.
    """
    from .agentgate import get_gate
    from .armor import get_screen, screen_is_live

    gate = get_gate()
    info = gate.info()
    rec.check(True, f"audience: {info['audience']}",
              detail="cards addressed elsewhere are refused; this is what replaces "
                     "verifyingContract in a domain that names no contract")

    verifiable = bool(info.get("human_binding_verifiable"))
    rec.check(
        verifiable,
        f"human tier: {'verifiable' if verifiable else 'UNVERIFIABLE'}",
        warn_only=True,
        detail=None if verifiable
        else "no readable HumanIdMirror, so a human claim is declined rather than "
             "granted — the tier is unreachable, not silently free",
    )
    rec.check(True, f"cards verified: {info['cards_verified']} "
                    f"(human tier granted {info['human_tier_granted']})")

    # THE ROTATION. Cluster ids are keccak(nullifier, salt, window) and the window
    # is seven days, so at 00:00 UTC on the boundary every resolution expires at
    # once: humans.n becomes 0, the People chip says "out of date", every new
    # settlement is stamped human:false, and a human-claiming card is a 401. The
    # re-resolve is a manual chore (`make resolve-humans ARGS=--commit`, it needs
    # the salt) and nothing scheduled does it — so the console FAILS here rather
    # than letting the operator learn it from a chip. Failure, not warning: the
    # human tier and the human-denominated bound are both gone until it runs.
    _humans_this_window(rec, info.get("rotation_window"))

    screen = get_screen()
    live = screen_is_live(screen)
    backend = screen.backend
    rec.check(
        live,
        f"screen backend: {backend}",
        warn_only=True,
        detail=None if live
        else ("offline pattern floor — six substrings, not Model Armor"
              if backend == "local"
              else "screening is switched off by configuration"),
    )
    # The counter that could only ever read zero while nothing called the screen.
    # Reported rather than asserted: a quiet service legitimately has none.
    counts = screen.info()
    rec.check(True, f"inspections: {counts['screened']} ({counts['blocked']} blocked)",
              detail="carded callers on POST /graph/query, both directions"
              if live else None)
    # The evidence that the screen is Google's, not a counter: the regional host
    # every call goes to and when it last answered. A console dashboard reads
    # from Cloud Monitoring and can show zero with this line saying otherwise.
    if live:
        at = counts.get("last_verdict_at")
        age = f"{(time.time() - at) / 60:.0f} min ago · {counts.get('last_latency_ms')} ms" if at else "no call yet this boot"
        rec.check(True, f"google last answered: {age}",
                  detail=f"{counts.get('endpoint')} · {counts.get('template_resource')}")


def _humans_this_window(rec: Recorder, window) -> None:
    from acr_oracle_client.humanid import current_window

    from .tca import _cfg, graph_query

    url, key = _cfg()
    now_window = int(window) if window else current_window()
    if not url:
        rec.check(None, "humans resolved this window: unknown (no subgraph)")
        return
    data = graph_query(
        url,
        "query Humans($w: BigInt!) { humanClusters(where: { window: $w }, first: 50) { id walletCount } }",
        {"w": str(now_window)},
        key,
    )
    if data is None:
        rec.check(None, "humans resolved this window: unknown (subgraph did not answer)")
        return
    clusters = data.get("humanClusters") or []
    wallets = sum(int(c.get("walletCount") or 0) for c in clusters)
    ends_in_h = ((now_window + 1) * 7 * 86400 - time.time()) / 3600
    rec.check(
        len(clusters) > 0,
        f"humans resolved this window: {len(clusters)} cluster(s), {wallets} wallet(s) · window {now_window} "
        f"ends in {ends_in_h:.0f}h",
        detail=None if clusters
        else "the 7-day rotation rolled and nothing re-resolved. A press with ACR_KEEPER_HUMANS=1 does it "
             "itself within the hour (its `keeper.humans` verdict on /health says why not); elsewhere run "
             "`make resolve-humans ARGS=--commit` (needs ACR_HUMANID_SALT). Until then the human tier is "
             "unreachable and new settlements are stamped human:false",
    )


def _operator(rec: Recorder) -> None:
    """The spend operator: its record, its queue, and the wallets it spends from.

    THE HAZARD THIS SECTION EXISTS FOR. Every other section on this page watches
    something on chain or in a subgraph — state that outlives the container.
    The operator's decisions are written to ``data/``, which is untracked AND in
    ``.dockerignore``, so the free tier erases them on every redeploy. Nothing
    was watching that: the record of what the agent decided could go to zero
    between two deploys and this page would have reported the system healthy,
    because everything it looked at was.

    Four checks, in the order the failures actually bite: can it spend at all,
    is there a person it can reach, is anybody waiting on that person, and is
    the record of what it did safe from the next deploy.
    """
    from pathlib import Path

    from . import businesses as reg
    from .operator import LOG_PATH
    from .statement import ARCHIVE_PATH, decision_key, pending_escalations, read_decisions

    # --- can it spend, and from a wallet that is really there ---------------
    #
    # `wallet_status` does an `eth_getCode`, deliberately: `Business.chain` is a
    # LABEL, and a label is what went wrong when the testnet→mainnet switch left
    # a mislabelled bundle behind. A CALL to a codeless address does not revert,
    # so an operator pointed at a wallet that exists on the other chain reports
    # a transaction hash for a payment that moved nothing.
    real = reg.real()
    spenders = [b for b in real if b.policy_wallet]
    rec.check(
        bool(real),
        f"{len(real)} real business(es) registered, {len(spenders)} with a wallet to spend from",
        warn_only=True,
        detail=None if real else "no business in businesses.json: nothing to operate for",
    )

    owner_seen = False
    for b in spenders:
        try:
            from acr_oracle_client.policy import PolicyClient

            client = PolicyClient(wallet_address=b.policy_wallet)
            status = client.wallet_status()
            owner_seen = owner_seen or client.can_escalate()
            kinds = client.signer_kinds()
        except Exception as exc:  # noqa: BLE001 — one business must not take the page
            rec.unknown(f"{b.slug}: wallet unread", str(exc)[:160])
            continue
        if status == "no_rpc":
            rec.unknown(f"{b.slug}: wallet unread", "the RPC would not answer eth_getCode")
            continue
        # WHICH CHANNEL THIS BUSINESS'S MONEY GOES OUT THROUGH. Circle's
        # developer-controlled wallet and a raw local key produce identical
        # calldata, so nothing downstream could tell them apart — and five
        # payments went out from a raw EOA while the deck allots a fifth of the
        # score to Circle tool usage. A warning rather than a failure: a raw key
        # pays correctly, it just is not the custody story.
        rec.check(
            kinds.get("agent") == "circle",
            f"{b.slug}: the agent pays through {kinds.get('agent')}"
            + (f", the owner through {kinds.get('owner')}" if kinds.get("owner") != "none" else ""),
            warn_only=True,
            detail=None if kinds.get("agent") == "circle"
            else "set ACR_CIRCLE_TAKER_WALLET_ID and ACR_CIRCLE_API_KEY to spend through "
                 "Circle custody; a raw key works and leaves the keys on the host",
        )
        rec.check(
            status == "ok",
            f"{b.slug}: PolicyWallet {b.policy_wallet[:10]}… {status}",
            detail=None if status == "ok"
            else "a CALL to a codeless address SUCCEEDS, so spending here would return a "
                 "transaction hash for a payment that moved nothing — the client refuses it, "
                 "and the registry's `chain` for this business is the thing to check",
        )

    # --- is there a human it can escalate TO --------------------------------
    if spenders:
        rec.check(
            owner_seen,
            "an owner key is configured" if owner_seen else "no owner key: escalations cannot be cleared",
            # Somebody else's key, not our correctness. The agent still runs
            # correctly without it — it escalates and waits, which is the honest
            # behaviour — so this is a warning and never a failure.
            warn_only=True,
            detail=None if owner_seen
            else "payments at or above the threshold will queue indefinitely: set the owner "
                 "signer (ACR_OWNER_PRIVATE_KEY) or clear them by hand with spendAsOwner",
        )

    # --- is anything actually running it ------------------------------------
    #
    # The question this section could not answer until the operator had a clock:
    # an agent somebody has to trigger is a tool, and "nobody has run it for a
    # week" looked identical to "there was nothing to do".
    from . import operator_keeper

    st = operator_keeper.status()
    armed = st["mode"] != "off"
    rec.check(
        armed,
        f"autorun: {st['mode']}"
        + (f" every {st['every_s'] / 60:.0f} min" if armed else " — every run is a person typing"),
        # Somebody's deployment choice, not our code being wrong. `off` is the
        # default and a legitimate state; it just must not be a silent one.
        warn_only=True,
        detail=None if armed
        else "set ACR_OPERATOR_AUTORUN=dry to prove the loop ticks on this host, "
             "then =live to let it pay what clears policy",
    )
    if armed:
        # A loop that has never reported is the failure this page exists for —
        # and it is not hypothetical on this host, where all three keeper chores
        # read `checked_at: null`.
        if st["checked_at"] is None:
            rec.unknown("autorun has not reported a pass yet", "armed, but no tick has landed")
        else:
            fresh = float(st["checked_age_s"] or 0.0) < float(st["every_s"]) * 2
            rec.check(
                fresh,
                f"last pass {float(st['checked_age_s']):.0f}s ago: {st['verdict'] or 'nothing to do'}",
                warn_only=True,
                detail=None if fresh else "the clock has missed at least one period",
            )

    # --- is anybody waiting on that human -----------------------------------
    #
    # SANDBOXES EXCLUDED, for the reason every traction figure excludes them and
    # one more. The sandbox's escalations are hand-written fixtures that exist so
    # the queue UI has something to render, which means they are DESIGNED to sit
    # there forever: counting them reported three demonstration invoices as an
    # operator backlog, and no age threshold could ever clear them. The first
    # version of this section did exactly that — a demonstration presented as
    # real operation, which is the same error as a traction overclaim pointed the
    # other way.
    slugs = {b.slug for b in real}
    mine = [r for r in read_decisions() if (r.get("business") or "") in slugs]
    queue = pending_escalations(mine)
    oldest_h = (
        (time.time() - min(float(q.get("at") or 0.0) for q in queue)) / 3600 if queue else 0.0
    )
    rec.check(
        oldest_h < OPERATOR_QUEUE_STALE_H,
        f"escalation queue: {len(queue)} obligation(s) waiting on a person"
        + (f", oldest {oldest_h:.1f}h" if queue else "") + ", sandboxes excluded",
        # Whether a human reads their queue is not our code being correct.
        warn_only=True,
        detail=None if oldest_h < OPERATOR_QUEUE_STALE_H
        else f"waiting longer than {OPERATOR_QUEUE_STALE_H:.0f}h, so nobody is reading the "
             "queue — they are on /operator/statement/{business}, and clearing one needs the "
             "owner key",
    )

    # --- is the record safe from the next deploy ----------------------------
    #
    # Sandboxes excluded HERE because the archiver excludes them: counting a
    # sandbox row as at-risk would raise a warning that `make archive-decisions`
    # can never clear, which trains an operator to ignore this line.
    archived = read_decisions(path=ARCHIVE_PATH)
    live = [r for r in read_decisions(path=LOG_PATH) if (r.get("business") or "") in slugs]
    have = {decision_key(r) for r in archived}
    at_risk = [r for r in live if decision_key(r) not in have]
    rec.check(
        not at_risk,
        f"decision record: {len(archived)} archived in the image, {len(live)} on the "
        f"erasable disk, {len(at_risk)} a redeploy would destroy",
        warn_only=True,
        detail=None if not at_risk
        else "run `make archive-decisions` and commit the archive, then rebuild — "
             f"{Path(LOG_PATH)} is in .dockerignore and does not survive a deploy",
    )

    newest = max((float(r.get("at") or 0.0) for r in mine), default=0.0)
    rec.check(
        True,
        f"{len(mine)} decision(s) on the record, newest {(time.time() - newest) / 3600:.1f}h ago"
        if mine
        # Not an unknown: every file was read and none held a decision for a real
        # business. An operator that has decided nothing yet is a legible state,
        # and it is the state of every first deploy.
        else "decision record: nothing decided for a real business yet, and read cleanly",
    )


SECTIONS = [
    ("oracle", "The oracle", _oracle),
    ("press", "The press", _press),
    ("cadence", "Press cadence", _cadence),
    ("keeper", "The keeper", _keeper),
    ("venue", "The venue", _venue),
    ("tape", "The tape", _tape),
    ("gate", "The paid gate", _gate),
    ("agent", "The agent gate", _agent),
    ("hedger", "The hedger", _hedger),
    ("funding", "Wallet runway", _funding),
    ("operator", "The spend operator", _operator),
]


def run_all() -> dict:
    """One full pass. Never raises — a dashboard that can take the press down
    with it is worse than no dashboard."""
    started = time.time()
    rec = Recorder()
    for name, title, fn in SECTIONS:
        _guard(rec, name, title, fn)
    failures, warnings, unknowns = rec.tally()
    return {
        "at": started,
        "duration_s": round(time.time() - started, 2),
        "sections": rec.sections,
        "failures": failures,
        "warnings": warnings,
        "unknowns": unknowns,
        # Four words, because "degraded" and "unreadable" are different operator
        # situations and one of them is not about the product at all.
        #
        # `unknowns` outranks `warnings` deliberately. It used to lose to them,
        # which meant a section that CRASHED (recorded as one unknown) plus any
        # unrelated warning rendered as a plain "degraded" — and the crash, the
        # more serious of the two, disappeared from the headline entirely. A
        # thing we could not read is not a thing we know to be merely degraded.
        "verdict": (
            "failed" if failures else
            "unread" if unknowns else
            "degraded" if warnings else "live"
        ),
    }
