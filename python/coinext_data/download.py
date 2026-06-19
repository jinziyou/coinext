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
) -> dict[str, int]:
    """Download the last ``days`` of ``interval`` bars for each symbol and write the lake.

    Returns ``{symbol: rows_written}``.
    """
    end = end_ms if end_ms is not None else _now_ms()
    start = end - int(days * 86_400_000)
    out: dict[str, int] = {}
    for symbol in symbols:
        rows = download_klines(symbol, interval, start_ms=start, end_ms=end, testnet=testnet)
        out[symbol] = lake.write_bars(venue, symbol, interval, rows)
    return out


__all__ = ["download_klines", "download_to_lake", "interval_to_ms"]
