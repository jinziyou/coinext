"""qv_analytics.bias — heuristic bias / overfitting screens for a backtest.

These are **screens, not proofs**: each function returns human-readable warning strings (never
raises) flagging patterns that commonly accompany look-ahead leakage or an overfit result. A clean
screen does not certify a strategy; a dirty one says "look here before trusting this backtest".

Two families, matching ``docs/ROADMAP.md`` (research side, item 2):

* **Look-ahead** — structural impossibilities in the run: equity timestamps that move backwards, or
  fills stamped before/off the bar grid the strategy was fed (a fill can only react to a bar it has
  already seen). Driven by the run's own timeline, so it needs no oracle.
* **Overfitting** — "too good to be true" shapes: an implausibly high Sharpe, a never-losing trade
  record, or a positive return with literally zero drawdown while actively trading. None is proof of
  a bug, but each is a known tell of leakage or curve-fitting and is worth a manual look.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Heuristic thresholds for the overfitting screen. Deliberately loose — these flag the egregious,
# not the merely good (see docs/ARCHITECTURE.md §11: thresholds tighten with evidence). The tells
# here are annualization-INVARIANT (win rate, drawdown shape): an annualized-Sharpe threshold is
# avoided on purpose because at minute-bar frequency the √525600 factor makes a modest per-bar edge
# annualize to a double-digit Sharpe, which would false-positive constantly.
_IMPLAUSIBLE_WIN_RATE = 0.95
_MIN_TRADES_FOR_WINRATE_FLAG = 8


def detect_lookahead_bias(
    equity_curve: list[tuple[int, float]],
    *,
    fills: list[tuple[int, int, float, float]] | None = None,
    bars: list[tuple[int, float]] | None = None,
) -> list[str]:
    """Structural look-ahead screen over a run's own timeline.

    Always checks that equity timestamps are strictly increasing (out-of-order marks-to-market are a
    replay/ordering bug). When ``fills`` are supplied, also checks that no fill is stamped before
    the first bar (execution acting on data that does not exist yet) or after the last bar plus one
    bar of execution latency (a fill materializing past the data the engine fed). ``bars``, when
    given, is the authoritative source for the first/last bar timestamps and the bar cadence;
    otherwise the equity curve's own span is used.

    Note the screen does NOT require fills to land exactly on a bar timestamp: the simulated
    exchange stamps a fill at ``bar_ts + execution_latency`` (the delayed-fill queue), so legitimate
    fills sit just past their triggering bar by design — flagging that would be a false positive.

    Back-compatible: called with only ``equity_curve`` it is the original monotonic-timestamp check.
    """
    warnings: list[str] = []

    for (t0, _), (t1, _) in zip(equity_curve, equity_curve[1:], strict=False):
        if t1 < t0:
            warnings.append(f"non-monotonic equity timestamp: {t1} < {t0}")

    if fills:
        # bars rows may be (ts, close) or (ts, o, h, l, c); only the timestamp (row[0]) is needed.
        grid = sorted(int(b[0]) for b in bars) if bars else [t for t, _ in equity_curve]
        if grid:
            first_ts, last_ts = grid[0], grid[-1]
            # One bar of slack past the last bar covers the execution latency on the final bar.
            step = (grid[-1] - grid[0]) // (len(grid) - 1) if len(grid) > 1 else 0
            late_bound = last_ts + step
            for ts, _side, _qty, _px in fills:
                if ts < first_ts:
                    warnings.append(
                        f"fill at ts={ts} precedes first bar ts={first_ts} "
                        "(execution before any market data)"
                    )
                elif ts > late_bound:
                    warnings.append(
                        f"fill at ts={ts} lands past the data span (last bar {last_ts} "
                        f"+ one bar); execution outside the fed series"
                    )

    return warnings


def detect_overfitting(metrics, trade_stats) -> list[str]:
    """"Too-good-to-be-true" screen over computed :class:`Metrics` + :class:`TradeStats`.

    Flags a near-perfect win rate over enough trades and a positive return with exactly zero
    drawdown while trading — both annualization-invariant tells of look-ahead leakage or overfit.
    ``metrics`` is a ``qv_analytics.Metrics``; ``trade_stats`` a ``qv_analytics.trades.TradeStats``.
    """
    warnings: list[str] = []
    ts = trade_stats

    if ts.n_trades >= _MIN_TRADES_FOR_WINRATE_FLAG and ts.win_rate >= _IMPLAUSIBLE_WIN_RATE:
        warnings.append(
            f"{ts.win_rate * 100:.0f}% win rate over {ts.n_trades} trades "
            f"(>= {_IMPLAUSIBLE_WIN_RATE * 100:.0f}%); real edges lose sometimes — check leakage"
        )

    if ts.n_trades > 0 and metrics.max_drawdown == 0.0 and metrics.total_return > 0.0:
        warnings.append(
            "zero drawdown with a positive return while actively trading; "
            "equity that never dips is a classic look-ahead signature"
        )

    return warnings


@dataclass
class BiasReport:
    """Aggregated output of the bias screens — warning lists by family."""

    lookahead: list[str] = field(default_factory=list)
    overfitting: list[str] = field(default_factory=list)

    @property
    def warnings(self) -> list[str]:
        """All warnings, flattened (look-ahead first)."""
        return [*self.lookahead, *self.overfitting]

    @property
    def clean(self) -> bool:
        """``True`` when no screen fired (does NOT certify the strategy — see module docstring)."""
        return not self.warnings


def screen_biases(result, *, metrics=None, trade_stats=None, bars=None) -> BiasReport:
    """Run every bias screen against a ``qv_py`` ``BacktestResult`` and bundle the warnings.

    Computes :class:`Metrics` and :class:`TradeStats` from ``result`` if not supplied. ``bars`` (the
    series the backtest ran over) enables the off-grid fill check; without it the look-ahead screen
    falls back to the structural (timestamp/pre-data) checks.
    """
    from . import compute_metrics
    from .trades import stats_from_result

    equity = [(int(ts), float(eq)) for ts, eq in result.equity_curve]
    fills = [(int(ts), int(s), float(q), float(p)) for ts, _sym, s, q, p in result.fills_log]
    if metrics is None:
        metrics = compute_metrics(equity)
    if trade_stats is None:
        trade_stats = stats_from_result(result)

    return BiasReport(
        lookahead=detect_lookahead_bias(equity, fills=fills, bars=bars),
        overfitting=detect_overfitting(metrics, trade_stats),
    )


__all__ = ["BiasReport", "detect_lookahead_bias", "detect_overfitting", "screen_biases"]
