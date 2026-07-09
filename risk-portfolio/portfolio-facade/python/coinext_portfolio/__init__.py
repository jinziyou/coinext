"""coinext_portfolio — Python facade mirroring the Rust ``Portfolio`` port.

The AUTHORITATIVE portfolio (balances, realized/unrealized PnL, exposure) lives in Rust
(``coinext-portfolio``), sourced from the Cache marks (ARCHITECTURE.md §3, §7). This package is a read
facade used by `coinext_live.TradingNode.publish_portfolio(...)` to turn an authoritative portfolio
snapshot into the `LivePositionPnl` bus payload consumed by the API and `risk-monitor`. It exposes the
SAME shape whether the data comes from ``coinext_py`` in-process or from the Redis bus out-of-process.

All money/size values keep the integer-backed domain semantics; here they surface as plain floats
for display/aggregation only (never used for matching — ARCHITECTURE.md §4).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PositionView:
    """A flattened position snapshot for one instrument."""

    symbol: str
    venue: str = "BINANCE"
    net_qty: float = 0.0  # signed; >0 long, <0 short
    avg_price: float = 0.0
    mark_price: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl_override: float | None = None
    notional_override: float | None = None

    @property
    def unrealized_pnl(self) -> float:
        """Mark-to-market PnL at the current mark (display-only float math)."""
        if self.unrealized_pnl_override is not None:
            return self.unrealized_pnl_override
        return (self.mark_price - self.avg_price) * self.net_qty

    @property
    def notional(self) -> float:
        """Absolute exposure of this position at the current mark."""
        if self.notional_override is not None:
            return self.notional_override
        return abs(self.net_qty) * self.mark_price

    @property
    def side(self) -> str:
        """Human-readable side derived from signed net quantity."""
        if self.net_qty > 0:
            return "long"
        if self.net_qty < 0:
            return "short"
        return "flat"

    def telemetry_row(self) -> dict[str, object]:
        """Wire-safe row for ``LivePositionPnl.positions``."""
        return {
            "symbol": self.symbol,
            "venue": self.venue,
            "side": self.side,
            "net_qty": str(self.net_qty),
            "avg_price": str(self.avg_price),
            "mark_price": str(self.mark_price),
            "realized_pnl": str(self.realized_pnl),
            "unrealized_pnl": str(self.unrealized_pnl),
            "notional": str(self.notional),
        }


@dataclass
class AccountView:
    """Balances + aggregate PnL for one account."""

    base_currency: str = "USDT"
    cash_balance: float = 0.0
    realized_pnl: float = 0.0


@dataclass
class Portfolio:
    """Facade mirroring the Rust ``Portfolio`` port.

    A real instance is fed by ``coinext_py`` (in-process) or reconstructed from bus events (out-of-proc).
    The methods mirror the Rust port surface so call sites read the same in either deployment.
    """

    account: AccountView = field(default_factory=AccountView)
    positions: dict[str, PositionView] = field(default_factory=dict)

    def position(self, symbol: str) -> PositionView | None:
        """Return the position view for ``symbol`` (or ``None`` if flat/unknown)."""
        return self.positions.get(symbol)

    def net_position(self, symbol: str) -> float:
        """Signed net quantity for ``symbol`` (0.0 if flat)."""
        p = self.positions.get(symbol)
        return p.net_qty if p else 0.0

    def total_equity(self) -> float:
        """Cash + realized + unrealized PnL across all positions."""
        return self.account.cash_balance + self.account.realized_pnl + self.unrealized_pnl()

    def gross_exposure(self) -> float:
        """Sum of absolute position notionals (feeds the ``max_gross_exposure`` risk limit)."""
        return sum(p.notional for p in self.positions.values())

    def net_exposure(self) -> float:
        """Signed notional exposure across positions."""
        return sum(p.net_qty * p.mark_price for p in self.positions.values())

    def unrealized_pnl(self) -> float:
        """Aggregate mark-to-market PnL across positions."""
        return sum(p.unrealized_pnl for p in self.positions.values())

    def telemetry_positions(self) -> list[dict[str, object]]:
        """Position rows for the ``LivePositionPnl`` bus payload."""
        return [p.telemetry_row() for p in self.positions.values()]

    def realized_pnl(self) -> float:
        """Account-level realized PnL."""
        return self.account.realized_pnl

    def apply_mark(self, symbol: str, mark_price: float) -> None:
        """Update the mark for ``symbol`` (the only input unrealized PnL depends on). TODO: bus."""
        p = self.positions.get(symbol)
        if p is not None:
            p.mark_price = mark_price

    @classmethod
    def from_native(cls, native: Any) -> Portfolio:
        """Build a facade from a ``coinext_py.PortfolioSnapshot`` or backtest result carrying one."""
        snapshot = getattr(native, "portfolio", native)
        rows = snapshot.positions
        positions: dict[str, PositionView] = {}
        unrealized_total = float(getattr(snapshot, "unrealized_pnl", 0.0))
        for row in rows:
            if isinstance(row, tuple):
                (
                    symbol,
                    venue,
                    net_qty,
                    avg_price,
                    mark_price,
                    realized_pnl,
                    unrealized_pnl,
                    notional,
                ) = row
            else:
                symbol = row.symbol
                venue = getattr(row, "venue", "BINANCE")
                net_qty = row.net_qty
                avg_price = row.avg_price
                mark_price = row.mark_price
                realized_pnl = row.realized_pnl
                unrealized_pnl = row.unrealized_pnl
                notional = row.notional
            positions[str(symbol)] = PositionView(
                symbol=str(symbol),
                venue=str(venue),
                net_qty=float(net_qty),
                avg_price=float(avg_price),
                mark_price=float(mark_price),
                realized_pnl=float(realized_pnl),
                unrealized_pnl_override=float(unrealized_pnl),
                notional_override=float(notional),
            )
        equity = getattr(snapshot, "equity", None)
        cash_balance = float(getattr(snapshot, "cash_balance", 0.0))
        realized = float(getattr(snapshot, "realized_pnl", 0.0))
        if equity is not None:
            cash_balance = float(equity) - realized - unrealized_total
        return cls(
            account=AccountView(cash_balance=cash_balance, realized_pnl=realized),
            positions=positions,
        )


__all__ = ["Portfolio", "PositionView", "AccountView"]
