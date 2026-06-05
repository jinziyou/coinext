"""qv_backtest — the BacktestNode.

The AUTHORITATIVE event-driven runner: it drives a Python ``Strategy`` through the Rust kernel via
``qv_py.run_backtest`` (the SAME engines + SimulatedExecutionClient the live path uses). A separate
vectorized ``populate_*`` screen (non-authoritative) is a future addition; only this path is
parity-valid.
"""

from __future__ import annotations

import math
from typing import Any

try:
    import qv_py  # the compiled Rust extension (maturin develop)
except ImportError as exc:  # pragma: no cover - surfaced as a clear setup error
    raise ImportError(
        "qv_py extension not built. Run: "
        "uvx maturin develop --manifest-path crates/qv-py/Cargo.toml --features python"
    ) from exc


def run(
    strategy: Any,
    *,
    symbol: str = "BTCUSDT",
    venue: str = "BINANCE",
    starting_balance: float = 100_000.0,
    bars: list[tuple[int, float]],
    price_precision: int = 2,
    size_precision: int = 3,
    maker_fee: float = 0.0002,
    taker_fee: float = 0.0004,
) -> Any:
    """Run ``strategy`` over ``bars`` (``(ts_ns, close)`` list); return the BacktestResult."""
    return qv_py.run_backtest(
        strategy,
        symbol,
        venue,
        starting_balance,
        bars,
        price_precision=price_precision,
        size_precision=size_precision,
        maker_fee=maker_fee,
        taker_fee=taker_fee,
    )


def synthetic_bars(
    n: int = 400,
    *,
    base: float = 50_000.0,
    amplitude: float = 1_500.0,
    period: int = 40,
    trend_per_bar: float = 12.0,
    start_ns: int = 1_700_000_000_000_000_000,
    step_ns: int = 60_000_000_000,
) -> list[tuple[int, float]]:
    """Deterministic sine-wave + trend close series (no RNG — reproducible)."""
    bars = []
    for i in range(n):
        phase = (i / period) * math.tau
        close = base + amplitude * math.sin(phase) + i * trend_per_bar
        bars.append((start_ns + i * step_ns, close))
    return bars


__all__ = ["run", "synthetic_bars"]
