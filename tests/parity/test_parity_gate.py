"""Pure-logic tests for the qv_parity promotion gate and advisory cross-check.

These exercise the metric math and the gate decision over fixtures (no network / no testnet). The
end-to-end ``run_gate`` test drives the AUTHORITATIVE ``qv_backtest.run`` through the Rust kernel,
so it requires the compiled ``qv_py`` extension (``importorskip``).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make the control-plane packages importable when pytest is run from the repo root without setting
# PYTHONPATH (mirrors how the other suites resolve qv_* packages).
_PYTHON_ROOT = Path(__file__).resolve().parents[2] / "python"
if str(_PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(_PYTHON_ROOT))

# The gate's run_gate path needs the compiled kernel; skip the whole module if it isn't built.
pytest.importorskip("qv_py")

import qv_backtest  # noqa: E402
from qv_parity import (  # noqa: E402
    AcceptanceCriterion,
    SessionResult,
    cross_check,
    evaluate,
    parity_metrics,
    render_verdict,
    run_gate,
)
from qv_strategy import SmaCross  # noqa: E402


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
    bars = qv_backtest.synthetic_bars(n=400)

    # Build the "sandbox" session from a real backtest, then nudge it: +1.5 bps on every fill price
    # and a 1e-5 equity wobble — a near-identical testnet recording that should clear the gate.
    base_result = qv_backtest.run(SmaCross(fast=10, slow=30), bars=bars)
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
    bars = qv_backtest.synthetic_bars(n=400)
    base_result = qv_backtest.run(SmaCross(fast=10, slow=30), bars=bars)
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
