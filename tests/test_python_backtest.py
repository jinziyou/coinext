"""Parity proof: a *Python* Strategy runs through the *Rust* kernel and produces a real backtest.

This is the cross-FFI demonstration of the backtest↔live parity invariant — the same engines, risk
gate, and SimulatedExecutionClient that run a native-Rust strategy run a Python one via
``coinext_py``'s ``PyStrategyAdapter`` (GIL acquired per ``on_bar``).
"""

from __future__ import annotations

import pytest

coinext_py = pytest.importorskip(
    "coinext_py",
    reason="build coinext_py first: see crates/coinext-py (uvx maturin develop --features python)",
)

from coinext_analytics import (  # noqa: E402
    compute_metrics,
    detect_lookahead_bias,
    screen_biases,
    stats_from_result,
    tear_sheet,
)
from coinext_backtest import run, synthetic_bars  # noqa: E402
from coinext_strategy import SmaCross  # noqa: E402


def test_python_strategy_runs_through_rust_kernel():
    bars = synthetic_bars(400)
    res = run(SmaCross(10, 30, 0.5), bars=bars)

    assert res.orders_submitted > 0
    assert res.fills == res.orders_submitted  # market orders fill fully in the sim
    assert res.orders_denied == 0
    assert len(res.equity_curve) == len(bars)
    assert res.starting_equity == pytest.approx(100_000.0)

    m = compute_metrics(list(res.equity_curve))
    assert -1.0 < m.total_return < 5.0
    assert m.max_drawdown >= 0.0
    assert detect_lookahead_bias(list(res.equity_curve)) == []
    assert "Coinext tear sheet" in tear_sheet(res, bars=bars)


def test_trade_stats_from_real_backtest():
    # An SmaCross over a sine+trend series opens and closes positions -> closed round-trips.
    bars = synthetic_bars(400)
    res = run(SmaCross(10, 30, 0.5), bars=bars)
    stats = stats_from_result(res)

    assert stats.n_trades >= 1
    assert stats.n_wins + stats.n_losses <= stats.n_trades
    assert 0.0 <= stats.win_rate <= 1.0
    assert 0.0 <= stats.exposure <= 1.0
    assert stats.turnover > 0.0  # it traded, so notional moved
    # Gross trade PnL should track the engine's realized PnL within fees (engine charges taker fee).
    tol = abs(res.realized_pnl) * 0.5 + 50.0
    assert stats.total_pnl == pytest.approx(res.realized_pnl, abs=tol)


def test_bias_screen_on_real_backtest_is_clean():
    bars = synthetic_bars(400)
    res = run(SmaCross(10, 30, 0.5), bars=bars)
    report = screen_biases(res, bars=bars)
    # A legitimate event-driven backtest on the real (non-leaky) kernel: no structural look-ahead.
    assert report.lookahead == []


def test_walk_forward_optimize_through_real_backtest():
    # Grid walk-forward over the AUTHORITATIVE Rust backtest: optimize fast/slow in-sample per fold,
    # validate out-of-sample, and confirm the report is well-formed with a finite OOS estimate.
    from coinext_analytics import compute_metrics
    from coinext_optimize import walk_forward_optimize

    bars = synthetic_bars(1200)

    def objective(params, window):
        if params["fast"] >= params["slow"] or len(window) < 2:
            return float("-inf")
        res = run(SmaCross(**params), bars=window)
        return compute_metrics(list(res.equity_curve)).sharpe

    report = walk_forward_optimize(
        bars,
        objective,
        param_grid={"fast": [5, 10, 15], "slow": [25, 40]},
        n_splits=3,
        optimizer="grid",
    )
    assert len(report.folds) == 3
    assert report.chosen_params["fast"] < report.chosen_params["slow"]
    assert all(f.params["fast"] < f.params["slow"] for f in report.folds)
    # The OOS mean is a real number (the series trades on every test window of this length).
    assert report.oos_mean == pytest.approx(report.oos_mean)  # not NaN
    assert "walk-forward" in report.render()


def test_coinext_kernel_run_backtest_wrapper_runs():
    # The coinext_kernel convenience wrapper must delegate to coinext_backtest.run (bar normalization +
    # symbol/venue/balance defaults), not call the native 6-wide pyfunction with raw 2-tuples.
    import coinext_kernel

    res = coinext_kernel.run_backtest(SmaCross(10, 30, 0.5), bars=synthetic_bars(200))
    assert res.orders_submitted > 0
    assert res.starting_equity == pytest.approx(100_000.0)


def test_backtest_is_deterministic():
    bars = synthetic_bars(200)
    a = run(SmaCross(5, 20, 0.3), bars=bars)
    b = run(SmaCross(5, 20, 0.3), bars=bars)
    assert list(a.equity_curve) == list(b.equity_curve)
    assert a.final_equity == b.final_equity
    assert a.fills == b.fills


def test_ohlc_limit_fills_on_intrabar_wick_not_close():
    # The OHLC-aware proof: a resting buy limit @ 95 fills only because a later bar's LOW wicks to
    # 94 — its close never leaves 100. A close-only series (high==low==close) misses the touch.
    from coinext_strategy import Strategy

    class LimitOnce(Strategy):
        def __init__(self):
            self.done = False

        def on_bar(self, bar, ctx):
            if not self.done:
                self.done = True
                ctx.submit_limit("buy", 1.0, 95.0)

    step, base = 60_000_000_000, 1_700_000_000_000_000_000
    ohlc = [
        (base + 0 * step, 100.0, 100.0, 100.0, 100.0),
        (base + 1 * step, 100.0, 101.0, 94.0, 100.0),  # low wicks below the 95 limit
        (base + 2 * step, 100.0, 101.0, 99.0, 100.0),
    ]
    close_only = [(base + i * step, 100.0) for i in range(3)]

    assert run(LimitOnce(), bars=ohlc).fills == 1
    assert run(LimitOnce(), bars=close_only).fills == 0


def test_large_limit_partial_fills_over_bars_via_bridge():
    # A resting buy limit of qty 5.0 vs volume-4.0 bars at participation 0.25 fills 1.0 per bar,
    # so ONE order produces FIVE partial fills over five bars (the volume-participation model).
    from coinext_strategy import Strategy

    class BigLimitOnce(Strategy):
        def __init__(self):
            self.done = False

        def on_bar(self, bar, ctx):
            if not self.done:
                self.done = True
                ctx.submit_limit("buy", 5.0, 95.0)

    step, base = 60_000_000_000, 1_700_000_000_000_000_000
    bars = [(base, 100.0, 100.0, 100.0, 100.0, 4.0)]  # submit bar: no cross (low=100 > 95)
    for i in range(1, 8):  # crossing bars (low 94 <= 95), volume 4 -> cap 1.0/bar
        bars.append((base + i * step, 100.0, 101.0, 94.0, 100.0, 4.0))

    res = run(BigLimitOnce(), bars=bars)
    assert res.orders_submitted == 1
    assert res.fills == 5  # qty 5.0 / (0.25 * 4.0 = 1.0 per bar) = 5 partial fills


def test_queue_position_delays_a_touched_limit():
    # With queue_ahead_factor > 0, a buy limit the price only TOUCHES (bar low == limit) waits
    # behind the queue, so it fills LATER than with the queue off. (A price THROUGH still fills.)
    from coinext_strategy import Strategy

    class LimitOnce(Strategy):
        def __init__(self):
            self.done = False

        def on_bar(self, bar, ctx):
            if not self.done:
                self.done = True
                ctx.submit_limit("buy", 0.5, 95.0)

    step, base = 60_000_000_000, 1_700_000_000_000_000_000
    # Bar 0 posts the limit @95 (close 100). Three TOUCH bars (low == 95) with volume 4.
    bars = [(base, 100.0, 100.0, 100.0, 100.0, 4.0)]
    for i in range(1, 4):
        bars.append((base + i * step, 100.0, 100.0, 95.0, 100.0, 4.0))  # low == limit -> touch

    no_queue = run(LimitOnce(), bars=bars, queue_ahead_factor=0.0)
    queued = run(LimitOnce(), bars=bars, queue_ahead_factor=0.5)
    # Queue off: fills on the first touch. Queue on: queue_ahead = 0.5*4 = 2.0, paid down by the 0.5
    # participation share each bar; 3 touch bars only pay it to 0.5 -> the order never fills here.
    assert no_queue.fills == 1
    assert queued.fills == 0


def test_large_market_order_participates_over_bars():
    # A single market BUY of qty 5.0 against volume-4 bars (participation 0.25 -> 1.0/bar) takes 1.0
    # at submit and fills the aggressive remainder 1.0 per later bar: ONE order, FIVE fills.
    from coinext_strategy import Strategy

    class BigMarketOnce(Strategy):
        def __init__(self):
            self.done = False

        def on_bar(self, bar, ctx):
            if not self.done:
                self.done = True
                ctx.submit_market("buy", 5.0)

    step, base = 60_000_000_000, 1_700_000_000_000_000_000
    bars = [(base + i * step, 100.0, 100.5, 99.5, 100.0, 4.0) for i in range(8)]  # OHLCV, vol 4
    res = run(BigMarketOnce(), bars=bars)
    assert res.orders_submitted == 1
    assert res.fills == 5  # qty 5.0 / (0.25 * 4.0 = 1.0 per bar)
    assert res.orders_denied == 0


def test_market_order_fills_fully_without_volume():
    # Backward compat: a close-only series carries no volume -> market orders fill in one shot.
    from coinext_strategy import Strategy

    class BuyOnce(Strategy):
        def __init__(self):
            self.done = False

        def on_bar(self, bar, ctx):
            if not self.done:
                self.done = True
                ctx.submit_market("buy", 5.0)

    base, step = 1_700_000_000_000_000_000, 60_000_000_000
    res = run(BuyOnce(), bars=[(base + i * step, 100.0) for i in range(4)])
    assert res.orders_submitted == 1
    assert res.fills == 1  # no volume -> no participation cap -> single full fill


def test_pybar_exposes_threaded_volume():
    from coinext_strategy import Strategy

    class VolRecorder(Strategy):
        def __init__(self):
            self.vols: list[float] = []

        def on_bar(self, bar, ctx):
            self.vols.append(bar.volume)

    step, base = 60_000_000_000, 1_700_000_000_000_000_000
    bars = [(base + i * step, 100.0, 101.0, 99.0, 100.0, 73.0) for i in range(3)]
    rec = VolRecorder()
    run(rec, bars=bars)
    assert rec.vols == [73.0, 73.0, 73.0]  # real volume reaches the strategy


def test_limit_maker_trades_on_synthetic_ohlc():
    from coinext_backtest import synthetic_ohlc_bars
    from coinext_strategy import LimitMaker

    bars = synthetic_ohlc_bars(400, wick=0.004)
    res = run(LimitMaker(dip_bps=30, rise_bps=30, qty=0.1), bars=bars)
    assert res.orders_submitted > 0
    assert res.fills > 0  # the resting limits fill on intrabar wicks
    # Only one order is outstanding at a time -> no runaway pile-up of denials.
    assert res.orders_denied == 0
    # The tear sheet accepts OHLC bars (the look-ahead screen must take row[0] regardless of width).
    assert "Coinext tear sheet" in tear_sheet(res, bars=bars)


def test_to_ohlcv_normalization_and_validation():
    from coinext_backtest import _to_ohlcv

    # close-only and OHLC default volume to 0 (no participation cap); OHLCV carries real volume.
    assert _to_ohlcv([(1, 100.0)]) == [(1, 100.0, 100.0, 100.0, 100.0, 0.0)]
    assert _to_ohlcv([(1, 99.0, 101.0, 98.0, 100.0)]) == [(1, 99.0, 101.0, 98.0, 100.0, 0.0)]
    assert _to_ohlcv([(1, 99.0, 101.0, 98.0, 100.0, 42.0)]) == [(1, 99.0, 101.0, 98.0, 100.0, 42.0)]
    with pytest.raises(ValueError):
        _to_ohlcv([(1, 2, 3)])  # 3-column rows match no accepted shape


def test_long_position_tracks_price_up():
    # Buy-and-hold on a monotonically rising price -> positive unrealized PnL at the end.
    from coinext_strategy import Strategy

    bars = [(i * 60_000_000_000, 50_000.0 + i * 100.0) for i in range(60)]

    class BuyAndHold(Strategy):
        def __init__(self):
            self.bought = False

        def on_bar(self, bar, ctx):
            if not self.bought:
                self.bought = True
                ctx.submit_market("buy", 1.0)

    res = run(BuyAndHold(), bars=bars)
    assert res.fills == 1
    # Held 1 unit from ~50000 to ~55900 -> equity well above start (minus fees).
    assert res.final_equity > res.starting_equity
