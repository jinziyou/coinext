"""coinext_parity — the pre-live promotion gate and the advisory cross-check.

Coinext's core invariant is **backtest↔live parity** (see ``docs/ARCHITECTURE.md`` §1, §5): ONE
Strategy API, ONE set of engines, ONE deterministic core. Only the Kernel-injected Clock, Cache
contents, and Data/Execution clients differ between ``Backtest`` / ``Sandbox`` / ``Live``. Because
the ``BrokerageModel`` economics are **shared** between backtest and live, a strategy run through
the ``SimulatedExecutionClient`` (backtest) and the testnet ``ExecutionClient`` (sandbox) should
agree closely. This module turns that promise into numbers.

Two distinct checks live here, matching ``docs/ARCHITECTURE.md`` §1/§10 and ``tests/parity/``:

* :func:`run_gate` — the **hard** promotion gate. A strategy may go live only if its event-driven
  backtest agrees with a recorded sandbox (testnet) session within :class:`AcceptanceCriterion`. The
  event-driven runner is the only parity-valid surface.
* :func:`cross_check` — the **advisory** vectorized-vs-event drift warning. The vectorized screen
  skips Risk/Exec/Brokerage, so absolute PnL will differ by design; a drift beyond the threshold
  emits a *warning* (returned, never raised), flagging that the fast screen is misleading for this
  strategy — it never validates or invalidates a strategy.

Pure stdlib — no heavy deps. A :class:`SessionResult` is the common shape both sides reduce to: an
equity curve and a fills log, exactly what the compiled ``coinext_py`` ``BacktestResult`` exposes.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# Default time bucket for matching fills across sessions: one minute (bar cadence). Backtest and
# sandbox clocks never align to the nanosecond (HistoricalClock vs LiveClock), so fills are matched
# at bar granularity rather than exact timestamps.
_DEFAULT_TS_BUCKET_NS = 60_000_000_000


# --------------------------------------------------------------------------------------------------
# Session shape — the common reduction of a backtest OR a sandbox run.
# --------------------------------------------------------------------------------------------------
@dataclass
class SessionResult:
    """A single run reduced to the two parity surfaces: an equity curve and a fills log.

    * ``equity_curve`` — list of ``(ts_ns, equity)``.
    * ``fills`` — list of ``(ts_ns, side, qty, px)`` where ``side`` is ``+1`` buy / ``-1`` sell.
    """

    equity_curve: list[tuple[int, float]]
    fills: list[tuple[int, int, float, float]]

    @classmethod
    def from_backtest(cls, result: Any) -> SessionResult:
        """Build a :class:`SessionResult` from a ``coinext_py`` ``BacktestResult``.

        ``BacktestResult`` exposes ``equity_curve`` (``(ts_ns, equity)``) and ``fills_log``
        (``(ts_ns, symbol, side, qty, px)``); both are normalized here. The parity gate is
        single-venue, so the per-fill ``symbol`` is dropped (matching is on ts-bucket + side).
        """
        equity = [(int(ts), float(eq)) for ts, eq in result.equity_curve]
        fills = [
            (int(ts), int(side), float(qty), float(px))
            for ts, _sym, side, qty, px in result.fills_log
        ]
        return cls(equity_curve=equity, fills=fills)

    @classmethod
    def from_fills_and_bars(
        cls,
        fills: list[tuple[int, int, float, float]],
        bars: list[tuple[int, float]],
        starting_balance: float,
        *,
        fee_rate: float = 0.0004,
    ) -> SessionResult:
        """Reconstruct a session (equity curve) from a fill log + bars, marking to bar close.

        Used by ``coinext testnet-gate`` to build the SANDBOX session: the fills carry REAL testnet
        execution prices stamped with the backtest's signal timestamps, and the equity curve is
        rebuilt by walking the same bars. Applying the IDENTICAL reconstruction to the backtest
        fills isolates the only real difference — the fill prices — so the gate measures execution
        fidelity rather than accounting artifacts.

        Fills are snapped to the bar grid before bucketing (``coinext_screen._snap_fills_to_grid``):
        event-driven fills land at ``bar_ts + execution_latency``, but REAL Binance klines close at
        ``:59.999``, so that latency pushes the fill across the minute boundary and an EXACT
        ``ts == bar_ts`` match would drop EVERY fill — leaving a flat equity curve and a vacuously
        blind gate on the only real-data path. Snapping each fill to its nearest bar restores the
        BAR that triggered it.
        """
        import numpy as np
        from coinext_screen import _snap_fills_to_grid

        bar_ts = np.fromiter((int(ts) for ts, _close in bars), dtype=np.int64, count=len(bars))
        snapped = _snap_fills_to_grid(fills, bar_ts) if len(bar_ts) else list(fills)
        by_ts: dict[int, list[tuple[int, float, float]]] = {}
        for ts, side, qty, px in snapped:
            by_ts.setdefault(int(ts), []).append((int(side), float(qty), float(px)))
        cash = float(starting_balance)
        pos = 0.0
        curve: list[tuple[int, float]] = []
        for ts, close in bars:
            for side, qty, px in by_ts.get(int(ts), ()):
                notional = px * qty
                fee = notional * fee_rate
                if side > 0:  # buy
                    cash -= notional + fee
                    pos += qty
                else:  # sell
                    cash += notional - fee
                    pos -= qty
            curve.append((int(ts), cash + pos * float(close)))
        return cls(
            equity_curve=curve,
            fills=[(int(t), int(s), float(q), float(p)) for t, s, q, p in snapped],
        )

    def final_return(self) -> float:
        """Total return over the equity curve (``final / initial - 1``); ``0.0`` if degenerate."""
        if len(self.equity_curve) < 2:
            return 0.0
        start = self.equity_curve[0][1]
        if start == 0.0:
            return 0.0
        return self.equity_curve[-1][1] / start - 1.0


# --------------------------------------------------------------------------------------------------
# Metrics.
# --------------------------------------------------------------------------------------------------
@dataclass
class ParityMetrics:
    """Quantified agreement between a backtest and a sandbox session.

    * ``signal_timing_agreement`` — matched-fraction of fills agreeing on ``(ts bucket, side)``
      (a symmetric Jaccard-style ratio in ``[0, 1]``; ``1.0`` = perfect agreement).
    * ``fill_price_deviation_bps`` — mean ``|sandbox_px - backtest_px| / backtest_px * 1e4`` over
      time-and-side-matched fills (basis points).
    * ``equity_correlation`` — Pearson correlation of the two equity curves aligned on index.
    * ``return_diff`` — ``|final_return_backtest - final_return_sandbox|``.
    """

    signal_timing_agreement: float
    fill_price_deviation_bps: float
    equity_correlation: float
    return_diff: float


def _bucket(ts: int, ts_bucket_ns: int) -> int:
    return ts // ts_bucket_ns if ts_bucket_ns > 0 else ts


def _fill_keys(
    fills: list[tuple[int, int, float, float]], ts_bucket_ns: int
) -> dict[tuple[int, int], list[float]]:
    """Group fill prices by ``(ts bucket, side)`` key (multiple fills per bucket allowed)."""
    keys: dict[tuple[int, int], list[float]] = {}
    for ts, side, _qty, px in fills:
        keys.setdefault((_bucket(ts, ts_bucket_ns), side), []).append(px)
    return keys


def _pearson(xs: list[float], ys: list[float]) -> float:
    """Pearson correlation; ``1.0`` for identical constant series, ``0.0`` when undefined."""
    n = min(len(xs), len(ys))
    if n < 2:
        return 0.0
    xs, ys = xs[:n], ys[:n]
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=False))
    if sxx == 0.0 and syy == 0.0:
        # Both curves are flat (e.g. no trades) — treat identical constants as perfectly correlated.
        return 1.0 if all(x == y for x, y in zip(xs, ys, strict=False)) else 0.0
    denom = math.sqrt(sxx * syy)
    if denom == 0.0:
        return 0.0
    return sxy / denom


def _resample(curve: list[tuple[int, float]], n: int) -> list[float]:
    """Resample an equity curve's values to exactly ``n`` points by index (nearest-rank)."""
    m = len(curve)
    if m == 0 or n <= 0:
        return []
    if m == n:
        return [eq for _, eq in curve]
    out: list[float] = []
    for i in range(n):
        # Map output index i in [0, n) onto a source index in [0, m).
        src = i * m // n
        if src >= m:
            src = m - 1
        out.append(curve[src][1])
    return out


def parity_metrics(
    backtest: SessionResult,
    sandbox: SessionResult,
    *,
    ts_bucket_ns: int = _DEFAULT_TS_BUCKET_NS,
) -> ParityMetrics:
    """Compute :class:`ParityMetrics` between a backtest and a sandbox session.

    Fills are matched at ``(ts bucket, side)`` granularity (clocks differ across environments).
    ``signal_timing_agreement`` is the matched-fraction ``2*|matched buckets| / (|a| + |b|)`` over
    distinct ``(bucket, side)`` keys — a symmetric ratio that is ``1.0`` iff the two sessions fired
    the same signals in the same buckets. ``fill_price_deviation_bps`` averages the absolute
    relative price difference over keys present in BOTH sessions (mean px per key). Equity curves
    are resampled to the shorter length before correlating.
    """
    bt_keys = _fill_keys(backtest.fills, ts_bucket_ns)
    sb_keys = _fill_keys(sandbox.fills, ts_bucket_ns)

    bt_set = set(bt_keys)
    sb_set = set(sb_keys)
    matched = bt_set & sb_set
    total = len(bt_set) + len(sb_set)
    if total == 0:
        # No fills on either side: vacuously perfect agreement (both did nothing).
        signal_agreement = 1.0
    else:
        signal_agreement = 2.0 * len(matched) / total

    # Mean absolute relative price deviation (bps) over time-and-side-matched fills.
    devs: list[float] = []
    for key in matched:
        bt_px = sum(bt_keys[key]) / len(bt_keys[key])
        sb_px = sum(sb_keys[key]) / len(sb_keys[key])
        if bt_px != 0.0:
            devs.append(abs(sb_px - bt_px) / abs(bt_px) * 1e4)
    fill_dev_bps = (sum(devs) / len(devs)) if devs else 0.0

    n = min(len(backtest.equity_curve), len(sandbox.equity_curve))
    bt_eq = _resample(backtest.equity_curve, n)
    sb_eq = _resample(sandbox.equity_curve, n)
    equity_corr = _pearson(bt_eq, sb_eq)

    return_diff = abs(backtest.final_return() - sandbox.final_return())

    return ParityMetrics(
        signal_timing_agreement=signal_agreement,
        fill_price_deviation_bps=fill_dev_bps,
        equity_correlation=equity_corr,
        return_diff=return_diff,
    )


# --------------------------------------------------------------------------------------------------
# Acceptance criterion + verdict.
# --------------------------------------------------------------------------------------------------
@dataclass
class AcceptanceCriterion:
    """Thresholds for the hard pre-live promotion gate (start tight; widen with evidence, §11).

    All four conditions must hold for a strategy to be promoted to live.
    """

    min_signal_agreement: float = 0.95
    max_fill_dev_bps: float = 5.0
    min_equity_corr: float = 0.90
    max_return_diff: float = 0.02


@dataclass
class Verdict:
    """Outcome of evaluating :class:`ParityMetrics` against an :class:`AcceptanceCriterion`."""

    passed: bool
    reasons: list[str]
    metrics: ParityMetrics


def evaluate(metrics: ParityMetrics, criterion: AcceptanceCriterion) -> Verdict:
    """Evaluate ``metrics`` against ``criterion``; ``reasons`` lists every failing condition."""
    reasons: list[str] = []

    if metrics.signal_timing_agreement < criterion.min_signal_agreement:
        reasons.append(
            f"signal_timing_agreement {metrics.signal_timing_agreement:.4f} "
            f"< min {criterion.min_signal_agreement:.4f}"
        )
    if metrics.fill_price_deviation_bps > criterion.max_fill_dev_bps:
        reasons.append(
            f"fill_price_deviation_bps {metrics.fill_price_deviation_bps:.4f} "
            f"> max {criterion.max_fill_dev_bps:.4f}"
        )
    if metrics.equity_correlation < criterion.min_equity_corr:
        reasons.append(
            f"equity_correlation {metrics.equity_correlation:.4f} "
            f"< min {criterion.min_equity_corr:.4f}"
        )
    if metrics.return_diff > criterion.max_return_diff:
        reasons.append(
            f"return_diff {metrics.return_diff:.4f} > max {criterion.max_return_diff:.4f}"
        )

    return Verdict(passed=not reasons, reasons=reasons, metrics=metrics)


# --------------------------------------------------------------------------------------------------
# The promotion gate.
# --------------------------------------------------------------------------------------------------
def run_gate(
    strategy_factory: Callable[[], Any],
    bars: list[tuple[int, float]],
    sandbox: SessionResult,
    criterion: AcceptanceCriterion | None = None,
    **backtest_kwargs: Any,
) -> Verdict:
    """The HARD pre-live promotion gate.

    Run the authoritative event-driven backtest (``coinext_backtest.run`` through the Rust kernel — the
    SAME engines + ``SimulatedExecutionClient`` the live path uses) for a fresh strategy instance
    over ``bars``, reduce it to a :class:`SessionResult`, compare it to the provided ``sandbox``
    (recorded testnet) session, and return the :class:`Verdict`. A strategy may go live only if this
    returns ``passed=True``.

    ``strategy_factory`` must be a zero-arg callable returning a fresh ``Strategy`` (strategies are
    stateful, so the gate constructs its own instance). Extra ``backtest_kwargs`` are forwarded to
    ``coinext_backtest.run``.
    """
    import coinext_backtest

    if criterion is None:
        criterion = AcceptanceCriterion()

    result = coinext_backtest.run(strategy_factory(), bars=bars, **backtest_kwargs)
    backtest_session = SessionResult.from_backtest(result)
    metrics = parity_metrics(backtest_session, sandbox)
    return evaluate(metrics, criterion)


# --------------------------------------------------------------------------------------------------
# Advisory cross-check (non-gating).
# --------------------------------------------------------------------------------------------------
def cross_check(
    event_result: SessionResult,
    vector_result: SessionResult,
    *,
    max_pnl_diff_bps: float = 50.0,
) -> list[str]:
    """ADVISORY event-driven-vs-vectorized drift warning (``docs/ARCHITECTURE.md`` §1, §10).

    The vectorized ``populate_*`` screen skips Risk/Exec/Brokerage, so absolute PnL will differ by
    design — this is a *fast screen*, never a parity surface. This returns warning strings (it never
    raises): a non-empty list flags that the fast screen is misleading for this strategy, not that
    the strategy is invalid. Only the event-driven result is a parity surface.

    Compared: signal timing (which buckets trigger fills) and a coarse return proxy. NOT expected to
    match: exact PnL (no fees/slippage/latency/partial fills in the vectorized path).
    """
    warnings: list[str] = []

    metrics = parity_metrics(event_result, vector_result)

    if metrics.signal_timing_agreement < 1.0:
        warnings.append(
            f"signal-timing drift: event vs vectorized agree on only "
            f"{metrics.signal_timing_agreement:.2%} of fills (buckets/sides differ)"
        )

    # Coarse return proxy: difference in final returns, expressed in bps.
    return_diff_bps = metrics.return_diff * 1e4
    if return_diff_bps > max_pnl_diff_bps:
        warnings.append(
            f"return-proxy drift {return_diff_bps:.1f} bps > advisory max {max_pnl_diff_bps:.1f} "
            f"bps (vectorized has no fees/slippage/latency — absolute PnL differs by design)"
        )

    return warnings


# --------------------------------------------------------------------------------------------------
# Text report.
# --------------------------------------------------------------------------------------------------
def render_verdict(verdict: Verdict) -> str:
    """Render a short text report of a :class:`Verdict` (the promotion-gate decision)."""
    m = verdict.metrics
    status = "PASS" if verdict.passed else "FAIL"
    lines = [
        "============== Coinext parity gate ===============",
        f"verdict                : {status}",
        f"signal agreement       : {m.signal_timing_agreement:>14.4f}",
        f"fill deviation (bps)   : {m.fill_price_deviation_bps:>14.4f}",
        f"equity correlation     : {m.equity_correlation:>14.4f}",
        f"return diff            : {m.return_diff:>14.4f}",
    ]
    if verdict.passed:
        lines.append("decision               : promote-eligible (gate PASSED)")
    else:
        lines.append("decision               : BLOCKED from live (gate FAILED)")
        for reason in verdict.reasons:
            lines.append(f"  - {reason}")
    lines.append("=====================================================")
    return "\n".join(lines)


__all__ = [
    "SessionResult",
    "ParityMetrics",
    "parity_metrics",
    "AcceptanceCriterion",
    "Verdict",
    "evaluate",
    "run_gate",
    "cross_check",
    "render_verdict",
]
