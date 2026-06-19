"""Unit tests for coinext_optimize: walk-forward splits, grid search, and the honest IS/OOS optimizer.

These use a synthetic scalar objective (no backtest), so they run without the compiled ``coinext_py``
extension or Optuna. A separate integration test in ``tests/parity`` drives the real backtest.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

_PYTHON_ROOT = Path(__file__).resolve().parents[1] / "python"
if str(_PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(_PYTHON_ROOT))

from coinext_optimize import (  # noqa: E402
    grid_search,
    walk_forward_optimize,
    walk_forward_splits,
)

# A simple bar series: value == index, so a window's mean is its midpoint.
BARS = [(i, float(i)) for i in range(100)]


# --------------------------------------------------------------------------------------------------
# Splits.
# --------------------------------------------------------------------------------------------------
def test_rolling_splits_are_contiguous_and_non_overlapping():
    splits = walk_forward_splits(BARS, n_splits=4, train_frac=0.5, mode="rolling")
    assert len(splits) == 4
    for s in splits:
        # Train precedes test within each segment.
        assert s.train[-1][0] < s.test[0][0]
        assert len(s.train) >= 1 and len(s.test) >= 1
    # Segments don't overlap: each test ends before the next train begins.
    for a, b in zip(splits, splits[1:], strict=False):
        assert a.test[-1][0] < b.train[0][0]


def test_anchored_splits_expand_train_window():
    splits = walk_forward_splits(BARS, n_splits=4, mode="anchored")
    assert len(splits) == 4
    # Anchored at the start; train grows fold over fold.
    assert all(s.train[0][0] == 0 for s in splits)
    train_lens = [len(s.train) for s in splits]
    assert train_lens == sorted(train_lens)
    assert train_lens[-1] > train_lens[0]
    # Each test window is out-of-sample (strictly after its train).
    for s in splits:
        assert s.test[0][0] > s.train[-1][0]


def test_splits_degenerate_inputs():
    assert walk_forward_splits([], n_splits=4) == []
    assert walk_forward_splits(BARS, n_splits=0) == []
    with pytest.raises(ValueError):
        walk_forward_splits(BARS, mode="bogus")


# --------------------------------------------------------------------------------------------------
# Grid search.
# --------------------------------------------------------------------------------------------------
def test_grid_search_picks_max():
    # Objective peaks at x == 3.
    obj = lambda p, w: -abs(p["x"] - 3)  # noqa: E731
    params, score = grid_search({"x": [1, 2, 3, 4, 5]}, obj, BARS, direction="maximize")
    assert params == {"x": 3}
    assert score == 0.0


def test_grid_search_minimize_and_skips_non_finite():
    obj = lambda p, w: float("-inf") if p["x"] == 2 else (p["x"] - 1) ** 2  # noqa: E731
    params, score = grid_search({"x": [1, 2, 3]}, obj, BARS, direction="minimize")
    # x==2 is non-finite (skipped); among {1,3}, x==1 minimizes (score 0).
    assert params == {"x": 1}
    assert score == 0.0


def test_grid_search_cartesian_product():
    obj = lambda p, w: -((p["a"] - 2) ** 2 + (p["b"] - 5) ** 2)  # noqa: E731
    params, _ = grid_search({"a": [1, 2, 3], "b": [4, 5, 6]}, obj, BARS)
    assert params == {"a": 2, "b": 5}


# --------------------------------------------------------------------------------------------------
# Walk-forward optimize (IS/OOS degradation).
# --------------------------------------------------------------------------------------------------
def test_walk_forward_reports_zero_degradation_when_optimum_is_stable():
    # Optimum is x==3 on EVERY window -> IS and OOS agree -> is_mean == 0 -> degradation n/a (nan).
    obj = lambda p, w: -abs(p["x"] - 3)  # noqa: E731
    report = walk_forward_optimize(
        BARS, obj, param_grid={"x": [1, 2, 3, 4, 5]}, n_splits=4, optimizer="grid"
    )
    assert len(report.folds) == 4
    assert all(f.params == {"x": 3} for f in report.folds)
    assert report.chosen_params == {"x": 3}
    assert all(f.oos_score == 0.0 for f in report.folds)
    assert math.isnan(report.degradation)  # is_mean == 0


def test_walk_forward_detects_overfit_degradation():
    # Objective rewards matching a window's own mean/10. The train optimum overfits that window's
    # mean; the OOS test window has a different mean, so OOS is worse -> positive degradation.
    def obj(p, w):
        target = (sum(v for _, v in w) / len(w)) / 10.0
        return -((p["x"] - target) ** 2)

    report = walk_forward_optimize(
        BARS,
        obj,
        param_grid={"x": [round(0.5 * k, 1) for k in range(0, 21)]},  # 0.0 .. 10.0 step 0.5
        n_splits=4,
        train_frac=0.5,
        mode="rolling",
        optimizer="grid",
    )
    assert len(report.folds) == 4
    # IS fits each train window's target well (near 0); OOS lands on a shifted target -> worse.
    assert report.is_mean >= report.oos_mean
    assert math.isfinite(report.degradation)
    assert report.degradation > 0.0  # OOS edge below IS edge: the overfitting guard fires
    # The report renders with the degradation figure.
    text = report.render()
    assert "OOS degradation" in text
    assert "per fold" in text


def test_walk_forward_optimize_validates_args():
    with pytest.raises(ValueError):
        walk_forward_optimize(BARS, lambda p, w: 0.0, optimizer="grid")  # no param_grid
    with pytest.raises(ValueError):
        walk_forward_optimize(BARS, lambda p, w: 0.0, optimizer="optuna")  # no search_space
    with pytest.raises(ValueError):
        walk_forward_optimize(
            BARS, lambda p, w: 0.0, param_grid={"x": [1]}, optimizer="bogus"
        )
