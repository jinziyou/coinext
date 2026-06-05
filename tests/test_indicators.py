"""qv_indicators — the Rust streaming indicators exposed to Python (via qv_py).

Unit tests assert the Python-visible values equal the Rust crate's (qv-indicators), and an
integration test drives an RSI strategy through the Rust kernel. Requires the compiled extension.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PYTHON_ROOT = Path(__file__).resolve().parents[1] / "python"
if str(_PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(_PYTHON_ROOT))

pytest.importorskip("qv_py", reason="build qv_py: uvx maturin develop --features python")

from qv_indicators import Atr, Ema, Rsi, Sma  # noqa: E402


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


@pytest.mark.parametrize("ctor", [Sma, Ema, Rsi, Atr])
def test_period_must_be_positive(ctor):
    with pytest.raises(ValueError):
        ctor(0)


def test_rsi_reversion_strategy_trades_through_the_kernel():
    import qv_backtest
    from qv_strategy import RsiReversion

    # A sine series swings RSI across the thresholds, so the strategy enters and exits.
    bars = qv_backtest.synthetic_bars(400, amplitude=2000.0, period=30, trend_per_bar=0.0)
    res = qv_backtest.run(RsiReversion(period=14, low=35.0, high=65.0, qty=0.5), bars=bars)
    assert res.orders_submitted > 0
    assert res.orders_denied == 0
    assert res.fills == res.orders_submitted  # market orders fill in the sim


def test_indicator_matches_handrolled_sma_on_a_real_backtest():
    # The Rust Sma fed bar.close must agree with qv_strategy's pure-Python _Sma on the same series.
    import qv_backtest
    from qv_strategy import _Sma

    bars = qv_backtest.synthetic_bars(120)
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
