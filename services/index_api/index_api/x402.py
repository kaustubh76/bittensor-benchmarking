"""x402 payment gate — the self-referential business model.

The index about machine commerce is bought BY machines. Every gated query is
behind an HTTP 402 Payment Required: an agent presents an x402 payment header,
the facilitator verifies (and settles) it, and the data is served. Every paid
query is simultaneously revenue and a Nanopayments dogfooding datapoint.

Two facilitators, selected by config:
  * ``DevFacilitator`` (default) — accepts a well-formed mock header
    ``x402 <payer>:<amount>`` so the loop is demonstrable offline.
  * ``CircleFacilitator`` — real x402 v2: returns a base64 ``PAYMENT-REQUIRED``
    (a PaymentRequirements object, ``exact``/GatewayWalletBatched scheme on Arc),
    then on retry POSTs the ``PAYMENT-SIGNATURE`` payload to the Circle Gateway
    facilitator's ``POST /v1/x402/verify`` + ``/v1/x402/settle`` and gates on
    the result. Fail-closed: any error → 402 (never serve unpaid).

Both facilitators raise the challenge as a :class:`PaymentRequired` carrying
the Gateway-shaped ``{"x402Version": 2, "resource": {...}, "accepts": [...]}``
JSON body — what x402 client SDKs parse (``x402Version`` MUST be 2 and
``resource`` MUST be an object, or Circle's Gateway API rejects the payload
shape) — which ``app.py`` renders via an exception handler. The legacy
``X-402-*`` / base64 ``PAYMENT-REQUIRED`` headers are kept alongside (the
Terminal console reads them).
"""

from __future__ import annotations

import base64
import json
import logging
import math
import os
import time
from abc import ABC, abstractmethod
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path

from acr_core import get_settings
from fastapi import Header, HTTPException, Request, Response

log = logging.getLogger("index_api.x402")

USDC_DECIMALS = 6
#: Dev pay-to address (a valid checksummed dummy — the live seller wallet comes
#: from ACR_X402_PAY_TO).
PAY_TO = "0xACacE0000000000000000000000000000000CafE"
ASSET = "USDC"


def price_usdc() -> float:
    """Per-query price in USDC (sub-cent — a Nanopayment). ``ACR_X402_PRICE_USDC``."""
    return get_settings().x402_price_usdc


def atomic_amount(price: float) -> str:
    """A USDC price as an atomic-unit decimal string (6 decimals).

    A positive sub-atomic price (< 1e-6) advertises 1 unit rather than "0" —
    an advertised zero the gate then rejects would strand every buyer.
    """
    atomic = int(round(price * 10**USDC_DECIMALS))
    if atomic == 0 and price > 0:
        atomic = 1
    return str(atomic)


class PaymentRequired(Exception):
    """The 402 challenge: spec-shaped JSON body + x402 headers.

    Raised by ``require_payment`` when no payment header is present; the app's
    exception handler renders ``body`` as the JSON response, so x402 client
    SDKs (which parse the top-level ``accepts`` array) and header-reading
    clients (the Terminal console decodes ``PAYMENT-REQUIRED``) both work.
    """

    status_code = 402

    def __init__(self, body: dict, headers: dict[str, str]) -> None:
        super().__init__("Payment Required")
        self.body = body
        self.headers = headers


def build_payment_requirements(
    resource: str, settings=None, pay_to: str | None = None, price: float | None = None
) -> dict:
    """One x402 PaymentRequirements object for ``resource``.

    Emits BOTH ``maxAmountRequired`` (the x402 v1 key) and ``amount`` (v2) with
    the same atomic value, so parsers of either vintage find their field.
    Shared by the facilitator challenges and the /marketplace/catalog listings.

    ``pay_to`` and ``price`` override the single platform payee and the single
    flat price. Both used to come only from settings, which is precisely what
    forced every settlement in the system to one wallet at one price — and a
    tape with no price dispersion cannot support transaction-cost analysis. The
    seller fleet passes its own listing's values here; every other endpoint
    passes neither and behaves exactly as before.
    """
    s = settings or get_settings()
    amount = atomic_amount(s.x402_price_usdc if price is None else price)
    return {
        "scheme": s.x402_scheme,
        "network": s.caip2(),
        "asset": s.usdc_address,
        "payTo": pay_to or s.x402_pay_to or PAY_TO,
        "maxAmountRequired": amount,
        "amount": amount,
        "resource": resource,
        "description": "ACR machine-commerce index query",
        "mimeType": "application/json",
        "maxTimeoutSeconds": s.x402_max_timeout_seconds,
        # Verified against @circle-fin/x402-batching v3: the buyer SDK selects
        # the option where extra == {name: "GatewayWalletBatched", version: "1",
        # verifyingContract: <GatewayWallet>} and signs EIP-3009 against that
        # verifyingContract (not the USDC token).
        "extra": {
            "name": "GatewayWalletBatched",
            "version": "1",
            "verifyingContract": s.x402_gateway_wallet,
        },
    }


def _fleet_terms(resource: str, settings) -> tuple[str, float]:
    """The payee and price for ``resource`` — a fleet listing's, or the platform's.

    Resolved in ONE place so both gates cannot disagree. A per-seller price that
    reached the dev gate but not the Circle gate would price the demo tape
    differently from the live one, and the difference would show up as slippage
    that no seller ever charged.
    """
    from .fleet import listing_for_resource

    listing = listing_for_resource(resource, settings)
    if listing is None:
        return (settings.x402_pay_to or PAY_TO, settings.x402_price_usdc)
    return (listing.seller, listing.amount_usdc)


def facilitator_endpoint(base: str, name: str) -> str:
    """Join the facilitator base URL with the Gateway x402 API path.

    Circle's facilitator lives under ``/v1/x402`` (``POST /v1/x402/verify``,
    ``POST /v1/x402/settle``); a base that already ends there is used as-is, so
    both ``https://gateway-api-testnet.circle.com`` and a full ``…/v1/x402``
    base work.
    """
    b = base.rstrip("/")
    if not b.endswith("/v1/x402"):
        b = f"{b}/v1/x402"
    return f"{b}/{name}"


def _resource_for(request: Request, settings) -> str:
    return (settings.x402_resource_base or str(request.base_url).rstrip("/")) + request.url.path



def _tier_of(request: Request) -> str:
    """The tier `optional_agent` recorded on this request, or ``anonymous``.

    `optional_agent` is declared BEFORE `require_payment` on every paid route, so
    by the time a facilitator runs the agent gate has either set
    ``request.state.agent`` or refused the card with a 401. No card sets nothing,
    and nothing is anonymous — read with a default rather than assumed present.
    """
    agent = getattr(getattr(request, "state", None), "agent", None)
    return str(getattr(agent, "tier", "") or "") or "anonymous"

def _resource_path(request) -> str:
    """The bare resource path being paid for — e.g. ``/curve/ACR-INF`` (what the
    settlement ledger records so ReceiptSource can resolve the service). Safe when
    ``request`` is ``None`` (some dev-gate unit tests pass no request)."""
    return getattr(getattr(request, "url", None), "path", "") or ""


def _resource_object(request: Request, settings) -> dict:
    """The challenge envelope's top-level ``resource`` — an OBJECT, not a string.

    Circle's Gateway x402 API requires ``paymentPayload.resource`` to be an object;
    the buyer SDK copies ``paymentRequired.resource`` verbatim into the payload it
    signs, so this shape must match ``{url, description, mimeType}`` (verified
    against @circle-fin/x402-batching's own server middleware).
    """
    return {
        "url": _resource_for(request, settings),
        "description": "ACR machine-commerce index query",
        "mimeType": "application/json",
    }


def _challenge_body(requirements: dict, resource: dict) -> dict:
    # x402Version MUST be 2 and `resource` MUST be an object — Circle's Gateway API
    # rejects a v1 envelope / string resource before it ever checks the signature.
    return {
        "x402Version": 2,
        "error": "payment required",
        "resource": resource,
        "accepts": [requirements],
    }


@dataclass
class PaymentReceipt:
    payer: str
    amount_usdc: float
    tx_ref: str
    network: str = ""
    scheme: str = ""
    #: Wall-clock settlement time (unix seconds) + the resource that was bought —
    #: stamped at record time so the durable ledger (ReceiptSource) knows *when*
    #: and *which service* each real payment settled. Defaulted so existing
    #: constructors and rehydration of legacy lines keep working.
    settled_at: float = 0.0
    resource: str = ""
    #: Who was PAID. Until the seller fleet existed every settlement went to one
    #: platform wallet, so the payee was implicit and a receipt did not carry it
    #: — which is exactly why the tape had no price dispersion and TCA had
    #: nothing to measure. Empty means "the platform wallet", i.e. a legacy row.
    seller: str = ""
    #: What was bought, so a unit price exists at all. ``unit`` names the service
    #: the quantity is denominated in (see ``acr_core.Service``); ``quantity`` is
    #: in that unit. Both optional so the committed archive and every legacy line
    #: in the durable ledger keep parsing.
    unit: str = ""
    quantity: float = 0.0
    #: Which tier the buyer's AGENT-CARD earned on this purchase: ``anonymous``,
    #: ``carded`` or ``human``. For a day the six paid routes verified the card and
    #: then dropped it, so "the card reaches where money moved" was true only of a
    #: counter on ``/agent/info``. Stamped here, a human-attributed settlement is a
    #: fact ON THE RECEIPT, the same row the ticker and the mirror read. Empty
    #: means a legacy line written before the field existed.
    tier: str = ""

    @property
    def unit_price(self) -> float | None:
        """USDC per service unit, or None when the receipt cannot support one.

        The denominator of every slippage number the tape publishes. A receipt
        with no quantity has no unit price — and must report that rather than a
        zero, which would read as "free" instead of "unknown".
        """
        if self.quantity <= 0:
            return None
        return self.amount_usdc / self.quantity


#: Schemes that represent REAL on-chain settlements (persisted to the durable
#: ledger + fed to ReceiptSource). Dev/sim receipts are demo-only and excluded.
_REAL_SCHEMES = frozenset({"exact"})


def _this_network() -> str:
    """This deployment's CAIP-2, read late so tests can switch chains."""
    from acr_core import get_settings

    return get_settings().caip2()


class Facilitator(ABC):
    """Base gate: bounded counters + the challenge/process seam ``require_payment``
    calls. Counters are a running total + a small ring of recent receipts, so a
    long-lived process serving sustained agent traffic does not leak memory."""

    def __init__(
        self, receipt_log_path: str | None = None, receipt_archive_path: str | None = None
    ) -> None:
        self.paid_queries = 0
        self._revenue = 0.0
        self.recent: deque[PaymentReceipt] = deque(maxlen=256)
        # None → read the path from settings (production); "" explicitly disables
        # the file (in-memory only — the default, so tests + the dev gate never
        # touch disk). Only REAL settlements (`_REAL_SCHEMES`) are persisted.
        self._log_path = get_settings().receipt_log_path if receipt_log_path is None else receipt_log_path
        # The committed record of real settlements — the only ledger that
        # survives a restart on a diskless host, because it ships in the image.
        #
        # OFF unless ACR_RECEIPT_ARCHIVE_PATH says otherwise, and that default
        # is deliberate. Pointing it at the repo's real archive by default made
        # every CircleFacilitator in the test suite start with two real
        # settlements already counted, which broke four tests and would have
        # meant a developer's local counters quietly showing production
        # revenue. A default that reaches into the repo for real financial data
        # is too magical to be safe; production opts in via render.yaml.
        #
        # Separate from the runtime-log parameter on purpose: CircleFacilitator
        # passes an explicit log path, so keying the archive off "log path is
        # None" would disable it on precisely the facilitator that needs it.
        self._archive_path = (
            os.environ.get("ACR_RECEIPT_ARCHIVE_PATH", "")
            if receipt_archive_path is None
            else receipt_archive_path
        )
        self._rehydrate()

    @property
    def revenue_usdc(self) -> float:
        # USDC carries six decimals, so 6dp is lossless — but a float sum of
        # 6dp values is not: seven settlements of 0.0001 accumulate to
        # 0.0007000000000000001, and that is what the public revenue counter
        # printed. Round at the boundary, where the number becomes a claim.
        return round(self._revenue, 6)

    def _rehydrate(self) -> None:
        """Reload the durable settlement ledger so /revenue + /marketplace/receipts
        survive a restart. Malformed/legacy lines are skipped.

        Reads TWO files, because on this deployment only one of them survives:

        * the runtime log (``ACR_RECEIPT_LOG_PATH``) — settlements from this
          container's own life, and empty on a host with no persistent disk,
          which the production free tier is;
        * the committed archive (``ACR_RECEIPT_ARCHIVE_PATH``) — real Gateway
          settlements that ship *inside the image*, so they outlive every
          restart.

        Without the second, the live product reported `paid_queries: 0` over a
        gate that had genuinely been paid — the pillar's only evidence existed
        in the repo and in the offline bundle, everywhere except the running
        API. Same shape as the faucet ledger, which rehydrates from Circle's own
        record rather than trusting a disk that is not there.
        """
        seen: set[str] = set()
        for path, label in (
            (self._log_path, "runtime log"),
            (self._archive_path, "committed archive"),
        ):
            if path:
                self._load_receipts(Path(path), label, seen)

    def _load_receipts(self, p: Path, label: str, seen: set[str]) -> None:
        """Merge one ledger file, skipping settlements already counted.

        ``seen`` is deduped on ``tx_ref`` ACROSS files: once the runtime log has
        a disk, a settlement can legitimately appear in both, and a counter that
        silently doubles its own revenue is worse than one that reads zero.
        """
        if not p.exists():
            return
        try:
            lines = p.read_text().splitlines()
        except Exception as exc:  # pragma: no cover - unreadable file
            log.warning("could not read receipt ledger %s: %s", p, exc)
            return
        loaded = 0
        for line in lines[-self.recent.maxlen :]:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                ref = str(row.get("tx_ref") or "")
                if not ref or ref in seen:
                    continue
                # The archive is the record of REAL payments. A dev or sim row
                # reaching it would put invented revenue on a public counter.
                if row.get("scheme") not in _REAL_SCHEMES:
                    continue
                # And a payment made on ANOTHER CHAIN is invented revenue here
                # for the same reason. The committed archive holds 112 real
                # settlements, all `eip155:5042002` — rehydrated unchanged on
                # mainnet they made `/revenue` report 113 paid queries and
                # $0.33 on a deployment that had earned $0.0001. A row with no
                # network recorded is left alone: pre-dating the field is not
                # evidence of the wrong chain, and every row in the archive has
                # one anyway.
                net = str(row.get("network") or "")
                if net and net != _this_network():
                    continue
                seen.add(ref)
                self.recent.append(PaymentReceipt(**row))
                self.paid_queries += 1
                self._revenue += float(row.get("amount_usdc", 0.0))
                loaded += 1
            except Exception:
                continue  # skip a corrupt/legacy line
        if loaded:
            log.info("receipts: rehydrated %d real settlement(s) from the %s (%s)",
                     loaded, label, p)

    def _persist(self, receipt: PaymentReceipt) -> None:
        # Durable ledger holds only real settlements (dev/sim receipts are demo).
        if not self._log_path or receipt.scheme not in _REAL_SCHEMES:
            return
        try:
            p = Path(self._log_path)
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a") as f:
                f.write(json.dumps(asdict(receipt), separators=(",", ":")) + "\n")
        except Exception as exc:  # never let logging break the settled response
            log.warning("could not append to receipt log: %s", exc)

    def _record(self, receipt: PaymentReceipt) -> PaymentReceipt:
        if not receipt.settled_at:
            receipt.settled_at = time.time()
        # Stamp who was paid and what was bought, here, where BOTH gates pass.
        # A unit price the tape can divide back out is the whole reason the
        # fleet exists; deriving it in each gate separately is how one of them
        # ends up recording the platform wallet against a seller's revenue.
        if not receipt.seller:
            from .fleet import listing_for_resource

            listing = listing_for_resource(receipt.resource)
            if listing is not None:
                receipt.seller = listing.seller
                receipt.unit = listing.unit
                receipt.quantity = listing.quantity
        self.paid_queries += 1
        self._revenue += receipt.amount_usdc
        self.recent.append(receipt)
        self._persist(receipt)
        # A REAL settlement asks the keeper to mirror it now rather than at the
        # next tick: in memory it survives only until the next restart, and the
        # money has already moved. Dev and sim rows never reach the chain.
        if receipt.scheme in _REAL_SCHEMES:
            try:
                from .keeper import kick_mirror

                kick_mirror()
            except Exception:  # noqa: BLE001 — the payment already succeeded
                log.warning("could not schedule the settlement mirror", exc_info=True)
        return receipt

    @abstractmethod
    def challenge(self, request: Request) -> PaymentRequired:
        """The 402 to raise when no payment header is present."""

    @abstractmethod
    async def process(self, request: Request, header: str, response: Response) -> PaymentReceipt:
        """Verify (and, live, settle) a presented payment; raise 402 if invalid."""


def _b64(obj: dict) -> str:
    return base64.b64encode(json.dumps(obj).encode()).decode()


class DevFacilitator(Facilitator):
    """Dev-mode gate — accepts a mock ``x402 <payer>:<amount>`` header. No chain."""

    def __init__(self) -> None:
        # The mock gate is never a real settlement — keep it purely in-memory so
        # it never reads from or writes to the durable settlement ledger.
        # No archive: a mock gate must never display real revenue.
        super().__init__(receipt_log_path="", receipt_archive_path="")

    def challenge(self, request: Request) -> PaymentRequired:
        s = get_settings()
        resource = _resource_for(request, s)
        pay_to, price = _fleet_terms(resource, s)
        body = _challenge_body(
            build_payment_requirements(resource, s, pay_to=pay_to, price=price),
            _resource_object(request, s),
        )
        return PaymentRequired(
            body=body,
            headers={
                "WWW-Authenticate": "x402",
                "X-402-Price": f"{price}",
                "X-402-Asset": ASSET,
                "X-402-Network": s.caip2(),
                "X-402-Pay-To": pay_to,
                # Same envelope as the JSON body — buyer SDKs (GatewayClient)
                # parse the header, not the body.
                "PAYMENT-REQUIRED": _b64(body),
            },
        )

    async def process(self, request: Request, header: str, response: Response) -> PaymentReceipt:
        try:
            scheme, rest = header.split(" ", 1)
            payer, amount = rest.split(":", 1)
            amt = float(amount)
        except Exception as exc:
            raise HTTPException(status_code=402, detail="malformed x402 payment") from exc
        if scheme.lower() != "x402":
            raise HTTPException(status_code=402, detail="unsupported payment scheme")
        # isfinite: "nan"/"inf" parse as floats, defeat the >= check (NaN
        # comparisons are all False), and a recorded non-finite amount poisons
        # the revenue counter — Starlette's JSON encoder then 500s /revenue.
        # Priced per RESOURCE, not per platform. When the price was a single
        # global this read `price_usdc()`, which would now wave through a buyer
        # paying the flat index fee for a fleet call that costs fifty times more.
        _, due = _fleet_terms(_resource_path(request), get_settings())
        if not math.isfinite(amt) or amt + 1e-12 < due:
            raise HTTPException(status_code=402, detail="insufficient payment")
        # tx_ref numbered AFTER the increment inside _record, so dev-N matches
        # the public ledger's seq N. Network is CAIP-2 like every other surface
        # (catalog, /x402/info) — only the legacy X-402-Network header keeps the
        # human "arc-testnet" name.
        net = get_settings().caip2()
        receipt = self._record(
            PaymentReceipt(payer=payer, amount_usdc=amt, tx_ref=f"dev-{self.paid_queries + 1}",
                           network=net, scheme="dev", resource=_resource_path(request),
                           tier=_tier_of(request))
        )
        # Same confirmation headers as the live gate, so buyer code decodes one
        # shape in both modes.
        confirmation = _b64(
            {"success": True, "transaction": receipt.tx_ref, "network": net,
             "payer": receipt.payer}
        )
        response.headers["PAYMENT-RESPONSE"] = confirmation
        response.headers["X-PAYMENT-RESPONSE"] = confirmation
        return receipt


class CircleFacilitator(Facilitator):
    """Real x402 v2 gate against a Circle Gateway facilitator (verify + settle)."""

    def __init__(self, http_client=None, settings=None) -> None:
        self.settings = settings or get_settings()
        # Durable ledger path comes from THIS gate's settings (injectable in tests),
        # not the global — so a test can point it at a tmp file.
        super().__init__(receipt_log_path=self.settings.receipt_log_path)
        self._http = http_client  # injectable httpx.AsyncClient for tests

    def payment_requirements(self, request: Request) -> dict:
        resource = _resource_for(request, self.settings)
        pay_to, price = _fleet_terms(resource, self.settings)
        return build_payment_requirements(
            resource, self.settings, pay_to=pay_to, price=price
        )

    def challenge(self, request: Request) -> PaymentRequired:
        # The b64 header carries the FULL {x402Version, accepts} envelope:
        # verified against @circle-fin/x402-batching v3, whose pay() decodes
        # PAYMENT-REQUIRED and reads `.accepts` (it never parses the body).
        body = _challenge_body(
            self.payment_requirements(request), _resource_object(request, self.settings)
        )
        return PaymentRequired(
            body=body,
            headers={
                "WWW-Authenticate": "x402",
                "PAYMENT-REQUIRED": _b64(body),
            },
        )

    # --- confirmed Gateway x402 API response shapes ---
    @staticmethod
    def _parse_verify(payload: dict) -> tuple[bool, str]:
        """``POST /v1/x402/verify`` → ``{isValid, invalidReason, payer}``."""
        return bool(payload.get("isValid")), str(payload.get("invalidReason") or "")

    @staticmethod
    def _parse_settle(payload: dict) -> tuple[bool, str, str, str]:
        """``POST /v1/x402/settle`` → ``{success, transaction, network, payer}``
        on success, ``{success: false, errorReason, …}`` on failure."""
        return (
            bool(payload.get("success")),
            str(payload.get("transaction") or ""),
            str(payload.get("network") or ""),
            str(payload.get("errorReason") or ""),
        )

    async def process(self, request: Request, header: str, response: Response) -> PaymentReceipt:
        reqs = self.payment_requirements(request)
        try:
            payload = json.loads(base64.b64decode(header))
        except Exception as exc:
            raise HTTPException(status_code=402, detail="malformed PAYMENT-SIGNATURE") from exc

        body = {"paymentPayload": payload, "paymentRequirements": reqs}
        base = self.settings.x402_facilitator_url
        # Gateway's Arc private-preview header. The preview ended (Gateway lists
        # Arc mainnet without it, probed 2026-09-21); the flag stays as an env
        # override because sending it is harmless and not sending it once broke
        # every verify with a reason that read like a bad payment.
        hdrs = {"X-ARC-PRIVATE-MAINNET-ENABLED": "true"} if self.settings.private_mainnet else {}
        try:
            client, owns = self._http, False
            if client is None:  # pragma: no cover - live path builds its own client
                import httpx

                client = httpx.AsyncClient(timeout=self.settings.x402_max_timeout_seconds)
                owns = True
            try:
                vr = await client.post(facilitator_endpoint(base, "verify"), json=body, headers=hdrs)
                valid, invalid_reason = self._parse_verify(vr.json())
                if not valid:
                    # Surface Circle's real reason in the server log — a bare 402
                    # reaching the buyer SDK collapses to "Payment Required".
                    log.warning("x402 verify rejected (HTTP %s): %s", vr.status_code, vr.text[:300])
                    raise HTTPException(
                        status_code=402,
                        detail=f"payment invalid: {invalid_reason or 'unspecified'}"[:160],
                    )
                sr = await client.post(facilitator_endpoint(base, "settle"), json=body, headers=hdrs)
                settle = sr.json()
                ok, tx, network, error_reason = self._parse_settle(settle)
                if not ok:
                    log.warning("x402 settle rejected (HTTP %s): %s", sr.status_code, sr.text[:300])
                    raise HTTPException(
                        status_code=402,
                        detail=f"settlement failed: {error_reason or 'unspecified'}"[:160],
                    )
            finally:
                if owns:  # pragma: no cover - live path
                    await client.aclose()
        except HTTPException:
            raise
        except Exception as exc:  # network/timeout/etc → fail closed
            log.warning("x402 facilitator error: %s", exc)
            raise HTTPException(status_code=402, detail="facilitator unavailable") from exc

        # The settle response's payer is authoritative (recovered from the
        # signature); the payload fields are a fallback for older facilitators.
        payer = str(settle.get("payer") or payload.get("from") or payload.get("payer") or "")
        receipt_network = network or reqs["network"]
        confirmation = _b64(
            {"success": True, "transaction": tx, "network": receipt_network, "payer": payer}
        )
        response.headers["PAYMENT-RESPONSE"] = confirmation
        response.headers["X-PAYMENT-RESPONSE"] = confirmation
        return self._record(
            # The AMOUNT CHARGED, which for a fleet listing is not the platform
            # flat price. Recording the flat price here would make every fleet
            # settlement's unit price wrong by the ratio between them — and the
            # unit price is what slippage is computed from.
            PaymentReceipt(payer=payer, amount_usdc=_fleet_terms(
                               _resource_path(request), self.settings)[1],
                           tx_ref=tx,
                           network=receipt_network, scheme=reqs["scheme"],
                           resource=_resource_path(request), tier=_tier_of(request))
        )


# --- module singleton + selection (mirrors onchain.get_reader / reset_reader) ---
_facilitator: Facilitator | None = None


def get_facilitator() -> Facilitator:
    global _facilitator
    if _facilitator is None:
        s = get_settings()
        mode = s.x402_mode.strip().lower()
        if mode == "dev":
            _facilitator = DevFacilitator()
        elif mode == "circle":
            # Explicit circle mode without config still fails closed: every
            # process() error is a 402, never an unpaid serve.
            if not (s.x402_facilitator_url and s.x402_pay_to):
                log.warning("x402_mode=circle but facilitator URL/pay-to unset — will fail closed")
            _facilitator = CircleFacilitator(settings=s)
        else:  # auto — Circle only if the URL + pay-to actually look real
            url, pay_to = s.x402_facilitator_url.strip(), s.x402_pay_to.strip()
            use_circle = url.startswith("http") and pay_to.startswith("0x")
            _facilitator = CircleFacilitator(settings=s) if use_circle else DevFacilitator()
    return _facilitator


def set_facilitator(fac: Facilitator) -> None:
    """Install a facilitator (tests inject a CircleFacilitator wired to a mock)."""
    global _facilitator
    _facilitator = fac


def reset_facilitator() -> None:
    global _facilitator
    _facilitator = None


def pass_receipt(desk_session: str) -> PaymentReceipt | None:
    """A receipt-shaped answer for a Desk session holding a pass, or None. Amount
    zero and a `pass:` reference, so nothing downstream mistakes it for a
    settlement — it never reaches the receipts tape."""
    from web3 import Web3

    from . import desk, feedpass
    from .desk import DeskError

    s = get_settings()
    try:
        w = desk.wallet_of(desk_session)
    except DeskError:
        return None
    except Exception:  # noqa: BLE001 — Circle down is "no pass", not a 500
        return None
    if not w:
        return None
    w3 = Web3(Web3.HTTPProvider(s.arc_rpc_url, request_kwargs={"timeout": 10}))
    has, until = feedpass.access(w3, w["address"])
    if not has:
        return None
    return PaymentReceipt(
        payer=w["address"], amount_usdc=0.0, tx_ref=f"pass:{until}", network=s.caip2(),
        scheme="feed-pass", settled_at=float(until), resource="", seller=s.x402_pay_to or "",
    )


async def require_payment(
    request: Request,
    response: Response,
    payment_signature: str | None = Header(default=None, alias="PAYMENT-SIGNATURE"),
    x_payment: str | None = Header(default=None, alias="X-Payment"),
    desk_session: str | None = Header(default=None, alias="DESK-SESSION"),
) -> PaymentReceipt:
    """FastAPI dependency: 402 unless a valid x402 payment is present. Accepts the
    canonical ``PAYMENT-SIGNATURE`` header or the legacy ``X-Payment`` — or a
    ``DESK-SESSION`` whose wallet holds a feed pass on chain (index_api.feedpass):
    the Desk's smart accounts cannot sign x402, so their payment is a USDC transfer
    the chain saw, and the gate asks the chain rather than a header."""
    fac = get_facilitator()
    header = payment_signature or x_payment
    if header is None and desk_session:
        receipt = pass_receipt(desk_session)
        if receipt is not None:
            request.state.payment = receipt
            return receipt
    if header is None:
        raise fac.challenge(request)
    receipt = await fac.process(request, header, response)
    request.state.payment = receipt
    log.info("x402 settled %s paid $%.4f (%s)", receipt.payer, receipt.amount_usdc, receipt.tx_ref)
    return receipt
