"""A read-only view of what the chain says about this subnet.

Serves the numbers a person needs to decide whether to join netuid 602: the
published rate, who is registered, who can actually be reached, and the block
every one of those was read at. It writes nothing and holds no wallet -- the
only key material on this host belongs to the miner, and a dashboard that could
sign would be a dashboard worth attacking.

TWO RULES SHAPE THIS MODULE.

1. **It must not be able to take the miner down.** A validator's epoch depends
   on `/print` answering inside five seconds. So the chain connection is built
   lazily, on the first request that needs it, inside a `try` -- never at import
   and never at startup. A testnet RPC outage then costs this one route a 503
   and costs the miner nothing. Constructing `Chain` at boot would have coupled
   the thing with an SLA to the thing without one.

2. **Every answer carries its own citation.** `read_at_block` and `age_s` ship
   with the payload because a rate without the block it was read at is a number
   somebody will later quote as current. The cache below exists so a polling
   page cannot hammer a public RPC into rate-limiting us -- the same reason
   `index_api`'s on-chain reads are TTL'd -- and it is exactly why the age has
   to be visible rather than implied.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from .bt_chain import Chain, endpoint_from_commitment

#: Long enough that a browser left open does not become a load test, short
#: enough that the page is not quoting a stale block at somebody. Testnet
#: blocks are ~12s, so this is a couple of blocks.
DEFAULT_TTL_S = 30.0


def print_from_commitment(value: Any) -> dict[str, Any] | None:
    """The published rate in a commitment, or None if it does not carry one.

    The sibling of `endpoint_from_commitment`, and tolerant for the same
    reason: this storage is shared with every other hotkey on the subnet, so
    anything that is not ours is not an error, it is somebody else's business.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        data = json.loads(value)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    if not str(data.get("v", "")).startswith("acr/"):
        return None
    if "p" not in data:
        return None
    out: dict[str, Any] = {
        "index_id": data.get("i"),
        "price": data.get("p"),
        "lo": data.get("lo"),
        "hi": data.get("hi"),
        "n_obs": data.get("n"),
        "source": data.get("src"),
        "window_s": data.get("ts"),
    }
    return out


@dataclass
class _Cached:
    """One TTL'd snapshot, plus the failure that produced it if it failed."""

    value: dict[str, Any] | None = None
    error: str | None = None
    at: float = field(default=0.0)

    def fresh(self, ttl_s: float) -> bool:
        return self.at > 0.0 and (time.monotonic() - self.at) < ttl_s


def read_subnet(chain: Chain, netuid: int) -> dict[str, Any]:
    """Everything the page shows, read in one pass over one connection.

    One `Chain` per read on purpose: a fresh `bt.subtensor()` per field trips
    the testnet's `ws_upgrade_60s` limiter with a 429, which this project has
    already paid for once.
    """
    head = chain.head()
    identity = chain.subnet_identity(netuid)
    reachable, unannounced = chain.endpoints(netuid)
    by_hotkey = {e.hotkey: e for e in reachable}

    published: list[dict[str, Any]] = []
    for hotkey, record in chain.commitments(netuid).items():
        value = getattr(record, "value", None)
        rate = print_from_commitment(value)
        url = endpoint_from_commitment(value)
        if rate is None and url is None:
            continue  # somebody else's payload; not ours to render
        published.append(
            {
                "hotkey": str(hotkey),
                "uid": getattr(record, "uid", None),
                "block": getattr(record, "block", None),
                "rate": rate,
                "url": url,
            }
        )

    neurons: list[dict[str, Any]] = []
    for n in chain.metagraph(netuid).neurons:
        hotkey = str(n.hotkey)
        ep = by_hotkey.get(hotkey)
        neurons.append(
            {
                "uid": int(n.uid),
                "hotkey": hotkey,
                "incentive": float(getattr(n, "incentive", 0.0) or 0.0),
                "stake": float(getattr(n, "stake", 0.0) or 0.0),
                "reachable_at": ep.url if ep is not None else None,
                "found_via": ep.source if ep is not None else None,
            }
        )

    return {
        "network": head.network,
        "netuid": netuid,
        "read_at_block": head.block,
        "cite": head.cite(),
        "identity": identity,
        "published": published,
        "neurons": neurons,
        "reachable": len(reachable),
        "unannounced": unannounced,
    }


def build_subnet_router(
    netuid: int,
    network: str,
    *,
    ttl_s: float = DEFAULT_TTL_S,
    chain_factory: Any = None,
) -> Any:
    """A router with one route, `/subnet`, and no way to write anything."""
    from fastapi import APIRouter, HTTPException  # noqa: PLC0415

    router = APIRouter()
    cache = _Cached()
    make_chain = chain_factory or (lambda: Chain(network=network))

    @router.get("/subnet")
    def subnet() -> dict[str, Any]:
        if cache.fresh(ttl_s):
            if cache.error is not None:
                # Negative results are cached too, deliberately. An RPC that is
                # refusing us is not helped by being asked again per page load.
                raise HTTPException(status_code=503, detail=cache.error)
            assert cache.value is not None
            return {**cache.value, "age_s": round(time.monotonic() - cache.at, 1)}
        try:
            value = read_subnet(make_chain(), netuid)
        except Exception as exc:  # noqa: BLE001 -- see rule 1 in the docstring
            cache.value, cache.error, cache.at = None, f"{type(exc).__name__}: {exc}", time.monotonic()
            raise HTTPException(status_code=503, detail=cache.error) from exc
        cache.value, cache.error, cache.at = value, None, time.monotonic()
        return {**value, "age_s": 0.0}

    return router
