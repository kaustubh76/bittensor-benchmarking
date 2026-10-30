"""Taostats — Bittensor chain state, for ACR-TAO and the subnet's own figures.

Chutes and Lium price the work; Taostats prices the chain the work runs on. Three
things ACR wants from it and cannot get anywhere else:

* **TAO/USD**, so a price quoted in TAO can be compared with one quoted in USD.
* **Subnet pool reserves**, so an alpha token can be valued against the compute
  its subnet actually delivers — the ACR-TAO idea.
* **The subnet registration cost**, which is the one number the Bittensor
  submission has to quote and which moves constantly.

Everything here is **optional at runtime**. No key means every method returns
``None``, nothing raises, and the subnet still runs and prints. That is not
politeness: the base install has to stay deployable with no credentials at all,
the same contract `acr_oracle_client`'s Circle extra and the Model Armor screen
already hold.

Four properties are load-bearing and each exists because of something measured
against the live API on 2026-10-29:

**The route table is data, in one place.** The `/api/...` routes **retire on
9 November 2026** and Taostats' own SDK still points at them, so the replacements
are not yet knowable. When they land, this is a one-dict edit rather than a
scattered-f-string hunt. ``/v1/price`` is already on the new scheme.

**A dead route is not a dead client.** ``/api/dtao/pool/latest/v1`` returns HTTP
520 today — persistently, server-side, not a 404 and not an auth failure. The
alpha price is therefore derived from the emission route, which carries both pool
reserves, and one broken endpoint degrades one method rather than the adapter.

**Credits are scarce.** The free tier is 5 requests/minute and 10,000/month,
which over hourly cycles is about 13.9 an hour. So there is a hard per-cycle call
budget, a netuid allowlist, and a cache — and a 429 ends the cycle instead of
starting a retry storm, because a retry storm against a 5/min limit is how you
lose the whole month's budget in an afternoon.

**RAO, not TAO.** `registration_cost` comes back as a *string* in RAO. Dividing
by 1e9 is not cosmetic: the difference between 259409813732 and 259.41 is the
difference between a nonsense figure in a submission and a true one.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from acr_core import get_settings

#: 1 TAO = 1e9 RAO.
RAO = 1_000_000_000

#: The route table. **One place, on purpose** — the `/api/...` entries retire
#: 2026-11-09 and this is what has to change. Verified live 2026-10-29; the
#: status column is what the probe actually returned.
#:
#:   price          /v1/price                              200  (new scheme)
#:   coldkeys       /api/subnet/distribution/coldkey/v1    200
#:   registration   /api/subnet/registration_cost/latest/v1 200
#:   emission       /api/dtao/subnet_emission/v1           200  (carries both reserves)
#:   pool           /api/dtao/pool/latest/v1               520  BROKEN — do not rely on it
ROUTES: dict[str, str] = {
    "price": "/v1/price",
    "coldkeys": "/api/subnet/distribution/coldkey/v1",
    "registration": "/api/subnet/registration_cost/latest/v1",
    "emission": "/api/dtao/subnet_emission/v1",
}

#: Netuids ACR cares about: 64 Chutes (inference), 51 Lium (GPU). An allowlist
#: rather than a parameter, because a sweep over 128 subnets would spend a
#: month's credits in one cycle.
DEFAULT_NETUIDS = (51, 64)

#: Hard per-cycle call budget. 10,000/month over hourly cycles is ~13.9/hour and
#: the per-minute cap is 5, so three leaves room for a retry and a second index.
MAX_CALLS_PER_CYCLE = 3

_FIXTURE = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "bt" / "taostats.json"


class TaostatsBudgetExhausted(RuntimeError):
    """The per-cycle call budget is spent. Raised rather than silently skipping,
    so a caller that wanted four numbers learns it got three."""


@dataclass(frozen=True)
class SubnetPool:
    """dTAO pool state for one subnet, and the alpha price it implies."""

    netuid: int
    block_number: int
    timestamp: str
    tao_in_pool: float
    alpha_in_pool: float
    alpha_rewards: float = 0.0

    @property
    def alpha_price_tao(self) -> float | None:
        """TAO per alpha = reserve ratio, which is what the pool price means.

        ``None`` rather than zero on an empty alpha side: a subnet with no alpha
        in its pool has no price, and 0.0 would read as "free".
        """
        if self.alpha_in_pool <= 0:
            return None
        return self.tao_in_pool / self.alpha_in_pool


@dataclass(frozen=True)
class RegistrationCost:
    """What it costs to register a new subnet, right now."""

    rao: int
    block_number: int
    timestamp: str

    @property
    def tao(self) -> float:
        return self.rao / RAO


class TaostatsClient:
    """Read-only Taostats access, budgeted and optional.

    ``offline`` is the normal state in tests and in CI: no key configured, every
    method returns ``None``, and nothing in the subnet notices.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        *,
        netuids: tuple[int, ...] = DEFAULT_NETUIDS,
        max_calls: int = MAX_CALLS_PER_CYCLE,
        timeout: float = 25.0,
        settings=None,
    ) -> None:
        s = settings or get_settings()
        key = api_key if api_key is not None else getattr(s, "taostats_api_key", "")
        self.api_key = (key or "").strip()
        # A commented-out .env line must not look like a configured key -- the
        # same guard `acr_oracle_client.signer.build_signer` applies.
        if self.api_key.startswith("#"):
            self.api_key = ""
        self.base_url = (
            base_url or getattr(s, "taostats_base_url", "") or "https://api.taostats.io"
        ).rstrip("/")
        self.netuids = netuids
        self.max_calls = max_calls
        self.timeout = timeout
        self.calls = 0
        self.rate_limited = False
        self._cache: dict[str, Any] = {}

    # -- plumbing --------------------------------------------------------
    @property
    def offline(self) -> bool:
        return not self.api_key

    def reset_cycle(self) -> None:
        """Start a new budget window. Call once per fixing cycle."""
        self.calls = 0
        self.rate_limited = False
        self._cache.clear()

    def _get(self, route_key: str, params: dict[str, Any] | None = None) -> Any | None:
        """One budgeted GET. ``None`` on anything that is not a clean 200."""
        if self.offline:
            return None
        path = ROUTES.get(route_key)
        if path is None:
            return None
        query = "&".join(f"{k}={v}" for k, v in sorted((params or {}).items()))
        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")

        if url in self._cache:
            return self._cache[url]
        if self.rate_limited:
            # Already told to slow down this cycle. Asking again is how a 5/min
            # limit turns into a month with no budget left.
            return None
        if self.calls >= self.max_calls:
            raise TaostatsBudgetExhausted(
                f"{self.calls} calls already made this cycle (max {self.max_calls}); "
                f"refusing {route_key}. Call reset_cycle() for the next window."
            )

        req = urllib.request.Request(
            url,
            headers={
                # No "Bearer" prefix -- verified against the official SDK, which
                # sets the raw key. A Bearer prefix returns 401.
                "Authorization": self.api_key,
                "accept": "application/json",
                "user-agent": "acr-taostats/0.1",
            },
        )
        self.calls += 1
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:  # noqa: S310
                return self._cache.setdefault(url, json.loads(r.read().decode("utf-8")))
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                self.rate_limited = True
            # 520 on /pool/latest is the known case: a server-side failure on one
            # route, which must not look like a broken client or a bad key.
            return None
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return None

    @staticmethod
    def _rows(payload: Any) -> list[dict]:
        """Unwrap the ``{pagination, data}`` envelope every route returns."""
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, list):
                return [r for r in data if isinstance(r, dict)]
        if isinstance(payload, list):
            return [r for r in payload if isinstance(r, dict)]
        return []

    # -- the four things ACR wants ----------------------------------------
    def tao_price_usd(self) -> float | None:
        rows = self._rows(self._get("price", {"asset": "tao"}))
        if not rows:
            return None
        try:
            return float(rows[0]["price"])
        except (KeyError, TypeError, ValueError):
            return None

    def registration_cost(self) -> RegistrationCost | None:
        """The live subnet lock cost. Verified 259.41 TAO at block 9388633."""
        rows = self._rows(self._get("registration"))
        if not rows:
            return None
        r = rows[0]
        try:
            return RegistrationCost(
                rao=int(r["registration_cost"]),
                block_number=int(r.get("block_number") or 0),
                timestamp=str(r.get("timestamp") or ""),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def subnet_pool(self, netuid: int) -> SubnetPool | None:
        """Pool reserves for one subnet, from the EMISSION route.

        Not from ``/dtao/pool/latest``, which returns 520. The emission route
        carries ``tao_in_pool`` and ``alpha_in_pool`` on every row, and the alpha
        price is their ratio — the same definition Taostats' own docs give.
        """
        if netuid not in self.netuids:
            return None
        rows = self._rows(self._get("emission", {"netuid": netuid}))
        if not rows:
            return None
        r = rows[0]
        try:
            # `r.get("netuid") or netuid` would substitute the requested netuid
            # for a response reporting 0 — the root network — because 0 is falsy.
            # Unreachable while the allowlist is (51, 64), and a trap for whoever
            # widens it. Same fix as `chutes_metagraph._netuid_of`.
            reported = r.get("netuid")
            return SubnetPool(
                netuid=netuid if reported is None else int(reported),
                block_number=int(r.get("block_number") or 0),
                timestamp=str(r.get("timestamp") or ""),
                tao_in_pool=float(r["tao_in_pool"]),
                alpha_in_pool=float(r["alpha_in_pool"]),
                alpha_rewards=float(r.get("alpha_rewards") or 0.0),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def coldkey_distribution(self, netuid: int) -> dict[str, int] | None:
        """``coldkey -> hotkeys controlled``, for one subnet.

        The independent check on `chutes_metagraph`: both report 207 coldkeys for
        netuid 64, from unrelated sources. Worth keeping precisely because it is
        redundant — a disagreement would mean one of them is wrong.
        """
        if netuid not in self.netuids:
            return None
        rows = self._rows(self._get("coldkeys", {"netuid": netuid}))
        if not rows:
            return None
        out: dict[str, int] = {}
        for r in rows:
            ck = r.get("coldkey")
            if ck:
                try:
                    out[str(ck)] = int(r.get("count") or 0)
                except (TypeError, ValueError):
                    continue
        return out or None


# --- offline fixture path -------------------------------------------------


def load_fixture(path: Path | str | None = None) -> dict | None:
    """The recorded Taostats snapshot, so tests need no key and no network."""
    p = Path(path) if path is not None else _FIXTURE
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):  # pragma: no cover - corrupt fixture
        return None
    return data if isinstance(data, dict) else None


def pool_from_fixture(netuid: int, path: Path | str | None = None) -> SubnetPool | None:
    fx = load_fixture(path)
    if not fx:
        return None
    r = (fx.get("pools") or {}).get(str(netuid))
    if not isinstance(r, dict):
        return None
    try:
        return SubnetPool(
            netuid=netuid,
            block_number=int(r.get("block_number") or 0),
            timestamp=str(r.get("timestamp") or ""),
            tao_in_pool=float(r["tao_in_pool"]),
            alpha_in_pool=float(r["alpha_in_pool"]),
            alpha_rewards=float(r.get("alpha_rewards") or 0.0),
        )
    except (KeyError, TypeError, ValueError):
        return None


def registration_cost_from_fixture(path: Path | str | None = None) -> RegistrationCost | None:
    fx = load_fixture(path)
    r = (fx or {}).get("registration")
    if not isinstance(r, dict):
        return None
    try:
        return RegistrationCost(
            rao=int(r["rao"]),
            block_number=int(r.get("block_number") or 0),
            timestamp=str(r.get("timestamp") or ""),
        )
    except (KeyError, TypeError, ValueError):
        return None


def capture(client: TaostatsClient, netuids: tuple[int, ...] = DEFAULT_NETUIDS) -> dict:
    """Record a fixture from the live API. Needs a key; spends several credits.

    The budget is reset between groups rather than raised: the point of the cap
    is that a caller cannot accidentally spend a month in one loop, and a capture
    is a deliberate act that knows how many calls it is making.
    """
    out: dict[str, Any] = {"captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}

    client.reset_cycle()
    price = client.tao_price_usd()
    reg = client.registration_cost()
    if price is not None:
        out["tao_price_usd"] = price
    if reg is not None:
        out["registration"] = {
            "rao": reg.rao, "block_number": reg.block_number, "timestamp": reg.timestamp,
        }

    pools: dict[str, Any] = {}
    colds: dict[str, Any] = {}
    for nid in netuids:
        client.reset_cycle()
        p = client.subnet_pool(nid)
        if p is not None:
            pools[str(nid)] = {
                "block_number": p.block_number, "timestamp": p.timestamp,
                "tao_in_pool": p.tao_in_pool, "alpha_in_pool": p.alpha_in_pool,
                "alpha_rewards": p.alpha_rewards,
            }
        d = client.coldkey_distribution(nid)
        if d:
            colds[str(nid)] = {"n_coldkeys": len(d), "n_hotkeys": sum(d.values())}
    if pools:
        out["pools"] = pools
    if colds:
        out["coldkeys"] = colds
    return out
