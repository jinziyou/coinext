"""FX book + multi-currency revaluation (no network)."""

from __future__ import annotations

from coinext_data.fx import (
    FxBook,
    convert_bars,
    mark_portfolio_value,
    revalue_bar_map,
    venue_currency,
    yahoo_fx_ticker,
)


def test_fallback_identity_and_inverse():
    book = FxBook.with_defaults()
    assert book.rate("USD", "USD") == 1.0
    r = book.rate("USD", "CNY")
    inv = book.rate("CNY", "USD")
    assert abs(r * inv - 1.0) < 1e-9


def test_triangulation_cny_hkd():
    book = FxBook.with_defaults()
    # Via USD when direct present too — should be positive.
    assert book.rate("CNY", "HKD") > 0
    assert book.rate("HKD", "CNY") > 0


def test_convert_bars_scales_ohlc():
    book = FxBook.with_defaults()
    book.set_rate("CNY", "USD", 0, 0.14)
    bars = [(1_000_000_000, 100.0, 110.0, 90.0, 105.0, 1000.0)]
    out = convert_bars(bars, book, quote="CNY", base="USD")
    assert len(out) == 1
    _ts, o, h, lo, c, v = out[0]
    assert abs(o - 14.0) < 1e-9
    assert abs(c - 14.7) < 1e-9
    assert v == 1000.0  # share count unchanged


def test_revalue_bar_map_multi_market():
    book = FxBook.with_defaults()
    bars = {
        "SSE:600519": [(1, 1700.0, 1710.0, 1690.0, 1705.0, 1e5)],
        "NASDAQ:AAPL": [(1, 190.0, 191.0, 189.0, 190.5, 1e6)],
    }
    venues = {"SSE:600519": "SSE", "NASDAQ:AAPL": "NASDAQ"}
    out = revalue_bar_map(bars, symbol_venues=venues, book=book, base="USD")
    # AAPL already USD — close unchanged.
    assert out["NASDAQ:AAPL"][0][4] == 190.5
    # A-share converted.
    assert out["SSE:600519"][0][4] != 1705.0
    assert out["SSE:600519"][0][4] < 1705.0  # CNY → USD shrinks number


def test_mark_portfolio_value():
    book = FxBook.with_defaults()
    val = mark_portfolio_value(
        {"SSE:600519": 100, "NASDAQ:AAPL": 10},
        {"SSE:600519": 1700.0, "NASDAQ:AAPL": 190.0},
        symbol_venues={"SSE:600519": "SSE", "NASDAQ:AAPL": "NASDAQ"},
        book=book,
        base="USD",
        cash=1000.0,
        cash_ccy="USD",
    )
    # 10*190 + 100*1700/USDCNY + 1000
    assert val > 1900.0


def test_venue_currency_and_yahoo_ticker():
    assert venue_currency("SSE") == "CNY"
    assert venue_currency("HKEX") == "HKD"
    assert venue_currency("NASDAQ") == "USD"
    assert yahoo_fx_ticker("USDCNY") == "USDCNY=X"


def test_download_fx_to_lake_and_reload(tmp_path, monkeypatch):
    import datetime as dt

    from coinext_data import DataLake, download_fx_to_lake
    from coinext_data import equity_download as ed
    from coinext_data.fx import FxBook

    def _payload(n=4):
        start = dt.datetime(2024, 1, 2, tzinfo=dt.UTC)
        base = int(start.timestamp())
        timestamps = [base + i * 86_400 for i in range(n)]
        closes = [7.2 + 0.01 * i for i in range(n)]
        return {
            "chart": {
                "result": [
                    {
                        "meta": {"currency": "CNY", "symbol": "USDCNY=X", "dataGranularity": "1d"},
                        "timestamp": timestamps,
                        "indicators": {
                            "quote": [
                                {
                                    "open": closes,
                                    "high": [c + 0.01 for c in closes],
                                    "low": [c - 0.01 for c in closes],
                                    "close": closes,
                                    "volume": [0.0] * n,
                                }
                            ]
                        },
                    }
                ],
                "error": None,
            }
        }

    monkeypatch.setattr(ed, "_fetch_chart", lambda *a, **k: _payload())
    lake = DataLake(str(tmp_path))
    counts = download_fx_to_lake(lake, ["USDCNY", "USDHKD"], days=30, pause=0.0)
    assert counts["USDCNY"] == 4
    assert ("FX", "USDCNY", "1d") in lake.list_series()

    book = FxBook.from_lake(lake, pairs=["USDCNY"])
    # mid of series ~7.2x
    r = book.rate("USD", "CNY", ts_ns=int(dt.datetime(2024, 1, 5, tzinfo=dt.UTC).timestamp() * 1e9))
    assert 7.0 < r < 7.5
