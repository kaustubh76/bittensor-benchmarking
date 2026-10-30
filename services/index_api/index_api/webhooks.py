"""Circle webhook receiver — the inbound side of the Circle wiring.

ACR submits its hourly ``postPrint`` / seller ``attest`` transactions through a
Circle Developer-Controlled wallet (``acr_oracle_client.signer``). This module
receives Circle's webhook notifications about those (and other Programmable
Wallets) events, verifies their ECDSA P-256 signature, and keeps a bounded ring
of recent events for the Terminal's "Webhook activity" panel.

Design mirrors the rest of the service: a module singleton guarded by a lock
with copy-on-read (like ``PrintStore``), and a bounded ``deque`` of records (like
``Facilitator.recent``). The endpoint always ACKs 200 so Circle's
subscription-confirmation ping activates the webhook even when a signature can't
be verified; unverified events are recorded as such and never acted on.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx
from acr_core import get_settings

log = logging.getLogger("index_api.webhooks")

# Circle signs webhook bodies with ECDSA over SHA-256 using a P-256 key; the
# signing key's id and the base64 signature ride in these headers.
# FLAG: confirm exact header names + public-key path against Circle docs.
SIG_HEADER = "X-Circle-Signature"
KEY_ID_HEADER = "X-Circle-Key-Id"
PUBLIC_KEY_PATH = "/v2/notifications/publicKey/{key_id}"


@dataclass
class WebhookEvent:
    id: str
    type: str
    verified: bool | None  # True/False when checkable; None when unverifiable
    received_at: float
    subscription_id: str
    summary: str
    payload: dict = field(default_factory=dict)


class WebhookStore:
    """In-memory ring of recent events, mirrored to a JSONL append-log so the
    feed survives restarts. Each ``record`` also emits an INFO log line."""

    def __init__(self, maxlen: int = 256, log_path: str | None = None) -> None:
        self._lock = threading.Lock()
        self._events: deque[WebhookEvent] = deque(maxlen=maxlen)
        self.received = 0
        # None → read the path from settings (production); "" explicitly disables
        # the file (in-memory only, used by tests).
        self._log_path = get_settings().webhook_log_path if log_path is None else log_path
        self._rehydrate()

    def _rehydrate(self) -> None:
        """Reload the tail of the JSONL log so /webhooks/recent + the panel are
        populated immediately after a restart. Malformed lines are skipped."""
        if not self._log_path:
            return
        p = Path(self._log_path)
        if not p.exists():
            return
        try:
            lines = p.read_text().splitlines()
        except Exception as exc:  # pragma: no cover - unreadable file
            log.warning("could not read webhook log %s: %s", p, exc)
            return
        loaded = 0
        for line in lines[-self._events.maxlen :]:
            line = line.strip()
            if not line:
                continue
            try:
                self._events.append(WebhookEvent(**json.loads(line)))
                loaded += 1
            except Exception:
                continue  # skip a corrupt/legacy line
        self.received = loaded
        if loaded:
            log.info("webhook log: rehydrated %d event(s) from %s", loaded, p)

    def _persist(self, ev: WebhookEvent) -> None:
        if not self._log_path:
            return
        try:
            p = Path(self._log_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a") as f:
                f.write(json.dumps(asdict(ev), separators=(",", ":")) + "\n")
        except Exception as exc:  # never let logging break the 200 ACK
            log.warning("could not append to webhook log: %s", exc)

    def record(self, ev: WebhookEvent) -> None:
        with self._lock:
            self._events.append(ev)
            self.received += 1
            self._persist(ev)
        # Log outside the lock — the visible per-event webhook log line.
        log.info(
            "circle webhook · %s · verified=%s · sub=%s · %s",
            ev.type,
            ev.verified,
            ev.subscription_id or "-",
            ev.summary,
        )
        if ev.verified is False:
            log.warning("circle webhook signature FAILED verification (type=%s)", ev.type)

    def recent(self, n: int = 25) -> list[dict]:
        with self._lock:
            return [asdict(e) for e in list(self._events)[-n:]]

    def clear(self) -> None:
        with self._lock:
            self._events.clear()
            self.received = 0


# --- module singleton (mirrors onchain.get_reader / x402.get_facilitator) ---
_store: WebhookStore | None = None
_pubkey_cache: dict[str, object] = {}  # key_id -> loaded EC public key


def get_webhook_store() -> WebhookStore:
    global _store
    if _store is None:
        _store = WebhookStore()
    return _store


def reset_webhook_store() -> None:
    global _store
    _store = None
    _pubkey_cache.clear()


def _load_pubkey(pem_or_der_b64: str):
    """Load a base64 DER (SPKI) EC public key. Returns the key or None."""
    try:
        from cryptography.hazmat.primitives.serialization import load_der_public_key

        return load_der_public_key(base64.b64decode(pem_or_der_b64))
    except Exception as exc:  # missing lib or bad key material
        log.warning("could not load webhook public key: %s", exc)
        return None


def _fetch_pubkey(key_id: str):
    """Fetch (and cache) Circle's webhook public key by id."""
    if key_id in _pubkey_cache:
        return _pubkey_cache[key_id]
    s = get_settings()
    if s.circle_webhook_public_key:  # pinned offline key wins
        key = _load_pubkey(s.circle_webhook_public_key)
        if key is not None:
            _pubkey_cache[key_id] = key
        return key
    if not (s.circle_api_key and key_id):
        return None
    url = s.circle_base_url.rstrip("/") + PUBLIC_KEY_PATH.format(key_id=key_id)
    try:
        r = httpx.get(url, headers={"Authorization": f"Bearer {s.circle_api_key}"}, timeout=5.0)
        r.raise_for_status()
        b64 = r.json().get("data", {}).get("publicKey", "")
        key = _load_pubkey(b64) if b64 else None
        if key is not None:
            _pubkey_cache[key_id] = key
        return key
    except Exception as exc:  # pragma: no cover - network path
        log.warning("webhook public-key fetch failed for %s: %s", key_id, exc)
        return None


def verify_signature(body: bytes, signature_b64: str | None, key_id: str | None) -> bool | None:
    """Verify a Circle webhook signature over the raw body.

    Returns True/False when a key is available, or None when verification is
    impossible (no headers, no key material, or the crypto lib is absent) — the
    caller still ACKs but records the event as unverified.
    """
    if not signature_b64 or not key_id:
        return None
    key = _fetch_pubkey(key_id)
    if key is None:
        return None
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec

        try:
            key.verify(base64.b64decode(signature_b64), body, ec.ECDSA(hashes.SHA256()))
            return True
        except InvalidSignature:
            return False
    except Exception as exc:  # pragma: no cover - lib missing / unexpected
        log.warning("webhook signature verify errored: %s", exc)
        return None


def summarize(payload: dict) -> tuple[str, str, str]:
    """Derive (type, subscription_id, one-line summary) from Circle's envelope."""
    etype = str(
        payload.get("notificationType")
        or payload.get("type")
        or payload.get("eventType")
        or "unknown"
    )
    sub = str(payload.get("subscriptionId") or payload.get("subscription_id") or "")
    note = payload.get("notification") or payload.get("data") or {}
    bits: list[str] = []
    if isinstance(note, dict):
        for k in ("state", "txHash", "amount", "walletId", "blockchain", "id"):
            v = note.get(k)
            if v:
                bits.append(f"{k}={v}")
    summary = " · ".join(bits) if bits else etype
    return etype, sub, summary
