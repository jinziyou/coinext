"""qv_backtest — the BacktestNode.

The AUTHORITATIVE event-driven runner: it drives a Python ``Strategy`` through the Rust kernel via
``qv_py.run_backtest`` (the SAME engines + SimulatedExecutionClient the live path uses). A separate
vectorized ``populate_*`` screen (non-authoritative) is a future addition; only this path is
parity-valid.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Instrument:
    """The instrument a backtest trades. The ``bars``/ticks ARE this instrument's own price series.

    Defaults to a spot pair (backward compatible). Use the constructors for derivatives — PnL scales
    by ``multiplier``, and the contract metadata (``strike``/``option_right``/``expiry_ns``/
    ``underlying``) is carried for the expiry-settlement and pricing phases. Examples::

        Instrument.equity()
        Instrument.future(multiplier=10.0, expiry_ns=ts, underlying="BTCUSDT")
        Instrument.option(strike=50_000, right="call", expiry_ns=ts, multiplier=1.0,
                          underlying="BTCUSDT")
    """

    asset_class: str = "spot"
    multiplier: float = 1.0
    strike: float | None = None
    option_right: str | None = None  # "call" / "put"
    expiry_ns: int | None = None
    underlying: str | None = None

    @classmethod
    def spot(cls) -> Instrument:
        return cls()

    @classmethod
    def equity(cls) -> Instrument:
        return cls(asset_class="equity")

    @classmethod
    def future(
        cls, *, multiplier: float = 1.0, expiry_ns: int, underlying: str | None = None
    ) -> Instrument:
        return cls(
            asset_class="future", multiplier=multiplier, expiry_ns=expiry_ns, underlying=underlying
        )

    @classmethod
    def option(
        cls,
        *,
        strike: float,
        right: str,
        expiry_ns: int,
        underlying: str,
        multiplier: float = 1.0,
    ) -> Instrument:
        if right.lower() not in ("call", "put", "c", "p"):
            raise ValueError("option right must be 'call' or 'put'")
        return cls(
            asset_class="option",
            multiplier=multiplier,
            strike=strike,
            option_right=right,
            expiry_ns=expiry_ns,
            underlying=underlying,
        )

try:
    import qv_py  # the compiled Rust extension (maturin develop)
except ImportError as exc:  # pragma: no cover - surfaced as a clear setup error
    raise ImportError(
        "qv_py extension not built. Run: "
        "uvx maturin develop --manifest-path crates/qv-py/Cargo.toml --features python"
    ) from exc


def _to_ohlcv(bars: list[tuple]) -> list[tuple[int, float, float, float, float, float]]:
    """Normalize a bar series to ``(ts, open, high, low, close, volume)`` for the Rust bridge.

    Accepts three row shapes (no mixing within one series):

    * ``(ts, close)`` — flattened to ``(ts, c, c, c, c, 0)``: no intrabar range, ``volume=0`` so the
      sim applies no participation cap (resting orders fill fully).
    * ``(ts, open, high, low, close)`` — real high/low (limit fills), ``volume=0`` (no cap).
    * ``(ts, open, high, low, close, volume)`` — full OHLCV: volume drives participation-based
      partial fills (a large resting order fills over several bars).
    """
    out: list[tuple[int, float, float, float, float, float]] = []
    for row in bars:
        if len(row) == 2:
            ts, c = row
            c = float(c)
            out.append((int(ts), c, c, c, c, 0.0))
        elif len(row) == 5:
            ts, o, h, lo, c = row
            out.append((int(ts), float(o), float(h), float(lo), float(c), 0.0))
        elif len(row) == 6:
            ts, o, h, lo, c, v = row
            out.append((int(ts), float(o), float(h), float(lo), float(c), float(v)))
        else:
            raise ValueError(
                "bar rows must be (ts, close), (ts, o, h, l, c), or (ts, o, h, l, c, volume); "
                f"got {len(row)} cols"
            )
    return out


def run(
    strategy: Any,
    *,
    symbol: str = "BTCUSDT",
    venue: str = "BINANCE",
    starting_balance: float = 100_000.0,
    bars: list[tuple],
    price_precision: int = 2,
    size_precision: int = 3,
    maker_fee: float = 0.0002,
    taker_fee: float = 0.0004,
    queue_ahead_factor: float = 0.0,
    quotes: list[tuple] | None = None,
    trades: list[tuple] | None = None,
    instrument: Instrument | None = None,
) -> Any:
    """Run ``strategy`` over ``bars``; return the BacktestResult.

    ``bars`` may be close-only ``(ts, close)``, OHLC ``(ts, o, h, l, c)``, or OHLCV
    ``(ts, o, h, l, c, volume)``. OHLC enables OHLC-aware fills (resting limits match each bar's
    high/low; market orders slip within the bar range); volume drives participation-based partial
    fills (a large resting order fills over several bars). ``queue_ahead_factor`` (>0) enables
    limit-order **queue position**: a passive order waits behind ~that × bar volume at a price the
    market only TOUCHES (a price trading THROUGH the level still fills).

    ``quotes`` (``(ts, bid, ask, bid_size, ask_size)``) and ``trades`` (``(ts, price, size,
    aggressor[+1 buy/-1 sell])``) are optional tick streams interleaved with the bars by timestamp;
    they fire ``on_quote`` / ``on_trade`` and also drive the mark + resting-limit fills against tick
    prices. Synthesize them from bars (:func:`synth_quotes`, :func:`synth_trades`) or feed real ones
    (``qv_data.fetch_binance_agg_trades``).

    ``instrument`` (an :class:`Instrument`) selects the asset class — spot (default), equity,
    future, or option. Derivatives scale PnL by the contract ``multiplier``; the ``bars`` are that
    instrument's own price series (e.g. an option's premium series).
    """
    inst = instrument or Instrument.spot()
    return qv_py.run_backtest(
        strategy,
        symbol,
        venue,
        starting_balance,
        _to_ohlcv(bars),
        price_precision=price_precision,
        size_precision=size_precision,
        maker_fee=maker_fee,
        taker_fee=taker_fee,
        queue_ahead_factor=queue_ahead_factor,
        asset_class=inst.asset_class,
        multiplier=inst.multiplier,
        strike=inst.strike,
        option_right=inst.option_right,
        expiry_ns=inst.expiry_ns,
        underlying=inst.underlying,
        quotes=[
            (int(ts), float(b), float(a), float(bs), float(az))
            for ts, b, a, bs, az in (quotes or [])
        ],
        trades=[(int(ts), float(p), float(s), int(side)) for ts, p, s, side in (trades or [])],
    )


def run_multi(
    strategy: Any,
    *,
    bars: dict[str, list[tuple]],
    venue: str = "BINANCE",
    starting_balance: float = 100_000.0,
    instruments: dict[str, dict] | None = None,
    price_precision: int = 2,
    size_precision: int = 3,
    maker_fee: float = 0.0002,
    taker_fee: float = 0.0004,
    queue_ahead_factor: float = 0.0,
) -> Any:
    """Run ``strategy`` over MULTIPLE instruments through one kernel; return the BacktestResult.

    ``bars`` maps ``symbol -> bar rows`` (each list close-only or OHLC, like :func:`run`). The
    strategy reads ``bar.symbol`` and targets orders with the optional ``symbol`` arg on
    ``ctx.submit_market``/``submit_limit``/``position``. Per-symbol instrument params can be
    overridden via ``instruments[symbol] = {"price_precision": .., "size_precision": .., ...}``;
    otherwise the ``*_precision``/``*_fee`` defaults apply to every symbol. All symbols share the
    venue, the settlement currency, and the single starting balance (one portfolio).
    """
    symbols = sorted(bars)
    if not symbols:
        raise ValueError("run_multi needs at least one symbol in `bars`")
    overrides = instruments or {}
    specs: list[tuple[str, int, int, float, float]] = []
    tagged: list[tuple[int, str, float, float, float, float, float]] = []
    for sym in symbols:
        ov = overrides.get(sym, {})
        specs.append(
            (
                sym,
                int(ov.get("price_precision", price_precision)),
                int(ov.get("size_precision", size_precision)),
                float(ov.get("maker_fee", maker_fee)),
                float(ov.get("taker_fee", taker_fee)),
            )
        )
        for ts, o, h, lo, c, v in _to_ohlcv(bars[sym]):
            tagged.append((int(ts), sym, o, h, lo, c, v))
    return qv_py.run_backtest_multi(
        strategy, venue, starting_balance, specs, tagged, queue_ahead_factor=queue_ahead_factor
    )


def synth_quotes(bars: list[tuple], *, spread_bps: float = 2.0) -> list[tuple]:
    """Synthesize a top-of-book quote per bar: bid/ask are the close ± half a ``spread_bps`` spread,
    sizes are half the bar volume each. A coarse stand-in for real bookTicker data (which Binance
    does not serve via REST history) — enough to exercise ``on_quote`` and quote-driven logic.
    """
    out: list[tuple] = []
    for ts, _o, _h, _lo, c, vol in _to_ohlcv(bars):
        half = c * spread_bps / 2.0 / 1e4
        out.append((int(ts), c - half, c + half, vol / 2.0, vol / 2.0))
    return out


def synth_trades(bars: list[tuple]) -> list[tuple]:
    """Synthesize one trade print per bar at the close, sized by bar volume, with the aggressor
    inferred from the bar direction (up bar -> buy-aggressor). A coarse stand-in for real aggTrades;
    use ``qv_data.fetch_binance_agg_trades`` for genuine per-print microstructure.
    """
    out: list[tuple] = []
    for ts, o, _h, _lo, c, vol in _to_ohlcv(bars):
        out.append((int(ts), c, vol, 1 if c >= o else -1))
    return out


def synthetic_bars(
    n: int = 400,
    *,
    base: float = 50_000.0,
    amplitude: float = 1_500.0,
    period: int = 40,
    trend_per_bar: float = 12.0,
    start_ns: int = 1_700_000_000_000_000_000,
    step_ns: int = 60_000_000_000,
) -> list[tuple[int, float]]:
    """Deterministic sine-wave + trend close series (no RNG — reproducible)."""
    bars = []
    for i in range(n):
        phase = (i / period) * math.tau
        close = base + amplitude * math.sin(phase) + i * trend_per_bar
        bars.append((start_ns + i * step_ns, close))
    return bars


def synthetic_ohlc_bars(
    n: int = 400,
    *,
    base: float = 50_000.0,
    amplitude: float = 1_500.0,
    period: int = 40,
    trend_per_bar: float = 12.0,
    wick: float = 0.004,
    volume: float = 100.0,
    start_ns: int = 1_700_000_000_000_000_000,
    step_ns: int = 60_000_000_000,
) -> list[tuple[int, float, float, float, float, float]]:
    """Deterministic OHLCV series: the sine+trend close wrapped with high/low wicks + a volume.

    ``wick`` is the fractional half-range of each bar (high/low sit ``wick`` above/below the
    open-close band), so bars have a real intrabar range that exercises OHLC-aware limit fills.
    ``volume`` is a constant per-bar traded size (ample by default, so typical small orders fill in
    one bar; a large order relative to it partial-fills over several). ``open`` is the previous
    bar's close (the first bar opens at its own close). No RNG.
    """
    closes = [c for _, c in synthetic_bars(n, base=base, amplitude=amplitude, period=period,
                                           trend_per_bar=trend_per_bar, start_ns=start_ns,
                                           step_ns=step_ns)]
    bars: list[tuple[int, float, float, float, float, float]] = []
    for i, close in enumerate(closes):
        open_ = closes[i - 1] if i > 0 else close
        hi = max(open_, close) * (1.0 + wick)
        lo = min(open_, close) * (1.0 - wick)
        bars.append((start_ns + i * step_ns, open_, hi, lo, close, volume))
    return bars


__all__ = [
    "Instrument",
    "run",
    "run_multi",
    "synth_quotes",
    "synth_trades",
    "synthetic_bars",
    "synthetic_ohlc_bars",
]
