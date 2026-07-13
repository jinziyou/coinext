"""Quote capture (REST path monkeypatched — no network)."""

from __future__ import annotations

from pathlib import Path

from coinext_data import quote_capture as qc


def test_capture_quotes_rest_monkeypatched(tmp_path: Path, monkeypatch):
    calls = {"n": 0}

    def fake_rest(symbol, *, testnet, timeout):  # noqa: ANN001
        calls["n"] += 1
        return (calls["n"] * 1_000, 100.0, 100.1, 1.0, 1.0)

    monkeypatch.setattr(qc, "_book_ticker_rest", fake_rest)
    # Force tiny sleep budget: seconds=0 still gets one loop if we structure carefully.
    # capture_quotes_rest loops while monotonic < deadline; with seconds=0 may get 0 quotes.
    # Use a fixed list instead by calling the public capture with patched sleep.
    monkeypatch.setattr(qc.time, "sleep", lambda _s: None)
    # Make monotonic advance so we exit after a few polls.
    times = [0.0, 0.0, 0.1, 0.2, 0.3, 10.0]

    def fake_mono():
        return times.pop(0) if times else 10.0

    monkeypatch.setattr(qc.time, "monotonic", fake_mono)
    out = tmp_path / "cap.json"
    result = qc.capture_quotes("BTCUSDT", seconds=1.0, interval=0.05, mode="rest", out=out)
    assert result["n"] >= 1
    assert result["path"] == str(out)
    assert out.is_file()
    assert "rest-bookTicker-poll" in result["source"]
