"""Trade-tick ingestion helpers for the local OHLCV data lake.

The public Binance ``aggTrades`` endpoint and the live ingestor both produce trade ticks. This module
turns those normalized ticks into interval OHLCV bars and writes them through the existing
:class:`coinext_data.lake.DataLake`, so research/backtest warm-up reads the same lake format.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence

from .download import interval_to_ms
from .lake import BarRow, DataLake

_NS_PER_MS = 1_000_000

TradeTick = Sequence[int | float]
TradeFetcher = Callable[[str, int], list[tuple[int, float, float, int]]]


def _bucket_close_ns(ts_ns: int, interval_ms: int) -> int:
    ts_ms = int(ts_ns) // _NS_PER_MS
    bucket_start_ms = (ts_ms // interval_ms) * interval_ms
    close_ms = bucket_start_ms + interval_ms - 1
    return close_ms * _NS_PER_MS


def _coerce_trade(tick: TradeTick) -> tuple[int, float, float]:
    try:
        ts_ns = int(tick[0])
        price = float(tick[1])
        size = float(tick[2])
    except (IndexError, TypeError, ValueError) as exc:
        raise ValueError(f"trade tick must be at least (ts_ns, price, size): {tick!r}") from exc
    if ts_ns < 0:
        raise ValueError(f"trade timestamp must be non-negative: {tick!r}")
    if price <= 0.0:
        raise ValueError(f"trade price must be positive: {tick!r}")
    if size < 0.0:
        raise ValueError(f"trade size must be non-negative: {tick!r}")
    return ts_ns, price, size


def trade_ticks_to_ohlcv(trades: Iterable[TradeTick], interval: str = "1m") -> list[BarRow]:
    """Aggregate normalized trade ticks into OHLCV bars.

    ``trades`` may be Binance aggTrade rows from :func:`fetch_binance_agg_trades` or equivalent live
    normalized trade rows. The first trade in a bucket is the open, the last is the close, high/low
    are extrema, volume is summed size, and ``ts_event`` is the interval close timestamp using the
    same close-time convention as Binance klines.
    """

    interval_ms = interval_to_ms(interval)
    buckets: dict[int, list[tuple[int, float, float]]] = {}
    for tick in trades:
        ts_ns, price, size = _coerce_trade(tick)
        buckets.setdefault(_bucket_close_ns(ts_ns, interval_ms), []).append((ts_ns, price, size))

    out: list[BarRow] = []
    for close_ns in sorted(buckets):
        rows = sorted(buckets[close_ns], key=lambda r: r[0])
        prices = [r[1] for r in rows]
        volume = sum(r[2] for r in rows)
        out.append((close_ns, prices[0], max(prices), min(prices), prices[-1], volume))
    return out


def _symbol_and_venue(symbol: str, venue: str) -> tuple[str, str]:
    raw = symbol.strip().upper()
    if "." in raw:
        base_symbol, parsed_venue = raw.split(".", 1)
        return base_symbol, parsed_venue or venue.upper()
    return raw, venue.upper()


def ingest_agg_trades_to_lake(
    symbol: str = "BTCUSDT",
    *,
    interval: str = "1m",
    venue: str = "BINANCE",
    limit: int = 1000,
    lake: DataLake | None = None,
    timeout: float = 15.0,
    fetcher: Callable[..., list[tuple[int, float, float, int]]] | None = None,
) -> dict[str, int]:
    """Fetch public aggregate trades, aggregate them into OHLCV bars, and write the local lake.

    Returns counts for operator output: raw trades fetched, bars aggregated, and distinct rows stored
    across affected lake partitions. ``fetcher`` is injectable so tests and offline smoke paths never
    need live network access.
    """

    lake_symbol, lake_venue = _symbol_and_venue(symbol, venue)
    if fetcher is None:
        from . import fetch_binance_agg_trades

        fetcher = fetch_binance_agg_trades
    trades = fetcher(lake_symbol, limit=int(limit), timeout=timeout)
    bars = trade_ticks_to_ohlcv(trades, interval=interval)
    target = lake or DataLake()
    stored_rows = target.write_bars(lake_venue, lake_symbol, interval, bars)
    return {"trades": len(trades), "bars": len(bars), "stored_rows": stored_rows}


__all__ = ["ingest_agg_trades_to_lake", "trade_ticks_to_ohlcv"]
