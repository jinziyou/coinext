"""coinext_optimize — walk-forward strategy optimization with out-of-sample validation.

Hyperparameter search over strategy params, validated with **walk-forward** (rolling or anchored
train/test) splits so a chosen parameter set is not overfit to one regime. Each evaluation runs the
AUTHORITATIVE ``coinext_backtest`` runner (the same Rust engines + SimulatedExecutionClient as live),
then scores the result with a ``coinext_analytics`` metric — so optimization is parity-valid, not a
vectorized shortcut (ARCHITECTURE.md §1, build step 12).

Two layers:

* :func:`walk_forward_optimize` — the **honest** walk-forward: for each fold it optimizes params on
  the *train* window and re-evaluates the winner on the *out-of-sample* (OOS) *test* window, then
  reports in-sample vs OOS **degradation** (the overfitting guard the roadmap calls out). The inner
  search is a pure-Python grid by default (no extra deps), or Optuna when ``optimizer="optuna"``.
* :class:`OptimizeNode` — the single Optuna study with CV-averaged scoring (kept for compatibility).
  It optimizes *directly* against the test folds, so its score is optimistic relative to the true
  OOS estimate from :func:`walk_forward_optimize`; prefer the latter for a trustworthy figure.

``optuna`` is optional and guarded: this module imports cleanly without it, and the grid-search path
of :func:`walk_forward_optimize` needs only the stdlib.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

# An objective maps a sampled param dict + a bar window to a scalar score (higher is better unless
# the caller passes direction="minimize").
Objective = Callable[[dict[str, Any], list[tuple[int, float]]], float]


# --------------------------------------------------------------------------------------------------
# Walk-forward splits (rolling / anchored).
# --------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Split:
    """One walk-forward fold: a train window followed by an out-of-sample test window."""

    train: list[tuple[int, float]]
    test: list[tuple[int, float]]


def walk_forward_splits(
    bars: list[tuple[int, float]],
    *,
    n_splits: int = 4,
    train_frac: float = 0.6,
    mode: str = "rolling",
) -> list[Split]:
    """Generate walk-forward :class:`Split` windows over ``bars``.

    * ``mode="rolling"`` — the series is cut into ``n_splits`` contiguous, NON-overlapping segments;
      each segment's first ``train_frac`` is train and the remainder is the OOS test. Each fold sees
      a fresh, independent regime.
    * ``mode="anchored"`` — an EXPANDING train window anchored at the start: fold *i* trains on
      ``bars[0 : (i+1)*seg]`` and tests on the next segment ``bars[(i+1)*seg : (i+2)*seg]``. Mirrors
      live retraining on all history to date, then trading the next out-of-sample block.
    """
    if n_splits < 1 or not bars:
        return []
    if mode == "anchored":
        seg = max(1, len(bars) // (n_splits + 1))
        splits: list[Split] = []
        for i in range(n_splits):
            train_end = (i + 1) * seg
            test_end = len(bars) if i == n_splits - 1 else (i + 2) * seg
            train = bars[:train_end]
            test = bars[train_end:test_end]
            if len(train) < 2 or len(test) < 1:
                continue
            splits.append(Split(train=train, test=test))
        return splits
    if mode != "rolling":
        raise ValueError(f"unknown walk-forward mode {mode!r} (expected 'rolling' or 'anchored')")

    seg = max(1, len(bars) // n_splits)
    splits = []
    for i in range(n_splits):
        start = i * seg
        end = len(bars) if i == n_splits - 1 else (i + 1) * seg
        window = bars[start:end]
        if len(window) < 2:
            continue
        cut = max(1, int(len(window) * train_frac))
        if cut >= len(window):  # need at least one test bar
            continue
        splits.append(Split(train=window[:cut], test=window[cut:]))
    return splits


# --------------------------------------------------------------------------------------------------
# Inner optimizers: pick the best params on a single (train) window.
# --------------------------------------------------------------------------------------------------
def _better(score: float, best: float, direction: str) -> bool:
    return score > best if direction == "maximize" else score < best


def grid_search(
    param_grid: dict[str, Sequence[Any]],
    objective: Objective,
    window: list[tuple[int, float]],
    *,
    direction: str = "maximize",
) -> tuple[dict[str, Any], float]:
    """Exhaustive grid search over the Cartesian product of ``param_grid`` (pure stdlib).

    Returns ``(best_params, best_score)``. Combinations scoring non-finite (e.g. an invalid
    fast>=slow guarded by the objective returning ``-inf``) are skipped.
    """
    names = list(param_grid)
    best_params: dict[str, Any] | None = None
    best_score = -math.inf if direction == "maximize" else math.inf
    for combo in itertools.product(*(param_grid[n] for n in names)):
        params = dict(zip(names, combo, strict=True))
        score = objective(params, window)
        if not math.isfinite(score):
            continue
        if best_params is None or _better(score, best_score, direction):
            best_params, best_score = params, score
    if best_params is None:  # nothing finite — return the first combo with its score
        first = {n: param_grid[n][0] for n in names}
        return first, objective(first, window)
    return best_params, best_score


def _optuna_search(
    search_space: Callable[[Any], dict[str, Any]],
    objective: Objective,
    window: list[tuple[int, float]],
    *,
    n_trials: int,
    direction: str,
) -> tuple[dict[str, Any], float]:
    import optuna  # type: ignore

    optuna.logging.set_verbosity(optuna.logging.WARNING)

    def _obj(trial: Any) -> float:
        return objective(search_space(trial), window)

    study = optuna.create_study(direction=direction)
    study.optimize(_obj, n_trials=n_trials)
    return dict(study.best_params), float(study.best_value)


# --------------------------------------------------------------------------------------------------
# Walk-forward optimization (the honest IS/OOS estimate).
# --------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class FoldResult:
    """One walk-forward fold: params chosen IN-SAMPLE and their score IN- and OUT-of-sample."""

    fold: int
    params: dict[str, Any]
    is_score: float  # in-sample score of `params` on the train window
    oos_score: float  # out-of-sample score of `params` on the test window
    n_train: int
    n_test: int


@dataclass
class WalkForwardReport:
    """Aggregated walk-forward result + the headline overfitting guard, OOS degradation.

    ``degradation`` is ``(is_mean - oos_mean) / |is_mean|`` — the fraction of in-sample edge lost
    out of sample (positive = OOS worse than IS, the usual case; ~0 = robust; large = overfit). It
    is ``nan`` when ``is_mean`` is zero. ``chosen_params`` is a final fit over ALL bars — the params
    you would deploy, with ``oos_mean``/``degradation`` as the trustworthy live-perf expectation.
    """

    folds: list[FoldResult]
    is_mean: float
    oos_mean: float
    degradation: float
    chosen_params: dict[str, Any]
    direction: str = "maximize"
    history: list[tuple[dict[str, Any], float]] = field(default_factory=list)

    def render(self) -> str:
        """Render a short text report of the walk-forward run."""
        lines = [
            "============ Coinext walk-forward optimize ============",
            f"folds              : {len(self.folds)}",
            f"in-sample  mean    : {self.is_mean:>14.4f}",
            f"out-of-sample mean : {self.oos_mean:>14.4f}",
            f"OOS degradation    : {self.degradation * 100:>13.2f}%"
            if math.isfinite(self.degradation)
            else "OOS degradation    :            n/a",
            f"chosen params      : {self.chosen_params}",
            "--------------------- per fold --------------------------",
        ]
        for f in self.folds:
            lines.append(
                f"  fold {f.fold}: IS={f.is_score:>10.4f}  OOS={f.oos_score:>10.4f}  "
                f"(train {f.n_train} / test {f.n_test})  {f.params}"
            )
        lines.append("=========================================================")
        return "\n".join(lines)


def walk_forward_optimize(
    bars: list[tuple[int, float]],
    objective: Objective,
    *,
    param_grid: dict[str, Sequence[Any]] | None = None,
    search_space: Callable[[Any], dict[str, Any]] | None = None,
    n_splits: int = 4,
    train_frac: float = 0.6,
    mode: str = "rolling",
    optimizer: str = "grid",
    n_trials: int = 50,
    direction: str = "maximize",
) -> WalkForwardReport:
    """Honest walk-forward optimization with out-of-sample validation.

    For each :func:`walk_forward_splits` fold: optimize ``objective`` on the TRAIN window (inner
    search), then re-score the winning params on the held-out TEST window. Aggregate the in-sample
    and out-of-sample means and report :class:`WalkForwardReport.degradation`. Finally, refit over
    ALL ``bars`` to produce ``chosen_params`` for deployment.

    ``optimizer="grid"`` (default) needs only ``param_grid`` (a dict of name -> candidate values)
    and the stdlib. ``optimizer="optuna"`` needs ``search_space`` (a ``trial -> params`` callable)
    plus ``optuna``. ``objective(params, window)`` must return a scalar; guard invalid combos by
    returning ``-inf`` (maximize) so the inner search skips them.
    """
    if optimizer == "grid" and param_grid is None:
        raise ValueError("optimizer='grid' requires param_grid")
    if optimizer == "optuna" and search_space is None:
        raise ValueError("optimizer='optuna' requires search_space")
    if optimizer not in ("grid", "optuna"):
        raise ValueError(f"unknown optimizer {optimizer!r} (expected 'grid' or 'optuna')")

    history: list[tuple[dict[str, Any], float]] = []

    def _optimize(window: list[tuple[int, float]]) -> tuple[dict[str, Any], float]:
        if optimizer == "grid":
            params, score = grid_search(
                param_grid,
                objective,
                window,
                direction=direction,  # type: ignore[arg-type]
            )
        else:
            params, score = _optuna_search(
                search_space,
                objective,
                window,
                n_trials=n_trials,
                direction=direction,  # type: ignore[arg-type]
            )
        history.append((params, score))
        return params, score

    splits = walk_forward_splits(bars, n_splits=n_splits, train_frac=train_frac, mode=mode)
    folds: list[FoldResult] = []
    for i, s in enumerate(splits):
        params, is_score = _optimize(s.train)
        oos_score = objective(params, s.test)
        folds.append(
            FoldResult(
                fold=i,
                params=params,
                is_score=is_score,
                oos_score=oos_score,
                n_train=len(s.train),
                n_test=len(s.test),
            )
        )

    finite_is = [f.is_score for f in folds if math.isfinite(f.is_score)]
    finite_oos = [f.oos_score for f in folds if math.isfinite(f.oos_score)]
    is_mean = sum(finite_is) / len(finite_is) if finite_is else float("nan")
    oos_mean = sum(finite_oos) / len(finite_oos) if finite_oos else float("nan")
    if math.isfinite(is_mean) and is_mean != 0.0 and math.isfinite(oos_mean):
        degradation = (is_mean - oos_mean) / abs(is_mean)
    else:
        degradation = float("nan")

    chosen_params, _ = _optimize(bars)

    return WalkForwardReport(
        folds=folds,
        is_mean=is_mean,
        oos_mean=oos_mean,
        degradation=degradation,
        chosen_params=chosen_params,
        direction=direction,
        history=history,
    )


# --------------------------------------------------------------------------------------------------
# Legacy single-study CV node (kept for compatibility; see module docstring for the caveat).
# --------------------------------------------------------------------------------------------------
class SamplerProtocol(Protocol):
    """Trial param sampler (subset of ``optuna.Trial`` we rely on)."""

    def suggest_int(self, name: str, low: int, high: int) -> int: ...
    def suggest_float(self, name: str, low: float, high: float) -> float: ...


@dataclass
class OptimizeResult:
    """Best params + score and the per-trial trace."""

    best_params: dict[str, Any]
    best_value: float
    n_trials: int
    history: list[tuple[dict[str, Any], float]] = field(default_factory=list)


@dataclass
class OptimizeNode:
    """Drives an Optuna study with walk-forward cross-validation (CV-averaged OOS test scoring).

    NOTE: this optimizes directly against the test folds, so ``best_value`` is an optimistic figure.
    For a trustworthy out-of-sample estimate use :func:`walk_forward_optimize`, which optimizes on
    train and validates on held-out test.
    """

    bars: list[tuple[int, float]]
    search_space: Callable[[SamplerProtocol], dict[str, Any]]
    objective: Objective
    n_splits: int = 4
    train_frac: float = 0.6
    direction: str = "maximize"

    def _cv_score(self, params: dict[str, Any]) -> float:
        splits = walk_forward_splits(self.bars, n_splits=self.n_splits, train_frac=self.train_frac)
        if not splits:
            return float("-inf")
        scores = [self.objective(params, s.test) for s in splits]
        return sum(scores) / len(scores)

    def run(self, n_trials: int = 50) -> OptimizeResult:
        """Run the study. Requires ``optuna`` (raises a clear ImportError otherwise)."""
        try:
            import optuna  # type: ignore
        except ImportError as exc:  # pragma: no cover - optional dep
            raise ImportError(
                "optuna not installed. Install the research extra: pip install 'coinext[research]'"
            ) from exc

        history: list[tuple[dict[str, Any], float]] = []

        def _trial_objective(trial: Any) -> float:
            params = self.search_space(trial)
            score = self._cv_score(params)
            history.append((params, score))
            return score

        study = optuna.create_study(direction=self.direction)
        study.optimize(_trial_objective, n_trials=n_trials)
        return OptimizeResult(
            best_params=dict(study.best_params),
            best_value=float(study.best_value),
            n_trials=n_trials,
            history=history,
        )


__all__ = [
    "Split",
    "walk_forward_splits",
    "grid_search",
    "FoldResult",
    "WalkForwardReport",
    "walk_forward_optimize",
    "OptimizeNode",
    "OptimizeResult",
    "SamplerProtocol",
    "Objective",
]
