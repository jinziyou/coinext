"""Paper equity broker (T+1 / 涨跌停) + IB fill loop with fake client."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from types import SimpleNamespace

import pytest
from coinext_broker import (
    IbConfig,
    IbPaperBroker,
    PaperEquityBroker,
    ib_contract_fields,
    is_t1_venue,
    price_limit_pct,
)


def test_paper_market_buy_and_sell():
    br = PaperEquityBroker(starting_cash={"USD": 50_000.0})
    br.connect()
    br.set_mark("NASDAQ", "AAPL", 100.0)
    o = br.submit_market("NASDAQ", "AAPL", "buy", 10)
    assert o.status == "filled"
    assert br.positions()["NASDAQ:AAPL"] == pytest.approx(10.0)
    assert br.cash()["USD"] < 50_000.0

    br.set_mark("NASDAQ", "AAPL", 110.0)
    o2 = br.submit_market("NASDAQ", "AAPL", "sell", 10)
    assert o2.status == "filled"
    assert br.positions().get("NASDAQ:AAPL", 0.0) == pytest.approx(0.0)
    assert br.cash()["USD"] > 49_000.0


def test_paper_limit_and_reject_no_mark():
    br = PaperEquityBroker(starting_cash={"USD": 10_000.0})
    br.connect()
    bad = br.submit_market("NYSE", "JPM", "buy", 1)
    assert bad.status == "rejected"

    br.set_mark("NYSE", "JPM", 150.0)
    lim = br.submit_limit("NYSE", "JPM", "buy", 5, 140.0)
    assert lim.status == "accepted"
    assert lim.filled_qty == 0.0
    br.set_mark("NYSE", "JPM", 139.0)
    assert br.positions().get("NYSE:JPM", 0.0) == pytest.approx(5.0)


def test_ashare_t1_blocks_same_day_sell():
    assert is_t1_venue("SSE")
    br = PaperEquityBroker(starting_cash={"CNY": 1_000_000.0}, enforce_t1=True)
    br.connect()
    br.set_session_day(dt.date(2024, 6, 3))
    br.set_mark("SSE", "600519", 1700.0)
    buy = br.submit_market("SSE", "600519", "buy", 100)
    assert buy.status == "filled"
    assert br.sellable_qty("SSE", "600519") == pytest.approx(0.0)
    sell = br.submit_market("SSE", "600519", "sell", 100)
    assert sell.status == "rejected"
    assert sell.reject_reason and "T+1" in sell.reject_reason
    # Next session: unlocked.
    br.set_session_day(dt.date(2024, 6, 4))
    assert br.sellable_qty("SSE", "600519") == pytest.approx(100.0)
    sell2 = br.submit_market("SSE", "600519", "sell", 100)
    assert sell2.status == "filled"


def test_ashare_price_limit_rejects_limit_above_band():
    assert price_limit_pct("SSE", "600519") == pytest.approx(0.10)
    assert price_limit_pct("SZSE", "300750") == pytest.approx(0.20)
    br = PaperEquityBroker(starting_cash={"CNY": 1_000_000.0}, enforce_price_limits=True)
    br.connect()
    br.set_prev_close("SSE", "600519", 100.0)
    br.set_mark("SSE", "600519", 100.0)
    # Limit buy above +10% → reject
    bad = br.submit_limit("SSE", "600519", "buy", 100, 111.0)
    assert bad.status == "rejected"
    assert bad.reject_reason and "limit band" in bad.reject_reason
    # At up-limit accepted; mark is 100 so buy limit 110 does not fill yet
    # (fill when mark walks down through 110 is N/A; use 99 for resting below market).
    ok = br.submit_limit("SSE", "600519", "buy", 100, 99.0)
    assert ok.status == "accepted"
    assert ok.filled_qty == 0.0


def test_market_order_clamps_to_limit():
    br = PaperEquityBroker(
        starting_cash={"CNY": 1_000_000.0},
        enforce_price_limits=True,
        clamp_to_limit=True,
    )
    br.connect()
    br.set_prev_close("SSE", "600519", 100.0)
    br.set_mark("SSE", "600519", 112.0)  # through limit
    o = br.submit_market("SSE", "600519", "buy", 100)
    assert o.status == "filled"
    assert o.avg_price == pytest.approx(110.0)  # clamped to up-limit


def test_ib_contract_mapping():
    assert ib_contract_fields("NASDAQ", "AAPL")["currency"] == "USD"
    assert ib_contract_fields("HKEX", "700")["symbol"] == "0700"
    assert ib_contract_fields("SSE", "600519")["exchange"] == "SEHKNTL"


# ---------------------------------------------------------------------------
# Fake IB client for fill-loop unit tests (no TWS / ib_insync required)
# ---------------------------------------------------------------------------


@dataclass
class _FakeOrder:
    orderId: int = 0
    orderRef: str = ""
    action: str = "BUY"
    totalQuantity: float = 0.0
    account: str = ""
    tif: str = "DAY"


@dataclass
class _FakeStatus:
    status: str = "Submitted"
    filled: float = 0.0
    avgFillPrice: float = 0.0
    whyHeld: str = ""


@dataclass
class _FakeTrade:
    order: _FakeOrder
    orderStatus: _FakeStatus = field(default_factory=_FakeStatus)
    contract: object | None = None


@dataclass
class _FakeExecution:
    shares: float
    price: float
    execId: str = "E1"


class FakeIB:
    """Minimal IB stand-in: market orders fill immediately; limits rest until fill_limit()."""

    def __init__(self) -> None:
        self.connected = False
        self.placed: list[tuple[object, _FakeOrder]] = []
        self.canceled: list[_FakeOrder] = []
        self.execDetailsEvent: list = []
        self.orderStatusEvent: list = []
        self._next_id = 1
        self._trades: dict[str, _FakeTrade] = {}

    def connect(self, host, port, clientId, timeout=15, readonly=False):  # noqa: ANN001
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def isConnected(self) -> bool:
        return self.connected

    def qualifyContracts(self, *contracts):  # noqa: ANN001
        return list(contracts)

    def placeOrder(self, contract, order):  # noqa: ANN001
        order.orderId = self._next_id
        self._next_id += 1
        self.placed.append((contract, order))
        trade = _FakeTrade(order=order, contract=contract)
        self._trades[order.orderRef] = trade
        # Market → instant fill
        if order.__class__.__name__ == "MarketOrder" or getattr(order, "orderType", "") == "MKT":
            self._fill(trade, order.totalQuantity, 100.0)
        else:
            trade.orderStatus.status = "Submitted"
            for h in self.orderStatusEvent:
                h(trade)
        return trade

    def cancelOrder(self, order):  # noqa: ANN001
        self.canceled.append(order)
        tr = self._trades.get(order.orderRef)
        if tr:
            tr.orderStatus.status = "Cancelled"
            for h in self.orderStatusEvent:
                h(tr)

    def sleep(self, secs):  # noqa: ANN001
        return None

    def accountValues(self):
        return [SimpleNamespace(tag="CashBalance", currency="USD", value="100000")]

    def positions(self):
        return []

    def openTrades(self):
        return [
            t
            for t in self._trades.values()
            if t.orderStatus.status in ("Submitted", "PreSubmitted", "PartiallyFilled")
        ]

    def reqMktData(self, contract, *a, **k):  # noqa: ANN001
        return SimpleNamespace(last=100.0, close=99.0)

    def cancelMktData(self, contract):  # noqa: ANN001
        return None

    def _fill(self, trade: _FakeTrade, qty: float, price: float) -> None:
        trade.orderStatus.status = "Filled"
        trade.orderStatus.filled = qty
        trade.orderStatus.avgFillPrice = price
        fill = SimpleNamespace(
            execution=_FakeExecution(shares=qty, price=price),
            commissionReport=SimpleNamespace(commission=1.0),
        )
        for h in self.execDetailsEvent:
            h(trade, fill)
        for h in self.orderStatusEvent:
            h(trade)

    def fill_limit(self, order_ref: str, price: float) -> None:
        tr = self._trades[order_ref]
        self._fill(tr, tr.order.totalQuantity, price)


# Monkeypatch Stock / MarketOrder / LimitOrder constructors used by ib_paper.
@pytest.fixture
def _patch_ib_insync(monkeypatch):
    import coinext_broker.ib_paper as ibmod

    class Stock:
        def __init__(self, symbol, exchange, currency, primaryExchange=""):
            self.symbol = symbol
            self.exchange = exchange
            self.currency = currency
            self.primaryExchange = primaryExchange

    class MarketOrder(_FakeOrder):
        def __init__(self, action, qty):
            super().__init__(action=action, totalQuantity=qty)
            self.orderType = "MKT"

    class LimitOrder(_FakeOrder):
        def __init__(self, action, qty, price):
            super().__init__(action=action, totalQuantity=qty)
            self.lmtPrice = price
            self.orderType = "LMT"

    monkeypatch.setattr(
        ibmod,
        "_make_contract",
        lambda fields: Stock(
            fields["symbol"], fields["exchange"], fields["currency"], fields["primaryExchange"]
        ),
    )

    def make_order(side, qty, *, order_type, limit_price, account, order_ref):
        if order_type == "market":
            o = MarketOrder("BUY" if side == "buy" else "SELL", qty)
        else:
            o = LimitOrder("BUY" if side == "buy" else "SELL", qty, limit_price)
        o.account = account
        o.orderRef = order_ref
        o.tif = "DAY"
        return o

    monkeypatch.setattr(ibmod, "_make_order", make_order)


def test_ib_mode_market_fill_loop(_patch_ib_insync):
    fake = FakeIB()
    br = IbPaperBroker(
        config=IbConfig(host="127.0.0.1", port=7497, fill_wait_s=0.0),
        mode="ib",
        ib_factory=lambda: fake,
    )
    br.connect()
    assert fake.connected
    o = br.submit_market("NASDAQ", "AAPL", "buy", 10)
    assert o.status == "filled"
    assert o.filled_qty == pytest.approx(10.0)
    assert o.avg_price == pytest.approx(100.0)
    assert len(br.fills()) == 1
    assert br.fills()[0].fee == pytest.approx(1.0)
    assert br.cash().get("USD") == pytest.approx(100_000.0)
    br.disconnect()
    assert not fake.connected


def test_ib_mode_cancel_limit(_patch_ib_insync):
    fake = FakeIB()
    br = IbPaperBroker(
        config=IbConfig(fill_wait_s=0.0),
        mode="ib",
        ib_factory=lambda: fake,
    )
    br.connect()
    o = br.submit_limit("NASDAQ", "AAPL", "buy", 5, 90.0)
    assert o.status == "accepted"
    c = br.cancel(o.client_order_id)
    assert c.status == "canceled"
    assert fake.canceled


def test_ib_paper_local_still_works():
    br = IbPaperBroker(mode="paper_local")
    br.connect()
    br.set_mark("NASDAQ", "AAPL", 50.0)
    o = br.submit_market("NASDAQ", "AAPL", "buy", 2)
    assert o.status == "filled"
