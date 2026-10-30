"""``ReceiptSource`` — the authoritative x402 settlement tape.

Reads the facilitator's own durable settlement ledger (``data/x402_receipts.jsonl``
— one real ``PaymentReceipt`` per line, written by the live seller) and decodes it
into the same ``TapeEvent`` shape the estimator consumes from the simulator.

**Why this is the authoritative source.** ``ArcSource`` scans raw on-chain USDC
transfers, which carry *no service and no size* — so it must guess the service and
derive size. The facilitator, by contrast, records exactly *which resource* each
payment bought, so ``service`` is resolved precisely from the paid path
(``/curve/ACR-INF`` → INFERENCE). This is the "facilitator's own settlement log"
that ``arc_source.py`` names as authoritative.

**Honesty (this is the crux).** The x402 query fee is a single flat price
(``x402_price_usdc``) paid to one platform wallet, so ``size`` is derived to make
``price`` equal the index reference level — recovering the settled notional
exactly and fabricating **no** price dispersion. That flat, single-seller flow is
*precisely* the degenerate pattern the manipulation-resistant estimator's cleaning
stack is built to reject: fed to ``estimate_index`` it yields **zero surviving
observations** (correct behavior — the estimator refusing to price wash-like
flow), so the published compute indices stay on the calibrated **sim** default,
exactly like ``ArcSource`` on thin testnet flow. What this source IS: the
authoritative, service-labeled **audit tape** of real machine-commerce settlements
(who paid for which index, when, for how much), and ready-made plumbing for the
day priced, multi-seller settlement flow exists. It never fabricates price signal
to force a number out. Enable with ``ACR_TAPE_SOURCE=receipts`` (needs
``receipt_log_path`` populated).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from pathlib import Path

from acr_core import (
    INDEX_REGISTRY,
    SellerAttestation,
    Service,
    TapeEvent,
    get_settings,
    index_for_service,
)

from .base import TapeSource

log = logging.getLogger("acr_tape.receipts")

#: Only REAL settlements feed the tape — mirrors ``x402._REAL_SCHEMES`` (dev/sim
#: receipts are demo-only and never reach the durable ledger, but we filter here
#: too so a hand-mixed log can't leak fake economic signal).
_REAL_SCHEMES = frozenset({"exact"})


class ReceiptSource(TapeSource):
    def __init__(
        self,
        log_path: str | None = None,
        default_service: Service = Service.INFERENCE,
        seller: str | None = None,
    ) -> None:
        # None → read the path from settings; "" → empty tape (no events).
        self.log_path = get_settings().receipt_log_path if log_path is None else log_path
        self.default_service = default_service
        self._seller = seller  # override; else resolved from settings at stream time

    def _service_for_resource(self, resource: str) -> Service:
        """Map the paid resource path to its index's service (e.g. ``/vol/ACR-GPU``
        → GPU). Non-index resources (``/prints`` covers all) fall back to the
        default so every settlement still lands on a real index."""
        for iid, spec in INDEX_REGISTRY.items():
            if iid in resource:
                return spec.service
        return self.default_service

    def _rows(self) -> list[dict]:
        if not self.log_path:
            return []
        p = Path(self.log_path)
        if not p.exists():
            return []
        try:
            lines = p.read_text().splitlines()
        except Exception as exc:  # pragma: no cover - unreadable file
            log.warning("ReceiptSource: could not read %s (%s)", p, exc)
            return []
        rows: list[dict] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except Exception:
                continue  # skip a corrupt/legacy line
        return rows

    def stream(self) -> Iterator[TapeEvent]:
        seller = self._seller or (get_settings().x402_pay_to or "acr-platform")
        # Collect (absolute_ts, row) first so ts can be normalized to seconds-from-
        # the-first-settlement — the store windows in seconds from 0, so absolute
        # epoch ts would misalign every window (same reason ArcSource normalizes).
        real: list[tuple[float, int, dict]] = []
        for i, r in enumerate(self._rows()):
            if r.get("scheme") not in _REAL_SCHEMES:
                continue
            ts = float(r.get("settled_at") or 0.0)
            if ts <= 0 or float(r.get("amount_usdc") or 0.0) <= 0:
                continue
            real.append((ts, i, r))
        if not real:
            return
        real.sort(key=lambda t: t[0])
        t0 = real[0][0]
        for ts, i, r in real:
            service = self._service_for_resource(str(r.get("resource", "")))
            # size derived so price == the index reference level (recovers notional
            # exactly; no fabricated price signal — same as ArcSource).
            ref = index_for_service(service).reference_level
            notional = float(r.get("amount_usdc") or 0.0)
            yield TapeEvent(
                event_id=f"{r.get('tx_ref', 'rcpt')}-{i}",
                ts=ts - t0,
                service=service,
                seller=seller,
                buyer=str(r.get("payer", "")),
                price=ref,
                size=notional / ref,
            )

    def attestations(self) -> list[SellerAttestation]:
        # The settlement log carries no attestations of its own; the store merges
        # the on-chain registry's attestations separately for every source.
        return []
