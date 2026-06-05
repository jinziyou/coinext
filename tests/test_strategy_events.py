"""The broadened Python Strategy event surface through the Rust kernel.

Exercises the handlers the bar-driven backtest actually fires — on_start/on_stop, on_order_filled,
on_order_event, on_timer — plus ctx.cancel and the cancelable client_order_id returned by
submit_*. Requires the compiled qv_py extension.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PYTHON_ROOT = Path(__file__).resolve().parents[1] / "python"
if str(_PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(_PYTHON_ROOT))

pytest.importorskip("qv_py", reason="build qv_py: uvx maturin develop --features python")

from qv_backtest import run  # noqa: E402
from qv_strategy import Strategy  # noqa: E402

STEP, BASE = 60_000_000_000, 1_700_000_000_000_000_000


def _ohlc(rows):  # rows: (low, high, close) per bar starting at BASE
    return [(BASE + i * STEP, c, h, lo, c) for i, (lo, h, c) in enumerate(rows)]


def test_lifecycle_hooks_fire_in_order():
    class Life(Strategy):
        def __init__(self):
            self.log: list[str] = []

        def on_start(self, ctx):
            self.log.append("start")

        def on_bar(self, bar, ctx):
            self.log.append("bar")

        def on_stop(self, ctx):
            self.log.append("stop")

    s = Life()
    run(s, bars=[(BASE + i * STEP, 100.0) for i in range(3)])
    assert s.log[0] == "start"
    assert s.log[-1] == "stop"
    assert s.log.count("bar") == 3


def test_on_order_filled_and_id_round_trip():
    # submit_market returns the exact client_order_id the fill will carry (deterministic id
    # prediction), and on_order_filled delivers the fill's details.
    class Buyer(Strategy):
        def __init__(self):
            self.submitted_id = None
            self.fills = []
            self.kinds = []
            self.done = False

        def on_bar(self, bar, ctx):
            if not self.done:
                self.done = True
                self.submitted_id = ctx.submit_market("buy", 1.0)

        def on_order_filled(self, fill, ctx):
            self.fills.append(fill)

        def on_order_event(self, event, ctx):
            self.kinds.append(event.kind)

    s = Buyer()
    run(s, bars=[(BASE + i * STEP, 100.0) for i in range(5)])
    assert len(s.fills) == 1
    f = s.fills[0]
    assert f.symbol == "BTCUSDT"
    assert f.side == +1
    assert f.qty == pytest.approx(1.0)
    assert f.price > 0
    assert f.client_order_id == s.submitted_id  # returned id == the fill's id
    assert "submitted" in s.kinds and "accepted" in s.kinds and "filled" in s.kinds


def test_cancel_prevents_a_resting_limit_from_filling():
    # Post a buy limit @95 on bar 0, cancel it on bar 1 (no cross yet); bar 2 dips to 90 (would have
    # crossed) but the order is gone -> zero fills. A no-cancel control fills once.
    class PostCancel(Strategy):
        def __init__(self, cancel: bool):
            self.cancel = cancel
            self.oid = None
            self.fills = 0
            self.n = 0

        def on_bar(self, bar, ctx):
            self.n += 1
            if self.n == 1:
                self.oid = ctx.submit_limit("buy", 1.0, 95.0)
            elif self.n == 2 and self.cancel:
                ctx.cancel(self.oid)

        def on_order_filled(self, fill, ctx):
            self.fills += 1

    flat, dip = (100.0, 100.0, 100.0), (90.0, 101.0, 100.0)  # (low, high, close)
    bars = _ohlc([flat, flat, dip, dip])
    canceled = PostCancel(cancel=True)
    run(canceled, bars=bars)
    assert canceled.fills == 0, "canceled order must not fill"

    control = PostCancel(cancel=False)
    run(control, bars=bars)
    assert control.fills == 1, "without the cancel the same bars fill the limit"


def test_set_timer_fires_on_timer():
    class Timed(Strategy):
        def __init__(self):
            self.armed = False
            self.fires = []

        def on_bar(self, bar, ctx):
            if not self.armed:
                self.armed = True
                ctx.set_timer("tick", ctx.now + 90_000_000_000)  # 1.5 bars ahead

        def on_timer(self, timer, ctx):
            self.fires.append(timer.name)

    s = Timed()
    run(s, bars=[(BASE + i * STEP, 100.0) for i in range(5)])
    assert s.fires == ["tick"]


def test_on_timer_can_submit_orders():
    # A timer handler can place an order (the ctx in on_timer is fully functional).
    class TimerTrader(Strategy):
        def __init__(self):
            self.armed = False
            self.fills = 0

        def on_bar(self, bar, ctx):
            if not self.armed:
                self.armed = True
                ctx.set_timer("buy_now", ctx.now + 90_000_000_000)

        def on_timer(self, timer, ctx):
            ctx.submit_market("buy", 1.0)

        def on_order_filled(self, fill, ctx):
            self.fills += 1

    s = TimerTrader()
    run(s, bars=[(BASE + i * STEP, 100.0) for i in range(6)])
    assert s.fills == 1
