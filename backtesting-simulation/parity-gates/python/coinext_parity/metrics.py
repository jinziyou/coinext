"""Parity metrics between backtest and sandbox sessions."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .session import SessionResult

_DEFAULT_TS_BUCKET_NS = 60_000_000_000


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
