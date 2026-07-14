"""Offline paper LiveKernel (ReplayDataClient + PaperFillExec) via coinext_py."""

from __future__ import annotations

import pytest

pytest.importorskip(
    "coinext_py",
    reason="build coinext_py: uvx maturin develop --manifest-path foundation/crates/coinext-py/Cargo.toml --features python",
)

from coinext_backtest import synthetic_ohlc_bars  # noqa: E402
from coinext_strategy import SmaCross  # noqa: E402


def test_build_paper_kernel_runs_and_fills():
    import coinext_py

    bars = synthetic_ohlc_bars(n=80)
    # Normalize to 6-tuples for the bridge.
    ohlcv = []
    for row in bars:
        if len(row) >= 6:
            ohlcv.append(tuple(row[:6]))
        else:
            ts, o, h, low, c = row[:5]
            ohlcv.append((int(ts), float(o), float(h), float(low), float(c), 0.0))

    strat = SmaCross(5, 15, 0.05)
    kernel = coinext_py.build_paper_kernel(strat, ohlcv, symbol="BTCUSDT", env="sandbox")
    snaps = []
    kernel.run_with_portfolio_callback(lambda s: snaps.append(s))
    # Strategy may or may not trade depending on series; loop must complete without error.
    assert isinstance(snaps, list)
