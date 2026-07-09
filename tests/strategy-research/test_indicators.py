"""coinext_indicators — the Rust streaming indicators exposed to Python (via coinext_py).

Unit tests assert the Python-visible values equal the Rust crate's (coinext-indicators), and an
integration test drives an RSI strategy through the Rust kernel. Requires the compiled extension.
"""

from __future__ import annotations

import pytest

pytest.importorskip("coinext_py", reason="build coinext_py: uvx maturin develop --features python")

from coinext_indicators import Atr, Bollinger, Ema, Macd, Resampler, Rsi, Sma, Vwap  # noqa: E402


def test_sma_window_matches_rust():
    s = Sma(3)
    s.update(1.0)
    s.update(2.0)
    assert s.value() is None and not s.is_ready()  # not warm
    s.update(3.0)
    assert s.value() == pytest.approx(2.0) and s.is_ready()
    s.update(6.0)  # window [2,3,6]
    assert s.value() == pytest.approx(11.0 / 3.0)


def test_ema_seeds_and_tracks():
    e = Ema(2)
    e.update(10.0)
    e.update(20.0)
    v = e.value()
    assert v is not None and 10.0 < v < 20.0


def test_rsi_all_gains_is_100():
    r = Rsi(3)
    for v in [1.0, 2.0, 3.0, 4.0, 5.0]:
        r.update(v)
    assert r.value() == pytest.approx(100.0)


def test_rsi_warmup_and_midrange():
    r = Rsi(4)
    assert r.value() is None
    for v in [10.0, 11.0, 10.5, 11.5, 11.0]:  # mixed gains/losses
        r.update(v)
    v = r.value()
    assert v is not None and 0.0 < v < 100.0


def test_atr_update_hlc():
    a = Atr(2)
    assert a.value() is None
    a.update(10.0, 8.0, 9.0)  # TR = 2 (no prev close)
    a.update(11.0, 9.0, 10.0)  # TR = max(2, |11-9|, |9-9|) = 2
    assert a.value() == pytest.approx(2.0)


def test_macd_histogram_consistent():
    m = Macd(3, 6, 4)
    assert m.value() is None
    for i in range(1, 31):
        m.update(float(i))
    macd, signal, hist = m.value()
    assert hist == pytest.approx(macd - signal)
    assert macd > 0.0  # rising series -> fast EMA leads


def test_bollinger_known_stddev():
    b = Bollinger(8, 1.0)  # 1-sigma
    for v in [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]:  # mean 5, population sd 2
        b.update(v)
    lo, mid, up = b.value()
    assert mid == pytest.approx(5.0)
    assert lo == pytest.approx(3.0) and up == pytest.approx(7.0)


def test_vwap_weights_by_volume():
    v = Vwap(2)
    assert v.value() is None
    v.update(100.0, 1.0)
    v.update(110.0, 3.0)  # 430 / 4
    assert v.value() == pytest.approx(107.5)


@pytest.mark.parametrize("ctor", [Sma, Ema, Rsi, Atr, Macd, Bollinger, Vwap])
def test_period_must_be_positive(ctor):
    with pytest.raises(ValueError):
        ctor(0)


# --------------------------------------------------------------------------------------------------
# Multi-timeframe resampler.
# --------------------------------------------------------------------------------------------------
def test_resampler_aggregates_ohlcv():
    r = Resampler(3)
    assert r.update(1, 10.0, 12.0, 9.0, 11.0, 5.0) is None
    assert r.update(2, 11.0, 13.0, 10.0, 12.0, 4.0) is None
    bar = r.update(3, 12.0, 12.5, 8.0, 9.0, 6.0)  # completes the 3-bar window
    # ts = last; open = first; high = max; low = min; close = last; volume = sum.
    assert bar == (3, 10.0, 13.0, 8.0, 9.0, 15.0)
    # The next window starts fresh.
    assert r.update(4, 9.0, 9.5, 8.5, 9.2, 1.0) is None


def test_resampler_rejects_bad_factor():
    with pytest.raises(ValueError):
        Resampler(0)


def test_multi_timeframe_strategy_runs_through_kernel():
    import coinext_backtest
    from coinext_strategy import Strategy

    class FiveMinSma(Strategy):
        def __init__(self):
            self.tf = Resampler(5)
            self.sma = Sma(3)
            self.bought = False

        def on_bar(self, bar, ctx):
            coarse = self.tf.update(bar.ts, bar.open, bar.high, bar.low, bar.close, bar.volume)
            if coarse is None:
                return
            self.sma.update(coarse[4])  # 5-bar (coarse) close
            if self.sma.is_ready() and coarse[4] > self.sma.value() and not self.bought:
                ctx.submit_market("buy", 0.5)
                self.bought = True

    bars = coinext_backtest.synthetic_ohlc_bars(200)
    res = coinext_backtest.run(FiveMinSma(), bars=bars)
    assert res.orders_denied == 0
    assert res.orders_submitted <= 1  # buys once at most


def test_rsi_reversion_strategy_trades_through_the_kernel():
    import coinext_backtest
    from coinext_strategy import RsiReversion

    # A sine series swings RSI across the thresholds, so the strategy enters and exits.
    bars = coinext_backtest.synthetic_bars(400, amplitude=2000.0, period=30, trend_per_bar=0.0)
    res = coinext_backtest.run(RsiReversion(period=14, low=35.0, high=65.0, qty=0.5), bars=bars)
    assert res.orders_submitted > 0
    assert res.orders_denied == 0
    assert res.fills == res.orders_submitted  # market orders fill in the sim


def test_indicator_matches_handrolled_sma_on_a_real_backtest():
    # The Rust Sma fed bar.close must agree with coinext_strategy's pure-Python _Sma on the same series.
    import coinext_backtest
    from coinext_strategy import _Sma

    bars = coinext_backtest.synthetic_bars(120)
    rust = Sma(10)
    py = _Sma(10)
    for _ts, close in bars:
        rust.update(close)
        py.update(close)
        rv, pv = rust.value(), py.value
        if pv is None:
            assert rv is None
        else:
            assert rv == pytest.approx(pv)
