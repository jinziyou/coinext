"""coinext_analytics.trades — round-trip trade reconstruction and trade-level statistics.

A backtest's ``fills_log`` (``(ts_ns, symbol, side, qty, px)``; ``side`` +1 buy / -1 sell) is a
flat stream of executions. Most trade analytics (win rate, profit factor, average trade) are defined
over *round-trip trades* — a position opened and later closed — not individual fills. This module
folds the fill stream into closed :class:`Trade` round-trips using **FIFO** matching (the first lot
opened is the first closed), the same convention the Rust ``Position`` PnL uses, then reduces them
to :class:`TradeStats`.

Pure stdlib; no dependency on the compiled ``coinext_py`` (it consumes the already-materialized fills),
so the math is unit-testable without building the extension.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

# Quantities below this (in size units) are treated as fully closed — guards float dust left after
# FIFO matching unequal lot sizes.
_QTY_EPS = 1e-12

Fill = tuple[int, int, float, float]  # (ts_ns, side[+1 buy/-1 sell], qty, px)


@dataclass(frozen=True)
class Trade:
    """One closed round-trip: a position opened at ``entry`` and fully/partially closed at ``exit``.

    ``side`` is the *position* direction: ``+1`` long (opened by a buy, closed by a sell) or ``-1``
    short. ``pnl`` is realized PnL net of the ``fee_rate`` charged on both legs (gross when
    ``fee_rate == 0``). ``return_pct`` is ``pnl`` over the entry notional (``entry_px * qty``).
    """

    entry_ts: int
    exit_ts: int
    side: int
    qty: float
    entry_px: float
    exit_px: float
    pnl: float
    return_pct: float

    @property
    def holding_ns(self) -> int:
        """Holding period in nanoseconds (``exit_ts - entry_ts``)."""
        return self.exit_ts - self.entry_ts

    @property
    def is_win(self) -> bool:
        return self.pnl > 0.0


def _coalesce_partial_fills(fills: list[Fill]) -> list[Fill]:
    """Merge consecutive fills with the same ``(side, price)`` into one.

    Volume-participation partial fills split ONE resting limit order into several same-side fills
    at the SAME limit price across consecutive bars. Coalescing collapses such a run so the order
    counts as a single trade leg (win rate / avg-trade granularity) instead of N — PnL, turnover
    and exposure are identical either way. Only STRICTLY consecutive runs merge, so two distinct
    orders that fill at the same price but with other fills between them stay separate.
    """
    out: list[Fill] = []
    for ts, side, qty, px in fills:
        if out and out[-1][1] == side and out[-1][3] == px:
            p_ts, p_side, p_qty, p_px = out[-1]
            out[-1] = (p_ts, p_side, p_qty + float(qty), p_px)  # keep the first ts as the entry
        else:
            out.append((int(ts), int(side), float(qty), float(px)))
    return out


def reconstruct_trades(fills: list[Fill], *, fee_rate: float = 0.0) -> list[Trade]:
    """Fold a fill stream into closed round-trip :class:`Trade` objects via FIFO lot matching.

    A fill in the same direction as the open position adds a lot; an opposing fill closes open lots
    oldest-first, realizing one :class:`Trade` per matched lot, and any residual quantity opens a
    new lot in the opposite direction (a position flip). Lots still open at the end of the stream
    are *not* emitted (they have no realized PnL) — see :func:`open_exposure`.

    Consecutive same-``(side, price)`` fills are coalesced first (:func:`_coalesce_partial_fills`)
    so a partially-filled order is one trade, not many. ``fee_rate`` is charged on the notional of
    both legs (``(entry_px+exit_px) * qty * fee_rate``), so trade PnL is net when a rate is given.
    """
    # Open lots, all in the SAME direction at any time (an opposing fill closes them before any
    # residual flips direction). Each: [ts, qty_remaining, px, dir(+1 long / -1 short)].
    open_lots: deque[list] = deque()
    trades: list[Trade] = []

    for ts, side, qty, px in _coalesce_partial_fills(fills):
        remaining = float(qty)
        # Close opposing lots oldest-first.
        while open_lots and open_lots[0][3] == -side and remaining > _QTY_EPS:
            lot = open_lots[0]
            matched = min(lot[1], remaining)
            entry_px, exit_px, lot_dir = lot[2], float(px), lot[3]
            gross = (exit_px - entry_px) * matched * lot_dir
            fees = (entry_px + exit_px) * matched * fee_rate
            pnl = gross - fees
            notional = entry_px * matched
            trades.append(
                Trade(
                    entry_ts=lot[0],
                    exit_ts=int(ts),
                    side=lot_dir,
                    qty=matched,
                    entry_px=entry_px,
                    exit_px=exit_px,
                    pnl=pnl,
                    return_pct=(pnl / notional) if notional else 0.0,
                )
            )
            lot[1] -= matched
            remaining -= matched
            if lot[1] <= _QTY_EPS:
                open_lots.popleft()
        # Residual opens a new lot in this fill's direction (fresh position or a flip).
        if remaining > _QTY_EPS:
            open_lots.append([int(ts), remaining, float(px), int(side)])

    return trades


def open_exposure(fills: list[Fill]) -> tuple[int, float]:
    """Return the ``(net_side, qty)`` of any position still open after the fill stream.

    ``net_side`` is ``+1`` long / ``-1`` short / ``0`` flat; ``qty`` is the open size. Useful to
    flag a backtest ending still in a position (final equity carries unrealized PnL not in stats).
    """
    net = 0.0
    for _ts, side, qty, _px in fills:
        net += side * qty
    if abs(net) <= _QTY_EPS:
        return 0, 0.0
    return (1 if net > 0 else -1), abs(net)


@dataclass
class TradeStats:
    """Trade-level performance reduced from a list of closed :class:`Trade` round-trips."""

    n_trades: int
    n_wins: int
    n_losses: int
    win_rate: float
    profit_factor: float
    avg_trade: float
    avg_win: float
    avg_loss: float
    largest_win: float
    largest_loss: float
    gross_profit: float
    gross_loss: float
    total_pnl: float
    avg_holding_ns: float
    exposure: float  # fraction of bars with a non-flat position (0 if not computable)
    turnover: float  # gross traded notional / starting equity (0 if not computable)


def compute_trade_stats(
    trades: list[Trade],
    *,
    exposure: float = 0.0,
    turnover: float = 0.0,
) -> TradeStats:
    """Reduce closed :class:`Trade` round-trips to headline :class:`TradeStats`.

    ``exposure`` (time-in-market fraction) and ``turnover`` (traded notional / capital) depend on
    the bar count and starting equity, so they are passed in (see :func:`stats_from_result`); the
    purely trade-derived fields are computed here. ``profit_factor`` is ``inf`` when there are wins
    but no losses, and ``0`` when there are no trades.
    """
    n = len(trades)
    if n == 0:
        return TradeStats(0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    wins = [t for t in trades if t.pnl > 0.0]
    losses = [t for t in trades if t.pnl < 0.0]
    gross_profit = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)  # positive magnitude
    total_pnl = sum(t.pnl for t in trades)

    if gross_loss > 0.0:
        profit_factor = gross_profit / gross_loss
    else:
        profit_factor = float("inf") if gross_profit > 0.0 else 0.0

    return TradeStats(
        n_trades=n,
        n_wins=len(wins),
        n_losses=len(losses),
        win_rate=len(wins) / n,
        profit_factor=profit_factor,
        avg_trade=total_pnl / n,
        avg_win=(gross_profit / len(wins)) if wins else 0.0,
        avg_loss=(-gross_loss / len(losses)) if losses else 0.0,
        largest_win=max((t.pnl for t in trades), default=0.0),
        largest_loss=min((t.pnl for t in trades), default=0.0),
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        total_pnl=total_pnl,
        avg_holding_ns=sum(t.holding_ns for t in trades) / n,
        exposure=exposure,
        turnover=turnover,
    )


def _exposure(by_symbol: dict[str, list[Fill]], equity_curve: list[tuple[int, float]]) -> float:
    """Fraction of bars with a non-flat position in ANY instrument (time-in-market).

    Walks the bar timestamps in ``equity_curve``, tracking each symbol's running net position from
    its own fill stream, and counts a bar as "in market" if any symbol is non-flat. Per-symbol
    netting is essential for multi-instrument runs (a BTC long and an ETH long are both exposure).
    """
    if not equity_curve:
        return 0.0
    streams = {sym: sorted(f, key=lambda x: x[0]) for sym, f in by_symbol.items()}
    idx = {sym: 0 for sym in streams}
    net = {sym: 0.0 for sym in streams}
    in_market = 0
    for ts, _eq in equity_curve:
        for sym, fills in streams.items():
            i = idx[sym]
            while i < len(fills) and fills[i][0] <= ts:
                net[sym] += fills[i][1] * fills[i][2]  # side * qty
                i += 1
            idx[sym] = i
        if any(abs(n) > _QTY_EPS for n in net.values()):
            in_market += 1
    return in_market / len(equity_curve)


def stats_from_result(result, *, fee_rate: float = 0.0) -> TradeStats:
    """Compute :class:`TradeStats` directly from a ``coinext_py`` ``BacktestResult``.

    Round-trip trades are reconstructed **per instrument** (FIFO must never match a BTC buy against
    an ETH sell), then aggregated; ``exposure`` (per-symbol time-in-market) and ``turnover`` (gross
    traded notional / ``starting_equity``) are layered on. Single-instrument runs are the one-group
    case. ``result.fills_log`` rows are ``(ts, symbol, side, qty, px)``.
    """
    by_symbol: dict[str, list[Fill]] = {}
    gross_notional = 0.0
    for ts, sym, side, qty, px in result.fills_log:
        by_symbol.setdefault(str(sym), []).append((int(ts), int(side), float(qty), float(px)))
        gross_notional += abs(float(qty)) * float(px)

    trades: list[Trade] = []
    for fills in by_symbol.values():
        trades.extend(reconstruct_trades(fills, fee_rate=fee_rate))

    equity = [(int(ts), float(eq)) for ts, eq in result.equity_curve]
    exposure = _exposure(by_symbol, equity)
    start = float(result.starting_equity)
    turnover = (gross_notional / start) if start else 0.0
    return compute_trade_stats(trades, exposure=exposure, turnover=turnover)


__all__ = [
    "Trade",
    "TradeStats",
    "Fill",
    "reconstruct_trades",
    "open_exposure",
    "compute_trade_stats",
    "stats_from_result",
]
