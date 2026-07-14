"""End-to-end A-share T+1 via the Python backtest path (Kernel + OMS).

Requires the compiled ``coinext_py`` extension (``just py-build``). SSE/SZSE **Equity**
instruments deny same-UTC-day sells of newly bought shares; US equities and crypto do not.
"""

from __future__ import annotations

import pytest

pytest.importorskip("coinext_py", reason="build coinext_py: just py-build")

import coinext_backtest as bt  # noqa: E402
from coinext_strategy import Strategy  # noqa: E402

# Align with exec-engine day_key: UTC epoch day = ns // 1e9 // 86400.
# Use noon UTC on two consecutive days so bar spacing stays clean.
_DAY_S = 86_400
_NS = 1_000_000_000
# 2024-06-03 12:00:00 UTC and next day (both weekdays).
_DAY0 = 1_717_416_000  # approx 2024-06-03 12:00 UTC
_DAY1 = _DAY0 + _DAY_S


def _ns(day_unix_s: int, bar: int = 0) -> int:
    """Bar timestamps within one UTC day (1h apart)."""
    return (day_unix_s + bar * 3600) * _NS


def _ohlc(ts: int, px: float) -> tuple[int, float, float, float, float, float]:
    return (ts, px, px + 1.0, px - 1.0, px, 1_000_000.0)


class BuyThenSell(Strategy):
    """Buy once, then sell the full lot once long (retry only after T+1 deny)."""

    def __init__(self, qty: float = 100.0):
        self.qty = qty
        self.bought = False
        self.sell_pending = False  # sell submitted, waiting fill or deny
        self.flat = False
        self.denied: list[str] = []
        self.events: list[str] = []
        self.fills = 0
        self.sell_attempts = 0

    def on_bar(self, bar, ctx):
        if not self.bought:
            ctx.submit_market("buy", self.qty)
            self.bought = True
            return
        if self.flat or self.sell_pending:
            return
        pos = ctx.position()
        if pos >= self.qty - 1e-9:
            self.sell_attempts += 1
            self.sell_pending = True
            ctx.submit_market("sell", self.qty)

    def on_order_filled(self, fill, ctx):
        self.fills += 1
        if fill.side < 0:
            self.flat = True
            self.sell_pending = False

    def on_order_event(self, event, ctx):
        self.events.append(event.kind)
        if event.kind == "denied" and event.reason:
            self.denied.append(event.reason)
            # T+1 (or other) deny on sell — allow retry next bar.
            self.sell_pending = False


def _day0_bars(n: int, px: float = 100.0) -> list:
    """``n`` hourly bars on UTC day0 (enough for buy → fill → same-day sell)."""
    return [_ohlc(_ns(_DAY0, i), px) for i in range(n)]


def _day1_bars(n: int, px: float = 101.0) -> list:
    return [_ohlc(_ns(_DAY1, i), px) for i in range(n)]


def test_sse_equity_denies_same_day_sell_allows_next_day():
    """Buy fills day0; same-day sell denied (T+1); next-day sell fills.

    Kernel cadence (no look-ahead):
      bar0 day0: submit buy (pending_open)
      bar1 day0: buy fills at open (+1ms latency), then on_bar may still see 0
      between bar1 and bar2: fill applied → position long
      bar2 day0: sell attempt → **TPlusOne denied**
      bar0 day1: sell attempt → accepted
      bar1 day1: sell fills
    """
    bars = _day0_bars(4, 100.0) + _day1_bars(3, 101.0)
    strat = BuyThenSell(100.0)
    res = bt.run(
        strat,
        symbol="600519",
        venue="SSE",
        bars=bars,
        instrument=bt.Instrument.equity(),
        starting_balance=1_000_000.0,
        price_precision=2,
        size_precision=0,
        maker_fee=0.00025,
        taker_fee=0.00075,
    )

    assert res.orders_submitted >= 2
    assert res.orders_denied >= 1, (
        f"expected ≥1 T+1 deny, got denied={res.orders_denied} events={strat.events} "
        f"reasons={strat.denied} fills={res.fills}"
    )
    assert any("TPlusOne" in r for r in strat.denied), strat.denied
    # Buy + next-day sell = 2 fills (same-day sell never fills).
    assert res.fills == 2, f"fills={res.fills} sell_attempts={strat.sell_attempts}"
    assert strat.fills == 2


def test_szse_equity_t_plus_one_same_as_sse():
    bars = _day0_bars(4, 10.0) + _day1_bars(3, 10.5)
    strat = BuyThenSell(100.0)
    res = bt.run(
        strat,
        symbol="000001",
        venue="SZSE",
        bars=bars,
        instrument=bt.Instrument.equity(),
        starting_balance=500_000.0,
        size_precision=0,
    )
    assert res.orders_denied >= 1
    assert any("TPlusOne" in r for r in strat.denied)
    assert res.fills == 2


def test_nasdaq_equity_allows_same_day_round_trip():
    """US equity: buy and sell same UTC day both fill (no A-share T+1)."""
    bars = _day0_bars(5, 190.0)
    strat = BuyThenSell(10.0)
    res = bt.run(
        strat,
        symbol="AAPL",
        venue="NASDAQ",
        bars=bars,
        instrument=bt.Instrument.equity(),
        starting_balance=100_000.0,
        size_precision=0,
    )
    assert res.orders_denied == 0, strat.denied
    assert res.fills == 2
    assert not any("TPlusOne" in r for r in strat.denied)


def test_sse_spot_not_equity_skips_t_plus_one():
    """SSE venue with default spot instrument must NOT apply A-share T+1."""
    bars = _day0_bars(5, 100.0)
    strat = BuyThenSell(10.0)
    res = bt.run(
        strat,
        symbol="600519",
        venue="SSE",
        bars=bars,
        instrument=bt.Instrument.spot(),  # not equity
        starting_balance=100_000.0,
        size_precision=0,
    )
    assert res.orders_denied == 0, strat.denied
    assert res.fills == 2


def test_sse_equity_blocks_naked_short_as_t_plus_one():
    """Cash A-share: sell with zero long is denied (sellable=0), not a naked short."""

    class SellFirst(Strategy):
        def __init__(self):
            self.n = 0
            self.denied: list[str] = []

        def on_bar(self, bar, ctx):
            self.n += 1
            if self.n == 1:
                ctx.submit_market("sell", 100.0)

        def on_order_event(self, event, ctx):
            if event.kind == "denied" and event.reason:
                self.denied.append(event.reason)

    bars = _day0_bars(3, 100.0)
    s = SellFirst()
    res = bt.run(
        s,
        symbol="600519",
        venue="SSE",
        bars=bars,
        instrument=bt.Instrument.equity(),
        starting_balance=1_000_000.0,
        size_precision=0,
    )
    assert res.orders_denied == 1
    assert any("TPlusOne" in r for r in s.denied)
    assert res.fills == 0


def test_sse_equity_price_limit_denies_limit_above_band():
    """涨跌停: after a prior-day mark of 100, limit buy @ 111 is denied; @ 110 accepted."""

    class LimitProbe(Strategy):
        def __init__(self):
            self.n = 0
            self.denied: list[str] = []
            self.kinds: list[str] = []

        def on_bar(self, bar, ctx):
            self.n += 1
            # Day0: seed last_mark via a resting limit at the print (no fill needed).
            if self.n == 1:
                ctx.submit_limit("buy", 1.0, 100.0)
            # Day1 first bar: try through the +10% band.
            elif self.n == 3:
                ctx.submit_limit("buy", 100.0, 111.0)
            # Day1 later: at-limit is allowed.
            elif self.n == 4:
                ctx.submit_limit("buy", 100.0, 110.0)

        def on_order_event(self, event, ctx):
            self.kinds.append(event.kind)
            if event.kind == "denied" and event.reason:
                self.denied.append(event.reason)

    # 2 bars day0 (seed), 3 bars day1 (probe + at-limit).
    bars = _day0_bars(2, 100.0) + _day1_bars(3, 100.0)
    s = LimitProbe()
    res = bt.run(
        s,
        symbol="600519",
        venue="SSE",
        bars=bars,
        instrument=bt.Instrument.equity(),
        starting_balance=1_000_000.0,
        size_precision=0,
    )
    assert res.orders_denied >= 1, s.denied
    assert any("PriceLimit" in r for r in s.denied), s.denied
    # Seed + at-limit should submit; through-band denied (not counted as fill).
    assert "submitted" in s.kinds
