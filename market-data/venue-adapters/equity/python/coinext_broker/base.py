"""Equity broker port + offline paper implementation.

Mirrors the spirit of ``coinext-ports::ExecutionClient`` without requiring the Rust kernel:
research scripts and future live adapters share :class:`EquityBroker`.

Paper rules for **A股** (SSE/SZSE):

* **T+1** — shares bought on session day ``D`` cannot be sold until day ``D+1``
  (tracked via :meth:`set_session_day` / bar timestamps).
* **涨跌停** — when a previous close is known (:meth:`set_prev_close` / :meth:`on_bar`),
  market/limit prices outside the ±limit band are rejected (or market orders clamp-to-limit
  when ``clamp_to_limit=True``).
"""

from __future__ import annotations

import datetime as dt
import itertools
import time
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from .rules import is_t1_venue, limit_band, trade_date_from_ns

OrderSide = Literal["buy", "sell"]
OrderType = Literal["market", "limit"]
OrderStatus = Literal[
    "pending",
    "accepted",
    "partial",
    "filled",
    "canceled",
    "rejected",
]


@dataclass(slots=True)
class BrokerOrder:
    """One working or terminal order at the broker."""

    client_order_id: str
    venue: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    qty: float
    limit_price: float | None = None
    status: OrderStatus = "pending"
    filled_qty: float = 0.0
    avg_price: float = 0.0
    ts_submit_ns: int = 0
    ts_update_ns: int = 0
    reject_reason: str | None = None


@dataclass(slots=True)
class BrokerFill:
    """A fill report (partial or final)."""

    client_order_id: str
    venue: str
    symbol: str
    side: OrderSide
    qty: float
    price: float
    fee: float
    ts_event_ns: int
    trade_id: str = ""


@runtime_checkable
class EquityBroker(Protocol):
    """Minimal broker port for cash equities / ETFs (A股 / 美股 / 港股)."""

    def connect(self) -> None: ...
    def disconnect(self) -> None: ...

    def submit_market(
        self, venue: str, symbol: str, side: OrderSide, qty: float
    ) -> BrokerOrder: ...

    def submit_limit(
        self, venue: str, symbol: str, side: OrderSide, qty: float, price: float
    ) -> BrokerOrder: ...

    def cancel(self, client_order_id: str) -> BrokerOrder: ...

    def open_orders(self) -> list[BrokerOrder]: ...

    def positions(self) -> dict[str, float]:
        """``{venue:symbol → signed qty}``."""
        ...

    def cash(self) -> dict[str, float]:
        """``{currency → balance}``."""
        ...


def _now_ns() -> int:
    return int(time.time() * 1_000_000_000)


@dataclass
class PaperEquityBroker:
    """Offline paper broker for multi-market equities.

    Market orders fill immediately at the last mark (caller must :meth:`set_mark`).
    Limit orders rest until :meth:`on_bar` / :meth:`set_mark` crosses the price.
    Fees come from :func:`coinext_data.instrument_spec` when available.

    A-share extras: T+1 sellability and daily price-limit checks (see module docstring).
    """

    starting_cash: dict[str, float] = field(default_factory=lambda: {"USD": 100_000.0})
    enforce_t1: bool = True
    enforce_price_limits: bool = True
    clamp_to_limit: bool = True
    """If True, market orders at/through limit fill at the limit price; else reject."""

    _cash: dict[str, float] = field(init=False)
    _positions: dict[str, float] = field(default_factory=dict)
    _orders: dict[str, BrokerOrder] = field(default_factory=dict)
    _fills: list[BrokerFill] = field(default_factory=list)
    _marks: dict[str, float] = field(default_factory=dict)  # venue:symbol → last px
    _prev_close: dict[str, float] = field(default_factory=dict)
    # T+1: bought qty on each session day — key (venue:symbol, date_iso) → qty
    _bought_on: dict[tuple[str, str], float] = field(default_factory=dict)
    _session_day: dt.date | None = None
    _id_seq: itertools.count = field(default_factory=lambda: itertools.count(1))
    _connected: bool = False

    def __post_init__(self) -> None:
        self._cash = {k.upper(): float(v) for k, v in self.starting_cash.items()}

    def connect(self) -> None:
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def set_session_day(self, day: dt.date | str) -> None:
        """Set the current trading session day for T+1 accounting."""
        if isinstance(day, str):
            day = dt.date.fromisoformat(day)
        self._session_day = day

    def set_prev_close(self, venue: str, symbol: str, prev_close: float) -> None:
        """Set previous session close (for 涨跌停 bands)."""
        key = f"{venue.upper()}:{symbol.upper()}"
        self._prev_close[key] = float(prev_close)

    def set_mark(self, venue: str, symbol: str, price: float) -> None:
        """Update last mark and try to fill resting limits."""
        key = f"{venue.upper()}:{symbol.upper()}"
        self._marks[key] = float(price)
        self._match_limits(venue.upper(), symbol.upper(), float(price))

    def on_bar(
        self,
        venue: str,
        symbol: str,
        *,
        high: float,
        low: float,
        close: float,
        open_: float | None = None,
        ts_ns: int | None = None,
    ) -> list[BrokerFill]:
        """Drive session day, prev-close roll, limit matching, and close mark.

        Call once per bar in chronological order. The **previous** close used for limits is the
        close from the prior :meth:`on_bar` (or :meth:`set_prev_close`).
        """
        before = len(self._fills)
        ts = ts_ns if ts_ns is not None else _now_ns()
        if self.enforce_t1:
            self._session_day = trade_date_from_ns(ts)
        v, s = venue.upper(), symbol.upper()
        key = f"{v}:{s}"
        # Match limits against high/low before rolling prev_close to today's close.
        for order in list(self._orders.values()):
            if order.status not in ("accepted", "partial"):
                continue
            if order.venue != v or order.symbol != s or order.limit_price is None:
                continue
            px = order.limit_price
            hit = (order.side == "buy" and low <= px) or (order.side == "sell" and high >= px)
            if hit:
                self._fill(order, px, order.qty - order.filled_qty, ts)
        self._marks[key] = float(close)
        # After the bar, today's close becomes next bar's prev_close.
        self._prev_close[key] = float(close)
        return self._fills[before:]

    def sellable_qty(self, venue: str, symbol: str, *, day: dt.date | None = None) -> float:
        """Shares available to sell under T+1 (total − bought today on A-shares)."""
        key = f"{venue.upper()}:{symbol.upper()}"
        pos = self._positions.get(key, 0.0)
        if not self.enforce_t1 or not is_t1_venue(venue):
            return pos
        d = day or self._session_day or dt.datetime.now(tz=dt.UTC).date()
        bought = self._bought_on.get((key, d.isoformat()), 0.0)
        return max(0.0, pos - bought)

    def submit_market(
        self, venue: str, symbol: str, side: OrderSide, qty: float
    ) -> BrokerOrder:
        self._require()
        order = self._new_order(venue, symbol, side, "market", qty, None)
        key = f"{order.venue}:{order.symbol}"
        mark = self._marks.get(key)
        if mark is None:
            order.status = "rejected"
            order.reject_reason = f"no mark for {key}; call set_mark first"
            order.ts_update_ns = _now_ns()
            return order
        px, reason = self._check_price(order.venue, order.symbol, side, mark, is_market=True)
        if px is None:
            order.status = "rejected"
            order.reject_reason = reason
            order.ts_update_ns = _now_ns()
            return order
        order.status = "accepted"
        self._fill(order, px, qty, _now_ns())
        return order

    def submit_limit(
        self, venue: str, symbol: str, side: OrderSide, qty: float, price: float
    ) -> BrokerOrder:
        self._require()
        order = self._new_order(venue, symbol, side, "limit", qty, float(price))
        px, reason = self._check_price(
            order.venue, order.symbol, side, float(price), is_market=False
        )
        if px is None:
            order.status = "rejected"
            order.reject_reason = reason
            order.ts_update_ns = _now_ns()
            return order
        order.limit_price = px
        order.status = "accepted"
        key = f"{order.venue}:{order.symbol}"
        mark = self._marks.get(key)
        if mark is not None:
            self._match_limits(order.venue, order.symbol, mark)
        return self._orders[order.client_order_id]

    def cancel(self, client_order_id: str) -> BrokerOrder:
        self._require()
        order = self._orders.get(client_order_id)
        if order is None:
            raise KeyError(f"unknown order {client_order_id}")
        if order.status in ("filled", "canceled", "rejected"):
            return order
        order.status = "canceled"
        order.ts_update_ns = _now_ns()
        return order

    def open_orders(self) -> list[BrokerOrder]:
        return [o for o in self._orders.values() if o.status in ("pending", "accepted", "partial")]

    def positions(self) -> dict[str, float]:
        return dict(self._positions)

    def cash(self) -> dict[str, float]:
        return dict(self._cash)

    def fills(self) -> list[BrokerFill]:
        return list(self._fills)

    # --- internals ---

    def _require(self) -> None:
        if not self._connected:
            raise RuntimeError("broker not connected; call connect()")

    def _new_order(
        self,
        venue: str,
        symbol: str,
        side: OrderSide,
        order_type: OrderType,
        qty: float,
        limit_price: float | None,
    ) -> BrokerOrder:
        if qty <= 0:
            raise ValueError("qty must be positive")
        oid = f"paper-{next(self._id_seq):08d}"
        now = _now_ns()
        order = BrokerOrder(
            client_order_id=oid,
            venue=venue.strip().upper(),
            symbol=symbol.strip().upper(),
            side=side,
            order_type=order_type,
            qty=float(qty),
            limit_price=limit_price,
            ts_submit_ns=now,
            ts_update_ns=now,
        )
        self._orders[oid] = order
        return order

    def _currency(self, venue: str) -> str:
        try:
            from coinext_data import instrument_spec

            return instrument_spec(venue).currency
        except Exception:
            return "USD"

    def _fee_rate(self, venue: str, symbol: str, side: OrderSide) -> float:
        try:
            from coinext_data import instrument_spec

            sp = instrument_spec(venue, symbol)
            return sp.taker_fee
        except Exception:
            return 0.0005

    def _check_price(
        self,
        venue: str,
        symbol: str,
        side: OrderSide,
        price: float,
        *,
        is_market: bool,
    ) -> tuple[float | None, str | None]:
        if not self.enforce_price_limits:
            return float(price), None
        key = f"{venue}:{symbol}"
        prev = self._prev_close.get(key)
        if prev is None:
            return float(price), None  # no band until prev_close known
        band = limit_band(venue, symbol, prev)
        if band is None:
            return float(price), None
        if band.allows(price, side=side):
            return float(price), None
        if is_market and self.clamp_to_limit:
            return band.clamp(price), None
        return None, (
            f"price {price} outside limit band [{band.down}, {band.up}] "
            f"(prev_close={prev}, pct={band.pct:.0%})"
        )

    def _match_limits(self, venue: str, symbol: str, mark: float) -> None:
        for order in list(self._orders.values()):
            if order.venue != venue or order.symbol != symbol:
                continue
            if order.status not in ("accepted", "partial") or order.limit_price is None:
                continue
            px = order.limit_price
            hit = (order.side == "buy" and mark <= px) or (order.side == "sell" and mark >= px)
            if hit:
                remain = order.qty - order.filled_qty
                if remain > 0:
                    self._fill(order, px, remain, _now_ns())

    def _fill(self, order: BrokerOrder, price: float, qty: float, ts_ns: int) -> None:
        if qty <= 0 or order.status in ("filled", "canceled", "rejected"):
            return
        ccy = self._currency(order.venue)
        fee_rate = self._fee_rate(order.venue, order.symbol, order.side)
        notional = abs(qty * price)
        fee = notional * fee_rate
        key = f"{order.venue}:{order.symbol}"
        pos = self._positions.get(key, 0.0)
        cash = self._cash.get(ccy, 0.0)
        day = self._session_day or trade_date_from_ns(ts_ns)

        if order.side == "buy":
            cost = notional + fee
            if cash < cost:
                order.status = "rejected"
                order.reject_reason = f"insufficient {ccy} cash ({cash:.2f} < {cost:.2f})"
                order.ts_update_ns = ts_ns
                return
            self._cash[ccy] = cash - cost
            self._positions[key] = pos + qty
            if self.enforce_t1 and is_t1_venue(order.venue):
                bk = (key, day.isoformat())
                self._bought_on[bk] = self._bought_on.get(bk, 0.0) + qty
        else:
            sellable = self.sellable_qty(order.venue, order.symbol, day=day)
            if sellable < qty - 1e-12:
                order.status = "rejected"
                if is_t1_venue(order.venue) and self.enforce_t1 and pos >= qty:
                    order.reject_reason = (
                        f"T+1: sellable {sellable:g} < {qty:g} "
                        f"(position {pos:g}, bought today locked)"
                    )
                else:
                    order.reject_reason = f"insufficient position ({pos} < {qty})"
                order.ts_update_ns = ts_ns
                return
            self._cash[ccy] = cash + notional - fee
            self._positions[key] = pos - qty

        prev_f = order.filled_qty
        order.filled_qty += qty
        if order.filled_qty > 0:
            order.avg_price = (
                (order.avg_price * prev_f + price * qty) / order.filled_qty if prev_f > 0 else price
            )
        order.status = "filled" if order.filled_qty >= order.qty - 1e-12 else "partial"
        order.ts_update_ns = ts_ns
        self._fills.append(
            BrokerFill(
                client_order_id=order.client_order_id,
                venue=order.venue,
                symbol=order.symbol,
                side=order.side,
                qty=qty,
                price=price,
                fee=fee,
                ts_event_ns=ts_ns,
                trade_id=f"f-{len(self._fills) + 1}",
            )
        )


__all__ = [
    "BrokerFill",
    "BrokerOrder",
    "EquityBroker",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "PaperEquityBroker",
]
