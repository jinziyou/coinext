"""qv_analytics — performance metrics, tear sheets, and bias detectors.

Computes returns/Sharpe/Sortino/drawdown from a backtest equity curve and renders a text tear
sheet. The lookahead/recursion bias detectors are stubs to be wired to the engine's event log.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

# Minute bars: 1440/day * 365.
_ANNUALIZATION_BARS_PER_YEAR = 525_600


@dataclass
class Metrics:
    total_return: float
    sharpe: float
    sortino: float
    max_drawdown: float
    volatility: float
    n_periods: int


def _returns(equity_curve: list[tuple[int, float]]) -> list[float]:
    rets = []
    for (_, prev), (_, cur) in zip(equity_curve, equity_curve[1:], strict=False):
        rets.append((cur - prev) / prev if prev else 0.0)
    return rets


def _std(xs: list[float], mean: float) -> float:
    if not xs:
        return 0.0
    var = sum((x - mean) ** 2 for x in xs) / len(xs)
    return math.sqrt(var)


def compute_metrics(
    equity_curve: list[tuple[int, float]],
    *,
    annualization: int = _ANNUALIZATION_BARS_PER_YEAR,
) -> Metrics:
    """Compute headline metrics from a ``(ts_ns, equity)`` curve."""
    if len(equity_curve) < 2:
        return Metrics(0.0, 0.0, 0.0, 0.0, 0.0, len(equity_curve))

    rets = _returns(equity_curve)
    mean = sum(rets) / len(rets)
    std = _std(rets, mean)
    downside = _std([r for r in rets if r < 0] or [0.0], 0.0)
    ann = math.sqrt(annualization)
    sharpe = (mean / std * ann) if std > 0 else 0.0
    sortino = (mean / downside * ann) if downside > 0 else 0.0

    peak = equity_curve[0][1]
    max_dd = 0.0
    for _, eq in equity_curve:
        peak = max(peak, eq)
        if peak > 0:
            max_dd = max(max_dd, (peak - eq) / peak)

    total_return = equity_curve[-1][1] / equity_curve[0][1] - 1.0
    return Metrics(
        total_return=total_return,
        sharpe=sharpe,
        sortino=sortino,
        max_drawdown=max_dd,
        volatility=std * ann,
        n_periods=len(equity_curve),
    )


def tear_sheet(result) -> str:
    """Render a text tear sheet from a BacktestResult (the object qv_backtest.run returns)."""
    m = compute_metrics(list(result.equity_curve))
    lines = [
        "================ VeloxQuant tear sheet ================",
        f"bars / periods    : {m.n_periods}",
        f"orders submitted  : {result.orders_submitted}",
        f"orders denied     : {result.orders_denied}",
        f"fills             : {result.fills}",
        f"starting equity   : {result.starting_equity:>14.2f}",
        f"final equity      : {result.final_equity:>14.2f}",
        f"total return      : {m.total_return * 100:>13.2f}%",
        f"realized PnL      : {result.realized_pnl:>14.2f}",
        f"volatility (ann)  : {m.volatility * 100:>13.2f}%",
        f"sharpe (ann)      : {m.sharpe:>14.3f}",
        f"sortino (ann)     : {m.sortino:>14.3f}",
        f"max drawdown      : {m.max_drawdown * 100:>13.2f}%",
        "======================================================",
    ]
    return "\n".join(lines)


def detect_lookahead_bias(equity_curve: list[tuple[int, float]]) -> list[str]:
    """Stub: timestamps must be strictly increasing (no out-of-order = no obvious lookahead)."""
    warnings = []
    for (t0, _), (t1, _) in zip(equity_curve, equity_curve[1:], strict=False):
        if t1 < t0:
            warnings.append(f"non-monotonic timestamp: {t1} < {t0}")
    return warnings


__all__ = ["Metrics", "compute_metrics", "tear_sheet", "detect_lookahead_bias"]
