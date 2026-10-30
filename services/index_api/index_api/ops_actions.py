"""Operator actions — the Makefile's money-moving targets, behind a key.

Everything here was a terminal ritual: settle the expired series, roll to a
fresh one, deepen the book, move USDC to the wallet that ran dry. The scripts
that do these things are still the reference implementations and still exist;
this is the subset an operator needs to reach from a phone when the venue is
degraded and there is no laptop.

Four rules hold regardless of what the UI sends, because a UI is not a
security boundary:

  1. **Disabled by default.** With no ``ACR_OPS_TOKEN`` set, the action routes
     404 — not 401. An endpoint that admits it exists is an endpoint worth
     guessing at, and the overwhelmingly common deployment wants none of this.
  2. **Dry-run is the default.** Every action prices itself and reports what it
     WOULD do unless the caller explicitly passes ``dry_run: false``. A missing
     field can therefore never spend money; only a present one can.
  3. **Caps are enforced here, not in the form.** The per-run USDC ceilings
     below are the same ones the scripts enforce, and they bind even if the
     request asks for more.
  4. **Everything is written down.** Every attempt — refused, dry-run or
     executed — appends to the audit log before the caller sees the result.

What is deliberately NOT here: ``setSigner``, ``transferOwnership``, and
contract deploys. Those change who controls the system rather than what it is
doing, they are needed roughly once, and a bearer token is not the right key
for them. They stay with the deploy scripts and a human.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import threading
import time

log = logging.getLogger("acr.ops.actions")

#: The key. Unset → the whole surface is off (routes 404).
TOKEN_ENV = "ACR_OPS_TOKEN"
#: Ceiling on a single collateral top-up. Mirrors COLLATERALIZE_MAX_USDC in
#: scripts/futures_collateralize.py — the operator console must not be a way
#: around a limit the CLI respects.
MAX_COLLATERALIZE_USDC = float(os.environ.get("COLLATERALIZE_MAX_USDC", "2.0"))
#: Ceiling on a single treasury→role transfer.
MAX_FUND_USDC = float(os.environ.get("OPS_MAX_FUND_USDC", "5.0"))
#: Ceiling on a single owner-approved payment. The PolicyWallet cap is the real
#: authority and it is enforced on chain; this is a second, smaller fence around
#: THIS console, because a bearer token is a weaker key than a wallet and should
#: not be able to spend the whole budget in one call.
MAX_APPROVE_USDC = float(os.environ.get("OPS_MAX_APPROVE_USDC", "25.0"))
#: Where the audit trail lives. Ephemeral on a free-tier host, which is why the
#: log is served back rather than merely written: an operator reading it in the
#: same session it was written is the case that matters.
AUDIT_PATH = os.environ.get("ACR_OPS_AUDIT_PATH", "data/ops_actions.jsonl")

_audit_lock = threading.Lock()


class ActionError(Exception):
    """A refusal with an HTTP status and a sentence an operator can act on."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def enabled() -> bool:
    return bool(os.environ.get(TOKEN_ENV, "").strip())


#: How many REJECTED keys this process will consider before it stops listening,
#: and for how long. Deliberately generous: it is a runaway guard, not the thing
#: standing between an attacker and the venue — that is the key's own entropy.
MAX_BAD_KEYS = int(os.environ.get("ACR_OPS_MAX_BAD_KEYS", "20"))
BAD_KEY_WINDOW_S = float(os.environ.get("ACR_OPS_BAD_KEY_WINDOW_S", "900"))


def authorize(token: str | None) -> None:
    """Constant-time check. Raises 404 when the surface is off, so a probe
    cannot distinguish "wrong key" from "no console here".

    **Failures are counted; successes are not.** The obvious design — a
    per-caller budget — does not work here and would actively hurt. An attacker
    guessing keys presents a different token every attempt, so a per-token
    counter never trips; and a per-IP counter is a per-EVERYONE counter, because
    the Render proxy makes every request share one source address. Rate-limiting
    the surface as a whole would therefore hand any stranger the power to lock
    the operator out of their own emergency controls during an incident — a
    worse failure than the guessing it prevents.

    Counting only rejections gets both: guessing is bounded, while an operator
    holding the right key is never rate-limited by someone else's noise.
    """
    from . import ratelimit

    expected = os.environ.get(TOKEN_ENV, "").strip()
    if not expected:
        raise ActionError(404, "not found")
    if not token or not hmac.compare_digest(str(token), expected):
        # One shared key: the bucket counts wrong answers, not callers.
        if not ratelimit._limiter.allow("ops:badkey", MAX_BAD_KEYS, BAD_KEY_WINDOW_S):
            raise ActionError(
                429, "too many bad operator keys — this console is not listening for a while"
            )
        raise ActionError(401, "bad operator key")


# --- audit ------------------------------------------------------------------


def record(action: str, params: dict, dry_run: bool, ok: bool,
           result: dict | None = None, error: str | None = None) -> dict:
    entry = {
        "at": time.time(),
        "action": action,
        "params": params,
        "dry_run": dry_run,
        "ok": ok,
        "result": result,
        "error": error,
    }
    try:
        with _audit_lock:
            os.makedirs(os.path.dirname(AUDIT_PATH) or ".", exist_ok=True)
            with open(AUDIT_PATH, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
    except Exception:  # pragma: no cover - a full disk must not eat the verdict
        log.warning("ops audit write failed", exc_info=True)
    return entry


def recent(limit: int = 50) -> list[dict]:
    try:
        with open(AUDIT_PATH, encoding="utf-8") as fh:
            rows = [json.loads(line) for line in fh if line.strip()]
    except FileNotFoundError:
        return []
    except Exception:  # pragma: no cover
        log.warning("ops audit read failed", exc_info=True)
        return []
    return rows[-limit:][::-1]


# --- helpers ----------------------------------------------------------------


def _venue(role: str = "maker"):
    """A write-capable venue client for ``role``, or a refusal that names what
    is missing rather than a generic 500."""
    from acr_core import get_settings
    from acr_oracle_client import FuturesClient, build_role_signer

    s = get_settings()
    if not s.futures_address:
        raise ActionError(503, "no futures venue configured")
    signer = build_role_signer(role, s)
    if signer is None:
        raise ActionError(503, f"no signer configured for the {role} role")
    fc = FuturesClient(
        rpc_url=s.arc_rpc_url, futures_address=s.futures_address, signer=signer
    )
    if not fc.can_write():
        raise ActionError(503, f"the {role} client cannot write")
    return fc, signer, s


_OWNER_ABI = [
    {"type": "function", "name": "owner", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "address"}]},
]
#: Roles worth trying for an owner-only call, best custody first. `owner` is in
#: the list but NOT preferred: `build_role_signer("owner", …)` falls back to the
#: ambient poster key when `circle_owner_wallet_id` is unset, and that key is
#: the retired deploy EOA.
_OWNER_CANDIDATES = ("maker", "owner", "poster")


def _owner_signer():
    """A signer that ACTUALLY owns the venue, chosen by asking the chain.

    ``setPaused`` is ``onlyOwner``. Naming a role and hoping is how this project
    already lost a roll: ``scripts/futures_roll.py`` records ``openSeries``
    reverting "not owner" after the 2026-08-03 handover, because the role that
    claimed ownership resolved to a raw key holding nothing. Ownership is a fact
    on-chain — read it, then pick the signer whose address matches.

    Returns ``(client, signer, role, owner)``. Raises a NAMED refusal when no
    configured signer matches, so the dry run can say so instead of the operator
    discovering it from a reverted transaction ninety seconds into an incident.
    """
    from acr_core import get_settings
    from acr_oracle_client import FuturesClient, build_role_signer
    from acr_oracle_client.futures import _rpc_retry
    from web3 import Web3

    from . import keeper

    s = get_settings()
    if not s.futures_address:
        raise ActionError(503, "no futures venue configured")
    try:
        w3 = Web3(Web3.HTTPProvider(s.arc_rpc_url, request_kwargs={"timeout": 20}))
        c = w3.eth.contract(
            address=w3.to_checksum_address(s.futures_address), abi=_OWNER_ABI
        )
        owner = str(_rpc_retry(c.functions.owner().call))
    except Exception as exc:  # noqa: BLE001
        raise ActionError(503, f"could not read the venue's owner: {str(exc)[:80]}") from exc

    tried: list[str] = []
    for role in _OWNER_CANDIDATES:
        try:
            sg = build_role_signer(role, s)
        except Exception:  # noqa: BLE001 - a misconfigured role is not fatal here
            continue
        addr = getattr(sg, "address", "") if sg else ""
        if not addr:
            continue
        tried.append(f"{role}={addr[:10]}…")
        # Same comparison the keeper uses to decide it may open a series.
        if keeper.may_open_series(owner, addr):
            fc = FuturesClient(
                rpc_url=s.arc_rpc_url, futures_address=s.futures_address, signer=sg
            )
            if not fc.can_write():
                raise ActionError(503, f"the {role} wallet owns the venue but cannot write")
            return fc, sg, role, owner
    raise ActionError(
        503,
        f"no configured wallet owns this venue (owner {owner[:10]}…; "
        f"tried {', '.join(tried) or 'nothing'}) — ownership moved, or the "
        "owner's credentials are not on this host",
    )


def _cap(value: float, ceiling: float, what: str) -> float:
    v = float(value)
    if v <= 0:
        raise ActionError(400, f"{what} must be positive")
    if v > ceiling:
        raise ActionError(
            400, f"{what} is capped at {ceiling:.2f} USDC per run — asked for {v:.2f}"
        )
    return v


# --- actions ----------------------------------------------------------------
#
# Each returns a plain dict. In dry-run it describes what WOULD happen, with
# enough of the real state read to be worth reading; live, it reports the tx.


def keeper_heartbeat(params: dict, dry_run: bool) -> dict:
    """Clear the heartbeat cooldown so the next warm tick trades."""
    from . import keeper

    if dry_run:
        st = keeper.status()
        return {"would": "clear the heartbeat cooldown", "current": st.get("heartbeat")}
    keeper._last_heartbeat = 0.0
    return {"cleared": "heartbeat cooldown", "note": "the next warm tick will trade"}


def keeper_roll_check(params: dict, dry_run: bool) -> dict:
    """Clear the roll-check cooldown so the next warm tick re-examines expiry."""
    from . import keeper

    if dry_run:
        st = keeper.status()
        return {"would": "clear the roll-check cooldown", "current": st.get("roll")}
    keeper._last_roll_check = 0.0
    return {"cleared": "roll cooldown", "note": "the next warm tick will re-check"}


def verify_run(params: dict, dry_run: bool) -> dict:
    """Recompute the systems ledger now instead of waiting for its timer."""
    from . import ops

    if dry_run:
        return {"would": "run a full systems sweep", "cadence_s": ops.OPS_VERIFY_S}
    led = ops.run_all()
    return {
        "verdict": led["verdict"],
        "failures": led["failures"],
        "warnings": led["warnings"],
        "unknowns": led["unknowns"],
        "duration_s": led["duration_s"],
    }


def venue_settle(params: dict, dry_run: bool) -> dict:
    """Settle an expired series with the project's own wallet.

    The complement to the reader-paid path on the Public Desk, not a
    replacement for it: the same freshness rule applies, and it is checked by
    the same function, so the two cannot disagree about what is settleable.
    """
    from .desk import DeskError, _settle_precheck

    sid = params.get("series_id")
    if sid is None:
        raise ActionError(400, "series_id is required")
    try:
        sid, age_s, max_age = _settle_precheck(int(sid))
    except DeskError as e:
        raise ActionError(e.status, str(e)) from e
    if dry_run:
        return {
            "would": f"settle series {sid}",
            "print_age_min": round(age_s / 60, 1),
            "window_min": round(max_age / 60),
        }
    fc, _, _ = _venue("maker")
    tx = fc.settle(sid)
    return {"settled": sid, "tx": tx}


def venue_roll(params: dict, dry_run: bool) -> dict:
    """Run the keeper's own roll check immediately.

    Deliberately delegates rather than reimplementing: the keeper decides
    whether a roll is due, whether this wallet may open a series, and what a
    healthy successor's collateral is. A second opinion here would eventually
    disagree with the unattended one, and the unattended one runs far more.
    """
    from . import keeper
    from .onchain import get_futures

    if dry_run:
        return {
            "would": "run the keeper's roll check now",
            "current": keeper.status().get("roll"),
        }
    keeper._last_roll_check = 0.0
    verdict = keeper.roll_if_needed(get_futures())
    keeper.record("roll", verdict)
    return {"verdict": verdict or "nothing was due"}


def venue_withdraw(params: dict, dry_run: bool) -> dict:
    """Reclaim the project's own collateral, across every series.

    The Public Desk gives a *reader* an exit; this is the same exit for the keys
    the project runs, which the desk cannot serve because they are not Circle
    user wallets. It matters because collateral is PER SERIES: a roll leaves a
    stake on the retired series, where nothing trades and nothing reclaims it,
    and the balance is invisible unless something walks the whole venue looking
    for it. The console could already `venue/roll` — i.e. strand collateral —
    without being able to get it back.

    Sizes with ``desk.free_collateral_units``, the SAME function that quotes
    readers. If this and the desk ever disagree about what is free, one of them
    is lying to somebody about their money.
    """
    from .desk import _margin_bps, free_collateral_units
    from .onchain import get_futures, get_reader

    role = str(params.get("role", "maker")).strip().lower()
    if role not in ("maker", "taker"):
        raise ActionError(400, "role must be maker or taker")
    only = params.get("series_id")

    fc, sg, _ = _venue(role)
    me = getattr(sg, "address", "")
    futures = get_futures()
    series = futures.all_series()
    if only is not None:
        series = [x for x in series if int(x["series_id"]) == int(only)]
        if not series:
            raise ActionError(404, f"no series #{only} on this venue")

    margin_bps = _margin_bps()
    reader = get_reader()
    claims: list[dict] = []
    for x in series:
        sid = int(x["series_id"])
        units = fc.collateral_units_of(sid, me)
        if units is None:
            # Refuse to guess — the script exits 1 here for the same reason.
            raise ActionError(503, f"series {sid}: could not read collateral; not guessing")
        if units == 0:
            continue
        pos = fc.position_of(sid, me) or {"contracts": 0.0}
        contracts = float(pos["contracts"])
        settled = bool(x["settled"])
        # An OPEN position must be sized against the LIVE oracle mark the
        # contract will margin against. A settled series' settlement_price is 0
        # until it settles, and quoting against zero would report the whole
        # balance as free and then revert "below margin" after paying gas.
        mark = 0.0
        if not settled and contracts != 0:
            print_ = reader.read_all().get(x["index_id"]) or reader.read(x["index_id"])
            mark = float((print_ or {}).get("value") or 0.0)
            if mark <= 0:
                claims.append({
                    "series_id": sid, "index_id": x["index_id"],
                    "held_usdc": round(units / 1e6, 6), "free_usdc": 0.0,
                    "note": "open position and no live mark: wait for the next print",
                })
                continue
        free = free_collateral_units(
            units, contracts, mark, x["multiplier"], margin_bps, settled
        )
        claims.append({
            "series_id": sid,
            "index_id": x["index_id"],
            "state": "settled" if settled else f"open, {contracts:+.2f} contracts",
            "held_usdc": round(units / 1e6, 6),
            "free_usdc": round(free / 1e6, 6),
            "_units": free,
        })

    free_total = sum(c.get("_units", 0) for c in claims)
    view = [{k: v for k, v in c.items() if k != "_units"} for c in claims]
    if dry_run:
        return {
            "would": f"withdraw {free_total / 1e6:.6f} USDC to the {role} wallet",
            "wallet": me,
            "series": view,
        }
    if free_total <= 0:
        raise ActionError(409, "nothing is free to withdraw right now")
    txs = []
    for c in claims:
        if c.get("_units", 0) > 0:
            txs.append({
                "series_id": c["series_id"],
                "tx": fc.withdraw_collateral(c["series_id"], c["_units"]),
                "usdc": c["free_usdc"],
            })
    return {"withdrawn_usdc": round(free_total / 1e6, 6), "to": role, "txs": txs}


def venue_collateralize(params: dict, dry_run: bool) -> dict:
    """Deepen the book by posting more maker collateral on a live series.

    Sized in USDC here rather than in trades. ``scripts/futures_collateralize.py``
    sizes by reader-sized trades and verifies with four independent witnesses,
    and it remains the right tool when there is a laptop; this is the blunt
    version for when there is not, which is why the per-run cap is low.
    """
    from .onchain import get_futures

    usdc = _cap(params.get("usdc", 0), MAX_COLLATERALIZE_USDC, "collateral")
    sid = params.get("series_id")
    if sid is None:
        raise ActionError(400, "series_id is required")
    sid = int(sid)
    series = next(
        (x for x in get_futures().all_series() if int(x["series_id"]) == sid), None
    )
    if series is None:
        raise ActionError(404, f"no series #{sid}")
    if series["settled"]:
        raise ActionError(409, "that series is settled — collateral cannot be posted to it")
    if dry_run:
        return {
            "would": f"post {usdc:.2f} USDC of collateral on series {sid}",
            "index_id": series["index_id"],
            "cap_usdc": MAX_COLLATERALIZE_USDC,
        }
    fc, _, _ = _venue("maker")
    tx = fc.post_collateral(sid, usdc)
    return {"posted_usdc": usdc, "series_id": sid, "tx": tx}


def funding_move(params: dict, dry_run: bool) -> dict:
    """Move USDC from the treasury wallet to a role wallet.

    **Read /1e18, write x1e6.** USDC *is* Arc's native token and the
    0x3600…0000 predeploy is an ERC-20 *view* of that same balance — so the two
    halves of this function legitimately disagree about decimals: the balance
    read below goes through ``_wallet_usdc`` (native, 18-decimal) while the
    transfer is ERC-20 ``transfer(address,uint256)`` calldata against the
    predeploy (6-decimal). That asymmetry is deliberate and matches the proven
    path in ``desk._send_stake``. Do not "simplify" either side to match the
    other: getting it backwards moves a millionth of the intended amount, or a
    million times it, which is the bug scripts/fund_role.py exists to warn about.
    """
    from acr_core import get_settings
    from acr_oracle_client import build_role_signer

    from .desk import FAUCET_RESERVE_USDC, FAUCET_USDC, USDC_PREDEPLOY, _wallet_usdc

    to_role = str(params.get("role", "")).strip().lower()
    if to_role not in ("maker", "taker"):
        raise ActionError(400, "role must be maker or taker")
    usdc = _cap(params.get("usdc", 0), MAX_FUND_USDC, "transfer")
    # Clamped at zero, because it is caller-supplied and it SUBTRACTS. A
    # negative value makes the subtrahend negative, inflating `free` above the
    # true headroom — one negative integer would walk straight through the
    # faucet reserve guard below.
    keep_drips = max(0, int(params.get("keep_drips", 4)))

    s = get_settings()
    src = build_role_signer("poster", s)
    dst = build_role_signer(to_role, s)
    if src is None or dst is None:
        raise ActionError(503, "both the treasury and the destination need a configured signer")
    # The failure this guards: build_role_signer falls back to any raw key it
    # can find, and ACR_POSTER_PRIVATE_KEY is usually present — so the
    # "treasury" leg would be signed by the retired deploy EOA, which holds
    # nothing. Assert Circle custody on both ends before moving anything.
    for name, sg in (("treasury", src), (to_role, dst)):
        if type(sg).__name__ != "CircleWalletSigner":
            raise ActionError(
                503, f"the {name} signer is not a Circle custody wallet — refusing to send"
            )

    # The treasury also pays every reader's stake. A transfer that leaves the
    # faucet unable to drip has traded a deeper book for a demo nobody can
    # start, so this is a REFUSAL rather than a clamp — a caller asking for
    # more than is free gets told, not quietly sent less.
    balance = _wallet_usdc(getattr(src, "address", "")) or 0.0
    free = round(max(0.0, balance - FAUCET_RESERVE_USDC - keep_drips * FAUCET_USDC), 6)
    if usdc > free:
        raise ActionError(
            409,
            f"{usdc:.2f} would leave the faucet unable to pay {keep_drips} more "
            f"stakes — only {free:.2f} USDC may leave (balance {balance:.2f}, "
            f"reserve {FAUCET_RESERVE_USDC:.2f})",
        )

    if dry_run:
        return {
            "would": f"send {usdc:.2f} USDC treasury → {to_role}",
            "from": getattr(src, "address", None),
            "to": getattr(dst, "address", None),
            "treasury_usdc": round(balance, 4),
            "free_to_send_usdc": free,
            "cap_usdc": MAX_FUND_USDC,
        }
    # Read /1e18, write x1e6: USDC IS Arc's native token and the predeploy is
    # its ERC-20 view of the same balance, so a balance read is 18-decimal and
    # a transfer amount is 6-decimal. Backwards moves a millionth of the
    # intended amount, or a million times it.
    units = int(round(usdc * 1_000_000))
    calldata = (
        "0xa9059cbb"
        + str(dst.address).lower().replace("0x", "").rjust(64, "0")
        + hex(units)[2:].rjust(64, "0")
    )
    tx = src.send_transaction(None, {"to": USDC_PREDEPLOY, "data": calldata})
    return {"sent_usdc": usdc, "to": to_role, "tx": tx}


def venue_pause(params: dict, dry_run: bool) -> dict:
    """Halt or resume the venue. The loudest thing on this page.

    Pausing stops every reader mid-session, so it asks for the word rather than
    a boolean the UI could send by accident.
    """
    paused = bool(params.get("paused"))
    if params.get("confirm") != "pause":
        raise ActionError(400, 'pausing requires confirm: "pause"')
    # Resolve the signer in BOTH modes. A dry run that skips this is worse than
    # no dry run: it shows green, and the operator learns the venue is owned by
    # someone else from a reverted transaction, ninety seconds into the incident
    # this control exists for.
    fc, sg, role, owner = _owner_signer()
    if dry_run:
        return {
            "would": f"set the venue paused = {paused}",
            "signed_by": f"{role} ({getattr(sg, 'address', '?')})",
            "owner_on_chain": owner,
            "custody": type(sg).__name__,
        }
    c = fc._contract()
    tx = fc._send(c.functions.setPaused(paused))
    return {"paused": paused, "signed_by": role, "tx": tx}


# --- the escalation inbox ---------------------------------------------------


def _pending(business: str, obligation_id: str) -> dict:
    """The escalated decision somebody is being asked to settle.

    Read from the operator's own log rather than passed in by the caller: the
    amount, the payee and the category must be the ones the agent recorded when
    it stopped, not whatever a form posts. Otherwise this console would be a way
    to pay an arbitrary address an arbitrary amount and have it filed as an
    approval of something else.
    """
    from .payments import owner_inbox_business
    from .statement import pending_escalations, read_decisions

    if owner_inbox_business(business):
        raise ActionError(409, "This business uses the owner-wallet inbox; legacy approval/rejection is disabled")
    rows = read_decisions(business=business)
    if not rows:
        raise ActionError(404, f"no decisions recorded for {business!r}")
    for d in pending_escalations(rows):
        if d.get("obligation_id") == obligation_id:
            return d
    raise ActionError(
        404, f"{obligation_id!r} is not waiting on anybody for {business!r}"
    )


def _business_wallet(business: str):
    """The business and a client for its wallet, or a refusal that says which."""
    from acr_oracle_client.policy import PolicyClient

    from .businesses import resolve

    b = resolve(business)
    if b is None:
        raise ActionError(404, f"no business registered as {business!r}")
    if not b.policy_wallet:
        raise ActionError(
            400,
            f"{b.slug} has no PolicyWallet, so there is nothing to pay from "
            "(it is being priced and metered, not spent for)",
        )
    return b, PolicyClient(wallet_address=b.policy_wallet)


def operator_approve(params: dict, dry_run: bool) -> dict:
    """Pay an escalated obligation from the OWNER's own wallet.

    The agent cannot do this and that is the point: above the per-transaction
    limit `PolicyWallet.spend` reverts, and `spendAsOwner` checks `msg.sender`.
    So this action does not grant the agent authority it lacks — it asks the
    owner's wallet to act, and the owner's key is what signs the transaction.

    The decision hash is re-derived from the LOGGED record through
    `hashable_record`, so the commitment the chain stores is the reasoning the
    agent actually wrote down when it stopped, hours earlier.
    """
    from .operator import hashable_record

    business = str(params.get("business") or "").strip()
    obligation_id = str(params.get("obligation_id") or "").strip()
    if not business or not obligation_id:
        raise ActionError(400, "approve needs a business and an obligation_id")

    row = _pending(business, obligation_id)
    b, client = _business_wallet(business)

    amount = float(row.get("billed_usdc") or 0.0)
    _cap(amount, MAX_APPROVE_USDC, "an approved payment")
    vendor = str(row.get("vendor") or "")
    category = str(row.get("category") or "")
    if not vendor or not category or amount <= 0:
        raise ActionError(422, f"the recorded decision for {obligation_id!r} is not payable")

    # Resolve the owner key in BOTH modes. A dry run that skips it shows green and
    # the operator learns the wallet is owned by someone else from a reverted
    # transaction — venue_pause takes the same care for the same reason.
    if not client.can_escalate():
        raise ActionError(
            400, "no owner key is configured, so nobody here can approve this"
        )
    # AND THE WALLET HAS TO BE THERE. The dry run is the gate, and it showed
    # green against an address with no code on this chain: `can_escalate()` is
    # only "is there an owner key", and a CALL to nothing neither reverts nor
    # fails to estimate. The live call then returned a real transaction hash for
    # a payment that moved nothing, and `_settle` wrote it down as evidence.
    status = client.wallet_status() if hasattr(client, "wallet_status") else "ok"
    if status != "ok":
        raise ActionError(
            400,
            {
                "no_wallet": f"{b.slug} has no wallet address",
                "no_rpc": "the chain could not be reached, so nothing is approved",
                "not_on_this_chain": (
                    f"{b.slug}'s wallet {b.policy_wallet} has no contract on the "
                    "chain this service reads. Paying there would record a "
                    "payment that never happened"
                ),
            }[status],
        )

    record = hashable_record(row)
    if dry_run:
        return {
            "would": f"pay {amount:.6f} USDC to {vendor} from {b.slug}'s owner wallet",
            "business": b.slug,
            "obligation_id": obligation_id,
            "category": category,
            "rule_it_stopped_on": row.get("rule"),
            "owner": getattr(client.owner_signer, "address", "?"),
            "custody": type(client.owner_signer).__name__,
            "budget": client.budget(category),
        }

    tx = client.spend_as_owner(category, vendor, amount, record)
    _settle(row, business, intent="pay", rule=f"owner approved: {row.get('rule')}",
            paid_usdc=amount, tx=tx)
    return {
        "business": b.slug, "obligation_id": obligation_id,
        "paid_usdc": amount, "to": vendor, "tx": tx,
    }


def operator_reject(params: dict, dry_run: bool) -> dict:
    """Decline an escalated obligation. Writes the refusal down and pays nothing.

    A rejection is a decision, so it clears the queue the same way an approval
    does — and it is recorded, because "the owner said no" is a fact a later
    reader needs as much as "the owner said yes".
    """
    business = str(params.get("business") or "").strip()
    obligation_id = str(params.get("obligation_id") or "").strip()
    if not business or not obligation_id:
        raise ActionError(400, "reject needs a business and an obligation_id")

    row = _pending(business, obligation_id)
    note = str(params.get("note") or "").strip()[:200]

    if dry_run:
        return {
            "would": f"decline {obligation_id} and record the refusal",
            "business": business,
            "billed_usdc": row.get("billed_usdc"),
            "rule_it_stopped_on": row.get("rule"),
        }

    _settle(row, business, intent="refuse",
            rule=f"owner rejected: {note or 'no reason given'}")
    return {"business": business, "obligation_id": obligation_id, "rejected": True}


def _settle(row: dict, business: str, *, intent: str, rule: str,
            paid_usdc: float = 0.0, tx: str | None = None) -> None:
    """Append the human's decision to the operator's log.

    Appended, never an edit: the escalation stays in the record exactly as the
    agent wrote it, and the resolution sits after it. `pending_escalations` reads
    the pair and stops showing the item, which is why approving clears the queue
    without anything being rewritten.

    MARKED `actor="owner"`, AND THAT IS NOT DECORATION. This row was written as a
    plain ``pay``, identical in every field to one the agent reached alone, so
    every figure downstream counted an owner's approval as autonomous work: it
    landed in `decided` and in `moved_usdc` with nothing to tell it apart. "How
    many obligations were settled WITHOUT A HUMAN TOUCHING THEM" was unanswerable
    from a log where the human left no mark.

    THE EVIDENCE COMES FORWARD TOO. The escalation carried the meter, the screen
    and the benchmark; this row carried none of them, so the ledger export and
    the audit saw a payment with no counterparty verdict and no metered count,
    and `ledger_audit` would read a settled obligation as unscreened. The facts
    did not change when a person agreed with them.
    """
    from .operator import ObligationDecision, log_decision

    def _num(key: str):
        v = row.get(key)
        return float(v) if isinstance(v, (int, float)) else None

    log_decision(
        ObligationDecision(
            at=time.time(),
            obligation_id=str(row.get("obligation_id") or ""),
            vendor=str(row.get("vendor") or ""),
            category=str(row.get("category") or ""),
            billed_usdc=float(row.get("billed_usdc") or 0.0),
            intent=intent,
            rule=rule,
            business=business,
            resource=str(row.get("resource") or ""),
            escalated=False,
            paid_usdc=paid_usdc,
            tx=tx,
            actor="owner",
            # What the agent said it would do, carried so the two can be
            # compared. This is the whole of "how often the human agreed".
            recommended_intent=str(row.get("recommended_intent") or ""),
            metered_quantity=_num("metered_quantity"),
            vendor_quantity=_num("vendor_quantity"),
            discrepancy=_num("discrepancy"),
            screen_risk=str(row.get("screen_risk") or ""),
            screen_matched=list(row.get("screen_matched") or []),
            screen_backend=str(row.get("screen_backend") or ""),
            par_usdc=_num("par_usdc"),
            best_usdc=_num("best_usdc"),
            over_par_bp=_num("over_par_bp"),
            saving_usdc=_num("saving_usdc"),
            due_at=_num("due_at"),
            # THE WINDOW THE ESCALATED BILL COVERED, carried forward. Dropped
            # here it would default to 0, `settled_through` would fall back to
            # the approval's own timestamp, and every settlement between the
            # period's true end and the moment the owner got round to it would
            # fall behind the next window — consumption that is never billed
            # again, because the boundary moved past it.
            period_start=_num("period_start") or 0.0,
            period_end=_num("period_end") or 0.0,
            invoice_ref=str(row.get("invoice_ref") or ""),
        )
    )


#: name → (handler, one-line description). The registry IS the allowlist: an
#: action not in here cannot be reached, whatever the proxy forwards.
ACTIONS = {
    "keeper/heartbeat": (keeper_heartbeat, "clear the heartbeat cooldown"),
    "keeper/roll-check": (keeper_roll_check, "clear the roll-check cooldown"),
    "verify/run": (verify_run, "recompute the systems ledger now"),
    "venue/settle": (venue_settle, "settle an expired series"),
    "venue/roll": (venue_roll, "run the keeper's roll check now"),
    "venue/collateralize": (venue_collateralize, "post more maker collateral"),
    "venue/withdraw": (venue_withdraw, "reclaim our collateral across every series"),
    "funding/move": (funding_move, "move USDC treasury → role wallet"),
    "venue/pause": (venue_pause, "halt or resume the venue"),
    "operator/approve": (operator_approve, "pay an escalated bill from the owner's wallet"),
    "operator/reject": (operator_reject, "decline an escalated bill, and say so"),
}


def run(action: str, params: dict, dry_run: bool) -> dict:
    """Dispatch one action, auditing every outcome including the refusals."""
    entry = ACTIONS.get(action)
    if entry is None:
        raise ActionError(404, f"unknown action {action!r}")
    fn, _desc = entry
    try:
        result = fn(params or {}, dry_run)
    except ActionError as e:
        record(action, params or {}, dry_run, False, error=str(e))
        raise
    except Exception as e:  # noqa: BLE001
        log.warning("ops action %s failed", action, exc_info=True)
        record(action, params or {}, dry_run, False, error=str(e)[:200])
        raise ActionError(500, f"{action} failed: {str(e)[:160]}") from e
    record(action, params or {}, dry_run, True, result=result)
    return {"action": action, "dry_run": dry_run, "result": result}
