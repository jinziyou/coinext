"""Pure-logic tests for the coinext_parity promotion gate and advisory cross-check.

These exercise the metric math and the gate decision over fixtures (no network / no testnet). The
end-to-end ``run_gate`` test drives the AUTHORITATIVE ``coinext_backtest.run`` through the Rust kernel,
so it requires the compiled ``coinext_py`` extension (``importorskip``).
"""

from __future__ import annotations

import pytest

# The gate's run_gate path needs the compiled kernel; skip the whole module if it isn't built.
pytest.importorskip("coinext_py")

import coinext_backtest  # noqa: E402
from coinext_parity import (  # noqa: E402
    AcceptanceCriterion,
    SessionResult,
    cross_check,
    evaluate,
    parity_metrics,
    render_verdict,
    run_gate,
)
from coinext_strategy import SmaCross  # noqa: E402


# --------------------------------------------------------------------------------------------------
# Fixtures / helpers.
# --------------------------------------------------------------------------------------------------
def _make_session() -> SessionResult:
    """A small deterministic session: 6 equity points + 3 fills across distinct minute buckets."""
    step = 60_000_000_000
    base = 1_700_000_000_000_000_000
    equity = [
        (base + 0 * step, 100_000.0),
        (base + 1 * step, 100_050.0),
        (base + 2 * step, 100_010.0),
        (base + 3 * step, 100_120.0),
        (base + 4 * step, 100_090.0),
        (base + 5 * step, 100_200.0),
    ]
    fills = [
        (base + 1 * step + 1, +1, 0.5, 50_000.0),
        (base + 3 * step + 1, -1, 0.5, 50_100.0),
        (base + 5 * step + 1, +1, 0.5, 50_050.0),
    ]
    return SessionResult(equity_curve=equity, fills=fills)


def _perturb_fills_bps(session: SessionResult, bps: float) -> list:
    """Return ``session.fills`` with every price scaled up by ``bps`` basis points."""
    factor = 1.0 + bps / 1e4
    return [(ts, side, qty, px * factor) for (ts, side, qty, px) in session.fills]


def _perturb_equity_noise(session: SessionResult, eps: float) -> list:
    """Return ``session.equity_curve`` with a tiny deterministic multiplicative wobble."""
    out = []
    for i, (ts, eq) in enumerate(session.equity_curve):
        wobble = 1.0 + eps * (1.0 if i % 2 == 0 else -1.0)
        out.append((ts, eq * wobble))
    return out


# --------------------------------------------------------------------------------------------------
# 1. Identical sessions -> PASS, perfect agreement, 0 bps deviation, corr ~1.0.
# --------------------------------------------------------------------------------------------------
def test_identical_sessions_pass():
    session = _make_session()
    metrics = parity_metrics(session, session)

    assert metrics.signal_timing_agreement == pytest.approx(1.0)
    assert metrics.fill_price_deviation_bps == pytest.approx(0.0)
    assert metrics.equity_correlation == pytest.approx(1.0)
    assert metrics.return_diff == pytest.approx(0.0)

    verdict = evaluate(metrics, AcceptanceCriterion())
    assert verdict.passed
    assert verdict.reasons == []
    # The report renders and announces a PASS.
    report = render_verdict(verdict)
    assert "PASS" in report
    assert "promote-eligible" in report


# --------------------------------------------------------------------------------------------------
# 2. +2 bps fills + tiny equity noise -> still PASS (within tolerance).
# --------------------------------------------------------------------------------------------------
def test_small_perturbation_still_passes():
    backtest = _make_session()
    sandbox = SessionResult(
        equity_curve=_perturb_equity_noise(backtest, eps=1e-5),
        fills=_perturb_fills_bps(backtest, bps=2.0),
    )
    metrics = parity_metrics(backtest, sandbox)

    # Same signals, same buckets/sides -> perfect timing agreement.
    assert metrics.signal_timing_agreement == pytest.approx(1.0)
    # +2 bps on prices -> ~2 bps mean deviation, under the 5 bps cap.
    assert metrics.fill_price_deviation_bps == pytest.approx(2.0, abs=0.1)
    assert metrics.equity_correlation > 0.90
    assert metrics.return_diff < 0.02

    verdict = evaluate(metrics, AcceptanceCriterion())
    assert verdict.passed, verdict.reasons


# --------------------------------------------------------------------------------------------------
# 3. +50 bps fills / signals dropped -> FAIL with clear reasons.
# --------------------------------------------------------------------------------------------------
def test_large_perturbation_and_dropped_signals_fail():
    backtest = _make_session()
    # Drop the last fill (a missed signal) AND blow the prices out by +50 bps.
    perturbed = _perturb_fills_bps(backtest, bps=50.0)
    sandbox = SessionResult(
        equity_curve=backtest.equity_curve,
        fills=perturbed[:-1],
    )
    metrics = parity_metrics(backtest, sandbox)

    # One of three signal buckets dropped -> agreement well below 0.95.
    assert metrics.signal_timing_agreement < 0.95
    # +50 bps deviation blows the 5 bps cap.
    assert metrics.fill_price_deviation_bps > 5.0

    verdict = evaluate(metrics, AcceptanceCriterion())
    assert not verdict.passed
    assert verdict.reasons
    joined = " ".join(verdict.reasons)
    assert "signal_timing_agreement" in joined
    assert "fill_price_deviation_bps" in joined

    report = render_verdict(verdict)
    assert "FAIL" in report
    assert "BLOCKED from live" in report


# --------------------------------------------------------------------------------------------------
# 4. run_gate end-to-end with SmaCross vs a near-identical sandbox -> PASS.
# --------------------------------------------------------------------------------------------------
def test_run_gate_end_to_end_passes():
    bars = coinext_backtest.synthetic_bars(n=400)

    # Build the "sandbox" session from a real backtest, then nudge it: +1.5 bps on every fill price
    # and a 1e-5 equity wobble — a near-identical testnet recording that should clear the gate.
    base_result = coinext_backtest.run(SmaCross(fast=10, slow=30), bars=bars)
    base_session = SessionResult.from_backtest(base_result)
    sandbox = SessionResult(
        equity_curve=_perturb_equity_noise(base_session, eps=1e-5),
        fills=_perturb_fills_bps(base_session, bps=1.5),
    )

    verdict = run_gate(lambda: SmaCross(fast=10, slow=30), bars, sandbox)
    assert verdict.passed, render_verdict(verdict)
    assert verdict.metrics.signal_timing_agreement >= 0.95
    assert verdict.metrics.fill_price_deviation_bps <= 5.0
    assert verdict.metrics.equity_correlation >= 0.90


def test_run_gate_blocks_on_divergent_sandbox():
    bars = coinext_backtest.synthetic_bars(n=400)
    base_result = coinext_backtest.run(SmaCross(fast=10, slow=30), bars=bars)
    base_session = SessionResult.from_backtest(base_result)
    # A sandbox whose fills are off by 80 bps -> must be blocked from live.
    sandbox = SessionResult(
        equity_curve=base_session.equity_curve,
        fills=_perturb_fills_bps(base_session, bps=80.0),
    )
    verdict = run_gate(lambda: SmaCross(fast=10, slow=30), bars, sandbox)
    assert not verdict.passed
    assert any("fill_price_deviation_bps" in r for r in verdict.reasons)


# --------------------------------------------------------------------------------------------------
# 5. Advisory cross-check never raises; warns on drift.
# --------------------------------------------------------------------------------------------------
def test_cross_check_no_drift_is_silent():
    session = _make_session()
    assert cross_check(session, session) == []


def test_cross_check_warns_on_drift_without_raising():
    event = _make_session()
    # Vectorized: drop a signal and diverge the FINAL return materially. Bumping only the last
    # equity point (no fees/slippage in the vectorized path) lifts final/initial - 1 by ~1%.
    vec_equity = list(event.equity_curve)
    ts_last, eq_last = vec_equity[-1]
    vec_equity[-1] = (ts_last, eq_last + 1_000.0)
    vector = SessionResult(
        equity_curve=vec_equity,
        fills=event.fills[:-1],
    )
    warnings = cross_check(event, vector, max_pnl_diff_bps=50.0)
    assert warnings  # advisory: returns warnings, never raises
    assert any("signal-timing drift" in w for w in warnings)
    assert any("return-proxy drift" in w for w in warnings)


def test_empty_sessions_are_vacuously_consistent():
    empty = SessionResult(equity_curve=[], fills=[])
    metrics = parity_metrics(empty, empty)
    # No fills on either side -> agreement is vacuously perfect; no deviation.
    assert metrics.signal_timing_agreement == pytest.approx(1.0)
    assert metrics.fill_price_deviation_bps == pytest.approx(0.0)


# --------------------------------------------------------------------------------------------------
# 6. Real-data path: fills at bar_ts + latency vs klines closing at :59.999 must NOT be dropped.
#    Regression for the gate silently flattening both equity curves (-> corr 1.0, return_diff 0.0)
#    on the ONLY real-data path. from_fills_and_bars snaps fills to the bar grid before bucketing.
# --------------------------------------------------------------------------------------------------
def test_from_fills_and_bars_snaps_real_kline_close_times():
    base, step = 1_700_000_000_000_000_000, 60_000_000_000
    close_offset = step - 1_000_000  # :59.999, like a real Binance kline closeTime
    latency = 2_000_000  # event fills land at bar_open + ~2ms -> across the :59.999 boundary

    # Real-style klines: timestamps at the minute close (:59.999); a clear up-then-down move so a
    # buy then a sell change the position (and therefore the reconstructed equity).
    closes = [100.0, 101.0, 103.0, 106.0, 110.0, 108.0]
    bars = [(base + i * step + close_offset, c) for i, c in enumerate(closes)]

    # Fills stamped at bar_open + latency: NONE equals any bar's :59.999 closeTime. Buy near bar 1,
    # sell near bar 4 — these MUST survive snapping or the equity curve stays flat.
    fills = [
        (base + 1 * step + latency, +1, 0.5, 101.0),
        (base + 4 * step + latency, -1, 0.5, 110.0),
    ]
    start = 100_000.0

    session = SessionResult.from_fills_and_bars(fills, bars, start)

    # Both fills survived the snap-and-bucket (without the fix, by_ts.get(bar_ts) misses every fill).
    assert len(session.fills) == 2
    # The equity curve is NOT flat: holding 0.5 from bar1->bar4 captures the 101->110 move minus
    # fees. A flat curve (every point == start) would mean all fills were dropped.
    equities = [eq for _ts, eq in session.equity_curve]
    assert not all(eq == pytest.approx(start) for eq in equities), "all fills dropped -> flat curve"
    assert max(equities) > start + 1.0  # the long position actually accrued PnL

    # The snapped fills land on real bar timestamps (the :59.999 grid), not the raw +latency stamps.
    grid = {ts for ts, _c in bars}
    assert all(ts in grid for ts, *_ in session.fills)


def test_real_kline_gate_reconstructs_nonflat_equity_and_real_return_diff():
    # The headline symptom: with fills at bar_ts+latency vs :59.999 klines, the equity reconstruction
    # collapses to FLAT on both sides, so the equity-derived criteria become vacuous -- return_diff
    # is exactly 0.0 (both curves never move) regardless of how far the fill PRICES diverge. The gate
    # would then weigh execution fidelity using a return signal that is structurally zero.
    base, step = 1_700_000_000_000_000_000, 60_000_000_000
    close_offset = step - 1_000_000
    latency = 2_000_000
    closes = [100.0, 102.0, 105.0, 104.0, 108.0, 112.0]
    bars = [(base + i * step + close_offset, c) for i, c in enumerate(closes)]
    bt_fills = [
        (base + 1 * step + latency, +1, 0.5, 102.0),
        (base + 4 * step + latency, -1, 0.5, 108.0),
    ]
    # Sandbox = same signal timestamps, real testnet prices off by +20 bps (well past the 5 bps cap).
    sb_fills = [(ts, s, q, px * (1.0 + 20.0 / 1e4)) for (ts, s, q, px) in bt_fills]
    start = 100_000.0

    bt_session = SessionResult.from_fills_and_bars(bt_fills, bars, start)
    sb_session = SessionResult.from_fills_and_bars(sb_fills, bars, start)

    # Each side reconstructs a NON-flat equity curve (pre-fix both stay pinned at `start`).
    assert bt_session.final_return() != pytest.approx(0.0)
    assert sb_session.final_return() != pytest.approx(0.0)

    metrics = parity_metrics(bt_session, sb_session)
    # The +20 bps price gap now actually moves the reconstructed returns apart: return_diff is no
    # longer the vacuous 0.0 it collapsed to when every fill was dropped.
    assert metrics.return_diff > 0.0
    # Fills are matched (not dropped) -> the +20 bps deviation is actually measured.
    assert metrics.signal_timing_agreement == pytest.approx(1.0)
    assert metrics.fill_price_deviation_bps == pytest.approx(20.0, abs=0.5)

    # And the gate is no longer vacuously blind: the 20 bps blowout is caught and blocks promotion.
    verdict = evaluate(metrics, AcceptanceCriterion())
    assert not verdict.passed
    assert any("fill_price_deviation_bps" in r for r in verdict.reasons)
