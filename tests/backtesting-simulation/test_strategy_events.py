"""The broadened Python Strategy event surface through the Rust kernel.

Exercises the handlers the bar-driven backtest actually fires — on_start/on_stop, on_order_filled,
on_order_event, on_timer — plus ctx.cancel and the cancelable client_order_id returned by
submit_*. Requires the compiled coinext_py extension.
"""

from __future__ import annotations

import pytest

pytest.importorskip("coinext_py", reason="build coinext_py: uvx maturin develop --features python")

from coinext_backtest import run  # noqa: E402
from coinext_strategy import Strategy  # noqa: E402

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


def test_invalid_submit_raises_and_keeps_id_prediction_aligned():
    # A submit the instrument precision can't represent (NaN qty) must RAISE — not silently drop on
    # replay, which would desync every later predicted client_order_id in the handler. After the
    # rejected submit, the next valid submit still gets the id its fill actually carries.
    class TryBad(Strategy):
        def __init__(self):
            self.raised = False
            self.good_id = None
            self.fills = []

        def on_bar(self, bar, ctx):
            if self.good_id is None:
                try:
                    ctx.submit_market("buy", float("nan"))  # invalid -> ValueError, no id consumed
                except ValueError:
                    self.raised = True
                self.good_id = ctx.submit_market("buy", 1.0)  # gets the FIRST seq, as predicted

        def on_order_filled(self, fill, ctx):
            self.fills.append(fill.client_order_id)

    s = TryBad()
    run(s, bars=[(BASE + i * STEP, 100.0) for i in range(4)])
    assert s.raised
    assert s.fills and s.fills[0] == s.good_id  # fill's id == predicted id (no desync)


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


def test_stop_market_fires_on_breakout():
    # A buy stop @105 rests while the market is below it, then fills when a bar's high breaks above.
    class StopEntry(Strategy):
        def __init__(self, trigger: float):
            self.trigger = trigger
            self.done = False
            self.fills = 0

        def on_bar(self, bar, ctx):
            if not self.done:
                self.done = True
                ctx.submit_stop("buy", 1.0, self.trigger)

        def on_order_filled(self, fill, ctx):
            self.fills += 1

    # bar0 posts the stop; bar1 high 103 (no break); bar2 high 106 breaks 105 -> fills.
    bars = _ohlc([(100, 100, 100), (100, 103, 102), (102, 106, 105), (104, 107, 106)])
    fired = StopEntry(105.0)
    run(fired, bars=bars)
    assert fired.fills == 1

    never = StopEntry(200.0)  # trigger never reached
    run(never, bars=bars)
    assert never.fills == 0


def test_stop_limit_fills_at_its_limit_after_trigger():
    # A sell stop-limit (trigger 99, limit 98): on the trigger it becomes a sell limit @98 and fills
    # only when the price comes back up to 98 — never below it (bounded slippage vs a plain stop).
    class StopLimitOnce(Strategy):
        def __init__(self, trigger, limit):
            self.trigger = trigger
            self.limit = limit
            self.done = False
            self.fills = 0
            self.px = None

        def on_bar(self, bar, ctx):
            if not self.done:
                self.done = True
                ctx.submit_stop_limit("sell", 1.0, self.trigger, self.limit)

        def on_order_filled(self, fill, ctx):
            self.fills += 1
            self.px = fill.price

    # bar0 posts; bar1 low 97 crosses the 99 stop (-> sell limit @98), high 97.5 < 98 so no fill;
    # bar2 high 99 >= 98 -> the limit fills at 98.
    bars = _ohlc([(100, 100, 100), (97.5, 97.5, 97), (98, 99, 98.5), (98, 99, 98.5)])
    s = StopLimitOnce(99.0, 98.0)
    run(s, bars=bars)
    assert s.fills == 1
    assert s.px == pytest.approx(98.0)


def test_trailing_stop_ratchets_up_then_fires_on_pullback():
    # A sell trailing stop (offset 3) trails a rising market, then fires when the price pulls back
    # past the offset — locking in a price ABOVE the entry (proof the stop ratcheted up).
    class TrailOnce(Strategy):
        def __init__(self):
            self.done = False
            self.fills = 0
            self.px = None

        def on_bar(self, bar, ctx):
            if not self.done:
                self.done = True
                ctx.submit_trailing("sell", 1.0, 3.0)  # initial stop = 100 - 3 = 97

        def on_order_filled(self, fill, ctx):
            self.fills += 1
            self.px = fill.price

    # mark starts ~100 (bar0). bar1 runs to 110 (stop trails to 107); bar2 holds; bar3 pulls back to
    # 106 < 107 -> fires near 107.
    bars = _ohlc([(100, 100, 100), (104, 110, 109), (108, 110, 109), (106, 109, 106)])
    s = TrailOnce()
    run(s, bars=bars)
    assert s.fills == 1
    assert s.px > 100.0  # ratcheted well above the entry, not the initial 97 stop


def test_trailing_stop_rejects_nonpositive_offset():
    # A 0 offset would degrade to a static stop at the mark -> reject it at submit (like other bad
    # args), and the rejection must not desync the next predicted client_order_id.
    class BadThenGood(Strategy):
        def __init__(self):
            self.raised = False
            self.fills = 0

        def on_bar(self, bar, ctx):
            if not self.raised:
                try:
                    ctx.submit_trailing("sell", 1.0, 0.0)  # invalid -> ValueError, no id consumed
                except ValueError:
                    self.raised = True

        def on_order_filled(self, fill, ctx):
            self.fills += 1

    s = BadThenGood()
    run(s, bars=[(BASE + i * STEP, 100.0) for i in range(3)])
    assert s.raised
    assert s.fills == 0  # nothing rested -> nothing fired


def test_stop_can_be_canceled_before_it_triggers():
    class StopThenCancel(Strategy):
        def __init__(self):
            self.oid = None
            self.n = 0
            self.fills = 0

        def on_bar(self, bar, ctx):
            self.n += 1
            if self.n == 1:
                self.oid = ctx.submit_stop("buy", 1.0, 105.0)
            elif self.n == 2:
                ctx.cancel(self.oid)  # cancel before the breakout bar

        def on_order_filled(self, fill, ctx):
            self.fills += 1

    # bar3 would break 105, but the stop was canceled on bar 2.
    bars = _ohlc([(100, 100, 100), (100, 102, 101), (101, 106, 105), (104, 107, 106)])
    s = StopThenCancel()
    run(s, bars=bars)
    assert s.fills == 0


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
