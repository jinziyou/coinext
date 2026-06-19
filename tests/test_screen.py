"""The fast vectorized research screen (coinext_screen) and its advisory cross-check.

The vectorized math is pure numpy (no coinext_py); the cross_check_vs_event integration drives the
AUTHORITATIVE event-driven runner and so needs the compiled extension (importorskip).
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

_PYTHON_ROOT = Path(__file__).resolve().parents[1] / "python"
if str(_PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(_PYTHON_ROOT))

from coinext_screen import (  # noqa: E402
    sma,
    sma_cross_positions,
    sweep_sma_cross,
    to_session,
    vector_backtest,
)


def test_sma_matches_reference():
    v = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    out = sma(v, 3)
    assert np.isnan(out[0]) and np.isnan(out[1])
    np.testing.assert_allclose(out[2:], [2.0, 3.0, 4.0])
    # period 1 is the identity.
    np.testing.assert_allclose(sma(v, 1), v)


def test_sma_cross_positions_enters_only_on_a_real_up_cross():
    # A dip-then-rise series produces a genuine up-cross at bar 4 -> long from there (stateful).
    closes = np.array([4.0, 3.0, 2.0, 3.0, 4.0, 5.0])
    pos = sma_cross_positions(closes, fast=2, slow=3, qty=1.0)
    np.testing.assert_allclose(pos, [0.0, 0.0, 0.0, 0.0, 1.0, 1.0])


def test_sma_cross_positions_flat_when_fast_starts_above_slow():
    # Strictly rising from the start: the fast SMA is already above the slow at the first warm bar,
    # so there is NO up-cross to observe -> flat throughout, like SmaCross (NOT a naive f>s level).
    closes = np.array([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    pos = sma_cross_positions(closes, fast=2, slow=3, qty=1.0)
    np.testing.assert_allclose(pos, np.zeros(6))


def test_vector_backtest_pnl_and_fills():
    bars = [(0, 100.0), (1, 110.0), (2, 105.0)]
    positions = np.array([1.0, 1.0, 0.0])  # long at bar0, hold, flat at bar2
    res = vector_backtest(bars, positions, starting_balance=100_000.0, fee_rate=0.0)
    assert res.equity_curve == [(0, 100_000.0), (1, 100_010.0), (2, 100_005.0)]
    assert res.fills == [(0, +1, 1.0, 100.0), (2, -1, 1.0, 105.0)]
    assert res.final_equity == pytest.approx(100_005.0)
    assert res.total_return == pytest.approx(5.0 / 100_000.0)


def test_vector_backtest_charges_fees_on_changes():
    bars = [(0, 100.0), (1, 100.0), (2, 100.0)]
    positions = np.array([1.0, 1.0, 0.0])  # open 1 @100, close 1 @100
    res = vector_backtest(bars, positions, fee_rate=0.001)
    # No price move -> only fees: open 100*1*0.001 + close 100*1*0.001 = 0.20.
    assert res.final_equity == pytest.approx(100_000.0 - 0.20)


def test_sweep_ranks_and_skips_invalid_grid():
    bars = [(i, 100.0 + (i % 20) - (i % 7)) for i in range(300)]
    rows = sweep_sma_cross(bars, fasts=[2, 5], slows=[3, 10], qty=0.5)
    # (2,3),(2,10),(5,10) are valid; (5,3) is skipped (fast >= slow).
    assert len(rows) == 3
    assert all(r.params["fast"] < r.params["slow"] for r in rows)
    # Sorted by sharpe descending.
    assert rows == sorted(rows, key=lambda r: r.sharpe, reverse=True)


def test_to_session_shape():
    bars = [(i, 100.0 + i) for i in range(10)]
    res = vector_backtest(bars, sma_cross_positions(np.array([b[1] for b in bars]), 2, 4, 0.5))
    session = to_session(res)
    assert session.equity_curve == res.equity_curve
    assert session.fills == res.fills


# --------------------------------------------------------------------------------------------------
# Integration: cross-check the screen against the AUTHORITATIVE event-driven runner.
# --------------------------------------------------------------------------------------------------
def test_cross_check_vs_event_runs_and_is_advisory():
    pytest.importorskip("coinext_py", reason="build coinext_py: uvx maturin develop --features python")
    import coinext_backtest
    from coinext_screen import cross_check_vs_event

    bars = coinext_backtest.synthetic_bars(400)
    warnings = cross_check_vs_event(bars, fast=10, slow=30, qty=0.5)
    # cross_check never raises; it returns advisory warning strings (possibly empty).
    assert isinstance(warnings, list)
    assert all(isinstance(w, str) for w in warnings)


def test_cross_check_aligns_signals_for_realistic_minute_end_bars():
    # Real Binance bars close at :59.999; the event fill at bar_ts + latency crosses the minute
    # boundary while the vector fill sits at bar_ts. Snapping to the bar grid restores signal
    # agreement, so no "signal-timing drift" warning remains (only the expected PnL/return drift).
    pytest.importorskip("coinext_py", reason="build coinext_py: uvx maturin develop --features python")
    import coinext_backtest
    from coinext_screen import cross_check_vs_event

    base, step = 1_700_000_000_000_000_000, 60_000_000_000
    close_offset = step - 1_000_000  # :59.999, like a real kline close time
    syn = coinext_backtest.synthetic_bars(400)
    bars = [(base + i * step + close_offset, c) for i, (_, c) in enumerate(syn)]

    warnings = cross_check_vs_event(bars, fast=10, slow=30, qty=0.5)
    assert not any("signal-timing drift" in w for w in warnings), warnings


def test_screen_signals_match_authoritative_sma_cross():
    # The faithful stateful proxy must enter/exit on the SAME bars as the event-driven SmaCross.
    pytest.importorskip("coinext_py", reason="build coinext_py: uvx maturin develop --features python")
    import coinext_backtest
    from coinext_screen import sma_cross_positions, vector_backtest
    from coinext_strategy import SmaCross

    bars = coinext_backtest.synthetic_bars(400)
    closes = np.array([c for _, c in bars])
    vec = vector_backtest(bars, sma_cross_positions(closes, 10, 30, 0.5))
    event = coinext_backtest.run(SmaCross(10, 30, 0.5), bars=bars)

    # Compare the set of (bar-bucket, side) signals — they must agree (fills count + bars).
    step = 60_000_000_000
    vec_sig = {(t // step, side) for t, side, _q, _p in vec.fills}
    ev_sig = {(t // step, side) for t, _sym, side, _q, _p in event.fills_log}
    assert vec_sig == ev_sig
    assert len(vec.fills) == event.fills
