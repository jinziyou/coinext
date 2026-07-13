"""Quote recording / synthesis helpers (no network)."""

from __future__ import annotations

from pathlib import Path

from coinext_data.quotes import (
    dump_quote_recording,
    load_quote_recording,
    quotes_from_trades,
    synth_quotes_from_bars,
)


def test_synth_quotes_from_close_only_bars():
    bars = [(1_000, 100.0), (2_000, 101.0)]
    qs = synth_quotes_from_bars(bars, spread_bps=2.0)
    assert len(qs) == 2
    ts, bid, ask, bid_sz, ask_sz = qs[0]  # type: ignore[misc]
    assert ts == 1_000
    assert bid < 100.0 < ask
    assert bid_sz == ask_sz == 1.0


def test_quote_recording_roundtrip(tmp_path: Path):
    quotes = synth_quotes_from_bars([(10, 50.0), (20, 51.0)])
    path = dump_quote_recording(tmp_path / "q.json", quotes, symbol="ETHUSDT", source="test")
    loaded = load_quote_recording(path)
    assert loaded["symbol"] == "ETHUSDT"
    assert loaded["source"] == "test"
    assert len(loaded["quotes"]) == 2
    assert loaded["quotes"][0][0] == 10


def test_quotes_from_trades():
    trades = [(1, 100.0, 0.5, 1), (2, 101.0, 0.2, -1)]
    qs = quotes_from_trades(trades, spread_bps=1.0)
    assert len(qs) == 2
    assert qs[0][1] < 100.0 < qs[0][2]
