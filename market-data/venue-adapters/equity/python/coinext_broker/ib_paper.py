"""Interactive Brokers paper / live gateway via ``ib_insync`` (optional).

Modes
-----
* ``paper_local`` — offline :class:`PaperEquityBroker` (default, no TWS).
* ``ib`` — connect to TWS / IB Gateway and place real paper/live orders.

Environment::

    COINEXT__IB__HOST=127.0.0.1
    COINEXT__IB__PORT=7497          # 7497 TWS paper, 7496 TWS live, 4002 Gateway paper
    COINEXT__IB__CLIENT_ID=1
    COINEXT__IB__ACCOUNT=DUxxxx     # optional; first managed account if empty
    COINEXT__IB__READONLY=0

Install optional dep: ``uv pip install ib_insync`` (or ``coinext[ib]``).

Tests inject a fake client via ``ib_factory=`` so the fill loop is covered offline.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from .base import BrokerFill, BrokerOrder, EquityBroker, OrderSide, OrderStatus, PaperEquityBroker

log = logging.getLogger(__name__)


@dataclass(slots=True)
class IbConfig:
    """Connection settings for IB TWS / IB Gateway."""

    host: str = "127.0.0.1"
    port: int = 7497  # 7497 TWS paper, 4002 Gateway paper
    client_id: int = 1
    account: str = ""
    readonly: bool = False
    timeout_s: float = 15.0
    fill_wait_s: float = 3.0
    """How long submit_* waits for a terminal fill/cancel before returning working status."""

    @classmethod
    def from_env(cls) -> IbConfig:
        return cls(
            host=os.environ.get("COINEXT__IB__HOST", "127.0.0.1"),
            port=int(os.environ.get("COINEXT__IB__PORT", "7497")),
            client_id=int(os.environ.get("COINEXT__IB__CLIENT_ID", "1")),
            account=os.environ.get("COINEXT__IB__ACCOUNT", ""),
            readonly=os.environ.get("COINEXT__IB__READONLY", "").lower() in ("1", "true", "yes"),
            timeout_s=float(os.environ.get("COINEXT__IB__TIMEOUT", "15")),
            fill_wait_s=float(os.environ.get("COINEXT__IB__FILL_WAIT", "3")),
        )


# Coinext venue → IB exchange / currency / secType defaults.
_IB_VENUE: dict[str, dict[str, str]] = {
    "NYSE": {"exchange": "SMART", "primaryExchange": "NYSE", "currency": "USD", "secType": "STK"},
    "NASDAQ": {
        "exchange": "SMART",
        "primaryExchange": "NASDAQ",
        "currency": "USD",
        "secType": "STK",
    },
    "AMEX": {"exchange": "SMART", "primaryExchange": "AMEX", "currency": "USD", "secType": "STK"},
    "HKEX": {"exchange": "SEHK", "primaryExchange": "SEHK", "currency": "HKD", "secType": "STK"},
    "SSE": {
        "exchange": "SEHKNTL",
        "primaryExchange": "SEHKNTL",
        "currency": "CNH",
        "secType": "STK",
    },
    "SZSE": {
        "exchange": "SEHKSZSE",
        "primaryExchange": "SEHKSZSE",
        "currency": "CNH",
        "secType": "STK",
    },
}


def ib_contract_fields(venue: str, symbol: str) -> dict[str, str]:
    """Map Coinext ``(venue, symbol)`` to IB contract kwargs (symbol normalized)."""
    v = venue.strip().upper()
    s = symbol.strip().upper()
    meta = _IB_VENUE.get(v)
    if meta is None:
        raise KeyError(f"no IB mapping for venue {venue!r}; known: {sorted(_IB_VENUE)}")
    if v == "HKEX" and s.isdigit():
        s = s.zfill(4)
    if v in ("SSE", "SZSE") and s.isdigit():
        s = s.zfill(6)
    return {
        "symbol": s,
        "secType": meta["secType"],
        "exchange": meta["exchange"],
        "primaryExchange": meta["primaryExchange"],
        "currency": meta["currency"],
    }


def _now_ns() -> int:
    return int(time.time() * 1_000_000_000)


class IbClient(Protocol):
    """Minimal surface of ``ib_insync.IB`` used by :class:`IbPaperBroker`."""

    def connect(
        self, host: str, port: int, clientId: int, timeout: float = 15, readonly: bool = False
    ) -> None: ...
    def disconnect(self) -> None: ...
    def isConnected(self) -> bool: ...
    def qualifyContracts(self, *contracts: Any) -> list[Any]: ...
    def placeOrder(self, contract: Any, order: Any) -> Any: ...
    def cancelOrder(self, order: Any) -> None: ...
    def sleep(self, secs: float) -> None: ...
    def accountValues(self) -> list[Any]: ...
    def positions(self) -> list[Any]: ...  # type: ignore[override]
    def openTrades(self) -> list[Any]: ...
    def reqMktData(self, contract: Any, *a: Any, **k: Any) -> Any: ...
    def cancelMktData(self, contract: Any) -> None: ...


def default_ib_factory() -> IbClient:
    """Construct a real ``ib_insync.IB`` instance (import error if package missing)."""
    try:
        from ib_insync import IB
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "ib_insync is required for mode='ib'. Install: uv pip install ib_insync "
            "(or uv sync --extra ib)"
        ) from exc
    return IB()  # type: ignore[return-value]


def _make_contract(fields: dict[str, str]) -> Any:
    from ib_insync import Stock

    return Stock(
        fields["symbol"],
        fields["exchange"],
        fields["currency"],
        primaryExchange=fields["primaryExchange"],
    )


def _make_order(
    side: OrderSide,
    qty: float,
    *,
    order_type: str,
    limit_price: float | None,
    account: str,
    order_ref: str,
) -> Any:
    from ib_insync import LimitOrder, MarketOrder

    action = "BUY" if side == "buy" else "SELL"
    if order_type == "market":
        o = MarketOrder(action, qty)
    else:
        if limit_price is None:
            raise ValueError("limit order requires price")
        o = LimitOrder(action, qty, limit_price)
    if account:
        o.account = account
    o.orderRef = order_ref
    o.tif = "DAY"
    return o


def _map_ib_status(status: str) -> OrderStatus:
    s = (status or "").lower()
    if s in ("filled",):
        return "filled"
    if s in ("cancelled", "canceled", "apicancelled"):
        return "canceled"
    if s in ("inactive", "pendingcancel"):
        return "canceled" if "cancel" in s else "accepted"
    if s in ("partiallyfilled",):
        return "partial"
    if s in ("submitted", "presubmitted", "pendingsubmit", "apitending"):
        return "accepted"
    if s in ("rejected", "error"):
        return "rejected"
    return "accepted"


@dataclass
class IbPaperBroker:
    """IB paper/live broker with offline paper_local fallback.

    Parameters
    ----------
    mode:
        ``paper_local`` | ``ib``
    ib_factory:
        Callable returning an :class:`IbClient` (default: real ``ib_insync.IB``).
        Tests pass a fake that records placeOrder/cancel and fires fills.
    """

    config: IbConfig = field(default_factory=IbConfig.from_env)
    mode: str = "paper_local"
    ib_factory: Callable[[], IbClient] = field(default=default_ib_factory)
    _paper: PaperEquityBroker = field(default_factory=PaperEquityBroker)
    _ib: IbClient | None = field(default=None, repr=False)
    _connected: bool = False
    _orders: dict[str, BrokerOrder] = field(default_factory=dict)
    _fills: list[BrokerFill] = field(default_factory=list)
    _trade_by_ref: dict[str, Any] = field(default_factory=dict)
    _order_by_ib_id: dict[int, str] = field(default_factory=dict)

    def connect(self) -> None:
        if self.mode == "paper_local":
            self._paper.connect()
            self._connected = True
            return
        if self.mode != "ib":
            raise ValueError(f"unknown mode {self.mode!r} (expected paper_local|ib)")
        ib = self.ib_factory()
        ib.connect(
            self.config.host,
            self.config.port,
            clientId=self.config.client_id,
            timeout=self.config.timeout_s,
            readonly=self.config.readonly,
        )
        self._ib = ib
        self._wire_events(ib)
        self._connected = True
        log.info(
            "IB connected %s:%s clientId=%s",
            self.config.host,
            self.config.port,
            self.config.client_id,
        )

    def disconnect(self) -> None:
        if self.mode == "paper_local":
            self._paper.disconnect()
        if self._ib is not None:
            try:
                if self._ib.isConnected():
                    self._ib.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._ib = None
        self._connected = False

    def submit_market(
        self, venue: str, symbol: str, side: OrderSide, qty: float
    ) -> BrokerOrder:
        self._ensure()
        fields = ib_contract_fields(venue, symbol)
        if self.mode == "paper_local":
            return self._paper.submit_market(venue, symbol, side, qty)
        return self._submit_ib(venue, symbol, side, qty, "market", None, fields)

    def submit_limit(
        self, venue: str, symbol: str, side: OrderSide, qty: float, price: float
    ) -> BrokerOrder:
        self._ensure()
        fields = ib_contract_fields(venue, symbol)
        if self.mode == "paper_local":
            return self._paper.submit_limit(venue, symbol, side, qty, price)
        return self._submit_ib(venue, symbol, side, qty, "limit", float(price), fields)

    def cancel(self, client_order_id: str) -> BrokerOrder:
        self._ensure()
        if self.mode == "paper_local":
            return self._paper.cancel(client_order_id)
        order = self._orders.get(client_order_id)
        if order is None:
            raise KeyError(f"unknown order {client_order_id}")
        trade = self._trade_by_ref.get(client_order_id)
        if trade is not None and self._ib is not None:
            self._ib.cancelOrder(trade.order)
            self._ib.sleep(min(1.0, self.config.fill_wait_s))
            self._sync_trade(client_order_id, trade)
        return self._orders[client_order_id]

    def open_orders(self) -> list[BrokerOrder]:
        self._ensure()
        if self.mode == "paper_local":
            return self._paper.open_orders()
        if self._ib is not None:
            for trade in self._ib.openTrades():
                ref = getattr(trade.order, "orderRef", "") or ""
                if ref in self._orders:
                    self._sync_trade(ref, trade)
        return [o for o in self._orders.values() if o.status in ("pending", "accepted", "partial")]

    def positions(self) -> dict[str, float]:
        self._ensure()
        if self.mode == "paper_local":
            return self._paper.positions()
        out: dict[str, float] = {}
        if self._ib is None:
            return out
        for p in self._ib.positions():
            # Best-effort: use contract symbol; venue unknown → prefix IB
            sym = getattr(p.contract, "symbol", "?")
            exch = getattr(p.contract, "primaryExchange", "") or getattr(
                p.contract, "exchange", "IB"
            )
            key = f"{exch}:{sym}"
            out[key] = float(getattr(p, "position", 0.0))
        return out

    def cash(self) -> dict[str, float]:
        self._ensure()
        if self.mode == "paper_local":
            return self._paper.cash()
        out: dict[str, float] = {}
        if self._ib is None:
            return out
        for av in self._ib.accountValues():
            if getattr(av, "tag", "") == "CashBalance" and getattr(av, "currency", ""):
                try:
                    out[av.currency] = float(av.value)
                except (TypeError, ValueError):
                    pass
        return out

    def fills(self) -> list[BrokerFill]:
        if self.mode == "paper_local":
            return self._paper.fills()
        return list(self._fills)

    def set_mark(self, venue: str, symbol: str, price: float) -> None:
        """Local paper only — push a mark for immediate market fills."""
        if self.mode == "paper_local":
            self._paper.set_mark(venue, symbol, price)

    def req_mark(self, venue: str, symbol: str) -> float | None:
        """Request a last/mid snapshot from IB (mode=ib only)."""
        self._ensure()
        if self.mode != "ib" or self._ib is None:
            return None
        fields = ib_contract_fields(venue, symbol)
        contract = _make_contract(fields)
        self._ib.qualifyContracts(contract)
        ticker = self._ib.reqMktData(contract, "", False, False)
        self._ib.sleep(1.0)
        last = getattr(ticker, "last", None) or getattr(ticker, "close", None)
        try:
            self._ib.cancelMktData(contract)
        except Exception:  # noqa: BLE001
            pass
        try:
            return float(last) if last and last == last else None  # NaN check
        except (TypeError, ValueError):
            return None

    # --- IB internals ---

    def _ensure(self) -> None:
        if not self._connected:
            raise RuntimeError("broker not connected")

    def _wire_events(self, ib: IbClient) -> None:
        # ib_insync Event objects support += ; fakes may use a plain list of handlers.
        def _bind(name: str, handler: Callable[..., None]) -> None:
            ev = getattr(ib, name, None)
            if ev is None:
                return
            if isinstance(ev, list):
                ev.append(handler)
                return
            # ib_insync.util.Event
            try:
                ev += handler  # type: ignore[operator]
            except TypeError:
                if hasattr(ev, "connect"):
                    ev.connect(handler)  # type: ignore[operator]

        _bind("execDetailsEvent", self._on_exec_details)
        _bind("orderStatusEvent", self._on_order_status)

    def _on_exec_details(self, trade: Any, fill: Any) -> None:
        ref = getattr(getattr(trade, "order", None), "orderRef", "") or ""
        if not ref or ref not in self._orders:
            return
        order = self._orders[ref]
        exec_ = getattr(fill, "execution", fill)
        qty = float(getattr(exec_, "shares", 0) or getattr(exec_, "cumQty", 0) or 0)
        px = float(getattr(exec_, "price", 0) or 0)
        if qty <= 0:
            return
        commission = 0.0
        cr = getattr(fill, "commissionReport", None)
        if cr is not None:
            try:
                commission = abs(float(getattr(cr, "commission", 0) or 0))
            except (TypeError, ValueError):
                commission = 0.0
        side: OrderSide = order.side
        self._fills.append(
            BrokerFill(
                client_order_id=ref,
                venue=order.venue,
                symbol=order.symbol,
                side=side,
                qty=qty,
                price=px,
                fee=commission,
                ts_event_ns=_now_ns(),
                trade_id=str(getattr(exec_, "execId", "") or f"ib-{len(self._fills)+1}"),
            )
        )
        # Aggregate filled qty from trade if available.
        self._sync_trade(ref, trade)

    def _on_order_status(self, trade: Any) -> None:
        ref = getattr(getattr(trade, "order", None), "orderRef", "") or ""
        if ref in self._orders:
            self._sync_trade(ref, trade)

    def _sync_trade(self, ref: str, trade: Any) -> None:
        order = self._orders[ref]
        st = getattr(trade, "orderStatus", None)
        if st is not None:
            order.status = _map_ib_status(getattr(st, "status", "") or "")
            try:
                order.filled_qty = float(getattr(st, "filled", 0) or 0)
            except (TypeError, ValueError):
                pass
            try:
                avg = float(getattr(st, "avgFillPrice", 0) or 0)
                if avg > 0:
                    order.avg_price = avg
            except (TypeError, ValueError):
                pass
            why = getattr(st, "whyHeld", "") or ""
            if order.status == "rejected" and why:
                order.reject_reason = why
        order.ts_update_ns = _now_ns()
        ib_id = getattr(getattr(trade, "order", None), "orderId", None)
        if isinstance(ib_id, int):
            self._order_by_ib_id[ib_id] = ref

    def _submit_ib(
        self,
        venue: str,
        symbol: str,
        side: OrderSide,
        qty: float,
        order_type: str,
        limit_price: float | None,
        fields: dict[str, str],
    ) -> BrokerOrder:
        assert self._ib is not None
        if self.config.readonly:
            raise RuntimeError("IB config is readonly — cannot submit orders")
        ref = f"ib-{len(self._orders) + 1:08d}"
        now = _now_ns()
        order = BrokerOrder(
            client_order_id=ref,
            venue=venue.strip().upper(),
            symbol=symbol.strip().upper(),
            side=side,
            order_type=order_type,  # type: ignore[arg-type]
            qty=float(qty),
            limit_price=limit_price,
            status="pending",
            ts_submit_ns=now,
            ts_update_ns=now,
        )
        self._orders[ref] = order
        contract = _make_contract(fields)
        try:
            self._ib.qualifyContracts(contract)
        except Exception as exc:  # noqa: BLE001
            order.status = "rejected"
            order.reject_reason = f"qualifyContracts failed: {exc}"
            return order
        ib_order = _make_order(
            side,
            qty,
            order_type=order_type,
            limit_price=limit_price,
            account=self.config.account,
            order_ref=ref,
        )
        try:
            trade = self._ib.placeOrder(contract, ib_order)
        except Exception as exc:  # noqa: BLE001
            order.status = "rejected"
            order.reject_reason = f"placeOrder failed: {exc}"
            return order
        self._trade_by_ref[ref] = trade
        # Sync once — market paper fills often complete inside placeOrder callbacks.
        self._sync_trade(ref, trade)
        if order.status == "pending":
            order.status = "accepted"
        # Wait briefly for remaining paper fills.
        deadline = time.time() + self.config.fill_wait_s
        while time.time() < deadline and order.status not in ("filled", "canceled", "rejected"):
            self._ib.sleep(0.2)
            self._sync_trade(ref, trade)
        return order


def _protocol_check() -> None:
    b: EquityBroker = IbPaperBroker()  # type: ignore[assignment]
    _ = b


__all__ = ["IbConfig", "IbClient", "IbPaperBroker", "default_ib_factory", "ib_contract_fields"]
