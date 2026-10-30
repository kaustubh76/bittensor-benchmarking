"""Avellaneda–Stoikov market maker for the ACR future.

Mission Control quotes the weekly future live in the demo. The A–S model sets a
*reservation price* that skews away from the mid as inventory builds (so the MM
naturally mean-reverts its position) and an optimal spread that trades off the
fill rate against inventory risk:

    r(s, q) = s − q · γ · σ² · (T − t)                       (reservation price)
    δ_a + δ_b = γ · σ² · (T − t) + (2/γ) · ln(1 + γ/κ)       (optimal spread)

with s the mid (the ACR oracle print), q inventory, γ risk aversion, σ index
volatility, κ order-book liquidity, and (T−t) time to expiry. Seeding this
quoting is how ACR bootstraps its own term structure rather than waiting for
liquidity to appear.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from acr_core import Quote, get_settings

from .future import Position


@dataclass
class ASParams:
    gamma: float = 0.1  # inventory risk aversion
    # Matches acr_core's as_kappa. It used to default to 1.5, which prices a
    # ~12,900 bp book: anything constructing ASParams() without settings got a
    # degenerate corridor while the deployment got 50 bp, and only a comment in
    # the test suite said so.
    kappa: float = 400.0  # order-book liquidity
    sigma: float = 0.02  # index volatility, PER WEEK (~14% annualized)
    horizon: float = 1.0  # normalized time-to-expiry at t=0


class AvellanedaStoikovMM:
    def __init__(self, index_id: str, expiry_ts: float, params: ASParams | None = None) -> None:
        self.index_id = index_id
        self.expiry_ts = expiry_ts
        if params is None:
            s = get_settings()
            params = ASParams(gamma=s.as_gamma, kappa=s.as_kappa)
        self.p = params

    def _time_left(self, now: float, start: float) -> float:
        """Fraction of THIS contract's life still to run, times the horizon.

        Note what this is not: it is a normalized 1→0 decay for one contract
        ageing toward its own expiry, NOT a duration. Feeding it a family of
        expiries to build a term structure divides the tenor out exactly
        (``(T-t)/(T-start)`` is 1.0 for every T when ``now == start``), which is
        precisely how /curve came to publish one quote under four tenor labels.
        Cross-sectional callers must pass ``tau`` to ``quote`` instead.
        """
        total = max(self.expiry_ts - start, 1e-9)
        return max(0.0, (self.expiry_ts - now) / total) * self.p.horizon

    def reservation_price(self, mid: float, inventory: float, tau: float) -> float:
        """Avellaneda-Stoikov's inventory lean, in the price's own units.

        The canonical form is ``r = s - q·γ·σ²·(T-t)`` with σ an ABSOLUTE price
        volatility. ``sigma`` here is fractional (0.02 = 2%), and
        ``optimal_half_spread`` is duly converted at the call site
        (``half * mid``) — but this term was not, so the lean came out as a
        fixed number of dollars applied to a family of indices spanning two
        orders of magnitude.

        Measured on the live book before the fix: the SAME inventory moved
        ACR-INF (level 0.49) by 2.7 bp and ACR-GPU (level 0.011) by 71.9 bp —
        wider than GPU's entire 50 bp spread, so spot fell outside the quoted
        corridor entirely and the /curve page looked broken. Scaling by ``mid``
        makes the lean ~0.4 bp per contract on every index, which is what
        "the same book leans the same way" has to mean for a family quoted in
        $/1k-tokens, $/GPU-sec and $/MB at once.
        """
        return mid - inventory * self.p.gamma * self.p.sigma**2 * tau * mid

    def optimal_half_spread(self, tau: float) -> float:
        inv_risk = self.p.gamma * self.p.sigma**2 * tau
        book = (2.0 / self.p.gamma) * math.log(1.0 + self.p.gamma / self.p.kappa)
        return 0.5 * (inv_risk + book)

    def quote(
        self,
        mid: float,
        position: Position,
        now: float,
        start: float,
        size: float = 1.0,
        tau: float | None = None,
    ) -> Quote:
        """Produce a two-sided quote around the oracle print ``mid``.

        ``tau`` overrides the computed time-to-expiry, in the SAME time unit
        ``sigma`` is quoted in. That unit was implicit until now, and its being
        implicit is what let the term structure collapse unnoticed: a caller
        pricing several tenors at once must supply the horizon explicitly,
        because ``_time_left`` normalizes it away. ``store.curve`` passes the
        tenor in weeks and therefore reads ``sigma`` as a WEEKLY volatility
        (0.02/week, about 14% annualized).
        """
        if tau is None:
            tau = self._time_left(now, start)
        r = self.reservation_price(mid, position.contracts, tau)
        half = self.optimal_half_spread(tau) * mid  # scale spread to price level
        bid = max(1e-9, r - half)
        # Clamp the ask above the bid too: at large inventory the reservation
        # price can go deeply negative, and Quote enforces ask > 0 (Field(gt=0)).
        ask = max(r + half, bid + 1e-9)
        return Quote(
            index_id=self.index_id,
            expiry_ts=self.expiry_ts,
            bid=bid,
            ask=ask,
            bid_size=size,
            ask_size=size,
            ts=now,
        )
