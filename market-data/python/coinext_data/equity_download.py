"""Yahoo equity/index OHLCV download into the local lake.

Status: verified. See docs/EQUITY_RESEARCH.md.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .calendar import filter_trading_bars
from .lake import BarRow, DataLake
from .venues import is_equity_venue, lake_symbol, resolve_venue, yahoo_symbol

_NS_PER_S = 1_000_000_000

# Coinext interval string -> Yahoo chart interval.
_YAHOO_INTERVAL: dict[str, str] = {
    "1m": "1m",
    "2m": "2m",
    "5m": "5m",
    "15m": "15m",
    "30m": "30m",
    "60m": "60m",
    "90m": "90m",
    "1h": "1h",
    "1d": "1d",
    "5d": "5d",
    "1wk": "1wk",
    "1mo": "1mo",
    "3mo": "3mo",
}

_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
# Yahoo rejects bare Python-urllib user agents on some edges; a browser-like UA is conventional.
_UA = "Mozilla/5.0 (compatible; CoinextResearch/0.1; +https://github.com/jinziyou/coinext)"


def equity_interval_to_yahoo(interval: str) -> str:
    try:
        return _YAHOO_INTERVAL[interval]
    except KeyError as exc:
        raise ValueError(
            f"unsupported equity interval {interval!r}; known: {sorted(_YAHOO_INTERVAL)}"
        ) from exc


def _now_s() -> int:
    return int(time.time())


def _fetch_chart(
    ticker: str,
    *,
    interval: str,
    period1: int,
    period2: int,
    timeout: float,
) -> dict[str, Any]:
    """GET Yahoo chart JSON for ``ticker`` over ``[period1, period2]`` (unix seconds)."""
    y_interval = equity_interval_to_yahoo(interval)
    # includePrePost=false keeps regular session bars for equities.
    query = (
        f"?period1={int(period1)}&period2={int(period2)}"
        f"&interval={y_interval}&includePrePost=false&events=div%2Csplit"
    )
    url = _CHART_URL.format(ticker=urllib.parse.quote(ticker, safe="^.")) + query
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (public host)
        return json.loads(resp.read().decode("utf-8"))


def _bars_from_chart(payload: dict[str, Any], *, adjust: bool = False) -> list[BarRow]:
    """Parse Yahoo chart payload into ``(ts_close_ns, o, h, l, c, v)`` rows.

    When ``adjust=True`` and Yahoo provides ``adjclose``, scale OHLC by ``adjclose/close``
    (forward-looking split/dividend adjustment commonly called 前复权 for research).
    """
    try:
        result = payload["chart"]["result"]
    except (KeyError, TypeError) as exc:
        raise ValueError(f"unexpected Yahoo chart payload shape: {payload!r}") from exc
    if not result:
        err = (payload.get("chart") or {}).get("error")
        raise ValueError(f"Yahoo returned no result: {err!r}")
    block = result[0]
    timestamps = block.get("timestamp") or []
    quote = ((block.get("indicators") or {}).get("quote") or [{}])[0]
    opens = quote.get("open") or []
    highs = quote.get("high") or []
    lows = quote.get("low") or []
    closes = quote.get("close") or []
    volumes = quote.get("volume") or []
    adj_block = ((block.get("indicators") or {}).get("adjclose") or [{}])[0]
    adj_closes = adj_block.get("adjclose") or []

    meta = block.get("meta") or {}
    # tradingPeriod / data granularity: use interval seconds when present for close stamp.
    # Yahoo timestamps are bar *open* time (unix s). Prefer close ≈ open + interval for alignment
    # with the Binance downloader (which stamps close time).
    gmtoffset = int(meta.get("gmtoffset") or 0)
    raw_interval = meta.get("dataGranularity") or meta.get("interval") or "1d"
    step_s = _interval_seconds(raw_interval)

    rows: list[BarRow] = []
    n = len(timestamps)
    for i in range(n):
        o, h, lo, c = (
            opens[i] if i < len(opens) else None,
            highs[i] if i < len(highs) else None,
            lows[i] if i < len(lows) else None,
            closes[i] if i < len(closes) else None,
        )
        if c is None or o is None or h is None or lo is None:
            continue  # skip unformed / halted bars with null OHLC
        vol = volumes[i] if i < len(volumes) and volumes[i] is not None else 0.0
        fo, fh, flo, fc = float(o), float(h), float(lo), float(c)
        if adjust and i < len(adj_closes) and adj_closes[i] is not None and fc != 0.0:
            factor = float(adj_closes[i]) / fc
            fo, fh, flo, fc = fo * factor, fh * factor, flo * factor, fc * factor
        open_s = int(timestamps[i])
        # Stamp at bar close (open + step - 1s), consistent with Binance closeTime semantics.
        close_s = open_s + max(step_s - 1, 0)
        ts_ns = close_s * _NS_PER_S
        # gmtoffset is informational only; timestamps are already UTC unix.
        _ = gmtoffset
        rows.append((ts_ns, fo, fh, flo, fc, float(vol)))
    return rows


def _interval_seconds(interval: str) -> int:
    """Best-effort bar length in seconds from a Yahoo interval string."""
    mapping = {
        "1m": 60,
        "2m": 120,
        "5m": 300,
        "15m": 900,
        "30m": 1800,
        "60m": 3600,
        "90m": 5400,
        "1h": 3600,
        "1d": 86_400,
        "5d": 5 * 86_400,
        "1wk": 7 * 86_400,
        "1mo": 30 * 86_400,
        "3mo": 90 * 86_400,
    }
    return mapping.get(interval, 86_400)


def download_equity_bars(
    symbol: str,
    interval: str = "1d",
    *,
    venue: str = "NYSE",
    start_s: int | None = None,
    end_s: int | None = None,
    days: float | None = None,
    timeout: float = 30.0,
    ticker: str | None = None,
    apply_calendar: bool = True,
    drop_flat_halts: bool = True,
    adjust: bool = False,
) -> list[BarRow]:
    """Download OHLCV for one equity/index symbol via Yahoo Finance.

    Parameters
    ----------
    symbol:
        Lake-facing symbol (e.g. ``AAPL``, ``0700``, ``600519``) or a full Yahoo ticker.
    venue:
        Catalog venue code (``NYSE``, ``HKEX``, …). Used for Yahoo suffix mapping.
    days:
        If set (and ``start_s`` is not), download the last ``days`` calendar days.
    ticker:
        Optional override of the Yahoo ticker (skips :func:`yahoo_symbol`).
    apply_calendar:
        When True (default) drop weekend / holiday sessions via :mod:`coinext_data.calendar`
        for daily (and coarser) bars. Intraday is left unfiltered (session hours not sliced).
    drop_flat_halts:
        Drop zero-volume flat OHLC prints (likely halted / untraded days).
    adjust:
        When True, scale OHLC by Yahoo ``adjclose/close`` (split/dividend adjusted / 前复权).
    """
    if not is_equity_venue(venue):
        info = resolve_venue(venue)
        raise ValueError(
            f"venue {info.code} asset_family={info.asset_family!r} is not equity/index; "
            "use the Binance downloader for crypto"
        )
    y_ticker = ticker or yahoo_symbol(venue, symbol)
    end = int(end_s if end_s is not None else _now_s())
    if start_s is not None:
        start = int(start_s)
    elif days is not None:
        start = end - int(float(days) * 86_400)
    else:
        start = end - 365 * 86_400  # default 1y of daily history
    if start >= end:
        raise ValueError(f"empty range: start_s={start} end_s={end}")

    try:
        payload = _fetch_chart(
            y_ticker, interval=interval, period1=start, period2=end, timeout=timeout
        )
    except urllib.error.HTTPError as exc:
        raise RuntimeError(
            f"Yahoo chart HTTP {exc.code} for {y_ticker!r} ({venue}/{symbol}): {exc.reason}"
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Yahoo chart network error for {y_ticker!r}: {exc.reason}") from exc

    rows = _bars_from_chart(payload, adjust=adjust)
    if apply_calendar:
        if interval in ("1d", "5d", "1wk", "1mo", "3mo"):
            rows, _stats = filter_trading_bars(
                rows,
                venue,
                drop_flat_halts=drop_flat_halts,
                drop_zero_volume=False,
            )
        elif interval in ("1m", "2m", "5m", "15m", "30m", "60m", "90m", "1h"):
            # Intraday: drop out-of-session + lunch break stamps.
            from .calendar import filter_session_bars

            rows, _stats = filter_session_bars(rows, venue)
            if drop_flat_halts:
                rows, _ = filter_trading_bars(
                    rows,
                    venue,
                    drop_weekends=False,
                    drop_holidays=False,
                    drop_flat_halts=True,
                    drop_zero_volume=False,
                )
    return rows


def download_equity_to_lake(
    lake: DataLake,
    symbols: list[str],
    interval: str = "1d",
    *,
    venue: str = "NYSE",
    days: float = 365.0,
    end_s: int | None = None,
    pause: float = 0.15,
    timeout: float = 30.0,
    apply_calendar: bool = True,
    adjust: bool = False,
) -> dict[str, int]:
    """Download equity/index history for each symbol and write the lake.

    Returns ``{lake_symbol: rows_written}``. Symbols are normalized via :func:`lake_symbol`.
    ``adjust=True`` stores split/dividend-adjusted OHLC (前复权).
    """
    resolve_venue(venue)  # fail fast on unknown venue
    end = end_s if end_s is not None else _now_s()
    out: dict[str, int] = {}
    for i, sym in enumerate(symbols):
        lake_sym = lake_symbol(venue, sym)
        rows = download_equity_bars(
            sym,
            interval,
            venue=venue,
            days=days,
            end_s=end,
            timeout=timeout,
            apply_calendar=apply_calendar,
            adjust=adjust,
        )
        out[lake_sym] = lake.write_bars(venue.upper(), lake_sym, interval, rows)
        if pause and i + 1 < len(symbols):
            time.sleep(pause)
    return out


__all__ = [
    "download_equity_bars",
    "download_equity_to_lake",
    "equity_interval_to_yahoo",
]
