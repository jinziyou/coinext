"""Tests for the local Parquet data lake (write/read/dedup/coverage) and the paginated downloader.

No network: the downloader's paging logic is exercised against a monkeypatched ``_fetch_page`` that
synthesizes >1000 klines across multiple pages, proving the 1000-bar limit is broken correctly.
"""

from __future__ import annotations

import datetime as dt

import pytest

pytest.importorskip("pyarrow", reason="the data lake needs pyarrow (`uv pip install pyarrow`)")

from qv_data import BarSpec, DataCatalog, DataLake, HistoryReader  # noqa: E402
from qv_data import download as dl  # noqa: E402

_NS = 1_000_000_000


def _ts(y, mo, d, h=0, mi=0) -> int:
    return int(dt.datetime(y, mo, d, h, mi, tzinfo=dt.UTC).timestamp()) * _NS


def _rows(start_ts, n, base=100.0):
    return [
        (start_ts + i * 60 * _NS, base + i, base + i + 1, base + i - 1, base + i + 0.5, 1.0)
        for i in range(n)
    ]


def test_write_read_roundtrip_sorted(tmp_path):
    lake = DataLake(str(tmp_path))
    rows = _rows(_ts(2024, 1, 1), 5)
    # Write out of order — the lake must store sorted by ts_event.
    n = lake.write_bars("BINANCE", "BTCUSDT", "1m", list(reversed(rows)))
    assert n == 5
    back = lake.read("BINANCE", "BTCUSDT", "1m")
    assert [r[0] for r in back] == sorted(r[0] for r in rows)
    assert back == rows
    assert lake.read_closes("BINANCE", "BTCUSDT", "1m")[0] == (rows[0][0], rows[0][4])


def test_append_dedup_last_wins(tmp_path):
    lake = DataLake(str(tmp_path))
    rows = _rows(_ts(2024, 1, 1), 3)
    lake.write_bars("BINANCE", "BTCUSDT", "1m", rows)
    # Re-write the first bar with a different close; row count stays 3, close updated.
    updated = (rows[0][0], 1.0, 2.0, 0.5, 999.0, 1.0)
    lake.write_bars("BINANCE", "BTCUSDT", "1m", [updated])
    back = lake.read("BINANCE", "BTCUSDT", "1m")
    assert len(back) == 3
    assert back[0][4] == 999.0


def test_month_partitioning_and_time_range(tmp_path):
    lake = DataLake(str(tmp_path))
    jan = _rows(_ts(2024, 1, 31, 23, 58), 5)  # spans Jan->Feb across the month boundary
    lake.write_bars("BINANCE", "BTCUSDT", "1m", jan)
    sdir = lake.series_dir("BINANCE", "BTCUSDT", "1m")
    import os

    files = sorted(f for f in os.listdir(sdir) if f.endswith(".parquet"))
    assert files == ["202401.parquet", "202402.parquet"]  # two month shards
    feb_only = lake.read_closes("BINANCE", "BTCUSDT", "1m", start_ns=_ts(2024, 2, 1))
    assert all(ts >= _ts(2024, 2, 1) for ts, _ in feb_only)
    assert 0 < len(feb_only) < 5


def test_coverage_and_catalog(tmp_path):
    lake = DataLake(str(tmp_path))
    rows = _rows(_ts(2024, 3, 1), 10)
    lake.write_bars("BINANCE", "ETHUSDT", "5m", rows)
    cov = lake.coverage("BINANCE", "ETHUSDT", "5m")
    assert cov.n_rows == 10
    assert cov.start_ns == rows[0][0]
    assert cov.end_ns == rows[-1][0]
    assert ("BINANCE", "ETHUSDT", "5m") in lake.list_series()
    entry = DataCatalog(str(tmp_path)).entry(BarSpec(symbol="ETHUSDT", interval="5m"))
    assert entry.n_rows == 10


def test_history_reader_uses_lake(tmp_path):
    lake = DataLake(str(tmp_path))
    rows = _rows(_ts(2024, 1, 1), 8)
    lake.write_bars("BINANCE", "BTCUSDT", "1m", rows)
    hr = HistoryReader(DataCatalog(str(tmp_path)))
    bars = hr.read_bars(BarSpec(symbol="BTCUSDT"))
    assert len(bars) == 8
    warm = hr.warmup_bars(BarSpec(symbol="BTCUSDT"), end_ns=rows[-1][0], n=3)
    assert len(warm) == 3
    assert warm[-1][0] == rows[-1][0]


def test_downloader_pages_past_1000_limit(monkeypatch):
    """The synthetic venue has 2500 one-minute bars; a single request caps at 1000, so the
    downloader must page three times and stitch a contiguous, deduped series."""
    interval_ms = 60_000
    g_start = _ts(2024, 6, 1) // 1_000_000  # ms
    total = 2500
    g_end = g_start + total * interval_ms

    def fake_fetch(base, symbol, interval, start_ms, end_ms, timeout):
        t = (max(start_ms, g_start) // interval_ms) * interval_ms
        out = []
        while t <= min(end_ms, g_end - interval_ms) and len(out) < 1000:
            if t >= g_start:
                close_t = t + interval_ms - 1
                px = 100.0 + (t - g_start) / interval_ms
                out.append([t, px, px + 1, px - 1, px + 0.5, 1.0, close_t, 0, 0, 0, 0, 0])
            t += interval_ms
        return out

    monkeypatch.setattr(dl, "_fetch_page", fake_fetch)
    rows = dl.download_klines("BTCUSDT", "1m", start_ms=g_start, end_ms=g_end, pause=0.0)
    assert len(rows) == total  # all 2500, no dupes despite paging
    ts_list = [r[0] for r in rows]
    assert ts_list == sorted(ts_list)
    assert len(set(ts_list)) == total  # deduped
    # contiguous 1-minute spacing
    diffs = {ts_list[i + 1] - ts_list[i] for i in range(len(ts_list) - 1)}
    assert diffs == {60 * _NS}


def test_interval_to_ms():
    assert dl.interval_to_ms("1m") == 60_000
    assert dl.interval_to_ms("1h") == 3_600_000
    assert dl.interval_to_ms("1d") == 86_400_000
    with pytest.raises(ValueError):
        dl.interval_to_ms("7s")


def test_lake_is_authoritative_over_stale_csv(tmp_path):
    """Regression (review finding): an empty bounded read must NOT fall through to a stale data.csv
    in the same series dir — the lake is authoritative whenever the series exists (parity)."""
    import os

    lake = DataLake(str(tmp_path))
    lake.write_bars("BINANCE", "BTCUSDT", "1m", _rows(_ts(2024, 1, 1), 5))
    # Drop a stale CSV (2023 rows) into the SAME directory as the month shards.
    sdir = lake.series_dir("BINANCE", "BTCUSDT", "1m")
    with open(os.path.join(sdir, "data.csv"), "w") as fh:
        fh.write("1672531260000000000,42.0\n1672531320000000000,43.0\n")

    hr = HistoryReader(DataCatalog(str(tmp_path)))
    spec = BarSpec(symbol="BTCUSDT")
    # A range entirely OUTSIDE 2024 coverage must be [] (not the 2023 CSV garbage).
    assert hr.read_bars(spec, start_ns=_ts(2023, 1, 1), end_ns=_ts(2023, 1, 2)) == []
    assert hr.warmup_bars(spec, end_ns=_ts(2023, 1, 1), n=2) == []
    # In-range reads still work.
    assert len(hr.read_bars(spec)) == 5


def test_cli_download_guards_when_pyarrow_absent(monkeypatch, capsys):
    """Regression (review finding): lake-backed CLI commands give a clean message + exit 1 when
    pyarrow is missing, not an opaque ``'NoneType' object is not callable``."""
    import qv_cli.main as cli
    import qv_data

    monkeypatch.setattr(qv_data, "_HAVE_LAKE", False)
    assert cli._cmd_download("BTCUSDT") == 1
    assert "pyarrow" in capsys.readouterr().out
