"""BookTicker capture daemon (public REST poll; optional raw WS when available).

Binance does not expose historical bookTicker over REST. This module records a **live** quote
series into the same JSON schema as :mod:`coinext_data.quotes` so research can replay real
spreads via ``load_quote_recording`` / ``coinext_backtest.run(..., quotes=...)``.

Default transport is **stdlib REST polling** of ``/api/v3/ticker/bookTicker`` (no API key). When the
optional ``websockets`` package is installed, ``mode="ws"`` uses the public combined stream
``{symbol}@bookTicker`` for lower-latency capture.

CLI::

    coinext capture-quotes --symbol BTCUSDT --seconds 30 --out data/sample/quotes/...
"""

from __future__ import annotations

import json
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .quotes import QuoteRow, dump_quote_recording


def _book_ticker_rest(symbol: str, *, testnet: bool, timeout: float) -> QuoteRow:
    base = "https://testnet.binance.vision" if testnet else "https://api.binance.com"
    url = f"{base}/api/v3/ticker/bookTicker?symbol={symbol}"
    with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
        raw = json.loads(resp.read().decode("utf-8"))
    ts = int(time.time() * 1_000_000_000)
    return (
        ts,
        float(raw["bidPrice"]),
        float(raw["askPrice"]),
        float(raw.get("bidQty") or 0.0),
        float(raw.get("askQty") or 0.0),
    )


def capture_quotes_rest(
    symbol: str = "BTCUSDT",
    *,
    seconds: float = 30.0,
    interval: float = 0.5,
    testnet: bool = False,
    timeout: float = 10.0,
    on_quote: Callable[[QuoteRow], None] | None = None,
) -> list[QuoteRow]:
    """Poll bookTicker for ``seconds`` at ``interval`` Hz and return the quote list."""
    deadline = time.monotonic() + max(0.0, float(seconds))
    out: list[QuoteRow] = []
    while time.monotonic() < deadline:
        q = _book_ticker_rest(symbol, testnet=testnet, timeout=timeout)
        out.append(q)
        if on_quote is not None:
            on_quote(q)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(float(interval), remaining))
    return out


def capture_quotes_ws(
    symbol: str = "BTCUSDT",
    *,
    seconds: float = 30.0,
    testnet: bool = False,
    on_quote: Callable[[QuoteRow], None] | None = None,
) -> list[QuoteRow]:
    """Capture bookTicker via public WS (requires the optional ``websockets`` package)."""
    try:
        import asyncio

        import websockets  # type: ignore
    except ImportError as exc:
        raise ImportError(
            "WS capture needs the websockets package: `uv pip install websockets`"
        ) from exc

    stream = f"{symbol.lower()}@bookTicker"
    host = "stream.testnet.binance.vision" if testnet else "stream.binance.com"
    url = f"wss://{host}:9443/ws/{stream}"
    out: list[QuoteRow] = []

    async def _run() -> None:
        deadline = time.monotonic() + max(0.0, float(seconds))
        async with websockets.connect(url, ping_interval=20) as ws:
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, remaining))
                except TimeoutError:
                    break
                msg = json.loads(raw)
                # Combined or single: {"b": bid, "B": bidQty, "a": ask, "A": askQty, ...}
                data = msg.get("data", msg)
                ts = int(time.time() * 1_000_000_000)
                q: QuoteRow = (
                    ts,
                    float(data["b"]),
                    float(data["a"]),
                    float(data.get("B") or 0.0),
                    float(data.get("A") or 0.0),
                )
                out.append(q)
                if on_quote is not None:
                    on_quote(q)

    asyncio.run(_run())
    return out


def capture_quotes(
    symbol: str = "BTCUSDT",
    *,
    seconds: float = 30.0,
    interval: float = 0.5,
    mode: str = "rest",
    testnet: bool = False,
    out: str | Path | None = None,
    venue: str = "BINANCE",
) -> dict[str, Any]:
    """Capture quotes and optionally write a recording file.

    Returns ``{"quotes": [...], "path": str|None, "source": str, "n": int}``.
    """
    mode_l = mode.lower().strip()
    if mode_l == "ws":
        quotes = capture_quotes_ws(symbol, seconds=seconds, testnet=testnet)
        source = "ws-bookTicker"
    elif mode_l == "rest":
        quotes = capture_quotes_rest(symbol, seconds=seconds, interval=interval, testnet=testnet)
        source = "rest-bookTicker-poll"
    else:
        raise ValueError("mode must be 'rest' or 'ws'")

    path: str | None = None
    if out is not None:
        dest = dump_quote_recording(out, quotes, symbol=symbol, venue=venue, source=source)
        path = str(dest)
    return {"quotes": quotes, "path": path, "source": source, "n": len(quotes)}
