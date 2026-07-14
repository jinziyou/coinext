"""Expanded regression goldens: multiple strategies + multi-instrument determinism.

Companion to ``test_regression_placeholder.py`` (SmaCross equity pin). These cases pin discrete
counts and bit-for-bit reproducibility for additional strategy shapes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip(
    "coinext_py",
    reason=(
        "build the extension: "
        "uvx maturin develop --manifest-path foundation/crates/coinext-py/Cargo.toml --features python"
    ),
)

from coinext_backtest import run, run_multi, synthetic_bars, synthetic_ohlc_bars  # noqa: E402
from coinext_strategy import LimitMaker, MultiSma, RsiReversion, SmaCross  # noqa: E402

_GOLDEN = Path(__file__).with_name("regression_suite_golden.json")
_TOL = 1e-6


def _pin(name: str, payload: dict) -> dict:
    if not _GOLDEN.exists():
        data = {}
    else:
        data = json.loads(_GOLDEN.read_text())
    if name not in data:
        data[name] = payload
        _GOLDEN.write_text(json.dumps(data, indent=2) + "\n")
        pytest.skip(f"pinned {name} into {_GOLDEN.name}; re-run to compare")
    return data[name]


def test_sma_cross_reproducible_and_pinned():
    bars = synthetic_bars(250)
    a = run(SmaCross(10, 30, 0.5), bars=bars)
    b = run(SmaCross(10, 30, 0.5), bars=bars)
    assert list(a.equity_curve) == list(b.equity_curve)
    golden = _pin(
        "sma_cross",
        {
            "final_equity": float(a.final_equity),
            "fills": int(a.fills),
            "orders_submitted": int(a.orders_submitted),
        },
    )
    assert float(a.final_equity) == pytest.approx(golden["final_equity"], abs=_TOL)
    assert int(a.fills) == int(golden["fills"])
    assert int(a.orders_submitted) == int(golden["orders_submitted"])


def test_rsi_reversion_reproducible_and_pinned():
    bars = synthetic_bars(300)
    a = run(RsiReversion(period=14, low=35.0, high=65.0), bars=bars)
    b = run(RsiReversion(period=14, low=35.0, high=65.0), bars=bars)
    assert list(a.equity_curve) == list(b.equity_curve)
    golden = _pin(
        "rsi_reversion",
        {
            "final_equity": float(a.final_equity),
            "fills": int(a.fills),
            "orders_submitted": int(a.orders_submitted),
        },
    )
    assert float(a.final_equity) == pytest.approx(golden["final_equity"], abs=_TOL)
    assert int(a.fills) == int(golden["fills"])


def test_limit_maker_ohlc_reproducible_and_pinned():
    bars = synthetic_ohlc_bars(200)
    a = run(LimitMaker(), bars=bars)
    b = run(LimitMaker(), bars=bars)
    assert list(a.equity_curve) == list(b.equity_curve)
    golden = _pin(
        "limit_maker",
        {
            "final_equity": float(a.final_equity),
            "fills": int(a.fills),
            "orders_submitted": int(a.orders_submitted),
        },
    )
    assert float(a.final_equity) == pytest.approx(golden["final_equity"], abs=_TOL)
    assert int(a.fills) == int(golden["fills"])


def test_multi_instrument_reproducible():
    bars = {
        "BTCUSDT": synthetic_bars(180, base=50_000.0, period=40),
        "ETHUSDT": synthetic_bars(180, base=3_000.0, period=55),
    }
    a = run_multi(MultiSma(10, 30), bars=bars)
    b = run_multi(MultiSma(10, 30), bars=bars)
    assert list(a.equity_curve) == list(b.equity_curve)
    assert a.fills == b.fills
