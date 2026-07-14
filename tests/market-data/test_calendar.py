"""Trading calendar + bar hygiene (no network)."""

from __future__ import annotations

import datetime as dt

from zoneinfo import ZoneInfo

from coinext_data.calendar import (
    calendar_for,
    filter_session_bars,
    filter_trading_bars,
    in_session,
    is_trading_day,
    previous_session_date,
    session_date,
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


def test_in_session_ashare_lunch():
    lunch_ts = int(dt.datetime(2024, 6, 3, 3, 45, tzinfo=dt.UTC).timestamp() * 1e9)
    assert not in_session(lunch_ts, "SSE")
    am_ts = int(dt.datetime(2024, 6, 3, 2, 0, tzinfo=dt.UTC).timestamp() * 1e9)
    assert in_session(am_ts, "SSE")
    pm_ts = int(dt.datetime(2024, 6, 3, 6, 0, tzinfo=dt.UTC).timestamp() * 1e9)
    assert in_session(pm_ts, "SSE")


def test_filter_session_bars_drops_lunch():
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


def test_session_date_ashare_uses_shanghai():
    # 2024-06-03 20:00 UTC = 2024-06-04 04:00 Shanghai → session date 2024-06-04
    ts = int(dt.datetime(2024, 6, 3, 20, 0, tzinfo=dt.UTC).timestamp() * 1e9)
    assert session_date(ts, "SSE") == dt.date(2024, 6, 4)
    ts2 = int(dt.datetime(2024, 6, 3, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp() * 1e9)
    assert session_date(ts2, "ASHARE") == dt.date(2024, 6, 3)
    assert previous_session_date("SSE", dt.date(2024, 6, 3)) == dt.date(2024, 5, 31)
