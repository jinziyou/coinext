"""Venue catalog + Yahoo equity symbology (no network).

Covers A股 / ETF / 美股 / 港股 market groups, presets, and listing routing.
"""

from __future__ import annotations

import pytest
from coinext_data.venues import (
    ETF_UNIVERSES,
    MARKET_GROUPS,
    SAMPLE_EQUITY_SERIES,
    all_venues,
    default_universe,
    equity_venues,
    etf_universe,
    expand_venues,
    format_market_groups,
    format_venue_table,
    get_venue,
    infer_ashare_venue,
    is_equity_venue,
    lake_symbol,
    resolve_listing,
    resolve_listings,
    resolve_market_group,
    resolve_symbols,
    resolve_venue,
    yahoo_symbol,
)


def test_catalog_includes_mainstream_stock_markets():
    codes = {v.code for v in all_venues()}
    for expected in (
        "BINANCE",
        "NYSE",
        "NASDAQ",
        "AMEX",
        "HKEX",
        "SSE",
        "SZSE",
        "LSE",
        "TSE",
        "XETRA",
        "EURONEXT",
        "TSX",
        "ASX",
        "NSE",
        "KRX",
        "TWSE",
        "SGX",
        "B3",
        "SIX",
        "INDEX",
        "FX",
    ):
        assert expected in codes


def test_equity_venues_are_equity_family():
    eqs = equity_venues()
    assert len(eqs) >= 10
    assert all(v.asset_family == "equity" for v in eqs)
    assert all(v.data_source == "yahoo" for v in eqs)


def test_resolve_venue_aliases():
    assert resolve_venue("nyse").code == "NYSE"
    assert resolve_venue("XHKG").code == "HKEX"
    assert resolve_venue("HK").code == "HKEX"
    assert resolve_venue("港股").code == "HKEX"
    assert resolve_venue("sha").code == "SSE"
    assert resolve_venue("jpx").code == "TSE"
    assert resolve_venue("amex").code == "AMEX"
    with pytest.raises(KeyError):
        resolve_venue("NOT_A_REAL_EXCHANGE")
    # Market groups are not concrete venues.
    with pytest.raises(KeyError, match="market group"):
        resolve_venue("ASHARE")
    with pytest.raises(KeyError, match="market group"):
        resolve_venue("美股")


def test_yahoo_symbol_mapping():
    assert yahoo_symbol("NYSE", "AAPL") == "AAPL"
    assert yahoo_symbol("NASDAQ", "msft") == "MSFT"
    assert yahoo_symbol("HKEX", "700") == "0700.HK"
    assert yahoo_symbol("HKEX", "0700") == "0700.HK"
    assert yahoo_symbol("SSE", "600519") == "600519.SS"
    assert yahoo_symbol("SSE", "510300") == "510300.SS"  # A-share ETF
    assert yahoo_symbol("SZSE", "000001") == "000001.SZ"
    assert yahoo_symbol("SZSE", "159915") == "159915.SZ"  # ChiNext ETF
    assert yahoo_symbol("LSE", "VOD") == "VOD.L"
    assert yahoo_symbol("TSE", "7203") == "7203.T"
    assert yahoo_symbol("XETRA", "SAP") == "SAP.DE"
    assert yahoo_symbol("EURONEXT", "MC") == "MC.PA"
    # Full Yahoo ticker passthrough.
    assert yahoo_symbol("INDEX", "^GSPC") == "^GSPC"
    assert yahoo_symbol("HKEX", "0700.HK") == "0700.HK"
    # Market-group routing for Yahoo.
    assert yahoo_symbol("ASHARE", "600519") == "600519.SS"
    assert yahoo_symbol("A股", "000001") == "000001.SZ"
    assert yahoo_symbol("港股", "700") == "0700.HK"
    assert yahoo_symbol("US", "AAPL") == "AAPL"
    with pytest.raises(ValueError):
        yahoo_symbol("BINANCE", "BTCUSDT")


def test_lake_symbol_strips_suffix():
    assert lake_symbol("HKEX", "0700.HK") == "0700"
    assert lake_symbol("HKEX", "700") == "0700"
    assert lake_symbol("SSE", "600519.SS") == "600519"
    assert lake_symbol("SSE", "510300.SS") == "510300"
    assert lake_symbol("NYSE", "AAPL") == "AAPL"
    assert lake_symbol("INDEX", "^HSI") == "^HSI"


def test_is_equity_venue():
    assert is_equity_venue("NYSE")
    assert is_equity_venue("INDEX")
    assert is_equity_venue("ASHARE")
    assert is_equity_venue("A股")
    assert is_equity_venue("美股")
    assert is_equity_venue("港股")
    assert is_equity_venue("ETF")
    assert not is_equity_venue("BINANCE")
    assert not is_equity_venue("UNKNOWN")


def test_format_venue_table_contains_headers():
    text = format_venue_table(family="equity")
    assert "NYSE" in text
    assert "HKEX" in text
    assert "AMEX" in text
    assert "CODE" in text
    assert "BINANCE" not in text  # filtered to equity
    groups = format_market_groups()
    assert "ASHARE" in groups
    assert "@etf" in groups


def test_get_venue_none_for_unknown():
    assert get_venue("NOPE") is None
    assert get_venue("binance") is not None
    assert get_venue("ASHARE") is None  # market group, not a venue


def test_default_universe_and_resolve_symbols():
    assert "AAPL" in default_universe("NASDAQ")
    assert "^GSPC" in default_universe("INDEX")
    assert resolve_symbols("NASDAQ", "@default") == list(default_universe("NASDAQ"))
    assert resolve_symbols("HKEX", "700,0941") == ["0700", "0941"]
    assert resolve_symbols("BINANCE", "btcusdt,ethusdt") == ["BTCUSDT", "ETHUSDT"]
    with pytest.raises(ValueError):
        resolve_symbols("COINBASE", "@default")  # no universe registered


def test_etf_universes_and_at_etf_preset():
    assert "SPY" in etf_universe("NYSE")
    assert "QQQ" in etf_universe("NASDAQ")
    assert "510300" in etf_universe("SSE")
    assert "159915" in etf_universe("SZSE")
    assert "2800" in etf_universe("HKEX")
    assert resolve_symbols("NYSE", "@etf") == list(etf_universe("NYSE"))
    assert resolve_symbols("SSE", "@etf") == list(etf_universe("SSE"))
    # Cross-market ETF group concatenates member presets.
    cross = etf_universe("ETF")
    assert "SPY" in cross and "510300" in cross and "2800" in cross
    for venue, syms in ETF_UNIVERSES.items():
        assert get_venue(venue) is not None
        assert len(syms) >= 1


def test_market_groups_expand():
    assert resolve_market_group("A股") == "ASHARE"
    assert resolve_market_group("ashare") == "ASHARE"
    assert resolve_market_group("美股") == "US"
    assert resolve_market_group("港股") == "HK"
    assert resolve_market_group("etf") == "ETF"
    assert expand_venues("ASHARE") == ["SSE", "SZSE"]
    assert expand_venues("US") == list(MARKET_GROUPS["US"])
    assert expand_venues("HK") == ["HKEX"]
    assert expand_venues("NASDAQ") == ["NASDAQ"]


def test_infer_ashare_venue():
    assert infer_ashare_venue("600519") == "SSE"
    assert infer_ashare_venue("510300") == "SSE"
    assert infer_ashare_venue("000001") == "SZSE"
    assert infer_ashare_venue("300750") == "SZSE"
    assert infer_ashare_venue("159915") == "SZSE"
    assert infer_ashare_venue("600519.SS") == "SSE"
    with pytest.raises(ValueError):
        infer_ashare_venue("AAPL")


def test_resolve_listing_routing():
    assert resolve_listing("ASHARE", "600519") == ("SSE", "600519")
    assert resolve_listing("A股", "000001") == ("SZSE", "000001")
    assert resolve_listing("A股", "510300") == ("SSE", "510300")
    assert resolve_listing("港股", "700") == ("HKEX", "0700")
    assert resolve_listing("US", "AAPL") == ("NASDAQ", "AAPL")
    assert resolve_listing("US", "JPM") == ("NYSE", "JPM")
    assert resolve_listing("US", "SPY") == ("NYSE", "SPY")
    assert resolve_listing("ETF", "510300") == ("SSE", "510300")
    assert resolve_listing("ETF", "2800") == ("HKEX", "2800")
    assert resolve_listing("ETF", "SPY") == ("NYSE", "SPY")
    assert resolve_listing("NASDAQ", "msft") == ("NASDAQ", "MSFT")


def test_resolve_listings_multi_venue_presets():
    ashare = resolve_listings("ASHARE", "@default")
    venues = {v for v, _ in ashare}
    assert venues == {"SSE", "SZSE"}
    assert any(s == "600519" for v, s in ashare if v == "SSE")
    assert any(s == "000001" for v, s in ashare if v == "SZSE")

    us_etf = resolve_listings("US", "@etf")
    assert any(s == "SPY" for _, s in us_etf)
    assert any(s == "QQQ" for _, s in us_etf)

    explicit = resolve_listings("ASHARE", "600519,000001,510300")
    assert ("SSE", "600519") in explicit
    assert ("SZSE", "000001") in explicit
    assert ("SSE", "510300") in explicit


def test_sample_equity_series_subset_of_catalog():
    for venue, _sym in SAMPLE_EQUITY_SERIES:
        assert get_venue(venue) is not None
    # Focus markets represented in the sample set.
    sample_venues = {v for v, _ in SAMPLE_EQUITY_SERIES}
    for expected in ("NASDAQ", "NYSE", "HKEX", "SSE", "SZSE"):
        assert expected in sample_venues
    sample_pairs = set(SAMPLE_EQUITY_SERIES)
    assert ("NYSE", "SPY") in sample_pairs
    assert ("SSE", "510300") in sample_pairs
    assert ("HKEX", "2800") in sample_pairs
    assert ("FX", "USDCNY") in sample_pairs
    assert ("FX", "USDHKD") in sample_pairs
    assert yahoo_symbol("FX", "USDCNY") == "USDCNY=X"
    assert lake_symbol("FX", "USDCNY=X") == "USDCNY"


def test_parse_user_symbol_prefixes_and_suffixes():
    from coinext_data.venues import parse_user_symbol

    assert parse_user_symbol("sh600519") == ("SSE", "600519")
    assert parse_user_symbol("sz000001") == ("SZSE", "000001")
    assert parse_user_symbol("hk700") == ("HKEX", "0700")
    assert parse_user_symbol("600519.SS") == ("SSE", "600519")
    assert parse_user_symbol("000001.SZ") == ("SZSE", "000001")
    assert parse_user_symbol("0700.HK") == ("HKEX", "0700")
    assert parse_user_symbol("^GSPC") == ("INDEX", "^GSPC")
    assert parse_user_symbol("AAPL") == (None, "AAPL")


def test_prefix_listing_routing():
    assert resolve_listing("ASHARE", "sh600519") == ("SSE", "600519")
    assert resolve_listing("ASHARE", "sz000001") == ("SZSE", "000001")
    assert resolve_listing("港股", "hk700") == ("HKEX", "0700")
    # Prefix wins even when --venue is a sibling board.
    assert resolve_listing("SSE", "sz000001") == ("SZSE", "000001")
    assert yahoo_symbol("ASHARE", "sh510300") == "510300.SS"


def test_is_etf_symbol():
    from coinext_data.venues import is_etf_symbol

    assert is_etf_symbol("NYSE", "SPY")
    assert is_etf_symbol("NASDAQ", "QQQ")
    assert is_etf_symbol("SSE", "510300")
    assert is_etf_symbol("SZSE", "159915")
    assert is_etf_symbol("HKEX", "2800")
    assert is_etf_symbol("ASHARE", "510300")
    assert not is_etf_symbol("SSE", "600519")
    assert not is_etf_symbol("NASDAQ", "AAPL")


def test_instrument_spec_by_market():
    from coinext_data.venues import instrument_spec

    a = instrument_spec("SSE", "600519")
    assert a.currency == "CNY" and a.lot_size == 100 and a.size_precision == 0
    assert a.kind == "equity" and a.taker_fee > a.maker_fee

    etf = instrument_spec("SSE", "510300")
    assert etf.kind == "etf" and etf.currency == "CNY"

    us = instrument_spec("NASDAQ", "AAPL")
    assert us.currency == "USD" and us.lot_size == 1 and us.maker_fee == 0.0

    hk = instrument_spec("港股", "0700")
    assert hk.venue == "HKEX" and hk.currency == "HKD" and hk.price_precision == 3

    crypto = instrument_spec("BINANCE", "BTCUSDT")
    assert crypto.kind == "crypto" and crypto.size_precision == 3


def test_suggest_equity_download_defaults():
    from coinext_data.venues import suggest_equity_download_defaults

    iv, days, syms, notes = suggest_equity_download_defaults(
        "ASHARE", interval="1m", days=7.0, symbols="BTCUSDT"
    )
    assert iv == "1d" and days == 365.0 and syms == "@default"
    assert notes

    # Explicit non-default interval/days are preserved (except 1m→1d nudge).
    iv2, days2, syms2, _ = suggest_equity_download_defaults(
        "NASDAQ", interval="1h", days=30.0, symbols="AAPL,MSFT"
    )
    assert iv2 == "1h" and days2 == 30.0 and syms2 == "AAPL,MSFT"

    # Crypto venue unchanged.
    iv3, days3, syms3, n3 = suggest_equity_download_defaults(
        "BINANCE", interval="1m", days=7.0, symbols="BTCUSDT"
    )
    assert (iv3, days3, syms3) == ("1m", 7.0, "BTCUSDT") and n3 == []
