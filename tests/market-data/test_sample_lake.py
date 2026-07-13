"""Committed ``data/sample`` Parquet fixtures are readable via HistoryReader / DataLake."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("pyarrow", reason="the data lake needs pyarrow")

from coinext_data import BarSpec, DataCatalog, DataLake, HistoryReader  # noqa: E402

_ROOT = Path(__file__).resolve().parents[2]
_SAMPLE = _ROOT / "data" / "sample"


def test_sample_lake_has_btc_and_eth_1m():
    lake = DataLake(str(_SAMPLE))
    btc = lake.read_ohlcv("BINANCE", "BTCUSDT", "1m")
    eth = lake.read_ohlcv("BINANCE", "ETHUSDT", "1m")
    assert len(btc) >= 100
    assert len(eth) >= 100
    assert btc[0][0] < btc[-1][0]
    # OHLCV shape
    assert len(btc[0]) == 6


def test_sample_history_reader_warmup():
    cat = DataCatalog(str(_SAMPLE))
    reader = HistoryReader(cat)
    bars = reader.warmup_bars(BarSpec(symbol="BTCUSDT", interval="1m"), end_ns=2**63 - 1, n=50)
    assert len(bars) == 50
    assert all(isinstance(ts, int) and isinstance(px, float) for ts, px in bars)
