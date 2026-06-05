"""Quote/trade tick feed: on_quote/on_trade fire, and ticks drive marks + resting-limit fills.

The bar-only backtest never emitted quotes/trades; passing the optional `quotes`/`trades` streams to
`qv_backtest.run` interleaves them with the bars so the handlers fire and the sim matches resting
limits against tick prices. Requires the compiled qv_py extension.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PYTHON_ROOT = Path(__file__).resolve().parents[1] / "python"
if str(_PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(_PYTHON_ROOT))

pytest.importorskip("qv_py", reason="build qv_py: uvx maturin develop --features python")

import qv_backtest as bt  # noqa: E402
from qv_strategy import Strategy  # noqa: E402

STEP, BASE = 60_000_000_000, 1_700_000_000_000_000_000


def test_on_quote_and_on_trade_fire_with_tick_data():
    class Recorder(Strategy):
        def __init__(self):
            self.quotes = []
            self.trades = []

        def on_quote(self, q, ctx):
            self.quotes.append((q.symbol, q.bid, q.ask))

        def on_trade(self, tr, ctx):
            self.trades.append((tr.symbol, tr.price, tr.size))

    bars = bt.synthetic_ohlc_bars(40)
    rec = Recorder()
    bt.run(rec, bars=bars, quotes=bt.synth_quotes(bars), trades=bt.synth_trades(bars))
    assert len(rec.quotes) == 40 and len(rec.trades) == 40
    assert all(s == "BTCUSDT" for s, _b, _a in rec.quotes)
    assert all(bid < ask for _s, bid, ask in rec.quotes)  # well-formed top of book


def test_equity_curve_stays_bar_cadence_with_ticks():
    # 40 bars + 40 quotes + 40 trades = 120 market events, but the equity curve must sample at BAR
    # cadence (40 points) so the per-bar annualized metrics aren't distorted by sub-bar ticks.
    from qv_strategy import SmaCross

    bars = bt.synthetic_ohlc_bars(40)
    q, t = bt.synth_quotes(bars), bt.synth_trades(bars)
    res = bt.run(SmaCross(5, 15), bars=bars, quotes=q, trades=t)
    assert len(res.equity_curve) == 40


def test_trades_only_do_not_change_the_backtest():
    # Trades fire on_trade and set the mark to the print price (= bar close) but add no bid/ask, so
    # market orders (which reference the mark) and the equity curve are unchanged vs no ticks.
    from qv_strategy import SmaCross

    bars = bt.synthetic_ohlc_bars(40)
    res = bt.run(SmaCross(5, 15), bars=bars, trades=bt.synth_trades(bars))
    plain = bt.run(SmaCross(5, 15), bars=bars)
    assert list(res.equity_curve) == list(plain.equity_curve)


def test_quotes_set_the_market_reference_to_bid_ask():
    # With a quote in the book, a market BUY fills at the ASK side (more realistic than the close
    # mark). Same bars, a wide quote -> the buy fills meaningfully above the close.
    class BuyOnBar2(Strategy):
        def __init__(self):
            self.n = 0

        def on_bar(self, bar, ctx):
            self.n += 1
            if self.n == 2:  # by bar 2, bar 1's quote is in the book
                ctx.submit_market("buy", 1.0)

    BuyOnce = BuyOnBar2
    bars = [(BASE + i * STEP, 100.0, 100.0, 100.0, 100.0, 10.0) for i in range(4)]
    quotes = [(BASE + i * STEP, 99.0, 101.0, 5.0, 5.0) for i in range(4)]  # bid 99 / ask 101
    with_q = bt.run(BuyOnce(), bars=bars, quotes=quotes)
    plain = bt.run(BuyOnce(), bars=bars)
    # The buy fill price is logged in fills_log (ts, symbol, side, qty, px).
    px_q = with_q.fills_log[0][4]
    px_plain = plain.fills_log[0][4]
    assert px_q > px_plain  # filled at the ask (~101), not the close (~100)


def test_quotes_and_trades_off_by_default():
    # Without the streams, the handlers never fire (a bar-only backtest is unchanged).
    class Recorder(Strategy):
        def __init__(self):
            self.q = 0
            self.t = 0

        def on_quote(self, q, ctx):
            self.q += 1

        def on_trade(self, tr, ctx):
            self.t += 1

    rec = Recorder()
    bt.run(rec, bars=bt.synthetic_bars(30))
    assert rec.q == 0 and rec.t == 0


def test_trade_tick_fills_a_resting_limit():
    # A buy limit @95 rests while the bars stay flat at 100 (no bar ever crosses it). A single TRADE
    # print at 94 makes the sim fill it; without that trade it never fills.
    class LimitOnce(Strategy):
        def __init__(self):
            self.done = False

        def on_bar(self, bar, ctx):
            if not self.done:
                self.done = True
                ctx.submit_limit("buy", 1.0, 95.0)

    bars = [(BASE + i * STEP, 100.0, 100.0, 100.0, 100.0, 10.0) for i in range(4)]  # flat at 100
    trade = [(BASE + STEP // 2, 94.0, 1.0, -1)]  # a sell-aggressor print at 94, after bar 0

    assert bt.run(LimitOnce(), bars=bars, trades=trade).fills == 1
    assert bt.run(LimitOnce(), bars=bars).fills == 0  # no trade -> the limit never crosses


def test_synth_helpers_shapes():
    bars = [(1, 100.0, 101.0, 99.0, 100.5, 8.0)]
    (q,) = bt.synth_quotes(bars, spread_bps=2.0)
    ts, bid, ask, bs, az = q
    assert ts == 1 and bid < 100.5 < ask
    assert ask - bid == pytest.approx(100.5 * 2.0 / 1e4)
    assert bs == pytest.approx(4.0) and az == pytest.approx(4.0)
    (t,) = bt.synth_trades(bars)
    assert t == (1, 100.5, 8.0, 1)  # up bar (close >= open) -> buy aggressor


def test_real_agg_trades_drive_on_trade():
    # Fetch REAL Binance aggTrades and confirm on_trade fires on the real prints. Skips if offline.
    from qv_data import fetch_binance_agg_trades

    try:
        trades = fetch_binance_agg_trades("BTCUSDT", limit=200)
    except Exception as exc:  # pragma: no cover - network-dependent
        pytest.skip(f"aggTrades fetch unavailable: {exc}")
    assert trades and all(len(t) == 4 and t[3] in (1, -1) for t in trades)

    # Wrap them around a couple of bars at the same price scale so the run is well-formed.
    px = trades[0][1]
    t0 = trades[0][0]
    bars = [(t0 - STEP, px), (t0 + len(trades) * 1_000_000 + STEP, px)]

    class Counter(Strategy):
        def __init__(self):
            self.n = 0

        def on_trade(self, tr, ctx):
            self.n += 1

    c = Counter()
    bt.run(c, bars=bars, trades=trades)
    assert c.n == len(trades)
