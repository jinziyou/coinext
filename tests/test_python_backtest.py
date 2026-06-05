"""Parity proof: a *Python* Strategy runs through the *Rust* kernel and produces a real backtest.

This is the cross-FFI demonstration of the backtest↔live parity invariant — the same engines, risk
gate, and SimulatedExecutionClient that run a native-Rust strategy run a Python one via
``qv_py``'s ``PyStrategyAdapter`` (GIL acquired per ``on_bar``).
"""

from __future__ import annotations

import pytest

qv_py = pytest.importorskip(
    "qv_py",
    reason="build qv_py first: see crates/qv-py (uvx maturin develop --features python)",
)

from qv_analytics import compute_metrics, detect_lookahead_bias, tear_sheet  # noqa: E402
from qv_backtest import run, synthetic_bars  # noqa: E402
from qv_strategy import SmaCross  # noqa: E402


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
    assert "VeloxQuant tear sheet" in tear_sheet(res)


def test_backtest_is_deterministic():
    bars = synthetic_bars(200)
    a = run(SmaCross(5, 20, 0.3), bars=bars)
    b = run(SmaCross(5, 20, 0.3), bars=bars)
    assert list(a.equity_curve) == list(b.equity_curve)
    assert a.final_equity == b.final_equity
    assert a.fills == b.fills


def test_long_position_tracks_price_up():
    # Buy-and-hold on a monotonically rising price -> positive unrealized PnL at the end.
    from qv_strategy import Strategy

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
