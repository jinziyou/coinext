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
    symbol: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    ts: int


class Quote(Protocol):
    symbol: str
    bid: float
    ask: float
    bid_size: float
    ask_size: float
    ts: int


class Trade(Protocol):
    symbol: str
    price: float
    size: float
    ts: int


class Fill(Protocol):
    symbol: str
    side: int  # +1 buy / -1 sell
    qty: float
    price: float
    client_order_id: str


class OrderEvent(Protocol):
    kind: str  # submitted/accepted/partially_filled/filled/denied/rejected/canceled/expired/...
    reason: str | None


class Timer(Protocol):
    name: str
    ts: int


class Ctx(Protocol):
    now: int

    def position(self, symbol: str | None = None) -> float: ...
    def submit_market(self, side: str, qty: float, symbol: str | None = None) -> str: ...
    def submit_limit(
        self, side: str, qty: float, price: float, symbol: str | None = None
    ) -> str: ...
    def submit_stop(
        self, side: str, qty: float, trigger: float, symbol: str | None = None
    ) -> str: ...
    def cancel(self, client_order_id: str) -> None: ...
    def set_timer(self, name: str, at: int) -> None: ...


class Strategy:
    """Base strategy. Override the handlers you need; defaults are no-ops.

    Every handler receives ``ctx`` (the platform surface: ``now``, ``position()``,
    ``submit_market``/``submit_limit`` which return a cancelable client_order_id, ``cancel``,
    ``set_timer``). ``on_quote``/``on_trade`` fire only when the feed provides quotes/trades (a
    bar-only backtest does not emit them); ``on_order_filled``/``on_order_event`` fire on the
    strategy's own orders; ``on_timer`` fires for timers armed via ``ctx.set_timer``.
    """

    def on_start(self, ctx: Ctx) -> None:  # pragma: no cover - lifecycle hook
        pass

    def on_bar(self, bar: Bar, ctx: Ctx) -> None:
        pass

    def on_quote(self, quote: Quote, ctx: Ctx) -> None:  # pragma: no cover - feed-dependent
        pass

    def on_trade(self, trade: Trade, ctx: Ctx) -> None:  # pragma: no cover - feed-dependent
        pass

    def on_order_filled(self, fill: Fill, ctx: Ctx) -> None:
        pass

    def on_order_event(self, event: OrderEvent, ctx: Ctx) -> None:
        pass

    def on_timer(self, timer: Timer, ctx: Ctx) -> None:
        pass

    def on_stop(self, ctx: Ctx) -> None:  # pragma: no cover - lifecycle hook
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


class LimitMaker(Strategy):
    """A single-order-at-a-time maker that rests LIMIT orders (the OHLC-aware path).

    When flat it rests a buy limit ``dip_bps`` below the close; once filled (position turns long) it
    rests a sell limit ``rise_bps`` above the close; on exit it cycles back. Exactly one order is
    outstanding at a time (guarded by the ``pending_*`` flags — no cancel API is needed and orders
    never pile up). Because the orders REST, they fill on a later bar whose low/high wicks across
    the price even if that bar's close does not — which OHLC-aware fills capture and close-only
    series miss.
    """

    def __init__(self, dip_bps: float = 20.0, rise_bps: float = 20.0, qty: float = 0.1) -> None:
        self.dip_bps = dip_bps
        self.rise_bps = rise_bps
        self.qty = qty
        self.pending_buy = False
        self.pending_sell = False

    def on_bar(self, bar: Bar, ctx: Ctx) -> None:
        if ctx.position() > 0.0:  # long -> the buy filled; rest the exit once
            self.pending_buy = False
            if not self.pending_sell:
                ctx.submit_limit("sell", self.qty, bar.close * (1.0 + self.rise_bps / 1e4))
                self.pending_sell = True
        else:  # flat -> rest an entry once
            self.pending_sell = False
            if not self.pending_buy:
                ctx.submit_limit("buy", self.qty, bar.close * (1.0 - self.dip_bps / 1e4))
                self.pending_buy = True


class RsiReversion(Strategy):
    """RSI mean-reversion: go long when RSI dips below ``low``, flat when it rises above ``high``.

    Uses the SHARED Rust ``qv_indicators.Rsi`` — the identical incremental implementation the
    native-Rust path and live warm-up use, not a re-rolled Python copy. The import is lazy (in
    ``__init__``) so ``qv_strategy`` still imports without the compiled ``qv_py`` present.
    """

    def __init__(
        self, period: int = 14, low: float = 30.0, high: float = 70.0, qty: float = 0.5
    ) -> None:
        from qv_indicators import Rsi

        self.rsi = Rsi(period)
        self.low = low
        self.high = high
        self.qty = qty
        self.in_pos = False

    def on_bar(self, bar: Bar, ctx: Ctx) -> None:
        self.rsi.update(bar.close)
        v = self.rsi.value()
        if v is None:
            return
        if v < self.low and not self.in_pos:
            ctx.submit_market("buy", self.qty)
            self.in_pos = True
        elif v > self.high and self.in_pos:
            ctx.submit_market("sell", self.qty)
            self.in_pos = False


class MultiSma(Strategy):
    """Per-symbol SMA crossover across MANY instruments through one kernel (``run_multi``).

    Keeps independent fast/slow SMAs and an in-position flag per ``bar.symbol``, and targets each
    order at that symbol via the ``symbol`` arg on ``ctx.submit_market``. Demonstrates that one
    strategy/engine pair runs a whole portfolio — positions, signals, and orders stay isolated per
    instrument (the same parity-valid path as the single-instrument case).
    """

    def __init__(self, fast: int = 10, slow: int = 30, qty: float = 0.5) -> None:
        self.fast_n = fast
        self.slow_n = slow
        self.qty = qty
        self._state: dict[str, dict] = {}

    def on_bar(self, bar: Bar, ctx: Ctx) -> None:
        st = self._state.get(bar.symbol)
        if st is None:
            st = {
                "fast": _Sma(self.fast_n),
                "slow": _Sma(self.slow_n),
                "prev_fast": None,
                "prev_slow": None,
                "in_pos": False,
            }
            self._state[bar.symbol] = st
        st["fast"].update(bar.close)
        st["slow"].update(bar.close)
        f, s = st["fast"].value, st["slow"].value
        if f is None or s is None:
            return
        pf, ps = st["prev_fast"], st["prev_slow"]
        if pf is not None and ps is not None:
            cross_up = pf <= ps and f > s
            cross_down = pf >= ps and f < s
            if cross_up and not st["in_pos"]:
                ctx.submit_market("buy", self.qty, bar.symbol)
                st["in_pos"] = True
            elif cross_down and st["in_pos"]:
                ctx.submit_market("sell", self.qty, bar.symbol)
                st["in_pos"] = False
        st["prev_fast"], st["prev_slow"] = f, s


__all__ = [
    "Strategy",
    "SmaCross",
    "LimitMaker",
    "MultiSma",
    "RsiReversion",
    "Bar",
    "Quote",
    "Trade",
    "Fill",
    "OrderEvent",
    "Timer",
    "Ctx",
]
