#!/usr/bin/env python
"""Prove the DEPLOYED product is live — every pillar, one command, one exit code.

``verify_deploy.py`` checks the contracts and ``desk_preflight.py`` checks the
desk's own gates; both read the chain and the local config. Neither answers the
question that actually matters before someone opens the site: **is the thing on
the internet working right now?** That question has been answered by hand, per
surface, and the gaps show — a settled series served as the live desk, a public
tape blank two hours in five, and `poster_last_tx: null` while three real posts
sat on chain. Each was found by looking, and each could have been found by a
check that runs.

Read-only and safe to run against production at any cadence: no writes, no
faucet drips, no Circle users created.

    uv run python scripts/verify_live.py          # == make verify-live
    VERIFY_STRICT=1 …                             # crons + runway also fail
    ACR_API_URL=… ACR_TERMINAL_URL=… …            # point at another deploy

Exit codes: 0 = every pillar live; 1 = something a visitor would notice.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import time
import urllib.error
import urllib.request

from acr_oracle_client.futures import _rpc_retry, collateral_or_none

# Arc MAINNET. The old default (`acr-api-1fto`, Arc testnet) outlived the chain
# switch: that service is suspended and answers 503, so every check below
# failed while the keepalive reported it as a red run nobody read.
API = os.environ.get("ACR_API_URL", "https://acr-api-mainnet.onrender.com").rstrip("/")
# `keepalive.yml` runs this every ten minutes, so this default IS the host the
# standing gate audits — and from 2026-10-08 to 10-09 it was a custom domain
# that never resolved to the deployment. The failure was not a clean red:
# Hostinger's parked page answers **HTTP 200** with HTML, which produced all
# three verdicts at once. `/api/futures` passed VACUOUSLY (HTML fails to parse
# -> `desks={}`, and the keepalive sets ACR_VERIFY_NO_VENUE=1, so the 200 alone
# satisfied it). `/api/humanid` WARNED with a wrong-but-exculpatory reason.
# `/api/wallet/balances` FAILED HARD, because its warn-only escape hatch is
# `st == 404` and a parked page is a 200 — so the job went red and blamed a
# Next route for a DNS problem.
#
# The keepalive now also sets ACR_TERMINAL_URL explicitly. Both, deliberately:
# the env var makes the running workflow right today, the default makes anyone
# invoking this by hand right too, and they become true at different times.
TERMINAL = os.environ.get(
    "ACR_TERMINAL_URL", "https://arc-compute-rate.vercel.app"
).rstrip("/")
#: Arc testnet. The terminal serves BOTH chains from one host and its route
#: handlers pick by the `chain` query key (`apps/terminal/lib/chainChoice.ts`,
#: CHAIN_PARAM), defaulting to mainnet. So a testnet run that called bare
#: `/api/futures` audited MAINNET's venue and reported its empty tape as a
#: testnet failure (measured 2026-10-14: "0 desk(s)" vs `?chain=testnet`'s own).
TESTNET_CHAIN_ID = 5042002


def terminal_url(path: str, chain_id: int) -> str:
    """The terminal `path` on the chain this run audits."""
    if int(chain_id) != TESTNET_CHAIN_ID:
        return f"{TERMINAL}{path}"
    return f"{TERMINAL}{path}{'&' if '?' in path else '?'}chain=testnet"


def _audited_chain() -> int:
    from acr_core import get_settings

    return int(get_settings().arc_chain_id)
#: Free-tier hosts sleep; a cold start is slow but not a fault.
TIMEOUT_S = float(os.environ.get("VERIFY_TIMEOUT_S", "90"))
#: HARD failure: ACRFutures.MAX_SETTLE_AGE is 7200s, so a print older than this
#: means the venue cannot be settled at all — not a slow press, a broken one.
PRINT_MAX_AGE_S = float(os.environ.get("VERIFY_PRINT_MAX_AGE_S", "7200"))
#: WARN: the press posts hourly, so 90 minutes means it has already missed a
#: slot and is heading for the settle window. Catching it here is the whole
#: point — a 216-minute gap went unnoticed because nothing looked until the
#: damage was done.
PRINT_WARN_AGE_S = float(os.environ.get("VERIFY_PRINT_WARN_AGE_S", "5400"))
#: Below this many days of gas, a wallet is a scheduled outage.
MIN_RUNWAY_DAYS = float(os.environ.get("VERIFY_MIN_RUNWAY_DAYS", "3"))
#: The venue's custody wallets need enough to open AND collateralize a series
#: (default roll: 1.5 collateral + gas). Below this a roll fails on its budget
#: guard, which is a silent expiry rather than a loud error.
#: PER ROLE, because they do different jobs and one floor for both is a check
#: that is wrong for at least one of them. The maker opens and collateralizes a
#: series (1.5 collateral + 1.0 gas); the taker only trades, at roughly
#: 0.002 USDC of gas a fill, and its collateral is already posted. Applying the
#: maker's number to the taker cried wolf at 1.78 USDC — hundreds of trades of
#: runway — and a floor that is wrong is a floor people learn to ignore.
VENUE_WALLET_FLOOR_USDC = float(os.environ.get("VERIFY_VENUE_FLOOR_USDC", "2.5"))
TAKER_WALLET_FLOOR_USDC = float(os.environ.get("VERIFY_TAKER_FLOOR_USDC", "1.0"))
#: The eleven-hour outage was not caused by the book being tight — it was
#: caused by nothing saying so until the trades were already reverting. So this
#: file wants a threshold, and the only defensible one is the desk's own
#: MAX_QTY: `feasible_qty` CLAMPS its answer there, so headroom saturates at
#: MAX_QTY and any floor above it would warn forever. Read from the desk rather
#: than restated, so the two can never drift apart. Overridable, but the default
#: is the property that matters on a public desk — whatever a reader is
#: offered, the book can actually fill.
_BOOK_HEADROOM_FLOOR = float(os.environ.get("VERIFY_BOOK_HEADROOM", "0")) or None
#: How many FULL reader-sized trades a book should still be able to absorb.
#: Headroom alone cannot say this — feasible_qty clamps at MAX_QTY, so a book
#: two trades from frozen and one eleven trades from frozen report the same
#: number. Four is "a demo session's worth of judges".
_BOOK_DEPTH_TRADES = float(os.environ.get("VERIFY_BOOK_DEPTH", "4"))
#: GitHub drops most scheduled ticks on a private repo, so "recent" has to be
#: generous or this check cries wolf — which is worse than not checking.
CRON_MAX_AGE_S = float(os.environ.get("VERIFY_CRON_MAX_AGE_S", "21600"))
STRICT = os.environ.get("VERIFY_STRICT", "") not in ("", "0", "false")
# This deployment has a venue contract and no series listed on it. On Arc
# mainnet that is the truth, and a hard failure for it makes the keepalive
# permanently red — which costs more than it catches, because red stops
# meaning anything. DECLARED, not inferred: without this flag an empty
# venue still fails, so a chain that really does have a book is still
# guarded. Mirrors ACR_SNAPSHOT_ALLOW_NO_FUTURES in gen_snapshot.py.
NO_VENUE = os.environ.get("ACR_VERIFY_NO_VENUE", "") not in ("", "0", "false")
#: A Circle Gateway settlement reference is a batch UUID. Anything else reaching
#: this surface is a placeholder that got published: `dev-…`/`sim-…` are what the
#: mock gate writes, and an empty string is a row that lost its reference on the
#: way through. Either renders in the hedger panel as a receipt a judge can
#: click, which is why this is a shape test and not a truthiness test.
_GATEWAY_REF = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I
)

GATED = ["/prints/ACR-INF", "/curve/ACR-INF", "/vol/ACR-INF", "/seller-scores/ACR-INF"]
UNGATED = ["/onchain/ACR-INF", "/marketplace/catalog", "/revenue", "/x402/info"]
ROUTES = ["/", "/curve", "/developers"]

#: The hosted MCP server (`apps/terminal/app/api/mcp`), the URL an agent adds with
#: no install. Its unit tests stub the press, so nothing here checked that the
#: DEPLOYED one answers: a broken endpoint would have left this whole run green
#: while every agent that connected to it got an error. These are the tools it
#: must offer. The two it must never offer need the agent's own machine:
#: `pay_and_read` signs a payment with a local key, `spend_report` reads a local
#: ledger file.
HOSTED_MCP_TOOLS = frozenset({
    "check_spend", "wallet_tca", "reroute_suggestion", "payment_receipts", "seller_rating",
    "benchmark_price", "get_rate", "query_tape", "can_i_pay",
})
HOSTED_MCP_LOCAL_ONLY = frozenset({"pay_and_read", "spend_report"})
#: The example bill /developers runs (`EXAMPLE_BILL` in lib/mcpPlayground.ts).
HOSTED_MCP_BILL = {"unit": "$/1k tokens", "billed_usdc": 0.47, "quantity": 23}

_failures: list[str] = []
_warnings: list[str] = []


def section(fn, *args):
    """Run one pillar's checks, converting a crash into a recorded failure.

    Every chain call here can meet Arc's 429, and an unguarded one takes the
    whole run down with a traceback instead of a verdict — the single worst
    outcome for a liveness gate, because a stack trace in CI reads as "the
    checker is broken" and gets muted. A throttled section reports itself and
    the rest still run.
    """
    try:
        return fn(*args)
    except Exception as exc:  # noqa: BLE001 — a verdict beats a traceback
        check(False, f"{fn.__name__} could not complete: {str(exc)[:90]}", warn_only=True)
        return None


def check(ok: bool, label: str, *, warn_only: bool = False) -> bool:
    """Record and print one result. ``warn_only`` marks the things that depend
    on somebody else's scheduler rather than on our code being correct."""
    if ok:
        mark = "✓"
    elif warn_only and not STRICT:
        mark = "!"
        _warnings.append(label)
    else:
        mark = "✗"
        _failures.append(label)
    print(f"  {mark} {label}")
    return ok


def _parse(raw: bytes) -> dict | None:
    """JSON if it is JSON, else None. The page routes serve HTML, and a checker
    that reports 0 because it could not parse a body it never needed is a
    checker that cries wolf — the status is the whole assertion there."""
    try:
        return json.loads(raw or b"null")
    except Exception:
        return None


def _card_headers() -> dict[str, str]:
    """An `AGENT-CARD` header when a reader key is configured, else nothing.

    THIS SCRIPT IS AN AGENT, so it presents a card — and presenting one against
    the LIVE service is the only way to find out that the gate works somewhere
    other than a test client. A card minted here exercises the full path: EIP-712
    over a 3-field domain, base64 in a header, signature recovered server-side.

    Anonymous is a working state — a verification script that refused to run
    without a credential would make the credential a prerequisite for checking
    anything at all. But it is a state that must be SAID, which is the bug this
    docstring used to describe as a feature: the `signer is None` branch returned
    an empty dict, printed nothing, and let every later check pass while proving
    nothing about the gate. Only an exception was visible. `_CARD_NOTE` now carries
    what happened so `verify_agent` can turn it into a visible `!` or `✗`.

    Cached for the process: one card comfortably outlives a single verification
    run, and re-minting per request would spend more time signing than asking.
    """
    global _CARD_HEADER, _CARD_NOTE
    if _CARD_HEADER is not None:
        return _CARD_HEADER
    _CARD_HEADER = {}
    try:
        from acr_core import get_settings
        from acr_oracle_client.agentcard import encode_header, mint, sign_card
        from acr_oracle_client.signer import build_role_signer

        settings = get_settings()
        signer = build_role_signer("reader", settings)
        if signer is None:
            _CARD_NOTE = "no ACR_READER_PRIVATE_KEY — every probe below is anonymous"
            return _CARD_HEADER
        card = mint(
            signer.address,
            name="acr-verify-live",
            role="reader",
            audience=settings.agent_audience or "acr-index-api",
            ttl_s=300,
        )
        sig = sign_card(card, signer, int(settings.arc_chain_id))
        _CARD_HEADER = {"AGENT-CARD": encode_header(card, sig)}
        _CARD_NOTE = f"minted for {signer.address[:6]}…{signer.address[-4:]}"
    except Exception as exc:  # noqa: BLE001 - a broken card must not stop the checks
        # A key that cannot sign is a DIFFERENT fact from no key at all, and it is
        # the one that means something is wrong rather than merely unconfigured.
        _CARD_NOTE = f"FAILED to mint: {type(exc).__name__}: {exc}"
    return _CARD_HEADER


#: Minted once per process by `_card_headers`. None means "not attempted yet";
#: an empty dict means "attempted and there is no key", which is not an error.
_CARD_HEADER: dict[str, str] | None = None
#: What happened when we tried. Read by `verify_agent`, which is the only reason
#: the outcome is recorded rather than discarded — a credential that can vanish
#: without comment makes every check downstream of it decorative.
_CARD_NOTE: str = "not attempted"


def get(url: str) -> tuple[int, dict | None]:
    """HTTP GET returning (status, parsed-json-or-None). A 402 body is a real
    answer here, not an error, so read the payload either way."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "acr-verify-live", **_card_headers()}
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:  # noqa: S310
            return r.status, _parse(r.read())
    except urllib.error.HTTPError as e:
        return e.code, _parse(e.read())
    except Exception:
        return 0, None


def post(url: str, body: dict) -> tuple[int, dict | None]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "acr-verify-live",
                 **_card_headers()},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:  # noqa: S310
            return r.status, _parse(r.read())
    except urllib.error.HTTPError as e:
        return e.code, _parse(e.read())
    except Exception:
        return 0, None


# --- pillars ----------------------------------------------------------------


def verify_oracle(w3, settings) -> None:
    from acr_oracle_client import OracleClient

    print("\noracle — the index other contracts settle against")
    oc = OracleClient(rpc_url=settings.arc_rpc_url, oracle_address=settings.oracle_address)
    now = time.time()
    for iid in ("ACR-GPU", "ACR-INF", "ACR-DATA"):
        p = oc.read_latest(iid)
        if not p or not p.get("value"):
            check(False, f"{iid}: no on-chain print")
            continue
        age = now - (p.get("posted_at") or 0)
        # Two thresholds, because "late" and "unsettleable" are different
        # failures and only one of them is an outage.
        check(
            age < PRINT_MAX_AGE_S,
            f"{iid} = {p['value']:.5f}, posted {age / 60:.0f}m ago"
            + (" — PAST THE SETTLE WINDOW" if age >= PRINT_MAX_AGE_S else ""),
        )
        if PRINT_WARN_AGE_S <= age < PRINT_MAX_AGE_S:
            check(False, f"{iid} print is {age / 60:.0f}m old — the press has missed a slot",
                  warn_only=True)


def _margin_bps_or_default(w3, settings) -> int:
    """MARGIN_BPS from the venue, or the deployed default. Never guess silently."""
    try:
        return int(_rpc_retry(
            w3.eth.contract(
                address=w3.to_checksum_address(settings.futures_address),
                abi=[{"type": "function", "name": "MARGIN_BPS", "stateMutability": "view",
                      "inputs": [], "outputs": [{"name": "", "type": "uint256"}]}],
            ).functions.MARGIN_BPS().call
        ))
    except Exception:
        return 2000


def verify_oracle_v2(w3, settings) -> None:
    """The tape's oracle — the one whose prints say how they were made.

    Staleness here is a WARN, not a failure, and the asymmetry is deliberate:
    v1 going stale makes every expired futures series unsettleable, which costs
    real collateral. v2 going stale only makes the subgraph's arrival ring
    sparse, which it already reports honestly as `benchmarked: false`. Grading
    them the same would train an operator to ignore the one that matters.
    """
    from acr_oracle_client import OracleClient
    from acr_oracle_client.client import ORACLE_V2

    v2_address = getattr(settings, "oracle_v2_address", "")
    if not v2_address:
        return  # not deployed yet — silence, not a failure

    print("\noracle v2 — the print that carries its own policy and window")
    oc = OracleClient(
        rpc_url=settings.arc_rpc_url, oracle_address=v2_address, schema=ORACLE_V2
    )
    now = time.time()
    for iid in ("ACR-GPU", "ACR-INF", "ACR-DATA"):
        p = oc.read_latest(iid)
        if not p or not p.get("value"):
            check(False, f"{iid}: no v2 print yet", warn_only=True)
            continue
        age = now - (p.get("posted_at") or 0)
        check(
            age < PRINT_MAX_AGE_S,
            f"{iid} = {p['value']:.5f}, posted {age / 60:.0f}m ago (tape feed)",
            warn_only=True,
        )


def verify_venue(w3, settings) -> dict | None:
    """The live series, returned so later checks can assert against the SAME one
    the chain says is live — that asymmetry is where a dead series slipped
    through before."""
    from acr_oracle_client import FuturesClient, OracleClient

    print("\nvenue — ACRFutures, where the index becomes a position")
    if not settings.futures_address:
        check(False, "ACR_FUTURES_ADDRESS unset")
        return None
    fc = FuturesClient(rpc_url=settings.arc_rpc_url, futures_address=settings.futures_address)
    now = int(_rpc_retry(lambda: w3.eth.get_block("latest"))["timestamp"])
    live = [s for s in fc.read_all_series() if not s["settled"] and s["expiry_ts"] > now]
    if not check(
        bool(live) or NO_VENUE,
        "a live, unexpired series exists"
        + (" — none listed on this chain, declared" if NO_VENUE and not live else ""),
        warn_only=NO_VENUE,
    ) or not live:
        return None
    # ONE live series per index, newest wins — not one series for the whole
    # venue. Collapsing the venue to `max(series_id)` verified whichever book
    # happened to have the highest id and said nothing at all about the others,
    # so a second index could be frozen, uncollateralized or expired and every
    # line here would still be green.
    by_index: dict[str, dict] = {}
    for row in live:
        cur = by_index.get(row["index_id"])
        if cur is None or row["series_id"] > cur["series_id"]:
            by_index[row["index_id"]] = row
    s = max(live, key=lambda x: x["series_id"])  # the primary, for the checks below
    check(True, f"{len(by_index)} index book(s) live: {', '.join(sorted(by_index))}")
    for idx in sorted(by_index):
        row = by_index[idx]
        left_h = (row["expiry_ts"] - now) / 3600
        check(left_h > 24, f"{idx} series {row['series_id']} has {left_h:.0f}h left")

    # `collateral_of(...) or 0.0` is the spelling this project has a helper to
    # avoid: it turns a THROTTLED READ into an empty account, and here that made
    # the verifier announce an uncollateralized venue whenever Arc was busy —
    # crying outage over RPC weather.
    maker_addr = w3.to_checksum_address(s["maker"])
    maker_coll: dict[str, float | None] = {}
    for idx in sorted(by_index):
        row = by_index[idx]
        c = collateral_or_none(fc, row["series_id"], w3.to_checksum_address(row["maker"]))
        maker_coll[idx] = c
        if c is None:
            check(False, f"{idx}: maker collateral — the chain would not say", warn_only=True)
        else:
            check(c > 0, f"{idx}: maker is collateralized ({c:.2f} USDC) — it is the counterparty")

    # Collateral is PER SERIES, and a roll posts a fresh stake without reclaiming
    # the old one — so every roll silently leaves the maker's money on a series
    # nobody trades. Measured on 08-03: 4.50 USDC sat on series 1 while the desk
    # quoted series 2 off 1.50, and since `feasible_qty` clamps a reader's size
    # by the maker's stake, the public book was a quarter of the depth the
    # project had already paid for. Nothing was broken, so nothing complained.
    # EVERY live book's series is legitimately funded, not just the newest one.
    # Excluding only `s` was right while the venue had one book; with three it
    # reported the maker's real stake on the other two as stranded — a false
    # alarm, which is worse than no check, because a warning that is wrong is a
    # warning people learn to scroll past.
    funded_ids = {row["series_id"] for row in by_index.values()}
    stranded = 0.0
    unreadable = 0
    for other in fc.read_all_series():
        if other["series_id"] in funded_ids:
            continue
        c = collateral_or_none(fc, other["series_id"], maker_addr, tries=2)
        if c is None:
            unreadable += 1
        elif c > 0:
            stranded += c
    if unreadable:
        check(False, f"{unreadable} series' maker balance unreadable", warn_only=True)
    # Deliberately does NOT promise the money back: part of a balance on an old
    # series is initial margin for a position still open there, which the
    # contract will not release. `futures_withdraw.py` sizes that properly (via
    # the same `free_collateral_units` the desk quotes readers); this check only
    # says where to look. A verifier that names a command which then recovers
    # nothing has told you something false in the course of being helpful.
    check(
        stranded == 0.0,
        "no maker collateral sitting off a traded series"
        + (
            f" (found {stranded:.2f} USDC on other series — run "
            "`make futures-withdraw` with WITHDRAW_DRY_RUN=1 to see how much "
            "of it is actually free)"
            if stranded
            else ""
        ),
        warn_only=True,
    )

    # "Collateralized" is not "tradable". The maker being above zero passed for
    # eleven hours while the book was frozen: the maker had drifted short 2.31
    # contracts against a 2.26 margin cap, so `feasible_qty` returned max_buy=0
    # and every buy the keeper wanted was impossible. A check that is green
    # while the thing it guards is broken is the failure this file exists to
    # prevent, so ask the desk's OWN arithmetic whether a trade can happen.
    try:
        from index_api.desk import FAUCET_USDC, MARGIN_SAFETY, MAX_QTY, feasible_qty

        # EVERY book, not just the newest series. This ran once against
        # max(series_id) — which is ACR-DATA — so the depth of ACR-GPU (the
        # index the DESK DEFAULTS TO) and of ACR-INF was never measured at all.
        # The one book it did check was the one nobody lands on.
        oc = OracleClient(
            rpc_url=settings.arc_rpc_url, oracle_address=settings.oracle_address
        )
        margin_bps = _margin_bps_or_default(w3, settings)
        for idx in sorted(by_index):
            desk = fc.read_desk(idx)
            mark = (oc.read_latest(idx) or {}).get("value")
            maker = maker_coll.get(idx)
            if not (desk and mark and maker is not None):
                continue
            buy, sell = feasible_qty(
                mark, desk["multiplier"], margin_bps,
                maker, 0.0, maker, desk.get("maker_inventory") or 0.0,
            )
            check(
                buy > 0 and sell > 0,
                f"{idx}: the book can absorb a trade both ways "
                f"(max_buy {buy}, max_sell {sell})",
                warn_only=True,
            )
            # Above-zero is a verdict that arrives the hour the book freezes.
            # Measure the distance to it instead, against the largest trade the
            # desk will ever quote — because the failure a reader actually
            # meets is being offered a size the book cannot fill, and pressing
            # BUY on it. Counted in reader-sized trades (what a faucet drip
            # buys) rather than in USDC, which nobody can judge by eye.
            room = min(buy, sell)
            floor = _BOOK_HEADROOM_FLOOR or MAX_QTY
            per_contract = mark * desk["multiplier"] * (margin_bps / 10_000)
            # CLAMPED at MAX_QTY, because that is the largest trade the desk
            # will quote. Unclamped, a cheap index (ACR-DATA at 0.002) made a
            # 0.50 drip cover ~100 contracts, so a saturated book reported
            # "0.0 reader-sized trades" — a healthy venue described as a dead
            # one by the very line that exists to say whether it is healthy.
            unit = min(MARGIN_SAFETY * FAUCET_USDC / per_contract, MAX_QTY) if per_contract > 0 else 0.0
            # DEPTH, which the line below cannot express. `feasible_qty` clamps
            # at MAX_QTY, so "headroom" saturates at 2.00 and reports the same
            # number for a book that can take two more reader trades and one
            # that can take eleven. That distinction is the whole question
            # during judging: the maker's UNCLAMPED cap, minus what it is
            # already carrying, divided by what one reader can put on.
            m_cap = (MARGIN_SAFETY * maker / per_contract) if per_contract > 0 else 0.0
            inv = desk.get("maker_inventory") or 0.0
            left = min(m_cap + inv, m_cap - inv) / unit if unit else 0.0
            check(
                left >= _BOOK_DEPTH_TRADES,
                f"{idx}: {left:.1f} more full reader trades before this book freezes"
                + ("" if left >= _BOOK_DEPTH_TRADES else " — deepen it (make futures-collateralize)"),
                warn_only=True,
            )
            check(
                room >= floor,
                f"{idx}: headroom {room:.2f} of the {floor:.2f} the desk may quote"
                + (f" — {room / unit:.1f} reader-sized trades" if unit else "")
                + (
                    "; top the maker up (make futures-collateralize)"
                    if room < floor
                    else ", so any size the desk offers can be filled"
                ),
                warn_only=True,
            )
    except Exception as exc:  # noqa: BLE001 — a verdict beats a traceback
        check(False, f"could not size the book ({str(exc)[:45]})", warn_only=True)

    # Who can open a series. The venue was handed from the deploy EOA to the
    # maker's own Circle wallet on 2026-08-03, which is what lets the keeper
    # roll unattended — so this is a capability check, not a vanity one. If
    # ownership ever moves back to a raw key the keeper silently stops being
    # able to roll, and the first symptom would be an expired series.
    # The KEEPER'S TAKER must hold collateral on every book it rotates onto.
    # Without a stake the heartbeat returns "no taker collateral" and does
    # nothing — silently, on that index only, while the maker stays funded, the
    # series stays live and every other line on this page stays green. With one
    # book that was invisible; with three it would be two thirds of the venue
    # quietly not trading.
    # Resolved through build_role_signer, the same seam the funding section
    # uses — an env var here would be a second source of truth for one address,
    # and the two would drift the first time a wallet is rotated.
    taker_addr = ""
    try:
        from acr_oracle_client import build_role_signer

        sg = build_role_signer("taker", settings)
        if sg is not None and type(sg).__name__ == "CircleWalletSigner":
            taker_addr = sg.address
    except Exception:  # noqa: BLE001 — not migrated here; the check just skips
        taker_addr = ""
    if taker_addr:
        for idx in sorted(by_index):
            row = by_index[idx]
            tc = collateral_or_none(fc, row["series_id"], w3.to_checksum_address(taker_addr))
            if tc is None:
                check(False, f"{idx}: taker collateral unreadable", warn_only=True)
            else:
                check(
                    tc > 0,
                    f"{idx}: the heartbeat taker is collateralized ({tc:.2f} USDC) "
                    "— it can actually trade this book",
                )

    try:
        owner = _rpc_retry(
            w3.eth.contract(
                address=w3.to_checksum_address(settings.futures_address),
                abi=[{"type": "function", "name": "owner", "stateMutability": "view",
                      "inputs": [], "outputs": [{"name": "", "type": "address"}]}],
            ).functions.owner().call
        )
        from acr_oracle_client import build_role_signer

        mk = build_role_signer("maker", settings)
        custody = mk is not None and type(mk).__name__ == "CircleWalletSigner"
        owned_by_keeper = custody and str(owner).lower() == str(mk.address).lower()
        check(
            owned_by_keeper,
            f"venue owner {str(owner)[:12]}… is the keeper's Circle wallet "
            "(so a roll needs no human)",
            warn_only=True,
        )
    except Exception as exc:  # noqa: BLE001 — a verdict beats a traceback
        check(False, f"could not read the venue owner ({str(exc)[:40]})", warn_only=True)

    # A venue with a live series but no taker stake is a book nobody can trade
    # into: every desk fill mirrors onto the maker, but the heartbeat's own
    # trades need its stake to still be on THIS series after a roll.
    for idx in sorted(by_index):
        row = by_index[idx]
        n = int(_rpc_retry(fc._contract().functions.traderCount(row["series_id"]).call))
        check(n >= 1, f"{idx}: {n} trader(s) posted on series {row['series_id']}")
    # The DESK's index, not the newest series venue-wide — verify_desk quotes
    # /desk/limits for ACR-INF, and handing it series 5 (ACR-DATA) made it call
    # a correct desk broken the moment a second book opened.
    return by_index.get("ACR-INF") or s


def verify_tape(settings) -> None:
    from acr_oracle_client import FuturesClient
    from acr_oracle_client.futures import TAPE_PAGE_BLOCKS

    # A CROSS-CHECK, not the verdict. This issues its own uncached getLogs and
    # so competes with the product for the same throttled Arc RPC — under load
    # it can come back empty while the terminal, serving a warmed cache, shows a
    # full tape. The visitor-facing assertion is the terminal's tape in
    # verify_terminal(), which is hard-failing; making this one hard too would
    # turn RPC weather into a false outage.
    print("\ntape — cross-check that fills exist on chain (product's tape is the verdict)")
    fc = FuturesClient(rpc_url=settings.arc_rpc_url, futures_address=settings.futures_address)
    w3 = fc._connect()
    tip = int(_rpc_retry(lambda: w3.eth.block_number))
    fills = fc.recent_trades()
    if not check(bool(fills), "recent fills readable direct from chain", warn_only=True):
        return
    oldest = min(f["block"] for f in fills)
    # Depth is warn-only ON PURPOSE. Paging stops on a throttle by design — a
    # shorter tape now beats a complete one a minute from now — so a single
    # probe can legitimately come back one page deep while the product, which
    # serves this warmed and cached, shows the full reach. Failing a liveness
    # run on the RPC's mood would make it cry wolf, and the terminal's own tape
    # check below is the assertion that reflects what a visitor actually sees.
    check(
        tip - oldest > TAPE_PAGE_BLOCKS or len(fills) >= 3,
        f"{len(fills)} fills, reaching {(tip - oldest) * 0.510 / 3600:.1f}h back",
        warn_only=True,
    )


def verify_seller() -> dict | None:
    print("\nseller — the paid API")
    status, health = get(f"{API}/health")
    if not check(status == 200 and bool(health), f"{API}/health"):
        return None
    check(health.get("oracle_configured") is True, "oracle configured")
    check(health.get("indices_live") == 3, f"{health.get('indices_live')}/3 indices live")
    # Which path carries the tape's queries — the one The Graph counts, or the
    # development URL it does not. A judge reading Studio's dashboard needs this.
    st, ops = get(f"{API}/graph/operations")
    t = (ops or {}).get("transport") or {}
    if t:
        via = t.get("via")
        pace = "measuring" if t.get("measuring") else f"~{t.get('pace_per_day')}/day"
        check(via in ("studio-dev", "gateway"),
              f"subgraph via {via}: {t.get('queries')} queries this boot, {t.get('cache_hits')} cached, "
              f"{pace}" + (f" (dev cap {t.get('daily_cap')})" if via == "studio-dev" else ""),
              warn_only=True)
        if via == "studio-dev":
            check(False, "queries go to Studio's development URL: capped at 3,000/day and shown on no dashboard "
                         "(publish + API key, docs/GRAPH-RUNBOOK.md step 6)", warn_only=True)
    # The defect this was written for: the press posts, but the product says
    # "awaiting first live post" because in-memory provenance never rehydrated.
    check(
        bool(health.get("poster_last_tx")),
        f"poster provenance present ({str(health.get('poster_last_tx'))[:14]}…)",
    )
    return health


def verify_x402() -> None:
    print("\nx402 — the gate that makes the data a product")
    for path in GATED:
        status, body = get(f"{API}{path}")
        a = ((body or {}).get("accepts") or [{}])[0]
        ok = status == 402 and a.get("scheme") == "exact" and a.get("payTo")
        # Report the status we GOT, never the one we wanted — a failure line
        # that prints the expected value is a failure line that misleads.
        detail = f"{a.get('network')} {a.get('maxAmountRequired')}" if ok else "no payment terms"
        check(bool(ok), f"{path} -> {status} {detail}")
    for path in UNGATED:
        status, _ = get(f"{API}{path}")
        check(status == 200, f"{path} -> {status} (ungated)")


def verify_agent() -> None:
    """The agent gate, the card we present, and the screen on agent traffic.

    THE REASON THIS PILLAR EXISTS: `get()` and `post()` have been splatting an
    AGENT-CARD into every request since `fd2e3b9`, and nothing asserted the service
    ever READ it. A card that silently failed to mint, or a gate that silently
    stopped verifying, would have left every line above still green — the card was
    decoration with a signature on it.

    404 is a WARNING rather than a failure while the branch is unmerged, per this
    file's own rule: fail on our code, warn on what somebody else has yet to
    deploy. Once `/agent/info` answers, every check below is load-bearing.
    """
    print("\nagent — who is calling, and what screens what they send")

    # What happened to OUR credential, said out loud. An anonymous run is allowed;
    # an anonymous run nobody mentioned is how this pillar's absence hid.
    carded = bool(_card_headers())
    check(carded, f"agent card: {_CARD_NOTE}", warn_only=not _CARD_NOTE.startswith("FAILED"))

    status, info = get(f"{API}/agent/info")
    if status == 404:
        check(False, "/agent/info -> 404 (the gate is not deployed yet)", warn_only=True)
        return
    info = info or {}
    check(status == 200 and bool(info.get("audience")),
          f"/agent/info -> {status} audience={info.get('audience')}")
    tiers = info.get("tiers") or []
    check(len(tiers) == 3, f"three tiers offered: {', '.join(tiers) or 'none'}")
    # Reported rather than asserted true: a read-only deployment that cannot reach
    # the mirror is a legitimate state, and one worth seeing.
    check(True, f"human tier verifiable here: {info.get('human_binding_verifiable')}",
          warn_only=not info.get("human_binding_verifiable"))

    status, ch = get(f"{API}/agent/challenge")
    ch = ch or {}
    check(status == 200 and bool(ch.get("domain") or ch.get("scheme")),
          f"/agent/challenge -> {status} (how to mint one, without reading our source)")

    # THE ASSERTION THIS PILLAR IS FOR. Anonymous without a card, carded or human
    # with one — read back from the service, so the card is proved to be read and
    # not merely sent.
    status, who = get(f"{API}/agent/whoami")
    who = who or {}
    tier = who.get("tier")
    if carded:
        check(status == 200 and tier in ("carded", "human"),
              f"/agent/whoami with our card -> {tier}")
        if who.get("human_note"):
            check(True, f"human claim: {who['human_note']}", warn_only=True)
    else:
        check(status == 200 and tier == "anonymous",
              f"/agent/whoami with no card -> {tier}")

    status, armor = get(f"{API}/armor/info")
    armor = armor or {}
    backend = armor.get("backend")
    check(status == 200 and bool(backend), f"/armor/info -> {status} backend={backend}")
    # `local` is the offline six-substring floor. It is a working state and it is
    # NOT Model Armor, so it warns rather than passes silently — the whole point of
    # reporting the backend is that these two look identical from outside.
    check(backend == "gcp",
          f"screen backend: {backend}"
          + (" (offline floor, not Model Armor)" if backend == "local" else ""),
          warn_only=backend != "gcp")
    applies = armor.get("applies_to") or []
    check(bool(applies), f"screened routes: {', '.join(applies) or 'NONE — the screen is inert'}")
    if backend == "gcp":
        # Only meaningful against a real backend: the floor counts inspections too,
        # and a climbing counter there would prove nothing about Google.
        check(int(armor.get("screened") or 0) > 0,
              f"inspections performed: {armor.get('screened')} (blocked {armor.get('blocked')})",
              warn_only=True)


def verify_humans() -> None:
    """Who is one person — the mirror, the current window, and the rotation fuse.

    MODULE W HAD NO LIVE ASSERTION AT ALL. Its tests are strong; nothing ever asked
    the deployment whether a human was resolved right now. And "right now" is the
    whole difficulty: cluster ids are keccak(nullifier, salt, WINDOW) and rotate
    every seven days, so on the first second of a new window every human reads as
    unresolved — a silent `n: 0` that looks exactly like nobody ever traded. The
    Terminal's chip now says "out of date"; this says it where a cron can fail.

    Two very different facts, graded differently:
      - a salt that does not match its commitment is OUR configuration, and it
        derives cluster ids that match nothing — a hard failure.
      - a window with no resolutions is an operator chore that is due, so it
        warns, and the warning carries the command that clears it.
    """
    from acr_oracle_client.humanid import current_window

    print("\nhumans — who is one person, and whether that is still current")
    window = current_window()

    status, info = get(f"{API}/humanid/info")
    info = info or {}
    check(status == 200 and bool(info.get("backend")),
          f"/humanid/info -> {status} backend={info.get('backend')} sandbox={info.get('sandbox')}")
    salt = info.get("salt_matches_commitment")
    # None is "nothing to compare against" and is not a failure; False is the one
    # misconfiguration that makes every surface read "no humans" while every call
    # succeeds — which is why it is the only hard check in this pillar.
    check(salt is not False,
          "salt matches its on-chain commitment" if salt else
          ("SALT MISMATCH: every cluster id derives to nothing the tape wrote"
           if salt is False else "salt: nothing to compare against (mirror not configured)"),
          warn_only=salt is None)

    status, body = post(f"{API}/graph/query", {"operation": "humans", "variables": {"first": 50}})
    clusters = (((body or {}).get("data") or {}).get("humanClusters")) or []
    in_window = [c for c in clusters if str(c.get("window")) == str(window)]
    wallets = sum(len(c.get("payers") or c.get("wallets") or []) for c in in_window)
    check(
        bool(in_window),
        f"window {window}: {len(in_window)} human(s), {wallets} wallet(s) resolved"
        if in_window else
        f"window {window} has NO resolutions — a press with ACR_KEEPER_HUMANS=1 resolves it within the "
        f"hour (its /health keeper.humans verdict says why not); else `make resolve-humans ARGS=--commit` "
        f"(ids rotate weekly; the last window's are expired, not wrong)",
        warn_only=True,
    )

    # The Terminal's own reading of the same fact, so the chip a judge sees and the
    # line a cron prints cannot quietly disagree.
    status, env = get(terminal_url("/api/humanid", _audited_chain()))
    humans = ((env or {}).get("data") or {}).get("humans") or {}
    if humans:
        check(int(humans.get("traded") or 0) > 0,
              f"terminal: {humans.get('n')} human(s), {humans.get('traded')} traded, "
              f"{humans.get('wallets')} wallet(s), window {humans.get('window')}",
              warn_only=True)
    else:
        check(False, f"terminal /api/humanid -> {status}: no human count (press cold, or tape dark)",
              warn_only=True)


def verify_desk(live_series: dict | None) -> None:
    print("\npublic desk — a reader trading from their own wallet")
    addr = "0x95DE70736E21e70DF921Fb3ab91dD56750965b59"  # a real past desk wallet
    status, body = post(f"{API}/desk/withdrawable", {"address": addr})
    check(status == 200 and body is not None, f"/desk/withdrawable -> {status}")
    # ACR-GPU FIRST, because that is what components/chain/PublicDesk.tsx
    # defaults to — a judge's first trade lands there. Until ACR-GPU had a
    # series the UI silently fell back to ACR-INF, so this only ever exercised
    # the index nobody starts on.
    for iid in ("ACR-GPU", "ACR-INF"):
        st, bd = post(f"{API}/desk/limits", {"address": addr, "index_id": iid})
        good = st == 200 and isinstance(bd, dict) and bd.get("mark", 0) > 0
        # A 404 here is "no open series for <index>" — the unseeded venue again,
        # reached through the desk rather than the chain. One fact, so one
        # declaration governs all three places it surfaces; the check still
        # fails hard on any OTHER status, and on a 404 when a venue is expected.
        no_series = NO_VENUE and st == 404
        check(
            good or no_series,
            f"/desk/limits {iid} -> {st}"
            + (f", mark {bd.get('mark'):.5f}, max_buy {bd.get('max_buy')}" if good else "")
            + (" — no open series, declared" if no_series else ""),
            warn_only=no_series,
        )
        if iid == "ACR-INF":
            status, body = st, bd
    ok = status == 200 and isinstance(body, dict) and body.get("mark", 0) > 0
    if ok and live_series is not None:
        # The desk must quote the series the CHAIN says is live FOR THIS INDEX.
        # Comparing against the newest series venue-wide was right with one
        # book and wrong the moment a second opened: the desk correctly quoted
        # ACR-INF's series 3 while ACR-DATA's series 5 was the newest anywhere,
        # and the check called a correct desk broken. Quoting a settled series
        # is the real failure — that is how a reader authorizes a doomed trade.
        check(
            body.get("series_id") == live_series["series_id"],
            f"desk quotes series {body.get('series_id')} == live ACR-INF series "
            f"{live_series['series_id']}",
        )


def verify_revenue(settings) -> None:
    """The two ways a HUMAN pays — the surfaces that turn visitors into revenue.
    Probed the way a visitor's browser would, without a wallet or a session: the
    shape must be right and the gate must say what it costs."""
    print("\nrevenue — the two ways a human pays")
    # 1. The browser payer needs the seller to advertise a Gateway batching
    #    option on THIS network with a verifying contract, or a wallet has
    #    nothing it can sign. /x402/info also has to name the SDK chain key.
    status, info = get(f"{API}/x402/info")
    ok = status == 200 and isinstance(info, dict)
    check(ok, f"/x402/info -> {status}")
    if ok:
        # Absent key = an API deployed before the browser payer existed: degraded,
        # not down. Present and empty = broken.
        check(bool(info.get("gateway_chain")),
              f"seller names its Gateway SDK chain ({info.get('gateway_chain') or 'not on this deploy yet'})",
              warn_only="gateway_chain" not in info)
        check(info.get("network") == settings.caip2(), f"seller's network {info.get('network')} == {settings.caip2()}")
        gated = (info.get("gated_endpoints") or ["/prints"])[0]
        import base64

        req = urllib.request.Request(f"{API}{gated}", headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:  # pragma: no cover
                st, hdr = r.status, r.headers.get("PAYMENT-REQUIRED")
        except urllib.error.HTTPError as e:
            st, hdr = e.code, e.headers.get("PAYMENT-REQUIRED")
        except Exception:  # noqa: BLE001
            st, hdr = 0, None
        check(st == 402 and bool(hdr), f"{gated} -> {st} with PAYMENT-REQUIRED (a wallet can start the exchange)")
        if hdr:
            try:
                env = json.loads(base64.b64decode(hdr))
                batching = [
                    o for o in env.get("accepts", [])
                    if o.get("network") == settings.caip2()
                    and (o.get("extra") or {}).get("name") == "GatewayWalletBatched"
                    and (o.get("extra") or {}).get("verifyingContract")
                ]
                check(bool(batching), "the 402 offers a Gateway batching option a browser wallet can sign")
            except Exception:  # noqa: BLE001
                check(False, "the PAYMENT-REQUIRED header decodes")
    # 2. The Desk pass: the route exists and refuses a stranger cleanly (a real
    #    session needs Circle's PIN ceremony, which no probe should run).
    st, body = post(f"{API}/desk/pass/status", {"user_token": "probe-not-a-session-token-0000"})
    check(st in (400, 401, 403, 404, 422, 502, 503) and st != 500,
          f"/desk/pass/status refuses a stranger with {st}, not a 500")
    # 3. The terminal's server-side balance read, with a known address: numbers
    #    back, and the one-next-step the storefront keys on.
    st, body = get(terminal_url("/api/wallet/balances?address=0x95DE70736E21e70DF921Fb3ab91dD56750965b59&price=0.001", settings.arc_chain_id))
    good = st == 200 and isinstance(body, dict) and "usdc_wallet" in body and "usdc_gateway" in body
    check(good,
          f"terminal /api/wallet/balances -> {st}"
          + (f", wallet {body.get('usdc_wallet')} · gateway {body.get('usdc_gateway')} USDC"
             f" · next {body.get('next', '?')}" if good
             else (" (route not on this deploy yet)" if st == 404 else "")),
          warn_only=st == 404)  # a terminal deployed before the wallet payer: degraded, not down
    # 4. Circle's facilitator must know the seller's network, or every signed
    #    payment is refused with a reason that reads like a bad signature. The
    #    seller's configured host is not public; the chain decides which one it
    #    must be (the -testnet host does not know 5042 and vice versa).
    fac = "https://gateway-api.circle.com" if settings.arc_chain_id == 5042 else "https://gateway-api-testnet.circle.com"
    st, body = get(f"{fac}/v1/x402/supported")
    nets = [k.get("network") for k in (body.get("kinds") or [])] if st == 200 and isinstance(body, dict) else []
    check(settings.caip2() in nets,
          f"Circle's facilitator {fac.split('//')[1]} advertises {settings.caip2()} for x402"
          + ("" if settings.caip2() in nets else f" (got {st}: {nets[:3]}…)"))
    # 5. The chain a visitor's wallet is handed answers, and is the chain the
    #    seller names: EIP-3085 with a dead or wrong RPC adds a chain nobody can use.
    st, chain = get(f"{API}/terminal/data")
    pub = (chain.get("chain") or {}).get("public_rpc_url") if st == 200 and isinstance(chain, dict) else None
    if pub:
        st2, cid = post(pub, {"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []})
        got = int(str((cid or {}).get("result", "0x0")), 16) if st2 == 200 and isinstance(cid, dict) else 0
        check(got == settings.arc_chain_id, f"public RPC {pub} answers chain {got} == {settings.arc_chain_id}")
    else:
        check(False, "payload names a public_rpc_url a wallet can be handed (not on this deploy yet)",
              warn_only=True)


def verify_hedger(settings) -> None:
    """The autonomous agent — did it actually pay and trade, or is it a panel?

    This lives here rather than in a dispatch workflow ON PURPOSE. Running the
    hedger from CI would mean shipping the Circle agent wallet's session into
    GitHub secrets — an email-authenticated credential that expires in weeks,
    which is exactly the "wrong tool for unattended automation" argument
    docs/WALLETS.md makes about agent wallets. This check needs no credential at
    all: it reads public state and asserts the evidence is there, so the public
    run record comes from the 10-minute keepalive for free.

    Warn-only: the agent is operator-run, so "has not traded recently" is a fact
    about the demo schedule, not an outage.
    """
    print("\nautonomous hedger — the agent that pays for what it trades on")
    status, body = get(f"{API}/hedger")
    if not check(status == 200 and isinstance(body, dict), f"/hedger -> {status}"):
        return
    if not body.get("configured"):
        check(False, "no hedger agent configured on this deployment", warn_only=True)
        return
    check(bool(body.get("agent")), f"agent wallet {str(body.get('agent'))[:12]}…")
    # Two identities, one agent — the pair is the point, so check both are named.
    check(
        bool(body.get("payer")),
        f"pays as {str(body.get('payer'))[:12]}… (the smart account's backing EOA)",
        warn_only=True,
    )
    fills = body.get("fills") or []
    # An agent AT its mandate stops trading, so its fills age out of the press's
    # paged log reach — while its position, which only fills can build, persists
    # in contract state. Either witness proves it traded; demanding a *recent*
    # fill from an agent whose success condition is "stop trading" turns
    # reaching the mandate into a failure (it did, on the first strict run).
    pos_witness = body.get("position_contracts") or 0.0
    check(
        len(fills) > 0 or abs(pos_witness) > 1e-9,
        f"{len(fills)} on-chain fill(s) in log reach"
        + (f" — position {pos_witness:+.2f} is the durable witness" if pos_witness else ""),
        warn_only=True,
    )
    paid = body.get("paid_queries")
    receipts = body.get("receipts")
    spent = body.get("spent_usdc")
    # `paid_queries` is a bare integer, and an integer is not evidence. The array
    # beside it is: every row is a Circle Gateway batch reference this agent's own
    # wallet settled, and HedgerPanel renders them as clickable receipts under
    # "1 · prints it bought". Checking only the counter meant a deployment serving
    # `paid_queries: 4` over `receipts: null` passed every gate here — a number
    # with nothing under it, which is the shape a fabricated claim has.
    check(
        bool(paid),
        f"{paid} x402 settlement(s) recorded against the agent's payer",
        warn_only=True,
    )
    if paid:
        # Past here the payload has ASSERTED a spend, so it owes the evidence.
        # build_hedger_state fills paid_queries, spent_usdc and receipts in ONE
        # pass over ONE filtered list, so a count with no rows under it is not
        # "it has not paid" — it is that derivation broken. Hence hard, per this
        # file's own rule: warn on somebody else's scheduler, fail on our code.
        if check(
            isinstance(receipts, list) and len(receipts) > 0,
            f"the receipts array carries the evidence for those {paid} ("
            + ("null — the counter has nothing under it"
               if receipts is None else f"{len(receipts or [])} row(s)")
            + ")",
        ):
            check(
                len(receipts) <= paid,
                f"{len(receipts)} receipt row(s) <= {paid} paid queries "
                "(the array is the newest ten of the count, never more than it)",
            )
            shown = sum(float(r.get("amount_usdc") or 0.0) for r in receipts)
            # The array caps at RECENT_RECEIPTS while spent_usdc sums the whole
            # filtered set, so equality is only OWED when nothing was truncated.
            # Asserting it unconditionally would go red the moment the agent's
            # eleventh payment lands, and a check that breaks on success is a
            # check people learn to mute. The tolerance is the upstream
            # round(…, 6) and nothing looser.
            if len(receipts) == paid:
                check(
                    spent is not None and abs(float(spent) - shown) <= 5e-6,
                    f"spent_usdc {spent} == the rows' own sum {shown:.6f}",
                )
            else:
                check(
                    spent is not None and shown <= float(spent) + 5e-6,
                    f"the {len(receipts)} shown rows sum to {shown:.6f}, "
                    f"inside spent_usdc {spent}",
                )
            bad_ref = next(
                (r.get("tx_ref") for r in receipts
                 if not _GATEWAY_REF.match(str(r.get("tx_ref") or ""))),
                None,
            )
            check(
                bad_ref is None,
                "every tx_ref is a Circle Gateway batch UUID"
                + (f" — got {str(bad_ref)[:28]!r}, a placeholder rather than a settlement"
                   if bad_ref is not None else ""),
            )
            want_net = settings.caip2()
            bad_net = next(
                (r.get("network") for r in receipts if r.get("network") != want_net),
                None,
            )
            check(
                bad_net is None,
                f"every receipt settled on {want_net}"
                + (f" — got {bad_net!r}, which is not this deployment's gate"
                   if bad_net is not None else ""),
            )
    pos, gap = body.get("position_contracts"), body.get("gap_contracts")
    if pos is not None:
        check(True, f"position {pos:+.2f} against a mandate of "
                    f"{body.get('target_contracts')} (gap {gap:+.2f})")


def verify_terminal(live_series: dict | None) -> None:
    print("\nterminal — the public surface")
    for path in ROUTES:
        status, _ = get(terminal_url(path, _audited_chain()))
        check(status == 200, f"{path} -> {status}")
    status, body = get(terminal_url("/api/futures", _audited_chain()))
    payload = (body or {}).get("data") or body or {}
    desks = payload.get("desks") or {}
    check(
        status == 200 and (bool(desks) or NO_VENUE),
        f"/api/futures -> {status}, {len(desks)} desk(s)"
        + (" — no series listed on this chain, declared" if NO_VENUE and not desks else ""),
        warn_only=NO_VENUE,
    )
    if desks and live_series is not None:
        # The check that would have caught a throttled crawl publishing a
        # settled series as the live desk.
        shown = {d.get("series_id") for d in desks.values()}
        check(
            live_series["series_id"] in shown and not any(d.get("settled") for d in desks.values()),
            f"terminal shows the live series {sorted(shown)}, none settled",
        )
    n_fills = len(payload.get("trades") or [])
    check(
        n_fills > 0 or NO_VENUE,
        f"{n_fills} fill(s) on the public tape"
        + (" — nothing to fill against yet, declared" if NO_VENUE and not n_fills else "")
        + ("" if n_fills else tape_cause(payload.get("freshness"))),
        warn_only=NO_VENUE,
    )


def mcp_rpc(chain_id: int, method: str, params: dict | None = None) -> tuple[int, dict | None]:
    """One JSON-RPC call to the hosted MCP server for `chain_id`. No Accept
    header, on purpose: a client that sends none must be answered too."""
    body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}
    return post(terminal_url("/api/mcp", chain_id), body)


def tool_answer(body: dict | None) -> tuple[dict | None, str]:
    """A `tools/call` reply as (the tool's JSON answer, why there is none).

    Three ways to fail look like success at the HTTP layer. A JSON-RPC error
    arrives with status 200. A failed tool is a RESULT with `isError`. A refusal
    the tool explains (bad arguments, a press that cannot answer) is a result
    without `isError`, carrying `available: false` and a reason. A gate that read
    only the status code would pass all three.
    """
    body = body or {}
    result = body.get("result")
    if not isinstance(result, dict):
        err = body.get("error") if isinstance(body.get("error"), dict) else {}
        return None, f"no result ({err.get('message') or 'unparseable reply'})"
    text = next(
        (c.get("text") for c in result.get("content") or [] if isinstance(c, dict) and c.get("type") == "text"),
        "",
    ) or ""
    if result.get("isError"):
        return None, f"tool error: {text[:90]}"
    try:
        answer = json.loads(text)
    except ValueError:
        return None, f"not JSON: {text[:90]}"
    if not isinstance(answer, dict):
        return None, f"not an object: {text[:90]}"
    if answer.get("available") is False:
        return None, f"refused: {str(answer.get('reason') or 'no reason given')[:90]}"
    return answer, ""


def verify_hosted_mcp(settings) -> None:
    print("\nhosted MCP — the endpoint an agent adds with no install")
    cid = int(settings.arc_chain_id)
    testnet = cid == TESTNET_CHAIN_ID

    status, body = mcp_rpc(cid, "initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "acr-verify-live", "version": "1"},
    })
    info = ((body or {}).get("result") or {}).get("serverInfo") or {}
    title = str(info.get("title") or "")
    # The title names the chain the server answers for. A testnet URL answered
    # by the mainnet server is the failure the `?chain=` param exists to prevent.
    check(
        status == 200 and info.get("name") == "acr-tca" and ("Testnet" in title) == testnet,
        f"handshake -> {status}, {title or 'no serverInfo'}",
    )

    status, body = mcp_rpc(cid, "tools/list")
    tools = ((body or {}).get("result") or {}).get("tools") or []
    offered = {t.get("name") for t in tools if isinstance(t, dict)}
    missing = sorted(HOSTED_MCP_TOOLS - offered)
    local = sorted(HOSTED_MCP_LOCAL_ONLY & offered)
    check(
        status == 200 and not missing and not local,
        f"tools/list -> {status}, {len(offered)} tool(s)"
        + (f" — MISSING {', '.join(missing)}" if missing else "")
        + (f" — OFFERS {', '.join(local)}, which need the agent's own machine" if local else ""),
    )

    # The identity check. Every print carries the oracle that holds it, and a
    # label can say testnet while the press behind it reads mainnet.
    _, body = mcp_rpc(cid, "tools/call", {"name": "get_rate", "arguments": {"index_id": "ACR-INF"}})
    rate, why = tool_answer(body)
    seen = str((rate or {}).get("oracle") or "")
    want = str(settings.oracle_address or "")
    if rate is None:
        check(False, f"get_rate ACR-INF -> {why}")
    else:
        check(
            bool(want) and seen.lower() == want.lower(),
            f"get_rate ACR-INF = {rate.get('value')}, from oracle {seen[:10]}…"
            + ("" if seen.lower() == want.lower()
               else f" — but this chain's oracle is {want[:10] or 'unset'}…: the hosted server reads another chain"),
        )

    _, body = mcp_rpc(cid, "tools/call", {"name": "check_spend", "arguments": HOSTED_MCP_BILL})
    spend, why = tool_answer(body)
    verdict = (spend or {}).get("verdict") or {}
    check(
        spend is not None and verdict.get("benchmarked") is True,
        "check_spend on the /developers example bill -> "
        + (str(verdict.get("verdict") or "benchmarked") if spend is not None and verdict.get("benchmarked")
           else why or f"not benchmarked: {verdict.get('reason') or 'no verdict'}"),
    )


def tape_cause(freshness: dict | None) -> str:
    """Why an empty tape is empty. The press states when it last READ the tape
    (`FuturesReader.freshness`): never means the read is failing (a throttled
    RPC), a recent read means there were simply no fills within its reach. The
    two call for opposite fixes, and `0 fill(s)` alone named neither."""
    if not isinstance(freshness, dict) or "tape_age_s" not in freshness:
        return ""
    age = freshness["tape_age_s"]
    if age is None:
        return " — the press has not managed to READ the tape since it booted (its RPC reads are failing)"
    return f" — read {age:.0f}s ago and empty: no fills within reach (is the venue's taker funded?)"


def verify_funding(w3, settings) -> None:
    """Gas is the failure nobody gets warned about: everything works until it
    doesn't, and then every pillar dies at once."""
    from index_api.desk import (
        FAUCET_RESERVE_USDC,
        FAUCET_USDC,
        PRESS_BURN_USDC_PER_DAY,
        PRESS_CRITICAL_FLOOR_USDC,
        post_reservation_usdc,
    )

    print("\nfunding — the runway under all of it")
    wallets: list[tuple[str, str, float]] = []
    # A balance is public information, so prefer a plain address over Circle
    # credentials. This check runs in CI, and the alternative — putting the
    # entity secret in repository secrets — would hand a warn-only balance read
    # the ability to move custody funds. Never buy a nice-to-have with an
    # escalation of what a compromised runner could do.
    custody = os.environ.get("ACR_CUSTODY_ADDRESS", "").strip()
    if not custody:
        try:
            from acr_oracle_client.signer import CircleWalletSigner

            custody = CircleWalletSigner(
                wallet_id=settings.circle_wallet_id,
                api_key=settings.circle_api_key,
                entity_secret=settings.circle_entity_secret,
                base_url=settings.circle_base_url,
            ).address
        except Exception as exc:
            check(
                False,
                f"custody wallet unknown — set ACR_CUSTODY_ADDRESS ({str(exc)[:40]})",
                warn_only=True,
            )
    if custody:
        wallets.append(("press wallet (funds every on-chain write)", custody, PRESS_BURN_USDC_PER_DAY))

    for label, addr, burn in wallets:
        bal = _rpc_retry(w3.eth.get_balance, w3.to_checksum_address(addr)) / 1e18
        days = bal / burn if burn > 0 else float("inf")
        # The runway line stays a WARNING and now says what it is: an estimate at
        # an assumed constant. Real burn scales with mirrored settlements — two
        # transactions per receipt — and on 2026-09-12 ran ~7x this figure.
        check(
            days >= MIN_RUNWAY_DAYS,
            f"{label}: {bal:.3f} USDC = {days:.0f} days at the assumed {burn}/day "
            "(real burn scales with mirroring; 2026-09-12 measured ~3/day)",
            warn_only=True,
        )
        # The floor FAILS. This is the one funding check that is about our code
        # continuing to run at all rather than about somebody's budget.
        # "Below the floor every write stops" was false, and the false version
        # cost a diagnosis: writes stop at a post's gas RESERVATION, ~0.0073 USDC
        # (see desk.post_reservation_usdc). Below the floor the runway is short;
        # below the reservation nothing can be sent at all, and that is said.
        try:
            base_fee = int(_rpc_retry(w3.eth.get_block, "latest")["baseFeePerGas"])
        except Exception:
            base_fee = None
        reserve = post_reservation_usdc(base_fee)
        check(
            bal >= PRESS_CRITICAL_FLOOR_USDC,
            f"{label} above the {PRESS_CRITICAL_FLOOR_USDC:.1f} USDC critical floor ({bal:.3f})"
            + (
                f" — posts are REFUSED NOW: each must reserve ~{reserve:.4f} up front"
                if bal < reserve
                else f" — {int(bal / reserve)} post reservations left; writes stop at ~{reserve:.4f}, not zero"
            ),
        )
        # The faucet and the press share this wallet, so say plainly how many
        # readers can still be onboarded before the floor bites.
        drips = max(0, int((bal - FAUCET_RESERVE_USDC) / FAUCET_USDC))
        check(
            drips > 0,
            f"{drips} faucet drip(s) left above the {FAUCET_RESERVE_USDC} USDC press floor",
            warn_only=True,
        )

    # The venue's own custody wallets. This section watched the press wallet
    # only, so the wallets that actually pay to roll a series and keep the tape
    # moving could run dry with nothing saying so — and on Arc "out of USDC"
    # means "cannot transact at all", not merely "cannot post collateral".
    from acr_oracle_client import build_role_signer

    for role, floor, why in (
        ("maker", VENUE_WALLET_FLOOR_USDC,
         "stands behind the book; pays to open + collateralize each series"),
        ("taker", TAKER_WALLET_FLOOR_USDC,
         "the hourly heartbeat that keeps the tape moving (gas only)"),
    ):
        try:
            sg = build_role_signer(role, settings)
            if sg is None or type(sg).__name__ != "CircleWalletSigner":
                continue  # not migrated on this deployment; nothing to check
            bal = _rpc_retry(w3.eth.get_balance, w3.to_checksum_address(sg.address)) / 1e18
        except Exception as exc:  # noqa: BLE001 — a verdict beats a traceback
            check(False, f"{role} wallet unreadable ({str(exc)[:40]})", warn_only=True)
            continue
        check(
            bal >= floor,
            f"{role} custody wallet {sg.address[:10]}…: {bal:.3f} USDC "
            f"(floor {floor}) — {why}",
            warn_only=True,
        )


def _settings_for_crons():
    from acr_core import get_settings

    return get_settings()


def verify_crons(w3) -> None:
    """The venue's chores now run on the TRUSTED HOST, not in Actions.

    So the question changed. It used to be "did GitHub fire the cron?" — which
    was always warn-only, because GitHub drops most scheduled ticks on a private
    repo and an alarm that cries wolf gets ignored. Now the honest question is
    "has the venue actually traded lately?", and the answer is on the chain
    rather than in a workflow run list. The workflows survive as dispatch-only
    fallbacks; their run age no longer says anything about liveness.
    """
    import subprocess

    print("\nautomation — the keeper on the trusted host, and the manual fallback")
    from acr_oracle_client import FuturesClient

    s = _settings_for_crons()
    try:
        fc = FuturesClient(rpc_url=s.arc_rpc_url, futures_address=s.futures_address)
        trades = fc.recent_trades(limit=5)
    except Exception as exc:  # noqa: BLE001 — a verdict beats a traceback
        check(False, f"could not read the tape ({str(exc)[:50]})", warn_only=True)
        trades = []
    if trades:
        # The BLOCK's timestamp, not `seen_at`: that field is stamped by the
        # service's FuturesReader when it first observes a tx, and a raw client
        # never sets it — so reading it here reported "999h ago" for a venue
        # that had traded twenty minutes earlier. A check that cries wolf gets
        # muted, which costs more than not having it.
        newest_block = max(int(x.get("block") or 0) for x in trades)
        try:
            newest = float(_rpc_retry(lambda: w3.eth.get_block(newest_block).timestamp))
        except Exception:  # noqa: BLE001
            newest = 0.0
        age_h = (time.time() - newest) / 3600 if newest else 999
        # The keeper trades hourly; two missed hours is a real signal, and it is
        # OUR code now rather than somebody else's scheduler — so it is worth
        # warning about where the cron age never was.
        check(age_h < 3, f"the venue traded {age_h:.1f}h ago (keeper cadence is hourly)",
              warn_only=True)
    else:
        check(False, "no fills on the tape at all", warn_only=True)

    for wf in ("futures-heartbeat.yml", "futures-lifecycle.yml"):
        try:
            out = subprocess.run(
                ["gh", "run", "list", "--workflow", wf, "--limit", "1",
                 "--json", "conclusion,createdAt", "-q",
                 '.[0] | "\\(.conclusion) \\(.createdAt)"'],
                capture_output=True, text=True, timeout=60,
            ).stdout.strip()
            concl, _, when = out.partition(" ")
            age = time.time() - time.mktime(time.strptime(when, "%Y-%m-%dT%H:%M:%SZ"))
            # Dispatch-only fallbacks: a stale run age means nobody needed
            # them, which is the point. Report it, never warn on it.
            print(f"  · {wf} (manual fallback): last run {concl}, {age / 3600:.1f}h ago")
        except Exception as exc:
            check(False, f"{wf}: could not read runs ({str(exc)[:40]})", warn_only=True)


def verify_route_drift() -> None:
    """Does this host serve what the repo builds? — `verify_deploy_drift.py`.

    Every other check in this file asks whether the deployment WORKS. This one
    asks whether it is the deployment this checkout describes, which is a
    different question and the one nothing asked: on 2026-10-06 the mainnet image
    served 44 routes against ~50 built, missing all five `/operator/*` and `/par`,
    so the product's own two pages rendered "No businesses onboarded yet" for a
    business that has nineteen decisions.

    WARN rather than fail by default, for the same reason the cron checks do: a
    deploy lagging a merge is somebody's scheduler, not our code being wrong. The
    run that cares is `VERIFY_STRICT=1`, where `check` promotes it — which is the
    run to use before claiming a surface is live.
    """
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
    from verify_deploy_drift import deployed_paths, repo_paths

    built = repo_paths()
    served = deployed_paths(API)
    if served is None:
        check(True, "route drift: /openapi.json unreadable, so nothing to compare", warn_only=True)
        return

    repo_only = sorted(built - served)
    deploy_only = sorted(served - built)
    check(
        not repo_only,
        f"built {len(built)} · served {len(served)}"
        + (f" · {len(repo_only)} BUILT BUT NOT SERVED: {', '.join(repo_only[:4])}"
           f"{'…' if len(repo_only) > 4 else ''}" if repo_only else ""),
        warn_only=True,
    )
    if deploy_only:
        check(
            False,
            f"{len(deploy_only)} route(s) SERVED BUT NOT BUILT — a stale image: "
            f"{', '.join(deploy_only[:4])}{'…' if len(deploy_only) > 4 else ''}",
            warn_only=True,
        )


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)
    from acr_core import get_settings
    from web3 import Web3

    s = get_settings()
    print(f"ACR liveness — api {API}\n               terminal {TERMINAL}")
    w3 = Web3(Web3.HTTPProvider(s.arc_rpc_url, request_kwargs={"timeout": 25}))
    if not check(w3.is_connected(), f"Arc RPC reachable ({s.arc_rpc_url})"):
        print("\nliveness: FAILED — no chain, nothing else is meaningful")
        sys.exit(1)
    cid = int(_rpc_retry(lambda: w3.eth.chain_id))
    check(
        cid == s.arc_chain_id,
        f"chain id {cid}" if cid == s.arc_chain_id
        else f"chain id {cid} from the RPC, but ACR_ARC_CHAIN_ID says {s.arc_chain_id}",
    )

    section(verify_oracle, w3, s)
    section(verify_oracle_v2, w3, s)
    live = section(verify_venue, w3, s)
    section(verify_tape, s)
    section(verify_seller)
    section(verify_x402)
    section(verify_agent)
    section(verify_humans)
    section(verify_desk, live)
    section(verify_revenue, s)
    section(verify_hedger, s)
    section(verify_terminal, live)
    section(verify_hosted_mcp, s)
    section(verify_funding, w3, s)
    section(verify_crons, w3)
    section(verify_route_drift)

    print()
    if _warnings:
        print(f"  {len(_warnings)} warning(s) — degraded, not down:")
        for w in _warnings:
            print(f"    ! {w}")
    if _failures:
        print(f"\nliveness: FAILED — {len(_failures)} check(s) a visitor would notice")
        for f in _failures:
            print(f"    ✗ {f}")
        sys.exit(1)
    print("liveness: ALL PILLARS LIVE")
    sys.exit(0)


if __name__ == "__main__":
    main()
