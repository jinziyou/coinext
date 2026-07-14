"""The end-to-end research-loop notebook runs clean (on synthetic data, no network).

`research_loop.py` under `strategy-research/notebooks` strings the whole workflow together
(screen → optimize → backtest → indicators → portfolio → ticks); this guards it against bit-rot
in CI. Needs the compiled coinext_py.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
# Flattened layout: <lifecycle>/python (matches pyproject pytest pythonpath).
PYTHONPATH_ENTRIES = [
    _ROOT / "foundation" / "python",
    _ROOT / "market-data" / "python",
    _ROOT / "strategy-research" / "python",
    _ROOT / "backtesting-simulation" / "python",
    _ROOT / "analytics-optimization" / "python",
    _ROOT / "risk-portfolio" / "python",
    _ROOT / "execution-live" / "python",
    _ROOT / "operations-interface" / "python",
    _ROOT,
]
for entry in PYTHONPATH_ENTRIES:
    value = str(entry)
    if value not in sys.path:
        sys.path.insert(0, value)

pytest.importorskip(
    "coinext_py",
    reason="build coinext_py: uvx maturin develop --manifest-path foundation/crates/coinext-py/Cargo.toml --features python",
)


def test_research_loop_notebook_runs(capsys, monkeypatch):
    # Prefer committed sample lake when present; force synthetic only if explicitly requested.
    monkeypatch.delenv("COINEXT_RESEARCH_USE_LAKE", raising=False)
    nb = _ROOT / "strategy-research" / "notebooks" / "research_loop.py"
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
    nb = _ROOT / "strategy-research" / "notebooks" / "research_loop.py"
    ns = runpy.run_path(str(nb), run_name="__main__")
    assert ns["USE_LAKE"] is False
    assert "research loop complete." in capsys.readouterr().out
