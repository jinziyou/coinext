"""coinext_analytics — performance metrics, trade statistics, tear sheets, and bias detectors.

Computes returns/Sharpe/Sortino/drawdown from a backtest equity curve (:func:`compute_metrics`),
reconstructs round-trip trades from the fill log for trade-level stats (:mod:`coinext_analytics.trades`),
screens for look-ahead / overfitting tells (:mod:`coinext_analytics.bias`), and renders a text or
graphical tear sheet (:func:`tear_sheet`, :func:`plot_tear_sheet`).

The equity/metrics math is pure stdlib. Plotting needs ``matplotlib`` (the ``research`` extra) and
is imported lazily, so the headline path stays dependency-free.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .bias import BiasReport, detect_lookahead_bias, detect_overfitting, screen_biases
from .trades import (
    Trade,
    TradeStats,
    compute_trade_stats,
    reconstruct_trades,
    stats_from_result,
)

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


def _fmt_pf(pf: float) -> str:
    """Profit factor display (``inf`` -> ``∞``)."""
    return "∞" if pf == float("inf") else f"{pf:.2f}"


def tear_sheet(
    result, *, bars: list[tuple[int, float]] | None = None, fee_rate: float = 0.0
) -> str:
    """Render a text tear sheet from a BacktestResult (the object ``coinext_backtest.run`` returns).

    Includes headline metrics, trade-level statistics (win rate, profit factor, expectancy,
    exposure, turnover), and any bias-screen warnings. Pass ``bars`` to enable the off-grid fill
    look-ahead check; ``fee_rate`` charges trade PnL net of fees (gross by default).
    """
    equity = list(result.equity_curve)
    m = compute_metrics(equity)
    ts = stats_from_result(result, fee_rate=fee_rate)
    report = screen_biases(result, metrics=m, trade_stats=ts, bars=bars)

    lines = [
        "================ Coinext tear sheet ================",
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
        "---------------- trades ------------------------------",
        f"round-trip trades : {ts.n_trades}",
        f"win rate          : {ts.win_rate * 100:>13.2f}%  ({ts.n_wins}W / {ts.n_losses}L)",
        f"profit factor     : {_fmt_pf(ts.profit_factor):>14}",
        f"avg trade PnL     : {ts.avg_trade:>14.2f}",
        f"avg win / loss    : {ts.avg_win:>9.2f} / {ts.avg_loss:.2f}",
        f"largest win/loss  : {ts.largest_win:>9.2f} / {ts.largest_loss:.2f}",
        f"exposure (in mkt) : {ts.exposure * 100:>13.2f}%",
        f"turnover (x cap)  : {ts.turnover:>14.2f}",
    ]
    if not report.clean:
        lines.append("---------------- bias screen -------------------------")
        for w in report.warnings:
            lines.append(f"  ⚠ {w}")
    lines.append("======================================================")
    return "\n".join(lines)


# Optional plotting (matplotlib) is imported lazily so the headline path stays dependency-free.
def plot_tear_sheet(result, *, path: str | None = None, show: bool = False):
    """Render a 3-panel tear sheet (equity/drawdown/returns). Requires ``matplotlib``."""
    from .plots import plot_tear_sheet as _impl

    return _impl(result, path=path, show=show)


__all__ = [
    "Metrics",
    "compute_metrics",
    "tear_sheet",
    "plot_tear_sheet",
    # trades
    "Trade",
    "TradeStats",
    "reconstruct_trades",
    "compute_trade_stats",
    "stats_from_result",
    # bias
    "BiasReport",
    "detect_lookahead_bias",
    "detect_overfitting",
    "screen_biases",
]
