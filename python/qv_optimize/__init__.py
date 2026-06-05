"""qv_optimize — Optuna walk-forward strategy optimization.

Hyperparameter search over strategy params, validated with **walk-forward** (rolling train/test)
splits so a chosen parameter set is not overfit to one regime. Each trial runs the AUTHORITATIVE
``qv_backtest`` runner (the same Rust engines + SimulatedExecutionClient as live), then scores the
result with a ``qv_analytics`` metric — so optimization is parity-valid, not a vectorized shortcut
(ARCHITECTURE.md §1, build step 12).

``optuna`` is optional and guarded: this module imports cleanly without it.
:func:`walk_forward_splits` and the :class:`OptimizeNode` plumbing are pure-Python; only
:meth:`OptimizeNode.run` needs Optuna.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

# An objective maps a sampled param dict + a bar window to a scalar score (higher is better).
Objective = Callable[[dict[str, Any], list[tuple[int, float]]], float]


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
) -> list[Split]:
    """Generate rolling walk-forward :class:`Split` windows over ``bars``.

    The series is cut into ``n_splits`` contiguous segments; each segment's first ``train_frac`` is
    train and the remainder is the out-of-sample test. Anchored/expanding-window variants are TODO.
    """
    if n_splits < 1 or not bars:
        return []
    seg = max(1, len(bars) // n_splits)
    splits: list[Split] = []
    for i in range(n_splits):
        start = i * seg
        end = len(bars) if i == n_splits - 1 else (i + 1) * seg
        window = bars[start:end]
        if len(window) < 2:
            continue
        cut = max(1, int(len(window) * train_frac))
        splits.append(Split(train=window[:cut], test=window[cut:]))
    return splits


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
    """Drives an Optuna study with walk-forward cross-validation.

    ``search_space`` is a callable that, given a sampler (an Optuna trial), returns a sampled param
    dict. ``objective`` scores one ``(params, test_window)`` evaluation. The node averages the
    out-of-sample score across folds so robustness across regimes is rewarded.
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
                "optuna not installed. Install the research extra: "
                "pip install 'veloxquant[research]'"
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
    "OptimizeNode",
    "OptimizeResult",
    "SamplerProtocol",
    "Objective",
]
