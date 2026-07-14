"""coinext_data.download — paginated Binance kline downloader (public REST, no API key).

Binance caps ``/api/v3/klines`` at 1000 bars per request; this pages by advancing ``startTime`` past
the last open time until the requested range is covered, so you can pull months of history rather
than the single 500/1000-bar window. Output rows are full OHLCV stamped with the bar **close** time
in nanoseconds — exactly what :mod:`coinext_data.lake` stores.
"""

from __future__ import annotations

import json
import time
import urllib.request

from .lake import BarRow, DataLake

_NS_PER_MS = 1_000_000

# Binance interval string -> milliseconds.
_INTERVAL_MS: dict[str, int] = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
}


def interval_to_ms(interval: str) -> int:
    try:
        return _INTERVAL_MS[interval]
    except KeyError as exc:
        raise ValueError(
            f"unsupported interval {interval!r}; known: {sorted(_INTERVAL_MS)}"
        ) from exc


def _now_ms() -> int:
    return int(time.time() * 1000)


def _fetch_page(
    base: str, symbol: str, interval: str, start_ms: int, end_ms: int, timeout: float
) -> list[list]:
    url = (
        f"{base}/api/v3/klines?symbol={symbol}&interval={interval}"
        f"&startTime={start_ms}&endTime={end_ms}&limit=1000"
    )
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 (trusted host)
        return json.loads(resp.read().decode("utf-8"))


def download_klines(
    symbol: str,
    interval: str = "1m",
    *,
    start_ms: int,
    end_ms: int | None = None,
    testnet: bool = False,
    pause: float = 0.05,
    timeout: float = 20.0,
    max_requests: int = 100_000,
) -> list[BarRow]:
    """Page through ``/api/v3/klines`` over ``[start_ms, end_ms]`` and return deduped OHLCV rows
    (``(ts_event_ns, open, high, low, close, volume)``, ts = bar close time)."""
    base = "https://testnet.binance.vision" if testnet else "https://api.binance.com"
    step = interval_to_ms(interval)
    end = end_ms if end_ms is not None else _now_ms()
    cursor = int(start_ms)
    by_ts: dict[int, BarRow] = {}
    requests = 0

    while cursor <= end and requests < max_requests:
        page = _fetch_page(base, symbol, interval, cursor, end, timeout)
        requests += 1
        if not page:
            break
        for k in page:
            ts_ns = int(k[6]) * _NS_PER_MS  # close time
            by_ts[ts_ns] = (
                ts_ns,
                float(k[1]),
                float(k[2]),
                float(k[3]),
                float(k[4]),
                float(k[5]),
            )
        last_open = int(page[-1][0])
        next_cursor = last_open + step
        # Stop when the venue returned a short (final) page or we can't advance.
        if len(page) < 1000 or next_cursor <= cursor:
            break
        cursor = next_cursor
        if pause:
            time.sleep(pause)

    return [by_ts[t] for t in sorted(by_ts)]


def download_to_lake(
    lake: DataLake,
    symbols: list[str],
    interval: str = "1m",
    *,
    days: float = 7.0,
    end_ms: int | None = None,
    testnet: bool = False,
    venue: str = "BINANCE",
    listings: list[tuple[str, str]] | None = None,
    apply_calendar: bool = True,
) -> dict[str, int]:
    """Download the last ``days`` of ``interval`` bars for each symbol and write the lake.

    Routes by venue catalog:

    * **crypto / Binance** (default): public Binance klines REST.
    * **equity / index**: Yahoo Finance chart API (see :mod:`coinext_data.equity_download`).
    * **market groups** (``ASHARE`` / ``US`` / ``HK`` / ``ETF``): pass ``listings`` from
      :func:`coinext_data.venues.resolve_listings` so each symbol lands on the right partition.

    Returns ``{symbol: rows_written}`` for single-venue calls, or
    ``{"VENUE/symbol": rows_written}`` when multiple venues are written (market groups).
    """
    from .venues import get_venue, is_equity_venue, resolve_market_group

    # Multi-venue path (A股 / 美股 / ETF group downloads).
    if listings is not None:
        from .equity_download import download_equity_to_lake

        end_s = None if end_ms is None else int(end_ms // 1000)
        # Group symbols by venue to reuse pause / write logic.
        by_venue: dict[str, list[str]] = {}
        for vcode, sym in listings:
            by_venue.setdefault(vcode, []).append(sym)
        out: dict[str, int] = {}
        multi = len(by_venue) > 1
        for vcode, syms in by_venue.items():
            counts = download_equity_to_lake(
                lake,
                syms,
                interval=interval,
                venue=vcode,
                days=days,
                end_s=end_s,
                apply_calendar=apply_calendar,
            )
            for sym, n in counts.items():
                key = f"{vcode}/{sym}" if multi else sym
                out[key] = n
        return out

    venue_code = venue.strip().upper() or "BINANCE"
    # Market-group strings without listings: fail with a clear message.
    if resolve_market_group(venue) is not None and get_venue(venue) is None:
        raise ValueError(
            f"venue {venue!r} is a market group — pass listings=resolve_listings(...) "
            "or a concrete venue (SSE, SZSE, NASDAQ, NYSE, HKEX, …)"
        )

    info = get_venue(venue_code)

    if is_equity_venue(venue_code) and get_venue(venue_code) is not None:
        from .equity_download import download_equity_to_lake

        end_s = None if end_ms is None else int(end_ms // 1000)
        return download_equity_to_lake(
            lake,
            symbols,
            interval=interval,
            venue=venue_code,
            days=days,
            end_s=end_s,
            apply_calendar=apply_calendar,
        )

    if info is not None and info.data_source not in ("binance", "none"):
        raise ValueError(
            f"venue {venue_code} has data_source={info.data_source!r}; no downloader wired"
        )
    if info is not None and info.data_source == "none" and info.asset_family == "crypto":
        raise ValueError(
            f"venue {venue_code} is registered but has no public history downloader yet "
            f"(only BINANCE klines + equity Yahoo venues are live)"
        )
    # Unknown venue codes still go to Binance for backward compatibility (custom labels).
    if info is None and venue_code not in ("BINANCE",):
        # Allow free-form crypto-style venue labels while still using Binance REST as the source
        # of bars (caller owns the partition key). Documented as advanced use.
        pass

    end = end_ms if end_ms is not None else _now_ms()
    start = end - int(days * 86_400_000)
    out = {}
    for symbol in symbols:
        rows = download_klines(symbol, interval, start_ms=start, end_ms=end, testnet=testnet)
        out[symbol] = lake.write_bars(venue_code, symbol, interval, rows)
    return out


__all__ = ["download_klines", "download_to_lake", "interval_to_ms"]
