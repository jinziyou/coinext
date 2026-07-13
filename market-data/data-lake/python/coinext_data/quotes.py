"""Quote-tick history: synthetic derivation, JSON recording, and optional live REST snapshot.

Research gap: ``on_quote`` in backtests usually runs on synthetic or trade-derived quotes.
This module closes the ergonomics gap without requiring a live WS capture session:

* :func:`synth_quotes_from_bars` — deterministic bid/ask around each bar close (research default).
* :func:`quotes_from_trades` — reconstruct a one-sided quote stream from aggTrade prints.
* :func:`dump_quote_recording` / :func:`load_quote_recording` — offline JSON fixtures for replay.
* :func:`fetch_binance_book_ticker` — one-shot public REST snapshot (no API key); useful for
  seeding a recording, not a historical time series (Binance does not expose full bookTicker
  history on REST).

Recorded layout (JSON)::

    {
      "schema_version": 1,
      "symbol": "BTCUSDT",
      "venue": "BINANCE",
      "source": "synth|trades|bookTicker|ws-capture",
      "quotes": [[ts_ns, bid, ask, bid_sz, ask_sz], ...]
    }
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path
from typing import Any

# Quote row accepted by coinext_backtest / coinext_py: (ts_ns, bid, ask[, bid_sz, ask_sz])
QuoteRow = tuple[int, float, float] | tuple[int, float, float, float, float]


def synth_quotes_from_bars(
    bars: list[tuple],
    *,
    spread_bps: float = 1.0,
    size: float = 1.0,
) -> list[QuoteRow]:
    """Build a quote at each bar close: mid = close, bid/ask = mid ± half-spread.

    ``bars`` rows may be ``(ts, close)`` or full OHLCV; close is always the last price field
    before optional volume (index 1 for close-only, index 4 for OHLCV).
    """
    half = max(0.0, float(spread_bps)) / 20_000.0  # bps of mid, each side
    out: list[QuoteRow] = []
    for row in bars:
        ts = int(row[0])
        if len(row) >= 5:
            mid = float(row[4])
        else:
            mid = float(row[1])
        bid = mid * (1.0 - half)
        ask = mid * (1.0 + half)
        out.append((ts, bid, ask, float(size), float(size)))
    return out


def quotes_from_trades(
    trades: list[tuple[int, float, float, int]],
    *,
    spread_bps: float = 1.0,
    size: float = 1.0,
) -> list[QuoteRow]:
    """Approximate quotes from trade prints (mid = trade price, symmetric spread)."""
    half = max(0.0, float(spread_bps)) / 20_000.0
    out: list[QuoteRow] = []
    for ts, px, _qty, _side in trades:
        mid = float(px)
        out.append((int(ts), mid * (1.0 - half), mid * (1.0 + half), float(size), float(size)))
    return out


def dump_quote_recording(
    path: str | Path,
    quotes: list[QuoteRow],
    *,
    symbol: str = "BTCUSDT",
    venue: str = "BINANCE",
    source: str = "synth",
) -> Path:
    """Write a versioned quote recording JSON for offline ``on_quote`` replay."""
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "symbol": symbol,
        "venue": venue,
        "source": source,
        "quotes": [list(q) for q in quotes],
    }
    dest.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return dest


def load_quote_recording(path: str | Path) -> dict[str, Any]:
    """Load a quote recording; returns metadata + ``quotes`` as ``QuoteRow`` list."""
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if int(raw.get("schema_version", 0)) != 1:
        raise ValueError(
            f"unsupported quote recording schema_version={raw.get('schema_version')!r}"
        )
    quotes: list[QuoteRow] = []
    for row in raw.get("quotes") or []:
        if len(row) < 3:
            raise ValueError(f"quote row too short: {row!r}")
        ts, bid, ask = int(row[0]), float(row[1]), float(row[2])
        if len(row) >= 5:
            quotes.append((ts, bid, ask, float(row[3]), float(row[4])))
        else:
            quotes.append((ts, bid, ask))
    return {
        "symbol": str(raw.get("symbol", "")),
        "venue": str(raw.get("venue", "BINANCE")),
        "source": str(raw.get("source", "")),
        "quotes": quotes,
    }


def fetch_binance_book_ticker(
    symbol: str = "BTCUSDT",
    *,
    testnet: bool = False,
    timeout: float = 10.0,
) -> QuoteRow:
    """One-shot public ``/api/v3/ticker/bookTicker`` snapshot (no API key).

    Returns a single quote row stamped with the receive time (ns). Not a historical series —
    use :func:`dump_quote_recording` after a WS capture for multi-tick history.
    """
    import time

    base = "https://testnet.binance.vision" if testnet else "https://api.binance.com"
    url = f"{base}/api/v3/ticker/bookTicker?symbol={symbol}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
        raw = json.loads(resp.read().decode("utf-8"))
    ts = int(time.time() * 1_000_000_000)
    bid = float(raw["bidPrice"])
    ask = float(raw["askPrice"])
    bid_sz = float(raw.get("bidQty") or 0.0)
    ask_sz = float(raw.get("askQty") or 0.0)
    return (ts, bid, ask, bid_sz, ask_sz)
