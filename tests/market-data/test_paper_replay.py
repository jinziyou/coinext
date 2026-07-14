"""Paper-equity bar replay + session-hour filter."""

from __future__ import annotations

import datetime as dt

import pytest
from coinext_broker import PaperEquityBroker, replay_bars
from coinext_data.calendar import filter_session_bars, in_session


def _daily_bars(n: int = 40, start: dt.date | None = None, base: float = 100.0):
    """Synthetic daily OHLCV on weekdays only (UTC noon)."""
    start = start or dt.date(2024, 3, 1)
    rows = []
    d = start
    i = 0
    while len(rows) < n:
        if d.weekday() < 5:
            # Trend then mean-revert to create a few SMA crosses.
            c = base + (i * 0.5 if i < n // 2 else (n - i) * 0.4)
            ts = int(dt.datetime(d.year, d.month, d.day, 7, 0, tzinfo=dt.UTC).timestamp() * 1e9)
            rows.append((ts, c - 0.5, c + 1.0, c - 1.0, c, 1_000_000.0))
            i += 1
        d += dt.timedelta(days=1)
    return rows


def test_replay_buyhold_ashare_t1():
    bars = _daily_bars(10, base=1700.0)
    br = PaperEquityBroker(starting_cash={"CNY": 2_000_000.0})
    res = replay_bars(
        bars,
        venue="SSE",
        symbol="600519",
        broker=br,
        strategy="buyhold",
        qty=100,
    )
    assert res.filled >= 1
    assert res.final_positions.get("SSE:600519", 0) == pytest.approx(100.0)
    # Same-day sell attempt would fail; buyhold doesn't sell.
    assert res.mark_to_market is not None
    assert res.mark_to_market > 0


def test_replay_sma_runs():
    bars = _daily_bars(50, base=50.0)
    res = replay_bars(bars, venue="NASDAQ", symbol="AAPL", strategy="sma", fast=3, slow=8, qty=10)
    assert res.bars == 50
    # May or may not trade depending on path; should not crash and cash stays finite.
    assert "USD" in res.final_cash


def test_in_session_ashare_lunch():
    # 2024-06-03 is a Monday; 11:45 Shanghai = 03:45 UTC
    lunch_ts = int(dt.datetime(2024, 6, 3, 3, 45, tzinfo=dt.UTC).timestamp() * 1e9)
    assert not in_session(lunch_ts, "SSE")
    # 10:00 Shanghai = 02:00 UTC
    am_ts = int(dt.datetime(2024, 6, 3, 2, 0, tzinfo=dt.UTC).timestamp() * 1e9)
    assert in_session(am_ts, "SSE")
    # 14:00 Shanghai = 06:00 UTC
    pm_ts = int(dt.datetime(2024, 6, 3, 6, 0, tzinfo=dt.UTC).timestamp() * 1e9)
    assert in_session(pm_ts, "SSE")


def test_filter_session_bars_drops_lunch():
    # Mix AM, lunch, PM stamps on a trading day.
    am = int(dt.datetime(2024, 6, 3, 2, 0, tzinfo=dt.UTC).timestamp() * 1e9)
    lunch = int(dt.datetime(2024, 6, 3, 3, 45, tzinfo=dt.UTC).timestamp() * 1e9)
    pm = int(dt.datetime(2024, 6, 3, 6, 0, tzinfo=dt.UTC).timestamp() * 1e9)
    bars = [
        (am, 10, 11, 9, 10.5, 100),
        (lunch, 10, 11, 9, 10.5, 100),
        (pm, 10, 11, 9, 10.5, 100),
    ]
    out, stats = filter_session_bars(bars, "SSE")
    assert len(out) == 2
    assert stats.output_rows == 2


def test_replay_portfolio_shared_broker():
    from coinext_broker import replay_portfolio

    a = _daily_bars(30, base=100.0)
    b = _daily_bars(30, start=dt.date(2024, 3, 1), base=50.0)
    # Offset B timestamps slightly so merge is deterministic.
    b = [(ts + 1, *rest) for ts, *rest in b]
    port = replay_portfolio(
        [("NASDAQ", "AAPL"), ("NYSE", "JPM")],
        {"NASDAQ:AAPL": a, "NYSE:JPM": b},
        starting_cash={"USD": 500_000.0},
        strategy="buyhold",
        qty=10,
    )
    assert len(port.results) == 2
    assert port.final_positions.get("NASDAQ:AAPL") == pytest.approx(10.0)
    assert port.final_positions.get("NYSE:JPM") == pytest.approx(10.0)
    assert port.final_cash["USD"] < 500_000.0
