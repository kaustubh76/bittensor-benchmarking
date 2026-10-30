"""``ArcSource`` — read real payment exhaust from Arc testnet.

Decodes on-chain USDC payment events into the same ``TapeEvent`` shape the
estimator already consumes from the simulator. Built to be *tolerant*: Arc
testnet flow is thin by design (methodology-first), so a missing RPC, an unset
address, or an empty log range all degrade to "no events" rather than raising.
The estimator therefore runs identically whether the tape is simulated or live.

**What is and isn't on-chain.** The x402 ``exact`` scheme settles USDC via
EIP-3009; the ``AuthorizationUsed(address authorizer, bytes32 nonce)`` event
marks an authorization but carries *no amount, size, or service*. The USDC amount
lives in the ERC-20 ``Transfer(from, to, value)`` event; ``size`` and ``service``
are **not observable on-chain**. Two decode modes:

* **Legacy audit decode** (default): ``buyer=from``, ``seller=to``,
  ``notional=value``, ``service`` from a configurable resolver (default
  INFERENCE), ``size`` derived so ``price`` equals the index reference level —
  recovers the notional exactly and fabricates no price signal.
* **Attested-market decode** (``ACR_ARC_ATTESTED_ONLY=1``): the market's
  convention is that one settlement covers a fixed, published quantity per
  index (``IndexSpec.arc_unit_qty``), so the transfer amount IS the price
  signal: ``price = notional / arc_unit_qty``, ``size = arc_unit_qty``. Each
  event's service resolves from the seller's on-chain EIP-712 attestation, and
  events from non-attested sellers are dropped — attestation earns index
  inclusion (the registry flywheel, made literal).
"""

from __future__ import annotations

import hashlib
import logging
import time
from collections.abc import Callable, Iterator

from acr_core import (
    SellerAttestation,
    Service,
    TapeEvent,
    get_settings,
    index_for_service,
)

from .base import TapeSource

log = logging.getLogger("acr_tape.arc")

#: Primary default: the ERC-20 event that actually carries the USDC amount.
USDC_TRANSFER_ABI: dict = {
    "anonymous": False,
    "name": "Transfer",
    "type": "event",
    "inputs": [
        {"indexed": True, "name": "from", "type": "address"},
        {"indexed": True, "name": "to", "type": "address"},
        {"indexed": False, "name": "value", "type": "uint256"},
    ],
}

#: The real EIP-3009 authorization marker (carries no value) — documented for
#: callers who want to join authorizations to transfers by tx/nonce.
EIP3009_AUTHORIZATION_ABI: dict = {
    "anonymous": False,
    "name": "AuthorizationUsed",
    "type": "event",
    "inputs": [
        {"indexed": True, "name": "authorizer", "type": "address"},
        {"indexed": True, "name": "nonce", "type": "bytes32"},
    ],
}

#: Map TapeEvent semantics ← decoded event args (overridable for custom events).
DEFAULT_FIELD_MAP: dict[str, str] = {"buyer": "from", "seller": "to", "value": "value"}

#: USDC has 6 decimals.
USDC_DECIMALS = 6


def _default_service_resolver(args: dict) -> Service:  # noqa: ARG001 - hook signature
    # Service is not on-chain — default everything to INFERENCE. Override with a
    # seller→service map or the facilitator's settlement log in production.
    return Service.INFERENCE


_SERVICES = (Service.INFERENCE, Service.GPU, Service.DATA)


def by_seller_service_resolver(args: dict) -> Service:
    """Deterministically spread events across all three services by hashing the
    seller address. Service isn't observable on-chain, so this fabricates no
    economic signal — it just lets ``ACR_TAPE_SOURCE=arc`` exercise every index
    for a liveness demo instead of pinning everything to INFERENCE. Opt-in."""
    seller = str(args.get("to") or args.get("seller") or "")
    h = int(hashlib.sha256(seller.encode()).hexdigest(), 16)
    return _SERVICES[h % len(_SERVICES)]


class ArcSource(TapeSource):
    def __init__(
        self,
        rpc_url: str | None = None,
        x402_address: str | None = None,
        registry_address: str | None = None,
        from_block: int | str | None = None,
        to_block: int | str = "latest",
        event_abi: dict | None = None,
        field_map: dict[str, str] | None = None,
        service_resolver: Callable[[dict], Service] | None = None,
        lookback_blocks: int | None = None,
        attested_only: bool | None = None,
    ) -> None:
        settings = get_settings()
        self.rpc_url = rpc_url or settings.arc_rpc_url
        # Default to the native USDC system contract on Arc.
        self.x402_address = x402_address or (settings.usdc_address or None)
        self.registry_address = registry_address or (settings.registry_address or None)
        # None → bound the range to the last ``lookback_blocks`` (public RPCs
        # reject an unbounded "earliest".."latest" eth_getLogs). Explicit values
        # (int / "earliest") are honoured for callers that know their range.
        self.from_block = from_block
        self.to_block = to_block
        self.lookback_blocks = (
            lookback_blocks
            if lookback_blocks is not None
            else getattr(settings, "arc_tape_lookback_blocks", 200_000)
        )
        self.event_abi = event_abi or USDC_TRANSFER_ABI
        self.event_name = self.event_abi["name"]
        self.field_map = field_map or DEFAULT_FIELD_MAP
        self.service_resolver = service_resolver or _default_service_resolver
        self.attested_only = (
            attested_only
            if attested_only is not None
            else bool(getattr(settings, "arc_attested_only", 0))
        )
        #: seller (lowercased) → attested service; built per stream() in
        #: attested-market mode so a mid-run registry read stays fresh.
        self._service_map: dict[str, Service] | None = None
        self._w3 = None
        self._block_time_cache: dict[int, int] = {}

    def _web3(self):
        if self._w3 is not None:
            return self._w3
        try:
            from web3 import Web3

            self._w3 = Web3(Web3.HTTPProvider(self.rpc_url, request_kwargs={"timeout": 5}))
        except Exception as exc:  # pragma: no cover - env dependent
            log.warning("ArcSource: web3 unavailable (%s); serving empty tape", exc)
            self._w3 = None
        return self._w3

    def _connected(self) -> bool:
        w3 = self._web3()
        if w3 is None:
            return False
        try:
            return bool(w3.is_connected())
        except Exception as exc:  # pragma: no cover - env dependent
            log.warning("ArcSource: RPC not reachable (%s)", exc)
            return False

    def _decode_log(self, log) -> TapeEvent | None:
        """Decode one already-processed ``EventData`` entry (as returned by
        ``event.get_logs()``) into a ``TapeEvent``. ``log['args']`` is the decoded
        event args; ts is set to the block *number* here and remapped to real
        seconds in ``stream()``."""
        try:
            a = log["args"]
            buyer = str(a[self.field_map["buyer"]])
            seller = str(a[self.field_map["seller"]])
            notional = int(a[self.field_map["value"]]) / (10**USDC_DECIMALS)
            if notional <= 0:
                return None
            if self._service_map is not None:
                # Attested-market decode: only attested sellers are indexed;
                # the seller's attestation names the service, and the published
                # per-index quantity convention turns the amount into a PRICE.
                service = self._service_map.get(seller.lower())
                if service is None:
                    return None
                qty = index_for_service(service).arc_unit_qty
                price = notional / qty
                size = qty
            else:
                service = self.service_resolver(dict(a))
                # size derived so price == the index reference level (recovers
                # notional exactly; no fabricated price signal — module docstring).
                price = index_for_service(service).reference_level
                size = notional / price
            txh = log.get("transactionHash")
            txh = txh.hex() if hasattr(txh, "hex") else str(txh)
            return TapeEvent(
                event_id=f"{txh}-{log.get('logIndex', 0)}",
                ts=float(log.get("blockNumber", 0)),
                service=service,
                seller=seller,
                buyer=buyer,
                price=price,
                size=size,
            )
        except Exception as exc:  # pragma: no cover - decode tolerant
            log.debug("ArcSource: skipping undecodable log (%s)", exc)
            return None

    def _block_time(self, w3, block_number: int) -> int:
        """Unix timestamp (seconds) of a block, cached per block number."""
        bn = int(block_number)
        if bn not in self._block_time_cache:
            self._block_time_cache[bn] = int(w3.eth.get_block(bn)["timestamp"])
        return self._block_time_cache[bn]

    def _resolve_from_block(self, w3) -> int | str:
        """Bound an unset range to the last ``lookback_blocks`` before head."""
        if self.from_block is not None:
            return self.from_block
        try:
            head = int(w3.eth.block_number)
        except Exception:  # pragma: no cover - env dependent
            return "earliest"
        return max(0, head - int(self.lookback_blocks))

    @staticmethod
    def _interpolate_times(
        events: list[TapeEvent], bn_lo: int, t_lo: int, bn_hi: int, t_hi: int
    ) -> list[TapeEvent]:
        """Map each event's ts (a block *number* at decode time) to seconds since
        the earliest block, by linear interpolation between the two endpoint
        blocks' real timestamps. The store windows in seconds from 0, so absolute
        epoch ts would misalign every window; interpolation needs only the 2
        endpoint block timestamps (not one RPC per block). Pure + testable."""
        if not events:
            return []
        span_bn = max(1, bn_hi - bn_lo)
        span_t = max(0, t_hi - t_lo)
        out = [
            e.model_copy(update={"ts": (int(e.ts) - bn_lo) / span_bn * span_t}) for e in events
        ]
        out.sort(key=lambda e: e.ts)
        return out

    def _fetch_logs_adaptive(self, w3, event):  # pragma: no cover - env dependent
        """Fetch logs over [from,to], halving toward the most-recent end whenever
        the RPC rejects the range as too large (413 / 'response too large') — Arc's
        USDC is the native gas token, so Transfer logs are dense. Retries transient
        429s with backoff. Returns [] if even a tiny range won't come back."""
        to_block = self.to_block
        from_block = self._resolve_from_block(w3)
        head = None
        if not isinstance(to_block, int):
            try:
                head = int(w3.eth.block_number)
                to_block = head
            except Exception:
                return []
        lo = from_block if isinstance(from_block, int) else max(0, to_block - self.lookback_blocks)
        for _ in range(8):  # shrink attempts
            for attempt in range(4):  # rate-limit retries
                try:
                    return event.get_logs(from_block=lo, to_block=to_block)
                except Exception as exc:
                    msg = str(exc).lower()
                    if "429" in msg or "too many" in msg or "rate" in msg:
                        time.sleep(1.5 * (attempt + 1))
                        continue
                    # "The range is too wide" arrives in more than one dialect:
                    # an HTTP 413, or a JSON-RPC -32602 whose message names a
                    # result cap ("query exceeds max results 20000, retry with
                    # the range X-Y"). Both mean shrink, and treating the second
                    # as a hard failure returned an empty tape on a chain that
                    # was answering perfectly well.
                    if (
                        "413" in msg
                        or "too large" in msg
                        or "entity too large" in msg
                        or "-32602" in msg
                        or "exceeds max results" in msg
                        or "query returned more than" in msg
                        or "log response size exceeded" in msg
                    ):
                        break  # shrink the range
                    log.warning("ArcSource: log fetch failed (%s); empty tape", exc)
                    return []
            span = to_block - lo
            if span <= 1:
                return []
            lo = to_block - span // 2  # keep the most-recent half
        return []

    def stream(self) -> Iterator[TapeEvent]:
        if not self._connected() or not self.x402_address:
            log.info("ArcSource: no live connection/address; empty tape")
            return
        if self.attested_only:
            atts = self.attestations()
            self._service_map = {a.seller.lower(): a.service for a in atts}
            if not self._service_map:
                log.warning("ArcSource: attested-only mode with empty registry; empty tape")
                return
        else:
            self._service_map = None
        w3 = self._web3()
        try:
            contract = w3.eth.contract(
                address=w3.to_checksum_address(self.x402_address),
                abi=[self.event_abi],
            )
            event = getattr(contract.events, self.event_name)()
        except Exception as exc:  # pragma: no cover - env dependent
            log.warning("ArcSource: contract build failed (%s); empty tape", exc)
            return
        logs = self._fetch_logs_adaptive(w3, event)
        decoded: list[TapeEvent] = []
        for raw in logs:
            ev = self._decode_log(raw)
            if ev is not None:
                decoded.append(ev)
        if not decoded:
            return
        blocks = [int(e.ts) for e in decoded]  # ts is a block number at decode time
        bn_lo, bn_hi = min(blocks), max(blocks)
        try:
            t_lo = self._block_time(w3, bn_lo)
            t_hi = self._block_time(w3, bn_hi)
        except Exception as exc:  # pragma: no cover - env dependent
            log.warning("ArcSource: block-time lookup failed (%s); using block deltas", exc)
            t_lo, t_hi = bn_lo, bn_hi  # fall back to block-number spacing
        yield from self._interpolate_times(decoded, bn_lo, t_lo, bn_hi, t_hi)

    def attestations(self) -> list[SellerAttestation]:
        """Read seller metadata from the on-chain ``AttestationRegistry``.

        Reuses ``RegistryClient`` (same tolerant pattern as the tape read): an
        unset registry address or an unreachable node yields ``[]``, so the
        estimator degrades to whatever attestations it already has.
        """
        if not self.registry_address:
            return []
        try:
            from acr_oracle_client import RegistryClient

            client = RegistryClient(
                rpc_url=self.rpc_url, registry_address=self.registry_address
            )
            return client.all_attestations()
        except Exception as exc:  # pragma: no cover - env dependent
            log.warning("ArcSource: registry read failed (%s); no attestations", exc)
            return []
