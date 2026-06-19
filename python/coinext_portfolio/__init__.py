"""coinext_portfolio — Python facade mirroring the Rust ``Portfolio`` port.

The AUTHORITATIVE portfolio (balances, realized/unrealized PnL, exposure) lives in Rust
(``coinext-portfolio``), sourced from the Cache marks (ARCHITECTURE.md §3, §7). This package is a read
facade for Python services (``api``, ``risk-monitor``, analytics): it exposes the SAME shape so
Python code can reason about positions/PnL without re-deriving them, whether the data comes from
``coinext_py`` in-process or from the Redis bus out-of-process.

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

    @property
    def unrealized_pnl(self) -> float:
        """Mark-to-market PnL at the current mark (display-only float math)."""
        return (self.mark_price - self.avg_price) * self.net_qty

    @property
    def notional(self) -> float:
        """Absolute exposure of this position at the current mark."""
        return abs(self.net_qty) * self.mark_price


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
        """Cash + unrealized PnL across all positions (mark-sourced)."""
        return self.account.cash_balance + sum(p.unrealized_pnl for p in self.positions.values())

    def gross_exposure(self) -> float:
        """Sum of absolute position notionals (feeds the ``max_gross_exposure`` risk limit)."""
        return sum(p.notional for p in self.positions.values())

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
        """Build a facade from a ``coinext_py`` portfolio handle.

        TODO: map the native Position/Account objects (integer domain) into the float views here.
        """
        raise NotImplementedError(
            "from_native is a stub; wire to coinext_py's Portfolio export once available."
        )


__all__ = ["Portfolio", "PositionView", "AccountView"]
