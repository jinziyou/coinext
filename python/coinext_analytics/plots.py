"""coinext_analytics.plots — optional tear-sheet plots (equity / drawdown / returns).

``matplotlib`` is an optional dependency (the ``research`` extra). This module imports cleanly
without it; only :func:`plot_tear_sheet` needs it and raises a clear :class:`ImportError` otherwise,
so the headline text tear sheet stays dependency-free.
"""

from __future__ import annotations


def _drawdown_series(equity_curve: list[tuple[int, float]]) -> list[float]:
    """Running drawdown (fraction below the high-water mark) for each equity point."""
    dd: list[float] = []
    peak = equity_curve[0][1] if equity_curve else 0.0
    for _ts, eq in equity_curve:
        peak = max(peak, eq)
        dd.append((peak - eq) / peak if peak > 0 else 0.0)
    return dd


def plot_tear_sheet(result, *, path: str | None = None, show: bool = False):
    """Render a 3-panel tear sheet (equity, drawdown, per-bar return histogram).

    Returns the matplotlib ``Figure``. Pass ``path`` to save a PNG, ``show=True`` to display.
    Requires ``matplotlib`` (``pip install 'coinext[research]'``).
    """
    try:
        import matplotlib

        if path is not None and not show:
            matplotlib.use("Agg")  # headless: no display needed when only saving
        import matplotlib.pyplot as plt
    except ImportError as exc:  # pragma: no cover - optional dep
        raise ImportError(
            "matplotlib not installed. Install the research extra: "
            "pip install 'coinext[research]'"
        ) from exc

    from . import _returns  # reuse the canonical per-bar return computation

    equity = [(int(ts), float(eq)) for ts, eq in result.equity_curve]
    xs = list(range(len(equity)))
    eq_vals = [eq for _ts, eq in equity]
    dd = _drawdown_series(equity)
    rets = _returns(equity)

    fig, (ax_eq, ax_dd, ax_ret) = plt.subplots(3, 1, figsize=(9, 9), constrained_layout=True)

    ax_eq.plot(xs, eq_vals, color="tab:blue", lw=1.2)
    ax_eq.set_title("Equity curve")
    ax_eq.set_ylabel("equity")
    ax_eq.grid(True, alpha=0.3)

    ax_dd.fill_between(xs, [-d * 100 for d in dd], 0.0, color="tab:red", alpha=0.4)
    ax_dd.set_title("Drawdown")
    ax_dd.set_ylabel("drawdown %")
    ax_dd.grid(True, alpha=0.3)

    if rets:
        ax_ret.hist([r * 100 for r in rets], bins=40, color="tab:green", alpha=0.7)
    ax_ret.set_title("Per-bar return distribution")
    ax_ret.set_xlabel("return %")
    ax_ret.set_ylabel("count")
    ax_ret.grid(True, alpha=0.3)

    if path is not None:
        fig.savefig(path, dpi=120)
    if show:  # pragma: no cover - interactive
        plt.show()
    return fig


__all__ = ["plot_tear_sheet"]
