"""The miner and the subnet view, as one deployable web service.

`bt_miner serve` stays exactly what it was -- the smallest honest thing a
validator can query -- and this module is what gets deployed: the same miner
app, plus the read-only `/subnet` view, plus the one piece of operational
machinery a free managed host needs to be dialable at all.

WHY A SELF-PING IS NOT OPTIONAL HERE. Render's free tier stops the instance
after ~15 idle minutes and cold-starts it on the next request, which takes tens
of seconds. `bt_validator.QUERY_TIMEOUT_S` is 5.0s, short on purpose: "an epoch
is a deadline, and a validator that waits indefinitely for one slow miner lets
that miner set the pace for everybody." Those two facts together mean a
sleeping miner is scored as a dead one. The fix belongs on this side -- a miner
that cannot answer in five seconds IS unreachable, and loosening the validator
to paper over a hosting choice would be the mechanism bending to the plumbing.

The pattern is `index_api`'s, which is measured to work on this exact platform:
`process_started_at` held steady across 17.5 idle minutes on the free plan.

AND THE TRAP THAT CAME WITH IT. In `index_api` this same loop died silently at
every boot for four weeks -- an `asyncio.create_task` raised on its first line,
and because nobody awaited the task until shutdown, nothing was logged and the
only visible symptom was a timestamp that never moved. So here: the loop body
catches and LOGS, the task gets a done-callback that logs, and a missing
self-URL is announced at startup rather than inferred later from an outage.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
from typing import Any

from ..miner import FourPillarMiner
from .bt_chain import NETWORK_TEST
from .bt_miner import build_app
from .subnet_api import build_subnet_router

log = logging.getLogger("acr_subnet.service")

#: 60s matches `ACR_CHAIN_WARM_SECONDS`, comfortably inside the ~15 minute
#: idle window, and is cheap: one local HTTP GET against our own /health.
DEFAULT_PING_SECONDS = 60.0


async def _ping_forever(url: str, every_s: float, stop: asyncio.Event) -> None:
    """GET `url` every `every_s` until told to stop. Never raises past here."""
    import httpx  # noqa: PLC0415

    while not stop.is_set():
        with contextlib.suppress(TimeoutError, asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=every_s)
        if stop.is_set():
            return
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                r = await client.get(url)
            log.info("self-ping %s -> %s", url, r.status_code)
        except Exception as exc:  # noqa: BLE001
            # Logged, not raised. A failed ping is worth seeing and is not
            # worth killing the loop that will retry in a minute.
            log.warning("self-ping %s failed: %s: %s", url, type(exc).__name__, exc)


def build_service_app(
    *,
    netuid: int,
    network: str,
    index_id: str,
    uid: int,
    self_url: str = "",
    ping_seconds: float = DEFAULT_PING_SECONDS,
) -> Any:
    """The miner app, with `/subnet` mounted and the keep-awake loop attached.

    Composed rather than mutated: `bt_miner.build_app` keeps owning what a
    miner serves, and this wraps it. The outer app exists only because the
    keep-awake loop needs a lifespan and the miner has no business having one.
    """
    from acr_tape.chutes_source import ChutesSource  # noqa: PLC0415
    from fastapi import FastAPI  # noqa: PLC0415

    miner_app = build_app(FourPillarMiner(uid=uid), ChutesSource(), index_id)

    @contextlib.asynccontextmanager
    async def lifespan(_: Any) -> Any:
        if not self_url:
            log.warning(
                "no ACR_SUBNET_SELF_URL: the self-ping is OFF. On a free managed "
                "host this instance will sleep after ~15 idle minutes and then "
                "cold-start past the validator's 5s timeout, which reads as a "
                "dead miner. Set it to this service's own public URL."
            )
            yield
            return

        stop = asyncio.Event()
        task = asyncio.create_task(
            _ping_forever(f"{self_url.rstrip('/')}/health", ping_seconds, stop)
        )

        def _done(fut: asyncio.Task[None]) -> None:
            # THE four-week bug: a task nobody awaits fails in silence.
            if fut.cancelled():
                return
            exc = fut.exception()
            if exc is not None:
                log.error("self-ping task DIED: %s: %s", type(exc).__name__, exc)

        task.add_done_callback(_done)
        log.info("self-ping every %.0fs against %s/health", ping_seconds, self_url)
        try:
            yield
        finally:
            stop.set()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(
        title="ACR subnet miner + view",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.include_router(miner_app.router)
    app.include_router(build_subnet_router(netuid, network))
    return app


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8091")))
    p.add_argument("--netuid", type=int, default=int(os.environ.get("ACR_SUBNET_NETUID", "602")))
    p.add_argument("--network", default=os.environ.get("ACR_SUBNET_NETWORK", NETWORK_TEST))
    p.add_argument("--index-id", default=os.environ.get("ACR_SUBNET_INDEX_ID", "ACR-INF"))
    p.add_argument("--uid", type=int, default=int(os.environ.get("ACR_SUBNET_UID", "0")))
    p.add_argument("--self-url", default=os.environ.get("ACR_SUBNET_SELF_URL", ""))
    p.add_argument(
        "--ping-seconds",
        type=float,
        default=float(os.environ.get("ACR_SUBNET_PING_SECONDS", DEFAULT_PING_SECONDS)),
    )
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    import uvicorn  # noqa: PLC0415

    app = build_service_app(
        netuid=args.netuid,
        network=args.network,
        index_id=args.index_id,
        uid=args.uid,
        self_url=args.self_url,
        ping_seconds=args.ping_seconds,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
