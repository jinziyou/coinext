"""Replay lake/OHLCV bars through :class:`PaperEquityBroker` (research paper path).

Applies A-share **T+1** and **涨跌停** via the paper broker while walking bars in time order.
This is intentionally separate from the Rust Kernel backtest — it validates broker-side rules
before a full equity ``ExecutionClient`` exists.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from .base import BrokerFill, BrokerOrder, PaperEquityBroker
from .rules import trade_date_from_ns


@dataclass(slots=True)
class ReplayResult:
    """Summary of a paper-equity bar replay."""

    venue: str
    symbol: str
    bars: int
    orders: list[BrokerOrder] = field(default_factory=list)
    fills: list[BrokerFill] = field(default_factory=list)
    final_cash: dict[str, float] = field(default_factory=dict)
    final_positions: dict[str, float] = field(default_factory=dict)
    rejected: int = 0
    filled: int = 0
    mark_to_market: float | None = None
    """Equity in listing currency: cash[ccy] + position * last close (if available)."""

    def summary(self) -> str:
        lines = [
            f"paper-equity {self.venue}/{self.symbol}: bars={self.bars} "
            f"fills={self.filled} rejected={self.rejected}",
            f"  positions={self.final_positions}",
            f"  cash={self.final_cash}",
        ]
        if self.mark_to_market is not None:
            lines.append(f"  mtm≈{self.mark_to_market:,.2f}")
        return "\n".join(lines)


def _ohlcv(row: Sequence) -> tuple[int, float, float, float, float, float]:
    if len(row) >= 6:
        return (
            int(row[0]),
            float(row[1]),
            float(row[2]),
            float(row[3]),
            float(row[4]),
            float(row[5]),
        )
    if len(row) == 5:
        ts, o, h, lo, c = row
        return int(ts), float(o), float(h), float(lo), float(c), 0.0
    if len(row) == 2:
        ts, c = row
        c = float(c)
        return int(ts), c, c, c, c, 0.0
    raise ValueError(f"unsupported bar shape len={len(row)}")


def _currency(venue: str) -> str:
    try:
        from coinext_data import instrument_spec

        return instrument_spec(venue).currency
    except Exception:
        return "USD"


def _lot(venue: str, symbol: str) -> int:
    try:
        from coinext_data import instrument_spec

        return max(1, int(instrument_spec(venue, symbol).lot_size))
    except Exception:
        return 1


def replay_bars(
    bars: Sequence[tuple],
    *,
    venue: str,
    symbol: str,
    broker: PaperEquityBroker | None = None,
    starting_cash: dict[str, float] | None = None,
    strategy: str = "sma",
    fast: int = 5,
    slow: int = 20,
    qty: float | None = None,
) -> ReplayResult:
    """Walk ``bars`` chronologically, submit paper orders under A-share rules.

    Strategies:

    * ``sma`` — cross of close SMAs (buy on golden, sell on death); respects T+1/limits.
    * ``buyhold`` — market-buy ``qty`` lots on first bar, hold to end.
    * ``none`` — only drive marks / session day (no orders); useful for mark plumbing tests.
    """
    v = venue.strip().upper()
    s = symbol.strip().upper()
    ccy = _currency(v)
    lot = _lot(v, s)
    default_qty = float(qty) if qty is not None else float(lot)

    if broker is None:
        cash = starting_cash or {ccy: 1_000_000.0}
        if ccy not in cash:
            cash = {**cash, ccy: 1_000_000.0}
        broker = PaperEquityBroker(starting_cash=cash)
    broker.connect()

    closes: list[float] = []
    all_orders: list[BrokerOrder] = []
    n_rejected = 0
    n_filled = 0
    last_close: float | None = None

    for row in bars:
        ts, o, h, lo, c, _vol = _ohlcv(row)
        day = trade_date_from_ns(ts, v)
        broker.set_session_day(day)
        # Drive OHLC matching + prev_close roll (session-local T+1 / 涨跌停).
        broker.on_bar(v, s, high=h, low=lo, close=c, open_=o, ts_ns=ts)
        last_close = c
        closes.append(c)

        if strategy == "none":
            continue

        if strategy == "buyhold":
            if len(closes) == 1:
                odr = broker.submit_market(v, s, "buy", default_qty)
                all_orders.append(odr)
                if odr.status == "rejected":
                    n_rejected += 1
                elif odr.status == "filled":
                    n_filled += 1
            continue

        if strategy == "sma":
            if len(closes) < slow:
                continue
            f = sum(closes[-fast:]) / fast
            sl = sum(closes[-slow:]) / slow
            prev_f = sum(closes[-fast - 1 : -1]) / fast if len(closes) > fast else f
            prev_sl = sum(closes[-slow - 1 : -1]) / slow if len(closes) > slow else sl
            key = f"{v}:{s}"
            pos = broker.positions().get(key, 0.0)
            # Golden cross → buy one lot if flat.
            if prev_f <= prev_sl and f > sl and pos < 1e-12:
                odr = broker.submit_market(v, s, "buy", default_qty)
                all_orders.append(odr)
                if odr.status == "rejected":
                    n_rejected += 1
                elif odr.status in ("filled", "partial"):
                    n_filled += 1
            # Death cross → sell sellable (T+1 may reject same-day).
            elif prev_f >= prev_sl and f < sl and pos > 1e-12:
                sell_qty = min(pos, broker.sellable_qty(v, s, day=day))
                if sell_qty > 1e-12:
                    odr = broker.submit_market(v, s, "sell", sell_qty)
                    all_orders.append(odr)
                    if odr.status == "rejected":
                        n_rejected += 1
                    elif odr.status in ("filled", "partial"):
                        n_filled += 1
            continue

        raise ValueError(f"unknown strategy {strategy!r} (sma|buyhold|none)")

    pos = broker.positions()
    cash = broker.cash()
    mtm = None
    if last_close is not None:
        key = f"{v}:{s}"
        mtm = cash.get(ccy, 0.0) + pos.get(key, 0.0) * last_close

    return ReplayResult(
        venue=v,
        symbol=s,
        bars=len(bars),
        orders=all_orders,
        fills=broker.fills(),
        final_cash=cash,
        final_positions=pos,
        rejected=n_rejected,
        filled=n_filled,
        mark_to_market=mtm,
    )


def replay_from_lake(
    venue: str,
    symbol: str,
    *,
    interval: str = "1d",
    lake_root: str | None = None,
    strategy: str = "sma",
    fast: int = 5,
    slow: int = 20,
    qty: float | None = None,
    starting_cash: dict[str, float] | None = None,
    apply_calendar: bool = True,
) -> ReplayResult:
    """Load OHLCV from the data lake and :func:`replay_bars`."""
    from coinext_data import DataLake, filter_trading_bars, resolve_listing

    v, s = resolve_listing(venue, symbol)
    lake = DataLake(lake_root)
    rows = lake.read_ohlcv(v, s, interval)
    if not rows:
        raise FileNotFoundError(
            f"no bars for {v}/{s} {interval} under {lake.root} — run "
            f"`coinext download --venue {v} --symbols {s}` first"
        )
    if apply_calendar and interval in ("1d", "5d", "1wk"):
        rows, _ = filter_trading_bars(rows, v)
    return replay_bars(
        rows,
        venue=v,
        symbol=s,
        strategy=strategy,
        fast=fast,
        slow=slow,
        qty=qty,
        starting_cash=starting_cash,
    )


@dataclass(slots=True)
class PortfolioReplayResult:
    """Aggregate paper-equity results across several listings."""

    results: list[ReplayResult]
    final_cash: dict[str, float]
    final_positions: dict[str, float]

    def summary(self) -> str:
        lines = [r.summary() for r in self.results]
        lines.append(f"portfolio cash={self.final_cash}")
        lines.append(f"portfolio positions={self.final_positions}")
        return "\n".join(lines)


def replay_portfolio(
    listings: Sequence[tuple[str, str]],
    bars_by_key: dict[str, Sequence[tuple]],
    *,
    starting_cash: dict[str, float] | None = None,
    strategy: str = "sma",
    fast: int = 5,
    slow: int = 20,
    qty: float | None = None,
) -> PortfolioReplayResult:
    """Replay many ``(venue, symbol)`` series on **one** shared :class:`PaperEquityBroker`.

    Bars are merge-sorted by timestamp so multi-market T+1 / cash interact correctly.
    """
    cash = starting_cash or {"USD": 1_000_000.0, "CNY": 1_000_000.0, "HKD": 1_000_000.0}
    broker = PaperEquityBroker(starting_cash=cash)
    broker.connect()

    # Build a unified event stream: (ts, venue, symbol, row)
    events: list[tuple[int, str, str, tuple]] = []
    for venue, symbol in listings:
        key = f"{venue}:{symbol}"
        series = bars_by_key.get(key) or bars_by_key.get(symbol) or []
        for row in series:
            events.append((int(row[0]), venue.upper(), symbol.upper(), tuple(row)))
    events.sort(key=lambda e: (e[0], e[1], e[2]))

    # Per-symbol close history for SMA.
    closes: dict[str, list[float]] = {}
    results_meta: dict[str, dict[str, Any]] = {}
    for venue, symbol in listings:
        key = f"{venue.upper()}:{symbol.upper()}"
        closes[key] = []
        results_meta[key] = {
            "venue": venue.upper(),
            "symbol": symbol.upper(),
            "bars": 0,
            "orders": [],
            "filled": 0,
            "rejected": 0,
        }

    for _ts, venue, symbol, row in events:
        key = f"{venue}:{symbol}"
        meta = results_meta[key]
        meta["bars"] += 1
        ts_i, o, h, lo, c, _v = _ohlcv(row)
        day = trade_date_from_ns(ts_i, venue)
        broker.set_session_day(day)
        broker.on_bar(venue, symbol, high=h, low=lo, close=c, open_=o, ts_ns=ts_i)
        closes[key].append(c)
        default_qty = float(qty) if qty is not None else float(_lot(venue, symbol))

        if strategy == "none":
            continue
        if strategy == "buyhold":
            if len(closes[key]) == 1:
                odr = broker.submit_market(venue, symbol, "buy", default_qty)
                meta["orders"].append(odr)
                if odr.status == "rejected":
                    meta["rejected"] += 1
                elif odr.status == "filled":
                    meta["filled"] += 1
            continue
        if strategy == "sma":
            hist = closes[key]
            if len(hist) < slow:
                continue
            f = sum(hist[-fast:]) / fast
            sl = sum(hist[-slow:]) / slow
            prev_f = sum(hist[-fast - 1 : -1]) / fast if len(hist) > fast else f
            prev_sl = sum(hist[-slow - 1 : -1]) / slow if len(hist) > slow else sl
            pos = broker.positions().get(key, 0.0)
            if prev_f <= prev_sl and f > sl and pos < 1e-12:
                odr = broker.submit_market(venue, symbol, "buy", default_qty)
                meta["orders"].append(odr)
                if odr.status == "rejected":
                    meta["rejected"] += 1
                elif odr.status in ("filled", "partial"):
                    meta["filled"] += 1
            elif prev_f >= prev_sl and f < sl and pos > 1e-12:
                sell_qty = min(pos, broker.sellable_qty(venue, symbol, day=day))
                if sell_qty > 1e-12:
                    odr = broker.submit_market(venue, symbol, "sell", sell_qty)
                    meta["orders"].append(odr)
                    if odr.status == "rejected":
                        meta["rejected"] += 1
                    elif odr.status in ("filled", "partial"):
                        meta["filled"] += 1

    out_results: list[ReplayResult] = []
    for key, meta in results_meta.items():
        last = closes[key][-1] if closes[key] else None
        pos = broker.positions().get(key, 0.0)
        ccy = _currency(meta["venue"])
        mtm = None
        if last is not None:
            mtm = broker.cash().get(ccy, 0.0) + pos * last
        out_results.append(
            ReplayResult(
                venue=meta["venue"],
                symbol=meta["symbol"],
                bars=meta["bars"],
                orders=meta["orders"],
                fills=[f for f in broker.fills() if f"{f.venue}:{f.symbol}" == key],
                final_cash=broker.cash(),
                final_positions={key: pos} if pos else {},
                rejected=meta["rejected"],
                filled=meta["filled"],
                mark_to_market=mtm,
            )
        )
    return PortfolioReplayResult(
        results=out_results,
        final_cash=broker.cash(),
        final_positions=broker.positions(),
    )


def replay_portfolio_from_lake(
    listings: Sequence[tuple[str, str]] | str,
    *,
    interval: str = "1d",
    lake_root: str | None = None,
    strategy: str = "sma",
    fast: int = 5,
    slow: int = 20,
    qty: float | None = None,
    starting_cash: dict[str, float] | None = None,
    apply_calendar: bool = True,
) -> PortfolioReplayResult:
    """Load several lake series and :func:`replay_portfolio`.

    ``listings`` may be ``[(venue, symbol), ...]`` or a market-group string expanded via
    ``resolve_listings(group, "@default")``.
    """
    from coinext_data import DataLake, filter_trading_bars, resolve_listings

    if isinstance(listings, str):
        pairs = resolve_listings(listings, "@default")
    else:
        pairs = list(listings)
    lake = DataLake(lake_root)
    bars: dict[str, list] = {}
    for v, s in pairs:
        rows = lake.read_ohlcv(v, s, interval)
        if not rows:
            raise FileNotFoundError(f"no bars for {v}/{s} {interval} under {lake.root}")
        if apply_calendar and interval in ("1d", "5d", "1wk"):
            rows, _ = filter_trading_bars(rows, v)
        bars[f"{v}:{s}"] = rows
    return replay_portfolio(
        pairs,
        bars,
        starting_cash=starting_cash,
        strategy=strategy,
        fast=fast,
        slow=slow,
        qty=qty,
    )


__all__ = [
    "PortfolioReplayResult",
    "ReplayResult",
    "replay_bars",
    "replay_from_lake",
    "replay_portfolio",
    "replay_portfolio_from_lake",
]
