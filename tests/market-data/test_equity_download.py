"""Equity / Yahoo downloader unit tests (no network — chart payload is mocked)."""

from __future__ import annotations

import datetime as dt

import pytest

pytest.importorskip("pyarrow", reason="the data lake needs pyarrow (`uv pip install pyarrow`)")

from coinext_data import DataLake  # noqa: E402
from coinext_data import download as dl  # noqa: E402
from coinext_data import equity_download as ed  # noqa: E402


def _chart_payload(n: int = 5, start: dt.datetime | None = None) -> dict:
    start = start or dt.datetime(2024, 1, 2, tzinfo=dt.UTC)
    base = int(start.timestamp())
    # Daily open timestamps.
    timestamps = [base + i * 86_400 for i in range(n)]
    opens = [100.0 + i for i in range(n)]
    highs = [o + 1 for o in opens]
    lows = [o - 1 for o in opens]
    closes = [o + 0.5 for o in opens]
    volumes = [1_000.0 * (i + 1) for i in range(n)]
    # One null bar in the middle is skipped by the parser.
    if n >= 3:
        closes[1] = None  # type: ignore[assignment]
    return {
        "chart": {
            "result": [
                {
                    "meta": {"currency": "USD", "symbol": "AAPL", "dataGranularity": "1d"},
                    "timestamp": timestamps,
                    "indicators": {
                        "quote": [
                            {
                                "open": opens,
                                "high": highs,
                                "low": lows,
                                "close": closes,
                                "volume": volumes,
                            }
                        ]
                    },
                }
            ],
            "error": None,
        }
    }


def test_bars_from_chart_skips_nulls_and_stamps_close():
    payload = _chart_payload(5)
    rows = ed._bars_from_chart(payload)
    assert len(rows) == 4  # one null close dropped
    ts0, o, h, lo, c, v = rows[0]
    assert o == 100.0 and h == 101.0 and lo == 99.0 and c == 100.5
    assert v == 1000.0
    # close stamp = open + 86400 - 1
    open_s = int(dt.datetime(2024, 1, 2, tzinfo=dt.UTC).timestamp())
    assert ts0 == (open_s + 86_400 - 1) * 1_000_000_000


def test_download_equity_bars_uses_yahoo_ticker(monkeypatch):
    seen: dict = {}

    def fake_fetch(ticker, *, interval, period1, period2, timeout):
        seen["ticker"] = ticker
        seen["interval"] = interval
        seen["period1"] = period1
        seen["period2"] = period2
        return _chart_payload(3)

    monkeypatch.setattr(ed, "_fetch_chart", fake_fetch)
    rows = ed.download_equity_bars(
        "700", "1d", venue="HKEX", days=10, end_s=1_700_000_000, apply_calendar=False
    )
    assert seen["ticker"] == "0700.HK"
    assert seen["interval"] == "1d"
    assert seen["period2"] == 1_700_000_000
    assert seen["period1"] == 1_700_000_000 - 10 * 86_400
    assert len(rows) == 2  # 3 bars, 1 null


def test_download_equity_to_lake(tmp_path, monkeypatch):
    monkeypatch.setattr(ed, "_fetch_chart", lambda *a, **k: _chart_payload(4))
    lake = DataLake(str(tmp_path))
    counts = ed.download_equity_to_lake(
        lake,
        ["AAPL", "MSFT"],
        interval="1d",
        venue="NASDAQ",
        days=30,
        pause=0.0,
        apply_calendar=False,
    )
    assert counts == {"AAPL": 3, "MSFT": 3}  # 4-1 null each
    assert lake.read_ohlcv("NASDAQ", "AAPL", "1d")
    assert ("NASDAQ", "AAPL", "1d") in lake.list_series()


def test_download_to_lake_routes_equity(tmp_path, monkeypatch):
    monkeypatch.setattr(ed, "_fetch_chart", lambda *a, **k: _chart_payload(3))
    lake = DataLake(str(tmp_path))
    counts = dl.download_to_lake(
        lake, ["600519"], interval="1d", days=60, venue="SSE", apply_calendar=False
    )
    assert "600519" in counts
    assert counts["600519"] == 2
    cov = lake.coverage("SSE", "600519", "1d")
    assert cov.n_rows == 2


def test_download_to_lake_market_group_listings(tmp_path, monkeypatch):
    """ASHARE multi-venue listings write correct SSE/SZSE partitions."""
    monkeypatch.setattr(ed, "_fetch_chart", lambda *a, **k: _chart_payload(3))
    lake = DataLake(str(tmp_path))
    listings = [("SSE", "600519"), ("SZSE", "000001"), ("SSE", "510300")]
    counts = dl.download_to_lake(
        lake, [], interval="1d", days=60, listings=listings, apply_calendar=False
    )
    assert counts == {
        "SSE/600519": 2,
        "SZSE/000001": 2,
        "SSE/510300": 2,
    }
    assert lake.read_ohlcv("SSE", "510300", "1d")
    assert lake.read_ohlcv("SZSE", "000001", "1d")


def test_download_to_lake_rejects_crypto_venue_without_source(tmp_path):
    # COINBASE is registered crypto with data_source=none.
    with pytest.raises(ValueError, match="no public history downloader"):
        dl.download_to_lake(DataLake(str(tmp_path)), ["BTC-USD"], venue="COINBASE")


def test_equity_interval_validation():
    with pytest.raises(ValueError, match="unsupported equity interval"):
        ed.equity_interval_to_yahoo("3m")
    assert ed.equity_interval_to_yahoo("1d") == "1d"
    assert ed.equity_interval_to_yahoo("1h") == "1h"
