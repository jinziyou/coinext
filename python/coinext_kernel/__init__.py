"""coinext_kernel — thin Python wrapper over the compiled ``coinext_py`` Kernel.

The Kernel is the synchronous deterministic core (ARCHITECTURE.md §2). This package is a *thin*
adapter: it picks the :class:`Environment`, builds the SAME ``RunConfig``, and asks ``coinext_py`` to
wire the core. Only three things differ per environment — the Clock, the Cache contents, and the
Data/Execution clients (behind byte-identical ports) — and that swap lives entirely on the Rust
side. Python never re-implements the loop.

``coinext_py`` is imported lazily so this module (and everything that imports it) loads with NO native
extension present; the import error is surfaced only when you actually try to build/run a kernel.
"""

from __future__ import annotations

from enum import StrEnum
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
            "uvx maturin develop --manifest-path crates/coinext-py/Cargo.toml --features python"
        ) from exc
    return coinext_py


def build_kernel(config: Any, env: Environment | str = Environment.BACKTEST) -> Any:
    """Build (but do not run) a Kernel for ``env`` from a ``coinext_config.RunConfig``.

    Returns the native kernel handle from ``coinext_py``. For BACKTEST the handle drives the merge-sorted
    deterministic loop; for SANDBOX/LIVE it owns the Tokio tasks behind the live clients.

    TODO: thread the venue/risk/brokerage sub-config through to ``coinext_py`` once the native builder
    accepts a structured ``RunConfig`` instead of positional backtest args.
    """
    env = Environment(env) if not isinstance(env, Environment) else env
    coinext_py = _coinext_py()
    builder = getattr(coinext_py, "build_kernel", None)
    if builder is None:  # pragma: no cover - native builder not yet exposed
        raise NotImplementedError(
            "coinext_py.build_kernel is not yet exposed; backtest currently uses coinext_py.run_backtest "
            "(see coinext_backtest.run). TODO: expose a structured Kernel builder for live/sandbox."
        )
    return builder(env.value, config)


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
