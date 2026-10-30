"""ACR-Weekly future — cash-settled against the oracle print.

Pillar 4. The surviving form of the earlier "capacity forwards" idea with its
hardest organ amputated: **no delivery, no bonds, no seller cooperation**. At
expiry the future simply cash-settles against ``ACROracle.latestPrint`` — the
same reason real commodity markets migrated from physical to cash settlement:
it deletes the delivery-enforcement problem entirely.

A single cash-settled future plus a market maker is enough to create the first
*term structure* in machine commerce.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Position:
    """A trader's net position in a future (in contracts; +long / -short)."""

    contracts: float = 0.0
    #: Volume-weighted average entry price.
    avg_price: float = 0.0
    realized_pnl: float = 0.0

    def apply_fill(self, qty: float, price: float) -> None:
        """Apply a fill of ``qty`` contracts (signed) at ``price``."""
        new_contracts = self.contracts + qty
        # Reducing or flipping realizes PnL on the closed portion.
        if self.contracts != 0 and (self.contracts > 0) != (qty > 0):
            closed = min(abs(qty), abs(self.contracts))
            direction = 1.0 if self.contracts > 0 else -1.0
            self.realized_pnl += closed * direction * (price - self.avg_price)
        if new_contracts == 0:
            self.avg_price = 0.0
        elif (self.contracts >= 0) == (qty >= 0) and self.contracts != 0:
            # Adding in the same direction: blend entry price.
            self.avg_price = (
                self.avg_price * abs(self.contracts) + price * abs(qty)
            ) / abs(new_contracts)
        elif (self.contracts > 0) != (qty > 0) and abs(qty) > abs(self.contracts):
            self.avg_price = price  # flipped past flat
        elif self.contracts == 0:
            self.avg_price = price
        self.contracts = new_contracts

    def unrealized_pnl(self, mark: float) -> float:
        return self.contracts * (mark - self.avg_price)

    def settle(self, settlement_price: float) -> float:
        """Cash-settle the whole position; returns total PnL and flattens."""
        self.realized_pnl += self.contracts * (settlement_price - self.avg_price)
        self.contracts = 0.0
        self.avg_price = 0.0
        return self.realized_pnl


@dataclass
class ACRFuture:
    """A weekly cash-settled future on an ACR index."""

    index_id: str
    expiry_ts: float
    #: Contract multiplier (USDC per 1.0 of index value).
    multiplier: float = 1000.0
    #: Fills recorded for audit / term-structure construction.
    trades: list[tuple[float, float, float]] = field(default_factory=list)  # (ts, qty, price)

    def is_expired(self, now: float) -> bool:
        return now >= self.expiry_ts

    def cash_settle(self, position: Position, oracle_print: float) -> float:
        """Settle ``position`` against the oracle print (in USDC)."""
        return position.settle(oracle_print) * self.multiplier

    def record(self, ts: float, qty: float, price: float) -> None:
        self.trades.append((ts, qty, price))
