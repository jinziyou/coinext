"""The end-to-end research-loop notebook runs clean (on synthetic data, no network).

`notebooks/research_loop.py` strings the whole workflow together (screen -> optimize -> backtest ->
indicators -> portfolio -> ticks); this guards it against bit-rot in CI. Needs the compiled qv_py.
"""

from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT / "python") not in sys.path:
    sys.path.insert(0, str(_ROOT / "python"))

pytest.importorskip("qv_py", reason="build qv_py: uvx maturin develop --features python")


def test_research_loop_notebook_runs(capsys):
    nb = _ROOT / "notebooks" / "research_loop.py"
    ns = runpy.run_path(str(nb), run_name="__main__")
    # The flow ran top to bottom and produced its key artifacts.
    assert ns["USE_LAKE"] is False  # the CI path uses synthetic bars
    assert ns["report"].chosen_params["fast"] < ns["report"].chosen_params["slow"]
    assert ns["result"].orders_submitted >= 0  # authoritative backtest result
    assert ns["counter"].n == len(ns["bars"])  # on_trade fired once per bar
    out = capsys.readouterr().out
    assert "research loop complete." in out
