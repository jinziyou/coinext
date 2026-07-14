"""coinext_data — Parquet data lake, HistoryReader, venue download helpers.

Status: verified. See root ARCHITECTURE.md and docs/STATUS.md.
"""

from __future__ import annotations

import csv
import os
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from .ashare_rules import (
    LIMIT_PCT_CHINEXT_STAR,
    LIMIT_PCT_MAIN,
    LIMIT_PCT_ST,
    T1_VENUES,
    LimitBand,
    is_t1_venue,
    limit_band,
    price_limit_pct,
    resolve_prev_close,
    round_tick,
    trade_date_from_ns,
)

# Calendar + FX (zero-dep helpers; FX Yahoo load needs network).
from .calendar import (
    FilterStats,
    SessionHours,
    TradingCalendar,
    bar_local_time,
    calendar_for,
    filter_session_bars,
    filter_trading_bars,
    in_session,
    is_trading_day,
    previous_session_date,
    session_date,
    session_hours,
    session_timezone,
)
from .fx import (
    FxBook,
    FxCurve,
    convert_bars,
    download_fx_to_lake,
    load_fx_book,
    mark_portfolio_value,
    revalue_bar_map,
    venue_currency,
    yahoo_fx_ticker,
)
from .quote_capture import capture_quotes, capture_quotes_rest, capture_quotes_ws
from .quotes import (
    dump_quote_recording,
    fetch_binance_book_ticker,
    load_quote_recording,
    quotes_from_trades,
    synth_quotes_from_bars,
)
from .venues import (
    DEFAULT_FX_PAIRS,
    DEFAULT_UNIVERSES,
    ETF_UNIVERSES,
    MARKET_GROUPS,
    SAMPLE_ADJ_SERIES,
    SAMPLE_EQUITY_SERIES,
    InstrumentSpec,
    VenueInfo,
    all_venues,
    default_universe,
    equity_venues,
    etf_universe,
    expand_venues,
    format_market_groups,
    format_venue_table,
    get_venue,
    infer_ashare_venue,
    instrument_spec,
    is_equity_venue,
    is_etf_symbol,
    lake_symbol,
    parse_user_symbol,
    resolve_listing,
    resolve_listings,
    resolve_market_group,
    resolve_symbols,
    resolve_venue,
    suggest_equity_download_defaults,
    yahoo_symbol,
)

# The Parquet lake (write/read/coverage), trade ingestion, and paginated downloader. Guarded so
# `import coinext_data` works even when pyarrow is absent — only lake-backed features then raise a
# clear error on use.
try:
    from .download import download_klines, download_to_lake, interval_to_ms
    from .equity_download import (
        download_equity_bars,
        download_equity_to_lake,
        equity_interval_to_yahoo,
    )
    from .ingest import ingest_agg_trades_to_lake, trade_ticks_to_ohlcv
    from .lake import DataLake, SeriesCoverage

    _HAVE_LAKE = True
except ImportError:  # pyarrow not installed
    _HAVE_LAKE = False

    def _need_pyarrow(*_a, **_k):  # noqa: ANN002, ANN003
        raise ImportError("the data lake needs pyarrow: `uv pip install pyarrow`")

    # Bind classes AND functions to the same raising shim, so any `DataLake()` / lake-backed helper
    # surfaces the actionable install message — never an opaque `'NoneType' is not callable`.
    DataLake = SeriesCoverage = _need_pyarrow  # type: ignore[assignment,misc]
    download_klines = download_to_lake = interval_to_ms = _need_pyarrow  # type: ignore[assignment]
    download_equity_bars = download_equity_to_lake = equity_interval_to_yahoo = (  # type: ignore[assignment]
        _need_pyarrow
    )
    ingest_agg_trades_to_lake = trade_ticks_to_ohlcv = _need_pyarrow  # type: ignore[assignment]


@dataclass(frozen=True)
class BarSpec:
    """Identifies one bar series in the lake."""

    venue: str = "BINANCE"
    symbol: str = "BTCUSDT"
    interval: str = "1m"


@dataclass
class CatalogEntry:
    """Metadata for one resolved series partition set."""

    spec: BarSpec
    path: str
    start_ns: int | None = None
    end_ns: int | None = None
    n_rows: int | None = None


class DataCatalog:
    """Discovery/metadata over the Parquet data lake.

    Resolves a :class:`BarSpec` to on-disk partitions and summarizes coverage (range/row stats) so
    callers can report what's available. ``entry()`` reads coverage from the Parquet lake when
    pyarrow is present; otherwise it returns paths only.
    """

    def __init__(self, lake_root: str | None = None) -> None:
        self.lake_root = lake_root or os.environ.get("COINEXT__DATA__LAKE_ROOT", "data")

    def partition_dir(self, spec: BarSpec) -> str:
        """Compute the partition directory for ``spec`` (does not require it to exist)."""
        return os.path.join(
            self.lake_root,
            "bars",
            f"venue={spec.venue}",
            f"symbol={spec.symbol}",
            f"interval={spec.interval}",
        )

    def entry(self, spec: BarSpec) -> CatalogEntry:
        """Return a :class:`CatalogEntry` for ``spec``, with coverage stats from the Parquet lake
        (rows + start/end) when pyarrow is available."""
        path = self.partition_dir(spec)
        if _HAVE_LAKE:
            cov = DataLake(self.lake_root).coverage(spec.venue, spec.symbol, spec.interval)
            return CatalogEntry(
                spec=spec, path=path, start_ns=cov.start_ns, end_ns=cov.end_ns, n_rows=cov.n_rows
            )
        return CatalogEntry(spec=spec, path=path)

    def list_symbols(self, venue: str = "BINANCE") -> list[str]:
        """List symbols present under ``venue`` (FS scan; empty when the lake is absent)."""
        base = os.path.join(self.lake_root, "bars", f"venue={venue}")
        if not os.path.isdir(base):
            return []
        out = []
        for name in sorted(os.listdir(base)):
            if name.startswith("symbol="):
                out.append(name[len("symbol=") :])
        return out


class HistoryReader:
    """Bounded historical reads, primarily for indicator **warm-up**.

    Warm-up is served from the LOCAL lake in both backtest and live so indicators are identical
    (root ARCHITECTURE.md §4–§6). ``warmup_bars`` returns the last ``n`` bars at/before ``end_ns`` —
    these are fed through the SAME streaming indicators the strategy uses before the first live bar.
    """

    def __init__(self, catalog: DataCatalog | None = None) -> None:
        self.catalog = catalog or DataCatalog()

    def read_bars(
        self,
        spec: BarSpec,
        *,
        start_ns: int | None = None,
        end_ns: int | None = None,
    ) -> list[tuple[int, float]]:
        """Read ``(ts_ns, close)`` rows for ``spec`` within ``[start_ns, end_ns]`` from the lake.

        The lake is AUTHORITATIVE whenever the series exists: an empty bounded read (a gap, or a
        range outside coverage) returns ``[]`` rather than silently shadowing stale CSV. The CSV
        on-ramp is used only when pyarrow is absent OR the lake has no such series — so backtest
        warm-up never sees data the live path (which reads the lake directly) would not.
        """
        if _HAVE_LAKE:
            lake = DataLake(self.catalog.lake_root)
            if os.path.isdir(lake.series_dir(spec.venue, spec.symbol, spec.interval)):
                return lake.read_closes(
                    spec.venue, spec.symbol, spec.interval, start_ns=start_ns, end_ns=end_ns
                )
        csv_path = os.path.join(self.catalog.partition_dir(spec), "data.csv")
        bars = load_bars(csv_path) if os.path.exists(csv_path) else []
        if start_ns is not None:
            bars = [b for b in bars if b[0] >= start_ns]
        if end_ns is not None:
            bars = [b for b in bars if b[0] <= end_ns]
        return bars

    def warmup_bars(self, spec: BarSpec, *, end_ns: int, n: int) -> list[tuple[int, float]]:
        """Return the last ``n`` bars at/before ``end_ns`` for indicator warm-up."""
        bars = self.read_bars(spec, end_ns=end_ns)
        return bars[-n:]


class DataProvider:
    """Backtest data-feed source: yields time-ordered bars for the Kernel to merge-sort.

    In live the analogous feed is the Redis-bus consumer (``coinext_bus``); the Kernel sees the same
    ``MarketEvent`` shape from either source (the parity seam on the data side).
    """

    def __init__(self, reader: HistoryReader | None = None) -> None:
        self.reader = reader or HistoryReader()

    def stream(self, spec: BarSpec) -> Iterator[tuple[int, float]]:
        """Yield ``(ts_ns, close)`` in ascending ``ts`` order (a guard against look-ahead)."""
        bars = self.reader.read_bars(spec)
        prev_ts = None
        for ts, close in bars:
            if prev_ts is not None and ts < prev_ts:
                raise ValueError(f"non-monotonic bar timestamp in feed: {ts} < {prev_ts}")
            prev_ts = ts
            yield ts, close


def load_bars(source: str | Iterable[tuple[int, float]]) -> list[tuple[int, float]]:
    """Load ``(ts_ns, close)`` bars from a CSV path or an inline iterable.

    * ``str`` → read a 2-column CSV (``ts_ns,close``), tolerating an optional header row.
    * iterable → coerce each item to ``(int(ts), float(close))``.

    This is the zero-heavy-dep on-ramp; the lake-backed Parquet/DuckDB path is TODO.
    """
    if isinstance(source, str):
        return _load_csv(source)
    out: list[tuple[int, float]] = []
    for ts, close in source:
        out.append((int(ts), float(close)))
    return out


def _load_csv(path: str) -> list[tuple[int, float]]:
    rows: list[tuple[int, float]] = []
    with open(path, newline="", encoding="utf-8") as fh:
        for record in csv.reader(fh):
            if not record:
                continue
            try:
                rows.append((int(record[0]), float(record[1])))
            except (ValueError, IndexError):
                # Skip header / malformed lines (lenient on-ramp; lake reader will be strict).
                continue
    return rows


def fetch_binance_klines(
    symbol: str = "BTCUSDT",
    interval: str = "1m",
    limit: int = 500,
    *,
    testnet: bool = False,
    timeout: float = 15.0,
) -> list[tuple[int, float]]:
    """Fetch REAL Binance klines via the PUBLIC REST endpoint (no API key) as ``(ts_ns, close)``.

    Uses only the stdlib (``urllib``) so it works with zero heavy deps. ``testnet=True`` hits
    ``testnet.binance.vision`` (sparse), ``False`` hits mainnet ``api.binance.com`` (real, liquid) —
    the latter is the right source for research/backtest data even when execution is on testnet
    (the sandbox design: real data, paper execution). The returned list feeds straight into
    ``coinext_backtest.run(strategy, bars=...)``.
    """
    import json
    import urllib.request

    base = "https://testnet.binance.vision" if testnet else "https://api.binance.com"
    url = f"{base}/api/v3/klines?symbol={symbol}&interval={interval}&limit={int(limit)}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 (trusted host)
        raw = json.loads(resp.read().decode("utf-8"))
    # Each kline: [openTime(ms), open, high, low, close, volume, closeTime(ms), ...].
    # Use the bar CLOSE time (closeTime) as ts_event to avoid look-ahead, in nanoseconds.
    return [(int(k[6]) * 1_000_000, float(k[4])) for k in raw]


def fetch_binance_agg_trades(
    symbol: str = "BTCUSDT", limit: int = 1000, *, timeout: float = 15.0
) -> list[tuple[int, float, float, int]]:
    """Fetch REAL recent Binance aggregated trades (public REST, no key) as
    ``(ts_ns, price, size, aggressor)`` with aggressor ``+1`` buy / ``-1`` sell.

    Genuine per-print microstructure (each row is a real trade), suitable for ``trades=`` in
    ``coinext_backtest.run`` so ``on_trade`` fires on real prints. ``aggTrades`` are dense (many per
    second), so ``limit`` (max 1000) covers a short window — enough to exercise trade-driven logic.
    The Binance ``m`` flag is *buyer-is-maker*; the taker aggressor is therefore the seller when
    ``m`` is true (-> -1) and the buyer otherwise (-> +1).
    """
    import json
    import urllib.request

    url = f"https://api.binance.com/api/v3/aggTrades?symbol={symbol}&limit={int(limit)}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 (trusted host)
        raw = json.loads(resp.read().decode("utf-8"))
    # Each: {"p": price, "q": qty, "T": timestamp(ms), "m": buyerIsMaker, ...}.
    return [
        (int(t["T"]) * 1_000_000, float(t["p"]), float(t["q"]), (-1 if t["m"] else 1)) for t in raw
    ]


__all__ = [
    "capture_quotes",
    "capture_quotes_rest",
    "capture_quotes_ws",
    "fetch_binance_book_ticker",
    "load_quote_recording",
    "dump_quote_recording",
    "quotes_from_trades",
    "synth_quotes_from_bars",
    "BarSpec",
    "CatalogEntry",
    "DataCatalog",
    "HistoryReader",
    "DataProvider",
    "load_bars",
    "fetch_binance_klines",
    "fetch_binance_agg_trades",
    "ingest_agg_trades_to_lake",
    "trade_ticks_to_ohlcv",
    # Venue catalog (zero-dep)
    "VenueInfo",
    "InstrumentSpec",
    "DEFAULT_UNIVERSES",
    "ETF_UNIVERSES",
    "MARKET_GROUPS",
    "SAMPLE_ADJ_SERIES",
    "SAMPLE_EQUITY_SERIES",
    "all_venues",
    "default_universe",
    "equity_venues",
    "etf_universe",
    "expand_venues",
    "format_market_groups",
    "format_venue_table",
    "get_venue",
    "infer_ashare_venue",
    "instrument_spec",
    "is_equity_venue",
    "is_etf_symbol",
    "lake_symbol",
    "parse_user_symbol",
    "resolve_listing",
    "resolve_listings",
    "resolve_market_group",
    "resolve_symbols",
    "resolve_venue",
    "suggest_equity_download_defaults",
    "yahoo_symbol",
    # A-share rules (shared with coinext_broker)
    "LIMIT_PCT_CHINEXT_STAR",
    "LIMIT_PCT_MAIN",
    "LIMIT_PCT_ST",
    "LimitBand",
    "T1_VENUES",
    "is_t1_venue",
    "limit_band",
    "price_limit_pct",
    "resolve_prev_close",
    "round_tick",
    "trade_date_from_ns",
    # Calendar + FX
    "FilterStats",
    "SessionHours",
    "TradingCalendar",
    "bar_local_time",
    "calendar_for",
    "filter_session_bars",
    "filter_trading_bars",
    "in_session",
    "is_trading_day",
    "previous_session_date",
    "session_date",
    "session_hours",
    "session_timezone",
    "DEFAULT_FX_PAIRS",
    "FxBook",
    "FxCurve",
    "convert_bars",
    "download_fx_to_lake",
    "load_fx_book",
    "mark_portfolio_value",
    "revalue_bar_map",
    "venue_currency",
    "yahoo_fx_ticker",
    # Parquet lake (require pyarrow)
    "DataLake",
    "SeriesCoverage",
    "download_klines",
    "download_to_lake",
    "interval_to_ms",
    "download_equity_bars",
    "download_equity_to_lake",
    "equity_interval_to_yahoo",
]
