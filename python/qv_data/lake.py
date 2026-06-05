"""qv_data.lake — the local Parquet data lake (write + read + coverage).

Partition layout (Hive-style dirs + per-month file shards)::

    {root}/bars/venue={v}/symbol={s}/interval={i}/{YYYYMM}.parquet

One Parquet file per calendar month (UTC, by bar **close** time), rows sorted by ``ts_event`` and
deduped by it (idempotent re-downloads). Schema is full OHLCV so later work (OHLC-aware fills,
richer analytics) has what it needs; backtests pull ``(ts_ns, close)`` via ``read_closes``.

This is the reproducibility foundation: download once, backtest many times over the SAME bytes
(``ARCHITECTURE.md`` §7 — the ONE HistoryReader path shared by backtest and live warm-up).
"""

from __future__ import annotations

import datetime as _dt
import os
from collections import defaultdict
from dataclasses import dataclass

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError as exc:  # pragma: no cover - clear setup error
    raise ImportError("the data lake needs pyarrow: `uv pip install pyarrow`") from exc

# A bar row: (ts_event_ns, open, high, low, close, volume). ts_event is the bar CLOSE time.
BarRow = tuple[int, float, float, float, float, float]

BAR_SCHEMA = pa.schema(
    [
        ("ts_event", pa.int64()),
        ("open", pa.float64()),
        ("high", pa.float64()),
        ("low", pa.float64()),
        ("close", pa.float64()),
        ("volume", pa.float64()),
    ]
)

_NS_PER_S = 1_000_000_000


def _yyyymm(ts_ns: int) -> int:
    """Calendar month (UTC) of a nanosecond close-time, as the int ``YYYYMM``."""
    secs = int(ts_ns) // _NS_PER_S
    dt = _dt.datetime.fromtimestamp(secs, tz=_dt.UTC)
    return dt.year * 100 + dt.month


@dataclass(frozen=True)
class SeriesCoverage:
    """Coverage summary for one series in the lake."""

    venue: str
    symbol: str
    interval: str
    n_rows: int
    start_ns: int | None
    end_ns: int | None

    def span_utc(self) -> tuple[str, str]:
        def iso(ns: int | None) -> str:
            if ns is None:
                return "-"
            return _dt.datetime.fromtimestamp(ns // _NS_PER_S, tz=_dt.UTC).isoformat()

        return iso(self.start_ns), iso(self.end_ns)


class DataLake:
    """A partitioned Parquet store of OHLCV bars on the local filesystem (or a mounted volume)."""

    def __init__(self, root: str | None = None) -> None:
        self.root = root or os.environ.get("VQ__DATA__LAKE_ROOT", "data")

    # --- paths ---
    def series_dir(self, venue: str, symbol: str, interval: str) -> str:
        return os.path.join(
            self.root, "bars", f"venue={venue}", f"symbol={symbol}", f"interval={interval}"
        )

    def _month_file(self, venue: str, symbol: str, interval: str, yyyymm: int) -> str:
        return os.path.join(self.series_dir(venue, symbol, interval), f"{yyyymm}.parquet")

    # --- write ---
    def write_bars(self, venue: str, symbol: str, interval: str, rows: list[BarRow]) -> int:
        """Write/merge OHLCV ``rows`` into the lake. Idempotent: existing bars at the same
        ``ts_event`` are overwritten (last wins), rows are deduped + sorted per month. Returns the
        number of DISTINCT rows now stored across the affected months.
        """
        if not rows:
            return 0
        by_month: dict[int, list[BarRow]] = defaultdict(list)
        for r in rows:
            by_month[_yyyymm(r[0])].append(r)

        written = 0
        for ym, month_rows in by_month.items():
            path = self._month_file(venue, symbol, interval, ym)
            merged: dict[int, BarRow] = {}
            if os.path.exists(path):
                for r in _read_file_rows(path):
                    merged[r[0]] = r
            for r in month_rows:
                merged[int(r[0])] = (int(r[0]), *(float(x) for x in r[1:]))
            ordered = [merged[k] for k in sorted(merged)]
            os.makedirs(os.path.dirname(path), exist_ok=True)
            _write_rows(path, ordered)
            written += len(ordered)
        return written

    # --- read ---
    def read(
        self,
        venue: str,
        symbol: str,
        interval: str,
        *,
        start_ns: int | None = None,
        end_ns: int | None = None,
    ) -> list[BarRow]:
        """Return OHLCV rows for the series within ``[start_ns, end_ns]`` (inclusive), ts-sorted.

        Only month files overlapping the requested range are opened (filename-based pruning).
        """
        sdir = self.series_dir(venue, symbol, interval)
        if not os.path.isdir(sdir):
            return []
        start_ym = _yyyymm(start_ns) if start_ns is not None else None
        end_ym = _yyyymm(end_ns) if end_ns is not None else None
        out: list[BarRow] = []
        for fname in sorted(os.listdir(sdir)):
            if not fname.endswith(".parquet"):
                continue
            try:
                ym = int(fname[:-8])
            except ValueError:
                continue
            if start_ym is not None and ym < start_ym:
                continue
            if end_ym is not None and ym > end_ym:
                continue
            for r in _read_file_rows(os.path.join(sdir, fname)):
                if start_ns is not None and r[0] < start_ns:
                    continue
                if end_ns is not None and r[0] > end_ns:
                    continue
                out.append(r)
        out.sort(key=lambda r: r[0])
        return out

    def read_closes(
        self,
        venue: str,
        symbol: str,
        interval: str,
        *,
        start_ns: int | None = None,
        end_ns: int | None = None,
    ) -> list[tuple[int, float]]:
        """Return ``(ts_ns, close)`` rows — the shape ``qv_backtest.run`` consumes."""
        return [
            (r[0], r[4])
            for r in self.read(venue, symbol, interval, start_ns=start_ns, end_ns=end_ns)
        ]

    # --- discovery / coverage ---
    def list_series(self) -> list[tuple[str, str, str]]:
        """Discover ``(venue, symbol, interval)`` series present in the lake."""
        base = os.path.join(self.root, "bars")
        series: list[tuple[str, str, str]] = []
        if not os.path.isdir(base):
            return series
        for vdir in sorted(_subdirs(base, "venue=")):
            venue = vdir.split("=", 1)[1]
            for sdir in sorted(_subdirs(os.path.join(base, vdir), "symbol=")):
                symbol = sdir.split("=", 1)[1]
                idir_base = os.path.join(base, vdir, sdir)
                for idir in sorted(_subdirs(idir_base, "interval=")):
                    interval = idir.split("=", 1)[1]
                    series.append((venue, symbol, interval))
        return series

    def coverage(self, venue: str, symbol: str, interval: str) -> SeriesCoverage:
        sdir = self.series_dir(venue, symbol, interval)
        n_rows = 0
        lo: int | None = None
        hi: int | None = None
        if os.path.isdir(sdir):
            for fname in sorted(os.listdir(sdir)):
                if not fname.endswith(".parquet"):
                    continue
                meta = pq.read_metadata(os.path.join(sdir, fname))
                n_rows += meta.num_rows
                # ts_event is column 0; use row-group statistics for min/max without full read.
                for rg in range(meta.num_row_groups):
                    stats = meta.row_group(rg).column(0).statistics
                    if stats is not None and stats.has_min_max:
                        lo = stats.min if lo is None else min(lo, stats.min)
                        hi = stats.max if hi is None else max(hi, stats.max)
        return SeriesCoverage(venue, symbol, interval, n_rows, lo, hi)

    def coverage_all(self) -> list[SeriesCoverage]:
        return [self.coverage(v, s, i) for (v, s, i) in self.list_series()]


def _subdirs(path: str, prefix: str) -> list[str]:
    if not os.path.isdir(path):
        return []
    return [
        d for d in os.listdir(path) if d.startswith(prefix) and os.path.isdir(os.path.join(path, d))
    ]


def _read_file_rows(path: str) -> list[BarRow]:
    table = pq.read_table(path, schema=BAR_SCHEMA)
    cols = {name: table.column(name).to_pylist() for name in BAR_SCHEMA.names}
    return list(
        zip(
            cols["ts_event"],
            cols["open"],
            cols["high"],
            cols["low"],
            cols["close"],
            cols["volume"],
            strict=True,
        )
    )


def _write_rows(path: str, rows: list[BarRow]) -> None:
    table = pa.table(
        {
            "ts_event": [int(r[0]) for r in rows],
            "open": [float(r[1]) for r in rows],
            "high": [float(r[2]) for r in rows],
            "low": [float(r[3]) for r in rows],
            "close": [float(r[4]) for r in rows],
            "volume": [float(r[5]) for r in rows],
        },
        schema=BAR_SCHEMA,
    )
    pq.write_table(table, path, compression="zstd")


__all__ = ["DataLake", "SeriesCoverage", "BAR_SCHEMA", "BarRow"]
