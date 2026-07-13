"""The end-to-end research-loop notebook runs clean (on synthetic data, no network).

`research_loop.py` under `strategy-research/research-notebooks` strings the whole workflow together (screen -> optimize -> backtest ->
indicators -> portfolio -> ticks); this guards it against bit-rot in CI. Needs the compiled coinext_py.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
PYTHONPATH_ENTRIES = [
    _ROOT / "foundation" / "python-contracts" / "python",
    _ROOT / "foundation" / "runtime-config" / "python",
    _ROOT / "market-data" / "data-lake" / "python",
    _ROOT / "strategy-research" / "strategy-api" / "python",
    _ROOT / "strategy-research" / "indicators" / "python",
    _ROOT / "backtesting-simulation" / "kernel" / "python",
    _ROOT / "backtesting-simulation" / "runner" / "python",
    _ROOT / "backtesting-simulation" / "parity-gates" / "python",
    _ROOT / "analytics-optimization" / "analytics" / "python",
    _ROOT / "analytics-optimization" / "screening" / "python",
    _ROOT / "analytics-optimization" / "optimizer" / "python",
    _ROOT / "analytics-optimization" / "derivatives" / "python",
    _ROOT / "risk-portfolio" / "risk-facade" / "python",
    _ROOT / "risk-portfolio" / "portfolio-facade" / "python",
    _ROOT / "execution-live" / "live-runtime" / "python",
    _ROOT / "operations-interface" / "bus" / "python",
    _ROOT / "operations-interface" / "cli" / "python",
    _ROOT,
]
for entry in PYTHONPATH_ENTRIES:
    value = str(entry)
    if value not in sys.path:
        sys.path.insert(0, value)

pytest.importorskip(
    "coinext_py",
    reason="build coinext_py: uvx maturin develop --manifest-path foundation/ffi-bridge/rust/coinext-py/Cargo.toml --features python",
)


def test_research_loop_notebook_runs(capsys, monkeypatch):
    # Prefer committed sample lake when present; force synthetic only if explicitly requested.
    monkeypatch.delenv("COINEXT_RESEARCH_USE_LAKE", raising=False)
    nb = _ROOT / "strategy-research" / "research-notebooks" / "notebooks" / "research_loop.py"
    ns = runpy.run_path(str(nb), run_name="__main__")
    # The flow ran top to bottom and produced its key artifacts.
    assert isinstance(ns["USE_LAKE"], bool)
    sample = _ROOT / "data" / "sample" / "bars"
    if sample.is_dir() and any(sample.rglob("*.parquet")):
        assert ns["USE_LAKE"] is True
    assert ns["report"].chosen_params["fast"] < ns["report"].chosen_params["slow"]
    assert ns["result"].orders_submitted >= 0  # authoritative backtest result
    assert ns["counter"].n_trades == len(ns["bars"])  # on_trade fired once per bar
    assert ns["counter"].n_quotes == len(ns["bars"])  # on_quote from synth quotes
    out = capsys.readouterr().out
    assert "research loop complete." in out


def test_research_loop_synthetic_override(capsys, monkeypatch):
    monkeypatch.setenv("COINEXT_RESEARCH_USE_LAKE", "0")
    nb = _ROOT / "strategy-research" / "research-notebooks" / "notebooks" / "research_loop.py"
    ns = runpy.run_path(str(nb), run_name="__main__")
    assert ns["USE_LAKE"] is False
    assert "research loop complete." in capsys.readouterr().out
