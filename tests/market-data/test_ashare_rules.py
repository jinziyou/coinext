"""Shared A-share rules (Python source of truth; Kernel OMS mirrors these %)."""

from __future__ import annotations

import datetime as dt

from coinext_data.ashare_rules import (
    LIMIT_PCT_CHINEXT_STAR,
    LIMIT_PCT_MAIN,
    LIMIT_PCT_ST,
    limit_band,
    price_limit_pct,
    resolve_prev_close,
)


def test_limit_pct_boards():
    assert price_limit_pct("SSE", "600519") == LIMIT_PCT_MAIN
    assert price_limit_pct("SZSE", "000001") == LIMIT_PCT_MAIN
    assert price_limit_pct("SZSE", "300750") == LIMIT_PCT_CHINEXT_STAR
    assert price_limit_pct("SSE", "688981") == LIMIT_PCT_CHINEXT_STAR
    assert price_limit_pct("SSE", "STXYZ") == LIMIT_PCT_ST
    assert price_limit_pct("NASDAQ", "AAPL") is None


def test_limit_band_tick_rounding():
    band = limit_band("SSE", "600519", 10.0)
    assert band is not None
    assert band.up == 11.0
    assert band.down == 9.0
    assert band.allows(11.0, side="buy")
    assert not band.allows(11.01, side="buy")
    # Non-round prev: 10.03 * 1.1 = 11.033 → 11.03
    b2 = limit_band("SSE", "600519", 10.03)
    assert b2 is not None
    assert b2.up == 11.03


def test_resolve_prev_close_skips_weekend_and_holiday():
    # Fri 2024-05-31 close 100; Mon 2024-06-03 should see prev=100 (skip Sat/Sun).
    closes = {
        dt.date(2024, 5, 31): 100.0,
        dt.date(2024, 6, 3): 105.0,
    }
    assert resolve_prev_close(closes, dt.date(2024, 6, 3), "SSE") == 100.0
    # After National Day week 2024-10-01..07, next session 2024-10-08 uses last pre-holiday.
    closes2 = {
        dt.date(2024, 9, 30): 50.0,  # Mon before holiday
        dt.date(2024, 10, 8): 52.0,
    }
    assert resolve_prev_close(closes2, dt.date(2024, 10, 8), "SSE") == 50.0
    assert resolve_prev_close({}, dt.date(2024, 6, 3), "SSE") is None
