"""Hard promotion gate and advisory cross-check."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .metrics import ParityMetrics, parity_metrics
from .session import SessionResult


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
    """ADVISORY event-driven-vs-vectorized drift warning (root ``ARCHITECTURE.md`` §1, §6).

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
