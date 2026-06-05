"""qv_strategy — the user-facing Strategy API (Python side of the parity invariant).

A Python ``Strategy`` subclass implements synchronous handlers (``on_bar`` …). The Rust core
invokes them through ``qv_py``'s ``PyStrategyAdapter`` (GIL acquired per event) — the SAME engines,
risk gate, and simulated/live execution as a native-Rust strategy. Authoring is identical across
backtest, sandbox, and live.

The handler signature mirrors what the Rust adapter calls:

    def on_bar(self, bar, ctx) -> None: ...

where ``bar`` exposes ``open/high/low/close/ts`` and ``ctx`` exposes ``now``, ``position()`` (signed
size for the instrument), and ``submit_market(side, qty)``.
"""

from __future__ import annotations

from collections import deque
from typing import Protocol


class Bar(Protocol):
    open: float
    high: float
    low: float
    close: float
    ts: int


class Ctx(Protocol):
    now: int

    def position(self) -> float: ...
    def submit_market(self, side: str, qty: float) -> None: ...


class Strategy:
    """Base strategy. Override the handlers you need; defaults are no-ops."""

    def on_start(self) -> None:  # pragma: no cover - lifecycle hook
        pass

    def on_bar(self, bar: Bar, ctx: Ctx) -> None:
        pass

    def on_stop(self) -> None:  # pragma: no cover - lifecycle hook
        pass


class _Sma:
    """Tiny streaming SMA (mirrors qv-indicators::Sma) so the example is self-contained."""

    def __init__(self, period: int) -> None:
        self.period = period
        self.buf: deque[float] = deque(maxlen=period)
        self._sum = 0.0

    def update(self, value: float) -> None:
        if len(self.buf) == self.period:
            self._sum -= self.buf[0]
        self.buf.append(value)
        self._sum += value

    @property
    def value(self) -> float | None:
        if len(self.buf) == self.period:
            return self._sum / self.period
        return None


class SmaCross(Strategy):
    """Classic SMA crossover: long when fast crosses above slow, flat when it crosses back below."""

    def __init__(self, fast: int = 10, slow: int = 30, qty: float = 0.5) -> None:
        self.fast = _Sma(fast)
        self.slow = _Sma(slow)
        self.qty = qty
        self.prev_fast: float | None = None
        self.prev_slow: float | None = None
        self.in_position = False

    def on_bar(self, bar: Bar, ctx: Ctx) -> None:
        self.fast.update(bar.close)
        self.slow.update(bar.close)
        f, s = self.fast.value, self.slow.value
        if f is None or s is None:
            return
        if self.prev_fast is not None and self.prev_slow is not None:
            cross_up = self.prev_fast <= self.prev_slow and f > s
            cross_down = self.prev_fast >= self.prev_slow and f < s
            if cross_up and not self.in_position:
                ctx.submit_market("buy", self.qty)
                self.in_position = True
            elif cross_down and self.in_position:
                ctx.submit_market("sell", self.qty)
                self.in_position = False
        self.prev_fast, self.prev_slow = f, s


__all__ = ["Strategy", "SmaCross", "Bar", "Ctx"]
