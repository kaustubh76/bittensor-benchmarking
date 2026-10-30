"""The shop floor's "release the buyer" run — a real x402 loop, in-page.

The shop floor was the `/exchange` page until it folded into `/curve`; the ASGI
base URL below still says `exchange.floor`, which is an internal hostname no
request ever leaves the process with, and renaming it would churn a transport
fixture for nothing.

``POST /demo/buyer/start`` claims the single-run slot and drives N paid
queries through the app's OWN payment gate: bare request → 402 challenge
(spec ``{x402Version, accepts}`` envelope) → priced retry with the mock
header → 200 + settlement confirmation → receipt on the public ledger. Each
step is a full ASGI round-trip through ``require_payment``, the facilitator,
and the ``PaymentRequired`` handler — the exact request/response contract an
external agent sees over HTTP, minus the TCP socket (``apps/agent`` is the
out-of-process twin). Because it pays with the dev mock header, ``start``
refuses on the Circle gate (which correctly fails that header closed).

Same single-flight discipline as ``demo.py``: one module-level run, a lock,
copy-on-read ``status()`` with a stall watchdog.
"""

from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
import uuid
from dataclasses import dataclass, field

from .marketplace import catalog_resources

STEP_TIMEOUT_S = 30.0  # watchdog: no progress for this long → errored
MAX_COUNT = 50
RECENT_KEEP = 8


@dataclass
class BuyerRun:
    state: str = "idle"  # idle | running | done | error
    payer: str = ""
    total: int = 0
    done: int = 0
    spent_usdc: float = 0.0
    recent: list[dict] = field(default_factory=list)  # newest first, capped
    error: str | None = None
    stepped_at: float = 0.0


_lock = threading.Lock()
_run = BuyerRun()


def new_payer() -> str:
    """A fresh floor-buyer identity per run (no ":" or spaces — header-safe)."""
    return f"0xfloor-{uuid.uuid4().hex[:6]}"


def status() -> dict:
    """Copy-on-read snapshot of the current run (never hands out live refs)."""
    with _lock:
        if _run.state == "running" and time.monotonic() - _run.stepped_at > STEP_TIMEOUT_S:
            _run.state = "error"
            _run.error = "buyer run stalled"
        return {
            "state": _run.state,
            "payer": _run.payer,
            "total": _run.total,
            "done": _run.done,
            "spent_usdc": _run.spent_usdc,
            "recent": [dict(r) for r in _run.recent],
            "error": _run.error,
        }


def try_start(total: int, payer: str) -> bool:
    """Claim the single run slot; False if a run is already in flight."""
    global _run
    with _lock:
        if _run.state == "running":
            return False
        _run = BuyerRun(
            state="running", payer=payer, total=total, stepped_at=time.monotonic()
        )
        return True


def reset() -> None:
    """Test seam — drop the run state."""
    global _run
    with _lock:
        _run = BuyerRun()


def _price_from_challenge(body: dict, headers) -> float:
    accepts = body.get("accepts") or []
    atomic = accepts[0].get("amount") or accepts[0].get("maxAmountRequired") if accepts else None
    if isinstance(atomic, str) and atomic.isdigit():
        return int(atomic) / 1e6
    fallback = headers.get("X-402-Price")
    if fallback is not None:
        return float(fallback)
    raise RuntimeError("402 challenge carries no readable price")


def _confirmation_ref(headers) -> str:
    raw = headers.get("PAYMENT-RESPONSE") or headers.get("X-PAYMENT-RESPONSE")
    if not raw:
        return ""
    try:
        return str(json.loads(base64.b64decode(raw)).get("transaction") or "")
    except Exception:
        return ""


async def execute(app, total: int, delay_s: float, payer: str, paths: list[str] | None = None) -> None:
    """Entry point for the demo task — surfaces failures as state, never raises."""
    try:
        await _execute(app, total, delay_s, payer, paths)
    except Exception as exc:  # pragma: no cover - defensive
        with _lock:
            _run.state = "error"
            _run.error = str(exc)


async def _execute(app, total: int, delay_s: float, payer: str, paths: list[str] | None) -> None:
    import httpx

    targets = paths or [path for path, _fam in catalog_resources()]
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://exchange.floor") as client:
        for i in range(total):
            path = targets[i % len(targets)]

            # Act I — bare request, expect the 402 challenge.
            r1 = await client.get(path)
            if r1.status_code != 402:
                raise RuntimeError(f"expected 402 challenge on {path}, got {r1.status_code}")
            price = _price_from_challenge(r1.json(), r1.headers)

            # Act II — pay at the advertised price and retry.
            r2 = await client.get(path, headers={"PAYMENT-SIGNATURE": f"x402 {payer}:{price}"})
            if r2.status_code != 200:
                raise RuntimeError(f"payment rejected on {path} ({r2.status_code})")
            ref = _confirmation_ref(r2.headers)

            with _lock:
                _run.done = i + 1
                _run.spent_usdc += price
                _run.recent.insert(0, {"path": path, "price_usdc": price, "tx_ref": ref})
                del _run.recent[RECENT_KEEP:]
                _run.stepped_at = time.monotonic()

            if delay_s > 0 and i + 1 < total:
                await asyncio.sleep(delay_s)

    with _lock:
        _run.state = "done"
        _run.stepped_at = time.monotonic()
