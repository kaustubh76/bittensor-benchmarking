"""Counterparty screening — is this an address we may pay at all?

RFB 2 asks for a workflow that "screens a vendor's wallet address before paying
it"; RFB 3 for "compliance screening at onboarding, before the first payment
clears". Prior Art #07 names the tool: self-host ``opensanctions/yente``,
re-screen on a schedule rather than once, and stop treating the answer as a
yes-or-no — *"a medium-risk counterparty gets a lower limit, not a refusal."*

Mirrors ``armor.py`` deliberately: same frozen verdict, same ABC seam, same
``build_/get_/set_/reset_`` registry, same insistence that "no screen ran" is a
different fact from "ran and found nothing". One reviewer's understanding of
that module covers this one.

THREE RISK STATES, NOT TWO, and the middle one is the point:

    clear     screened, nothing matched. Pay under the ordinary rules.
    flagged   screened, something matched. Never paid automatically.
    unknown   no screen reached a verdict. NOT "clear" — a screening service
              that times out must not read as a clean bill of health, which is
              the single most important line in this file.

An ``unknown`` does not stop the operator by default, because an operator that
refuses everything without a screening service is an operator nobody runs. It
records the gap on every decision instead, and ``ACR_SCREEN_REQUIRED=1`` turns
it into an escalation for a business that wants that. The gap is always visible;
only the consequence is configurable.
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass

log = logging.getLogger("index_api.counterparty")

CLEAR = "clear"
FLAGGED = "flagged"
UNKNOWN = "unknown"

#: How long a screen may take before it is `unknown`. A payment decision should
#: not hang on a third party; it should record that it could not check.
TIMEOUT_S = float(os.environ.get("ACR_SCREEN_TIMEOUT_S", "4"))

#: Whether an unscreened counterparty blocks the payment. Default off — see the
#: module docstring for why refusing everything is not a safer default, it is a
#: product nobody runs.
REQUIRED = os.environ.get("ACR_SCREEN_REQUIRED", "") not in ("", "0", "false")


@dataclass(frozen=True)
class CounterpartyVerdict:
    """What a screen decided about one address, and enough to act on.

    ``matched`` names the lists or datasets that fired, so an operator can tell
    a sanctions hit from a PEP note without re-running the query. The address is
    kept because it is public and the whole point is auditability; nothing else
    about the counterparty is.
    """

    address: str
    risk: str
    backend: str
    matched: tuple[str, ...] = ()
    reason: str = ""
    #: False when no screen ran at all. A different fact from a clean result and
    #: it must never render as one.
    screened: bool = True

    @property
    def payable(self) -> bool:
        """May the agent pay this on its own authority?

        ``unknown`` is payable unless screening is required: the gap is recorded
        either way, and a business that wants the stricter reading sets the flag.
        """
        if self.risk == FLAGGED:
            return False
        if self.risk == UNKNOWN:
            return not REQUIRED
        return True

    def as_dict(self) -> dict:
        return {
            "address": self.address,
            "risk": self.risk,
            "backend": self.backend,
            "matched": list(self.matched),
            "reason": self.reason,
            "screened": self.screened,
        }


class CounterpartyScreen(ABC):
    """The seam. Subclasses turn an address into a verdict."""

    backend = "base"

    def __init__(self) -> None:
        self.checked = 0
        self.flagged = 0

    @abstractmethod
    def _check(self, address: str) -> CounterpartyVerdict: ...

    def check(self, address: str) -> CounterpartyVerdict:
        """Never raises. A screen that cannot answer returns ``unknown``.

        Raising here would make a third party's outage indistinguishable from a
        bug in the operator, and the caller's only sane response to either is the
        same: record that the check did not happen.
        """
        addr = (address or "").strip()
        if not addr:
            return CounterpartyVerdict(
                address="", risk=UNKNOWN, backend=self.backend,
                reason="no address to screen", screened=False,
            )
        try:
            v = self._check(addr)
        except Exception as exc:  # noqa: BLE001
            log.warning("counterparty: %s could not answer (%s)", self.backend, exc)
            return CounterpartyVerdict(
                address=addr, risk=UNKNOWN, backend=self.backend,
                reason=f"the screen could not answer: {str(exc)[:120]}",
                screened=False,
            )
        self.checked += 1
        if v.risk == FLAGGED:
            self.flagged += 1
        return v


class NullCounterpartyScreen(CounterpartyScreen):
    """No screening, stated. Off is a configuration, not an absence."""

    backend = "off"

    def _check(self, address: str) -> CounterpartyVerdict:
        return CounterpartyVerdict(
            address=address, risk=UNKNOWN, backend=self.backend,
            reason="counterparty screening is switched off", screened=False,
        )


class DenyListScreen(CounterpartyScreen):
    """An explicit local denylist, honest about being a floor rather than a screen.

    Exists for the same three reasons ``LocalScreen`` does: the tests must not
    need a network, the base image must deploy without one, and the fallback
    should not be silence. It answers ``clear`` only for addresses it has
    actually compared against a list, and it says which list.
    """

    backend = "denylist"

    def __init__(self, denied: set[str] | None = None) -> None:
        super().__init__()
        raw = os.environ.get("ACR_SCREEN_DENYLIST", "")
        self.denied = {a.strip().lower() for a in raw.split(",") if a.strip()}
        if denied:
            self.denied |= {a.lower() for a in denied}

    def _check(self, address: str) -> CounterpartyVerdict:
        if address.lower() in self.denied:
            return CounterpartyVerdict(
                address=address, risk=FLAGGED, backend=self.backend,
                matched=("local-denylist",),
                reason="this address is on the operator's own denylist",
            )
        if not self.denied:
            # A SCREEN THAT CANNOT FAIL IS NOT A SCREEN. With no list configured
            # this used to answer CLEAR with the reason "not on a local list of
            # 0" — and the reason is dropped at the record boundary, so every
            # vendor rendered a teal "clear · checked, fine" chip whose entire
            # basis was a comparison against nothing. That is the module's own
            # `unknown` is never `clear` rule, one step further out: the
            # distinction it protects is between a verdict and the absence of
            # one, and an empty list produces the absence.
            #
            # `unknown` stays payable unless ACR_SCREEN_REQUIRED says otherwise,
            # so this changes what we CLAIM, not what we allow. `screening()`
            # counts unknown-and-paid as `paid_unscreened`, which now reports
            # the truth about a host with no list.
            return CounterpartyVerdict(
                address=address, risk=UNKNOWN, backend=self.backend,
                reason=(
                    "no denylist is configured, so nothing was compared: set "
                    "ACR_SCREEN_DENYLIST or ACR_SCREEN_YENTE_URL"
                ),
                screened=False,
            )
        return CounterpartyVerdict(
            address=address, risk=CLEAR, backend=self.backend,
            reason=f"not on a local list of {len(self.denied)}",
        )


class YenteScreen(CounterpartyScreen):
    """OpenSanctions ``yente`` over its search API — the tool Prior Art #07 names.

    A hit is ``flagged`` rather than refused outright: this module reports risk
    and ``decide()`` chooses, because "medium risk gets a lower limit, not a
    refusal" is the RFB's own framing and a screen that decides policy cannot be
    reused by a business with a different policy.

    Anything other than a clean 200 with a readable body is ``unknown``, and the
    base class turns an exception into the same. Never ``clear`` by accident.
    """

    backend = "yente"

    def __init__(self, base_url: str, dataset: str = "default") -> None:
        super().__init__()
        self.base_url = base_url.rstrip("/")
        self.dataset = dataset

    def _check(self, address: str) -> CounterpartyVerdict:
        import httpx

        r = httpx.get(
            f"{self.base_url}/search/{self.dataset}",
            params={"q": address, "limit": 5},
            timeout=TIMEOUT_S,
        )
        r.raise_for_status()
        body = r.json()
        results = body.get("results") or []
        if results:
            names = tuple(
                sorted({str(x.get("dataset") or "unknown") for x in results})
            )[:5]
            return CounterpartyVerdict(
                address=address, risk=FLAGGED, backend=self.backend,
                matched=names,
                reason=f"{len(results)} match(es) in {', '.join(names)}",
            )
        return CounterpartyVerdict(
            address=address, risk=CLEAR, backend=self.backend,
            reason=f"no match in {self.dataset}",
        )


def build_screen(settings=None) -> CounterpartyScreen:
    """The screen this host is configured for.

    ``ACR_SCREEN_YENTE_URL`` picks yente. Otherwise a denylist, which is a floor
    and says so. Never raises: a misconfigured screen must not stop the service
    from starting, it must make every verdict ``unknown`` and visible.
    """
    url = os.environ.get("ACR_SCREEN_YENTE_URL", "").strip()
    if url:
        return YenteScreen(url, os.environ.get("ACR_SCREEN_DATASET", "default"))
    if os.environ.get("ACR_SCREEN_MODE", "").strip() == "off":
        return NullCounterpartyScreen()
    return DenyListScreen()


_screen: CounterpartyScreen | None = None


def get_screen() -> CounterpartyScreen:
    global _screen
    if _screen is None:
        _screen = build_screen()
    return _screen


def set_screen(screen: CounterpartyScreen) -> None:
    """Test seam."""
    global _screen
    _screen = screen


def reset_screen() -> None:
    global _screen
    _screen = None
