"""qv_screen — the FAST, NON-AUTHORITATIVE vectorized research screen.

For coarse parameter sweeps: compute signals → target positions → mark-to-market PnL with numpy in
ONE vectorized pass, skipping the Risk / Execution / Brokerage engines. This is **not** parity-valid
(no fees-beyond-a-flat-rate, no slippage, no latency, no partial fills, no queue) — use it to narrow
a large parameter space cheaply, then confirm the survivors with the AUTHORITATIVE
``qv_backtest.run`` (the event-driven Rust kernel). The advisory ``qv_parity.cross_check`` flags
when the screen drifts materially from the event-driven runner (see :func:`cross_check_vs_event`).

Pure ``numpy`` (a core dependency); importing this module needs no compiled ``qv_py``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

# A vectorized result reduces to the SAME two surfaces a qv_py BacktestResult exposes, so the parity
# cross-check can compare a screen run against an event-driven run directly.
Fill = tuple[int, int, float, float]  # (ts_ns, side[+1/-1], qty, px)


@dataclass
class VectorResult:
    """A vectorized screen run reduced to an equity curve + a (synthetic) fills log."""

    equity_curve: list[tuple[int, float]]
    fills: list[Fill]
    final_equity: float
    total_return: float


def _closes(bars: Sequence[tuple]) -> tuple[np.ndarray, np.ndarray]:
    """Extract ``(ts, close)`` arrays from close-only / OHLC / OHLCV bar rows."""
    ts = np.fromiter((int(b[0]) for b in bars), dtype=np.int64, count=len(bars))
    close = np.fromiter(
        (float(b[1]) if len(b) == 2 else float(b[4]) for b in bars), dtype=float, count=len(bars)
    )
    return ts, close


def sma(values: np.ndarray, period: int) -> np.ndarray:
    """Simple moving average; ``NaN`` for the first ``period-1`` bars (window not yet full)."""
    values = np.asarray(values, dtype=float)
    if period <= 1:
        return values.copy()
    out = np.full(len(values), np.nan)
    if len(values) >= period:
        csum = np.cumsum(np.insert(values, 0, 0.0))
        out[period - 1 :] = (csum[period:] - csum[:-period]) / period
    return out


def sma_cross_positions(
    closes: np.ndarray, fast: int, slow: int, qty: float = 0.5
) -> np.ndarray:
    """Vectorized long/flat SMA-crossover TARGET position per bar — a faithful proxy of
    ``qv_strategy.SmaCross``.

    Matches the STATEFUL event-driven strategy: start flat; ENTER (``qty``) only on a strict
    up-cross (``prev_fast <= prev_slow`` and ``fast > slow``); EXIT (``0``) only on a strict
    down-cross (``prev_fast >= prev_slow`` and ``fast < slow``); HOLD on an exact touch
    (``fast == slow``) and through warm-up. This is NOT the naive stateless ``fast > slow`` level —
    that would spuriously enter at a fast-above-slow warm-up start and flip flat on a touch, so the
    cross-check would then flag a self-inflicted divergence instead of a real screen↔runner one.
    """
    closes = np.asarray(closes, dtype=float)
    f = sma(closes, fast)
    s = sma(closes, slow)
    warm = ~np.isnan(f) & ~np.isnan(s)
    prev_warm = np.roll(warm, 1)
    prev_warm[0] = False
    pf = np.roll(f, 1)
    ps = np.roll(s, 1)
    both = warm & prev_warm
    up = both & (pf <= ps) & (f > s)  # prev not-above, now strictly above -> enter
    down = both & (pf >= ps) & (f < s)  # prev not-below, now strictly below -> exit

    # Forward-fill the last cross direction (+1 enter / -1 exit); flat before the first cross.
    state = np.zeros(len(closes))
    state[up] = 1.0
    state[down] = -1.0
    last = np.where(state != 0.0, np.arange(len(closes)), 0)
    np.maximum.accumulate(last, out=last)
    direction = state[last]
    return np.where(direction > 0.0, float(qty), 0.0)


def vector_backtest(
    bars: Sequence[tuple],
    positions: np.ndarray,
    *,
    starting_balance: float = 100_000.0,
    fee_rate: float = 0.0004,
) -> VectorResult:
    """Vectorized mark-to-market PnL of holding ``positions[i]`` over bar ``i → i+1``.

    ``bars`` may be close-only / OHLC / OHLCV (only ts + close are used). Equity at bar ``i`` is the
    starting balance plus the cumulative ``position * price_change`` less a flat ``fee_rate`` on the
    notional of each position CHANGE. Fills are synthesized at the bars where the position changes
    (so the result has the same shape as an event-driven session for the cross-check).
    """
    ts, close = _closes(bars)
    n = len(close)
    if n == 0:
        return VectorResult([], [], starting_balance, 0.0)
    pos = np.asarray(positions, dtype=float)
    if len(pos) != n:
        raise ValueError(f"positions length {len(pos)} != bars length {n}")

    dprice = np.diff(close, prepend=close[0])  # price change into bar i (0 at i=0)
    pnl = pos.copy()
    pnl[1:] = pos[:-1] * dprice[1:]  # PnL realized holding the PRIOR position into bar i
    pnl[0] = 0.0
    dpos = np.diff(pos, prepend=0.0)  # position change at bar i (opens the initial position at i)
    fees = np.abs(dpos) * close * fee_rate
    equity = starting_balance + np.cumsum(pnl) - np.cumsum(fees)

    equity_curve = [(int(ts[i]), float(equity[i])) for i in range(n)]
    fills: list[Fill] = [
        (int(ts[i]), (1 if dpos[i] > 0 else -1), abs(float(dpos[i])), float(close[i]))
        for i in range(n)
        if dpos[i] != 0.0
    ]
    final = float(equity[-1])
    total_return = (final / starting_balance - 1.0) if starting_balance else 0.0
    return VectorResult(equity_curve, fills, final, total_return)


def to_session(result: VectorResult):
    """Convert a :class:`VectorResult` to a ``qv_parity.SessionResult`` for the cross-check."""
    from qv_parity import SessionResult

    return SessionResult(equity_curve=result.equity_curve, fills=result.fills)


@dataclass
class ScreenRow:
    """One swept parameter set and its vectorized score (higher is better)."""

    params: dict[str, Any]
    total_return: float
    sharpe: float
    n_trades: int


def _sharpe(equity_curve: list[tuple[int, float]]) -> float:
    if len(equity_curve) < 2:
        return 0.0
    eq = np.array([e for _, e in equity_curve], dtype=float)
    rets = np.diff(eq) / np.where(eq[:-1] != 0.0, eq[:-1], 1.0)
    sd = rets.std()
    if sd == 0.0:
        return 0.0
    # Annualized at minute-bar cadence (matches qv_analytics).
    return float(rets.mean() / sd * np.sqrt(525_600))


def sweep_sma_cross(
    bars: Sequence[tuple],
    fasts: Sequence[int],
    slows: Sequence[int],
    *,
    qty: float = 0.5,
    starting_balance: float = 100_000.0,
    fee_rate: float = 0.0004,
) -> list[ScreenRow]:
    """FAST coarse vectorized sweep over an SMA-crossover ``(fast, slow)`` grid (non-authoritative).

    Returns rows sorted by vectorized Sharpe (descending). Combos with ``fast >= slow`` are skipped.
    Use this to rank a big grid in milliseconds, then re-run the top few through the AUTHORITATIVE
    ``qv_backtest.run`` (and check :func:`cross_check_vs_event`) before trusting any of them.
    """
    _, close = _closes(bars)
    rows: list[ScreenRow] = []
    for fast in fasts:
        for slow in slows:
            if fast >= slow:
                continue
            pos = sma_cross_positions(close, fast, slow, qty)
            res = vector_backtest(
                bars, pos, starting_balance=starting_balance, fee_rate=fee_rate
            )
            n_trades = sum(1 for _ts, side, _q, _p in res.fills if side > 0)  # entries
            rows.append(
                ScreenRow(
                    params={"fast": int(fast), "slow": int(slow)},
                    total_return=res.total_return,
                    sharpe=_sharpe(res.equity_curve),
                    n_trades=n_trades,
                )
            )
    rows.sort(key=lambda r: r.sharpe, reverse=True)
    return rows


def _snap_fills_to_grid(fills: list[Fill], bar_ts: np.ndarray) -> list[Fill]:
    """Snap each fill's timestamp to the nearest bar timestamp.

    Event-driven fills land at ``bar_ts + execution_latency``; with real bars that close at the last
    millisecond of the minute (``:59.999``), that latency can push a fill across a minute boundary,
    so a floor-bucketed comparison against the vectorized fills (stamped at ``bar_ts``) would
    spuriously disagree. Snapping both streams to the bar grid compares which BAR triggered a fill.
    """
    bt = np.sort(np.asarray(bar_ts, dtype=np.int64))
    out: list[Fill] = []
    for ts, side, qty, px in fills:
        i = int(np.searchsorted(bt, ts))
        cands = [j for j in (i - 1, i) if 0 <= j < len(bt)]
        best = min(cands, key=lambda j: abs(int(bt[j]) - int(ts))) if cands else None
        snapped_ts = int(bt[best]) if best is not None else int(ts)
        out.append((snapped_ts, int(side), float(qty), float(px)))
    return out


def cross_check_vs_event(
    bars: Sequence[tuple],
    fast: int,
    slow: int,
    *,
    qty: float = 0.5,
    symbol: str = "BTCUSDT",
    max_pnl_diff_bps: float = 50.0,
) -> list[str]:
    """Advisory drift check: run the vectorized screen AND the AUTHORITATIVE event-driven runner on
    the same bars, then return ``qv_parity.cross_check`` warnings (never raises).

    A non-empty list means the fast screen is misleading for this strategy (signal timing or the
    return proxy drifts beyond tolerance vs the parity-valid runner) — only the event-driven result
    is a parity surface. Both fill streams are snapped to the bar grid first (see
    :func:`_snap_fills_to_grid`). Needs the compiled ``qv_py`` for the event-driven leg.
    """
    import qv_backtest
    from qv_parity import SessionResult, cross_check
    from qv_strategy import SmaCross

    ts, close = _closes(bars)
    vec = vector_backtest(bars, sma_cross_positions(close, fast, slow, qty))
    event = SessionResult.from_backtest(
        qv_backtest.run(SmaCross(fast=fast, slow=slow, qty=qty), symbol=symbol, bars=bars)
    )
    vec_session = SessionResult(
        equity_curve=vec.equity_curve, fills=_snap_fills_to_grid(vec.fills, ts)
    )
    event_session = SessionResult(
        equity_curve=event.equity_curve, fills=_snap_fills_to_grid(event.fills, ts)
    )
    return cross_check(event_session, vec_session, max_pnl_diff_bps=max_pnl_diff_bps)


__all__ = [
    "VectorResult",
    "ScreenRow",
    "sma",
    "sma_cross_positions",
    "vector_backtest",
    "to_session",
    "sweep_sma_cross",
    "cross_check_vs_event",
]
