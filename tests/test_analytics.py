"""Unit tests for coinext_analytics: trade reconstruction, trade stats, and bias screens.

The trade/bias math is pure stdlib (it consumes already-materialized fills/equity), so these run
without the compiled ``coinext_py`` extension. A separate integration test in
``tests/test_python_backtest.py`` exercises the same code against a real Rust-kernel backtest.
"""

from __future__ import annotations

import pytest
from coinext_analytics.bias import detect_lookahead_bias, detect_overfitting  # noqa: E402
from coinext_analytics.trades import (  # noqa: E402
    compute_trade_stats,
    open_exposure,
    reconstruct_trades,
)

STEP = 60_000_000_000
BASE = 1_700_000_000_000_000_000


def _ts(i: int) -> int:
    return BASE + i * STEP


# --------------------------------------------------------------------------------------------------
# Trade reconstruction (FIFO).
# --------------------------------------------------------------------------------------------------
def test_single_round_trip_long_win():
    # Buy 1 @ 100, sell 1 @ 110 -> one winning long trade, +10 PnL.
    fills = [(_ts(0), +1, 1.0, 100.0), (_ts(1), -1, 1.0, 110.0)]
    trades = reconstruct_trades(fills)
    assert len(trades) == 1
    t = trades[0]
    assert t.side == +1
    assert t.qty == 1.0
    assert t.pnl == pytest.approx(10.0)
    assert t.return_pct == pytest.approx(0.10)
    assert t.is_win


def test_short_round_trip_win():
    # Sell 1 @ 110 (open short), buy 1 @ 100 (cover) -> +10 PnL short.
    fills = [(_ts(0), -1, 1.0, 110.0), (_ts(1), +1, 1.0, 100.0)]
    trades = reconstruct_trades(fills)
    assert len(trades) == 1
    assert trades[0].side == -1
    assert trades[0].pnl == pytest.approx(10.0)


def test_fees_reduce_pnl():
    fills = [(_ts(0), +1, 1.0, 100.0), (_ts(1), -1, 1.0, 110.0)]
    gross = reconstruct_trades(fills, fee_rate=0.0)[0].pnl
    net = reconstruct_trades(fills, fee_rate=0.001)[0].pnl
    # Fee on both legs: (100 + 110) * 1.0 * 0.001 = 0.21.
    assert gross == pytest.approx(10.0)
    assert net == pytest.approx(10.0 - 0.21)


def test_partial_close_and_fifo_order():
    # Buy 2 @ 100, buy 1 @ 104, sell 2 @ 110: FIFO closes the 2@100 lot first.
    fills = [
        (_ts(0), +1, 2.0, 100.0),
        (_ts(1), +1, 1.0, 104.0),
        (_ts(2), -1, 2.0, 110.0),
    ]
    trades = reconstruct_trades(fills)
    assert len(trades) == 1  # only the first (2@100) lot fully closed
    assert trades[0].qty == pytest.approx(2.0)
    assert trades[0].entry_px == pytest.approx(100.0)
    assert trades[0].pnl == pytest.approx(20.0)
    # 1 unit (the 104 lot) remains open.
    assert open_exposure(fills) == (1, pytest.approx(1.0))


def test_position_flip_emits_trade_and_opens_opposite():
    # Long 1 @ 100, then sell 2 @ 120: closes the long (+20) and opens a short of 1.
    fills = [(_ts(0), +1, 1.0, 100.0), (_ts(1), -1, 2.0, 120.0)]
    trades = reconstruct_trades(fills)
    assert len(trades) == 1
    assert trades[0].side == +1
    assert trades[0].pnl == pytest.approx(20.0)
    assert open_exposure(fills) == (-1, pytest.approx(1.0))


# --------------------------------------------------------------------------------------------------
# Trade stats reduction.
# --------------------------------------------------------------------------------------------------
def test_trade_stats_win_rate_and_profit_factor():
    # 3 wins (+10 each via 100->110), 1 loss (-5 via 100->95).
    fills = []
    for i in range(3):
        fills += [(_ts(2 * i), +1, 1.0, 100.0), (_ts(2 * i + 1), -1, 1.0, 110.0)]
    fills += [(_ts(100), +1, 1.0, 100.0), (_ts(101), -1, 1.0, 95.0)]
    stats = compute_trade_stats(reconstruct_trades(fills))
    assert stats.n_trades == 4
    assert stats.n_wins == 3
    assert stats.n_losses == 1
    assert stats.win_rate == pytest.approx(0.75)
    assert stats.gross_profit == pytest.approx(30.0)
    assert stats.gross_loss == pytest.approx(5.0)
    assert stats.profit_factor == pytest.approx(6.0)
    assert stats.avg_trade == pytest.approx((30.0 - 5.0) / 4)
    assert stats.largest_win == pytest.approx(10.0)
    assert stats.largest_loss == pytest.approx(-5.0)


def test_partial_fills_coalesce_into_one_trade():
    # A resting buy that fills as 5 partials of 1.0 @ 100 (consecutive, same side+price), then a
    # single sell of 5.0 @ 110, is ONE round-trip trade of qty 5 — not five trades of qty 1.
    fills = [(_ts(i), +1, 1.0, 100.0) for i in range(5)]  # 5 partial buys @ 100
    fills.append((_ts(10), -1, 5.0, 110.0))  # close the whole 5.0 @ 110
    trades = reconstruct_trades(fills)
    assert len(trades) == 1
    assert trades[0].qty == pytest.approx(5.0)
    assert trades[0].entry_px == pytest.approx(100.0)
    assert trades[0].pnl == pytest.approx(50.0)  # (110-100)*5
    # Distinct prices are NOT coalesced (genuinely separate lots).
    distinct = [(_ts(0), +1, 1.0, 100.0), (_ts(1), +1, 1.0, 101.0), (_ts(2), -1, 2.0, 110.0)]
    assert len(reconstruct_trades(distinct)) == 2


def test_stats_from_result_reconstructs_trades_per_instrument():
    # fills_log rows are (ts, symbol, side, qty, px). Two instruments at very different price scales
    # interleave; instrument-blind FIFO would match an AAA buy against a BBB sell and report an
    # absurd +10000 trade. Per-instrument reconstruction keeps each round-trip at its own scale.
    from dataclasses import dataclass

    from coinext_analytics import stats_from_result

    @dataclass
    class _R:  # minimal stand-in for a coinext_py BacktestResult
        equity_curve: list
        fills_log: list
        starting_equity: float = 100_000.0

    fills_log = [
        (_ts(0), "AAA", +1, 1.0, 100.0),  # AAA long @100
        (_ts(1), "BBB", +1, 1.0, 10_000.0),  # BBB long @10000
        (_ts(2), "BBB", -1, 1.0, 10_100.0),  # close BBB: +100  (blind FIFO -> AAA lot -> +10000!)
        (_ts(3), "AAA", -1, 1.0, 110.0),  # close AAA: +10   (blind FIFO -> BBB lot -> -9890!)
    ]
    eq = [(_ts(i), 100_000.0) for i in range(5)]
    stats = stats_from_result(_R(eq, fills_log))

    assert stats.n_trades == 2
    assert stats.n_wins == 2 and stats.n_losses == 0  # blind matching would give 1W/1L
    assert stats.total_pnl == pytest.approx(110.0)  # PnL is conserved either way
    assert stats.largest_win == pytest.approx(100.0)  # blind matching would be ~10000
    assert stats.largest_win < 1_000.0  # rules out cross-instrument matching


def test_trade_stats_empty_and_all_wins():
    assert compute_trade_stats([]).n_trades == 0
    assert compute_trade_stats([]).profit_factor == 0.0
    fills = [(_ts(0), +1, 1.0, 100.0), (_ts(1), -1, 1.0, 110.0)]
    stats = compute_trade_stats(reconstruct_trades(fills))
    assert stats.profit_factor == float("inf")  # wins, no losses


# --------------------------------------------------------------------------------------------------
# Bias screens.
# --------------------------------------------------------------------------------------------------
def test_lookahead_monotonic_back_compat():
    good = [(_ts(0), 100.0), (_ts(1), 101.0), (_ts(2), 100.5)]
    assert detect_lookahead_bias(good) == []
    bad = [(_ts(2), 100.0), (_ts(0), 101.0)]
    assert detect_lookahead_bias(bad)  # non-monotonic flagged


def test_lookahead_pre_data_and_past_span_fills():
    equity = [(_ts(1), 100.0), (_ts(2), 101.0), (_ts(3), 102.0)]
    bars = [(_ts(1), 100.0), (_ts(2), 101.0), (_ts(3), 102.0)]
    # A fill before the first bar and one well past the last bar + one-bar latency.
    fills = [(_ts(0), +1, 1.0, 100.0), (_ts(10), -1, 1.0, 101.0)]
    warnings = detect_lookahead_bias(equity, fills=fills, bars=bars)
    assert any("precedes first bar" in w for w in warnings)
    assert any("past the data span" in w for w in warnings)


def test_lookahead_latency_offset_fills_are_clean():
    # Fills stamped just AFTER their bar (simulated execution latency) must NOT be flagged.
    equity = [(_ts(1), 100.0), (_ts(2), 101.0), (_ts(3), 102.0)]
    bars = [(_ts(1), 100.0), (_ts(2), 101.0), (_ts(3), 102.0)]
    latency = 1_000_000  # 1ms, like the sim's delayed-fill queue
    clean = [(_ts(1) + latency, +1, 1.0, 100.0), (_ts(3) + latency, -1, 1.0, 102.0)]
    assert detect_lookahead_bias(equity, fills=clean, bars=bars) == []


def test_overfitting_screen_flags_too_good():
    from coinext_analytics import Metrics

    perfect = Metrics(
        total_return=0.5, sharpe=12.0, sortino=20.0, max_drawdown=0.0, volatility=0.1, n_periods=400
    )
    # 10 round-trips, every one a winner -> 100% win rate.
    fills = []
    for i in range(10):
        fills += [(_ts(2 * i), +1, 1.0, 100.0), (_ts(2 * i + 1), -1, 1.0, 110.0)]
    stats = compute_trade_stats(reconstruct_trades(fills))
    warnings = detect_overfitting(perfect, stats)
    assert any("win rate" in w for w in warnings)
    assert any("zero drawdown" in w for w in warnings)


def test_overfitting_screen_silent_on_normal_result():
    from coinext_analytics import Metrics

    normal = Metrics(
        total_return=0.08, sharpe=1.3, sortino=1.8, max_drawdown=0.07, volatility=0.2, n_periods=400
    )
    fills = []
    for i in range(5):
        fills += [(_ts(2 * i), +1, 1.0, 100.0), (_ts(2 * i + 1), -1, 1.0, 110.0)]
    fills += [(_ts(100), +1, 1.0, 100.0), (_ts(101), -1, 1.0, 95.0)]  # a loser
    stats = compute_trade_stats(reconstruct_trades(fills))
    assert detect_overfitting(normal, stats) == []


# --------------------------------------------------------------------------------------------------
# Optional plotting (matplotlib): renders a 3-panel figure to a PNG.
# --------------------------------------------------------------------------------------------------
def test_plot_tear_sheet_writes_png(tmp_path):
    pytest.importorskip("matplotlib", reason="plotting needs the research extra (matplotlib)")
    from dataclasses import dataclass

    from coinext_analytics import plot_tear_sheet

    @dataclass
    class _Result:  # minimal stand-in for a coinext_py BacktestResult
        equity_curve: list
        fills_log: list
        starting_equity: float = 100_000.0

    equity = [(_ts(i), 100_000.0 + 50.0 * i - (i % 7) * 30.0) for i in range(50)]
    # (ts, symbol, side, qty, px)
    fills = [(_ts(1), "AAA", +1, 1.0, 100.0), (_ts(10), "AAA", -1, 1.0, 101.0)]
    out = tmp_path / "tear.png"
    fig = plot_tear_sheet(_Result(equity, fills), path=str(out))
    assert out.exists() and out.stat().st_size > 0
    assert len(fig.axes) == 3
