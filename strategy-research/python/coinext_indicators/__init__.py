"""coinext_indicators — Python facades over streaming indicators.

Status: verified. See root ARCHITECTURE.md and docs/STATUS.md.
"""

from __future__ import annotations

try:
    import coinext_py  # the maturin-built Rust extension
except ImportError as exc:  # pragma: no cover - surfaced as a clear setup error
    raise ImportError(
        "coinext_py extension not built. Run: "
        "uvx maturin develop --manifest-path foundation/crates/coinext-py/Cargo.toml --features python"
    ) from exc

# Re-export the compiled indicator types under clean names.
Sma = coinext_py.Sma
Ema = coinext_py.Ema
Rsi = coinext_py.Rsi
Atr = coinext_py.Atr
Macd = coinext_py.Macd
Bollinger = coinext_py.Bollinger
Vwap = coinext_py.Vwap


class Resampler:
    """Aggregate a finer bar stream into a coarser timeframe (multi-timeframe research).

    Feed each finer bar via :meth:`update`; it returns a completed coarser ``(ts, open, high, low,
    close, volume)`` tuple every ``factor`` bars (e.g. ``factor=5`` turns 1m bars into 5m), else
    ``None``. The coarse bar's ``ts`` is its LAST finer bar's ts (close time), ``open`` the first
    open, ``high``/``low`` the extremes, ``close`` the last close, ``volume`` the sum. Pure Python —
    a strategy can keep one per timeframe and drive indicators off the emitted coarse bars.

    Example::

        tf5 = Resampler(5)
        sma = Sma(20)
        def on_bar(self, bar, ctx):
            coarse = tf5.update(bar.ts, bar.open, bar.high, bar.low, bar.close, bar.volume)
            if coarse is not None:
                sma.update(coarse[4])  # the 5m close
    """

    def __init__(self, factor: int) -> None:
        if factor <= 0:
            raise ValueError("Resampler factor must be > 0")
        self.factor = factor
        self._n = 0
        self._ts = 0
        self._open = 0.0
        self._high = float("-inf")
        self._low = float("inf")
        self._close = 0.0
        self._volume = 0.0

    def update(
        self, ts: int, open_: float, high: float, low: float, close: float, volume: float = 0.0
    ) -> tuple[int, float, float, float, float, float] | None:
        """Fold one finer bar; return the completed coarse bar every ``factor`` bars (else None)."""
        if self._n == 0:
            self._open = float(open_)
            self._high = float("-inf")
            self._low = float("inf")
            self._volume = 0.0
        self._n += 1
        self._ts = int(ts)
        self._high = max(self._high, float(high))
        self._low = min(self._low, float(low))
        self._close = float(close)
        self._volume += float(volume)
        if self._n >= self.factor:
            bar = (self._ts, self._open, self._high, self._low, self._close, self._volume)
            self._n = 0
            return bar
        return None


__all__ = ["Sma", "Ema", "Rsi", "Atr", "Macd", "Bollinger", "Vwap", "Resampler"]
