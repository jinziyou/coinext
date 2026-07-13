"""coinext_kernel — thin Python wrapper over the compiled ``coinext_py`` Kernel.

The Kernel is the synchronous deterministic core (ARCHITECTURE.md §5). This package is a *thin*
adapter: it picks the :class:`Environment`, builds the SAME ``RunConfig``, and asks ``coinext_py`` to
wire the core. Only three things differ per environment — the Clock, the Cache contents, and the
Data/Execution clients (behind byte-identical ports) — and that swap lives entirely on the Rust
side. Python never re-implements the loop.

``coinext_py`` is imported lazily so this module (and everything that imports it) loads with NO native
extension present; the import error is surfaced only when you actually try to build/run a kernel.
"""

from __future__ import annotations

from enum import StrEnum
from types import SimpleNamespace
from typing import Any


class Environment(StrEnum):
    """The three parity environments (mirrors the Rust ``coinext_kernel::Environment``).

    Authoring is identical across all three; the Kernel injects different runtime pieces:

    * ``BACKTEST`` — ``HistoricalClock`` + HistoryReader feed + ``SimulatedExecutionClient``.
    * ``SANDBOX``  — ``LiveClock`` + Binance *testnet* clients (same ports as live).
    * ``LIVE``     — ``LiveClock`` + Binance production clients.
    """

    BACKTEST = "backtest"
    SANDBOX = "sandbox"
    LIVE = "live"

    @property
    def is_live(self) -> bool:
        """True for SANDBOX/LIVE (wall-clock + real venue I/O); False for BACKTEST."""
        return self in (Environment.SANDBOX, Environment.LIVE)


def _coinext_py() -> Any:
    """Import the compiled extension lazily with an actionable error message."""
    try:
        import coinext_py  # the maturin-built Rust extension
    except ImportError as exc:  # pragma: no cover - surfaced as a clear setup error
        raise ImportError(
            "coinext_py extension not built. Run: "
            "uvx maturin develop --manifest-path foundation/ffi-bridge/rust/coinext-py/Cargo.toml --features python"
        ) from exc
    return coinext_py


def _namespace_tree(value: Any) -> Any:
    """Recursively adapt dict configs to attribute-style objects for the PyO3 builder."""
    if isinstance(value, dict):
        return SimpleNamespace(**{key: _namespace_tree(item) for key, item in value.items()})
    return value


def build_kernel(
    config: Any,
    env: Environment | str = Environment.BACKTEST,
    strategy: Any | None = None,
) -> Any:
    """Build a native Kernel handle for ``env`` from a ``coinext_config.RunConfig``.

    ``SANDBOX``/``LIVE`` return the PyO3 ``coinext_py.Kernel`` handle, which owns the native
    ``LiveKernel`` and can run with a portfolio callback. Backtests still use
    :func:`coinext_backtest.run`, because a build-only handle would need an explicit historical event
    stream and would not be the public backtest API.
    """
    env = Environment(env) if not isinstance(env, Environment) else env
    if not env.is_live:
        raise NotImplementedError(
            "coinext_kernel.build_kernel builds sandbox/live native handles; use "
            "coinext_backtest.run or coinext_kernel.run_backtest for backtests."
        )
    if strategy is None:
        raise ValueError(
            "coinext_kernel.build_kernel requires a Strategy instance for sandbox/live"
        )
    coinext_py = _coinext_py()
    builder = getattr(coinext_py, "build_kernel", None)
    if builder is None:  # pragma: no cover - native builder not yet exposed
        raise NotImplementedError(
            "coinext_py.build_kernel is not exposed by this extension build. Rebuild with: "
            "uvx maturin develop --manifest-path foundation/ffi-bridge/rust/coinext-py/Cargo.toml "
            "--features python"
        )
    return builder(env.value, _namespace_tree(config), strategy)


def run_backtest(strategy: Any, bars: list[tuple], **kwargs: Any) -> Any:
    """Convenience pass-through to the authoritative backtest runner.

    Delegates to :func:`coinext_backtest.run`, which normalizes ``bars`` (close-only / OHLC / OHLCV via
    ``_to_ohlcv``) and supplies the ``symbol``/``venue``/``starting_balance`` defaults the native
    ``coinext_py.run_backtest`` requires. Kept here so callers with a Kernel-shaped mental model have a
    single import surface. (Calling ``coinext_py.run_backtest`` directly would need 6-wide bar tuples and
    the required positional args.)
    """
    from coinext_backtest import run as _run

    return _run(strategy, bars=bars, **kwargs)


__all__ = ["Environment", "build_kernel", "run_backtest"]
