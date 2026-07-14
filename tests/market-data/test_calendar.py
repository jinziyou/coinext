"""Trading calendar + bar hygiene (no network)."""

from __future__ import annotations

import datetime as dt

from coinext_data.calendar import (
    calendar_for,
    filter_trading_bars,
    is_trading_day,
    session_hours,
)


def test_ashare_weekends_and_holidays():
    cal = calendar_for("ASHARE")
    assert not cal.is_trading_day(dt.date(2024, 2, 10))  # Sat
    assert not cal.is_trading_day(dt.date(2024, 10, 1))  # National Day
    assert cal.is_trading_day(dt.date(2024, 10, 8))  # resumed after holiday week (Tue)


def test_us_rule_based_holidays():
    assert not is_trading_day("NYSE", "2024-07-04")
    assert not is_trading_day("NASDAQ", "2024-12-25")
    assert is_trading_day("NYSE", "2024-07-05")


def test_hk_calendar():
    assert not is_trading_day("港股", "2024-10-01")
    assert is_trading_day("HKEX", "2024-10-08")


def test_session_hours_ashare_has_lunch():
    sess = session_hours("SSE")
    assert sess is not None
    assert sess.open_hm == (9, 30)
    assert sess.lunch_break is not None


def _ns(y, m, d, h=7):
    return int(dt.datetime(y, m, d, h, 0, tzinfo=dt.UTC).timestamp() * 1_000_000_000)


def test_filter_drops_weekend_holiday_and_flat_halt():
    bars = [
        (_ns(2024, 10, 8), 10.0, 11.0, 9.0, 10.5, 1000.0),  # trading day
        (_ns(2024, 10, 5), 10.0, 10.0, 10.0, 10.0, 0.0),  # Sat + flat halt
        (_ns(2024, 10, 1), 10.0, 11.0, 9.0, 10.5, 1000.0),  # CN holiday
        (_ns(2024, 10, 9), 10.0, 10.0, 10.0, 10.0, 0.0),  # flat halt mid-week
        (_ns(2024, 10, 10), 10.0, 11.0, 9.5, 10.2, 500.0),  # ok
    ]
    out, stats = filter_trading_bars(bars, "SSE")
    assert len(out) == 2
    assert stats.dropped_weekend >= 1
    assert stats.dropped_holiday >= 1
    assert stats.dropped_halt >= 1
