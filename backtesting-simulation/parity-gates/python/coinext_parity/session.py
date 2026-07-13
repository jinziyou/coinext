"""Session shapes and sandbox recording I/O for coinext_parity."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_DEFAULT_TS_BUCKET_NS = 60_000_000_000


@dataclass
class SessionResult:
    """A single run reduced to the two parity surfaces: an equity curve and a fills log.

    * ``equity_curve`` — list of ``(ts_ns, equity)``.
    * ``fills`` — list of ``(ts_ns, side, qty, px)`` where ``side`` is ``+1`` buy / ``-1`` sell.
    """

    equity_curve: list[tuple[int, float]]
    fills: list[tuple[int, int, float, float]]

    @classmethod
    def from_backtest(cls, result: Any) -> SessionResult:
        """Build a :class:`SessionResult` from a ``coinext_py`` ``BacktestResult``.

        ``BacktestResult`` exposes ``equity_curve`` (``(ts_ns, equity)``) and ``fills_log``
        (``(ts_ns, symbol, side, qty, px)``); both are normalized here. The parity gate is
        single-venue, so the per-fill ``symbol`` is dropped (matching is on ts-bucket + side).
        """
        equity = [(int(ts), float(eq)) for ts, eq in result.equity_curve]
        fills = [
            (int(ts), int(side), float(qty), float(px))
            for ts, _sym, side, qty, px in result.fills_log
        ]
        return cls(equity_curve=equity, fills=fills)

    @classmethod
    def from_fills_and_bars(
        cls,
        fills: list[tuple[int, int, float, float]],
        bars: list[tuple[int, float]],
        starting_balance: float,
        *,
        fee_rate: float = 0.0004,
    ) -> SessionResult:
        """Reconstruct a session (equity curve) from a fill log + bars, marking to bar close.

        Used by ``coinext testnet-gate`` to build the SANDBOX session: the fills carry REAL testnet
        execution prices stamped with the backtest's signal timestamps, and the equity curve is
        rebuilt by walking the same bars. Applying the IDENTICAL reconstruction to the backtest
        fills isolates the only real difference — the fill prices — so the gate measures execution
        fidelity rather than accounting artifacts.

        Fills are snapped to the bar grid before bucketing (``coinext_screen._snap_fills_to_grid``):
        event-driven fills land at ``bar_ts + execution_latency``, but REAL Binance klines close at
        ``:59.999``, so that latency pushes the fill across the minute boundary and an EXACT
        ``ts == bar_ts`` match would drop EVERY fill — leaving a flat equity curve and a vacuously
        blind gate on the only real-data path. Snapping each fill to its nearest bar restores the
        BAR that triggered it.
        """
        import numpy as np
        from coinext_screen import _snap_fills_to_grid

        bar_ts = np.fromiter((int(ts) for ts, _close in bars), dtype=np.int64, count=len(bars))
        snapped = _snap_fills_to_grid(fills, bar_ts) if len(bar_ts) else list(fills)
        by_ts: dict[int, list[tuple[int, float, float]]] = {}
        for ts, side, qty, px in snapped:
            by_ts.setdefault(int(ts), []).append((int(side), float(qty), float(px)))
        cash = float(starting_balance)
        pos = 0.0
        curve: list[tuple[int, float]] = []
        for ts, close in bars:
            for side, qty, px in by_ts.get(int(ts), ()):
                notional = px * qty
                fee = notional * fee_rate
                if side > 0:  # buy
                    cash -= notional + fee
                    pos += qty
                else:  # sell
                    cash += notional - fee
                    pos -= qty
            curve.append((int(ts), cash + pos * float(close)))
        return cls(
            equity_curve=curve,
            fills=[(int(t), int(s), float(q), float(p)) for t, s, q, p in snapped],
        )

    def final_return(self) -> float:
        """Total return over the equity curve (``final / initial - 1``); ``0.0`` if degenerate."""
        if len(self.equity_curve) < 2:
            return 0.0
        start = self.equity_curve[0][1]
        if start == 0.0:
            return 0.0
        return self.equity_curve[-1][1] / start - 1.0


@dataclass(frozen=True)
class SandboxRecording:
    """A recorded sandbox/testnet session that can be replayed through the parity gate.

    The JSON fixture stores the exact bars that produced the strategy signals and the sandbox fills
    captured for those signals. Replaying both together keeps the gate offline and deterministic
    while still measuring the live-side execution seam once a real testnet recording is available.
    """

    symbol: str
    interval: str
    starting_balance: float
    bars: list[tuple[int, float]]
    fills: list[tuple[int, int, float, float]]
    environment: str = "unknown"
    strategy: dict[str, Any] | None = None
    source: str | None = None

    def to_session(self, *, fee_rate: float = 0.0004) -> SessionResult:
        """Rebuild the sandbox :class:`SessionResult` from recorded fills and bars."""
        return SessionResult.from_fills_and_bars(
            self.fills,
            self.bars,
            self.starting_balance,
            fee_rate=fee_rate,
        )


def _require_str(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"sandbox recording missing non-empty string `{key}`")
    return value


def _parse_bars(raw: Any) -> list[tuple[int, float]]:
    if not isinstance(raw, list):
        raise ValueError("sandbox recording `bars` must be a list of [ts_ns, close]")
    bars: list[tuple[int, float]] = []
    last_ts: int | None = None
    for idx, row in enumerate(raw):
        if not isinstance(row, list | tuple) or len(row) != 2:
            raise ValueError(f"sandbox recording bar {idx} must be [ts_ns, close]")
        ts = int(row[0])
        close = float(row[1])
        if ts <= 0:
            raise ValueError(f"sandbox recording bar {idx} has non-positive timestamp")
        if close <= 0.0 or not math.isfinite(close):
            raise ValueError(f"sandbox recording bar {idx} has invalid close")
        if last_ts is not None and ts <= last_ts:
            raise ValueError("sandbox recording bars must be strictly timestamp-sorted")
        bars.append((ts, close))
        last_ts = ts
    if not bars:
        raise ValueError("sandbox recording has no bars")
    return bars


def _parse_fills(raw: Any) -> list[tuple[int, int, float, float]]:
    if not isinstance(raw, list):
        raise ValueError("sandbox recording `fills` must be a list of [ts_ns, side, qty, px]")
    fills: list[tuple[int, int, float, float]] = []
    for idx, row in enumerate(raw):
        if not isinstance(row, list | tuple) or len(row) != 4:
            raise ValueError(f"sandbox recording fill {idx} must be [ts_ns, side, qty, px]")
        ts = int(row[0])
        side = int(row[1])
        qty = float(row[2])
        px = float(row[3])
        if ts <= 0:
            raise ValueError(f"sandbox recording fill {idx} has non-positive timestamp")
        if side not in {-1, 1}:
            raise ValueError(f"sandbox recording fill {idx} side must be +1 or -1")
        if qty <= 0.0 or not math.isfinite(qty):
            raise ValueError(f"sandbox recording fill {idx} has invalid qty")
        if px <= 0.0 or not math.isfinite(px):
            raise ValueError(f"sandbox recording fill {idx} has invalid px")
        fills.append((ts, side, qty, px))
    if not fills:
        raise ValueError("sandbox recording has no fills")
    return fills


def load_sandbox_recording(path: str | Path) -> SandboxRecording:
    """Load and validate a recorded sandbox/testnet session JSON fixture.

    Required JSON keys:

    ``version`` = 1, ``symbol``, ``interval``, ``starting_balance``, ``bars``, ``fills``.

    ``bars`` are ``[ts_ns, close]`` rows. ``fills`` are ``[ts_ns, side, qty, px]`` rows where
    ``side`` is ``+1`` buy / ``-1`` sell. The shape matches :class:`SessionResult`, so the fixture can
    be replayed without network credentials.
    """
    source = Path(path)
    raw = json.loads(source.read_text())
    if not isinstance(raw, dict):
        raise ValueError("sandbox recording root must be a JSON object")
    if int(raw.get("version", 0)) != 1:
        raise ValueError("sandbox recording version must be 1")
    starting_balance = float(raw.get("starting_balance", 0.0))
    if starting_balance <= 0.0 or not math.isfinite(starting_balance):
        raise ValueError("sandbox recording `starting_balance` must be positive")
    strategy = raw.get("strategy")
    if strategy is not None and not isinstance(strategy, dict):
        raise ValueError("sandbox recording `strategy` must be an object when present")
    return SandboxRecording(
        symbol=_require_str(raw, "symbol"),
        interval=_require_str(raw, "interval"),
        starting_balance=starting_balance,
        bars=_parse_bars(raw.get("bars")),
        fills=_parse_fills(raw.get("fills")),
        environment=str(raw.get("environment", "unknown")),
        strategy=strategy,
        source=str(source),
    )


def dump_sandbox_recording(
    path: str | Path,
    *,
    symbol: str,
    interval: str,
    starting_balance: float,
    bars: list[tuple[int, float]],
    fills: list[tuple[int, int, float, float]],
    environment: str,
    strategy: dict[str, Any] | None = None,
) -> None:
    """Write a replayable sandbox recording fixture using the version-1 JSON schema."""
    payload = {
        "version": 1,
        "environment": environment,
        "symbol": symbol,
        "interval": interval,
        "strategy": strategy or {},
        "starting_balance": float(starting_balance),
        "bars": [[int(ts), float(close)] for ts, close in bars],
        "fills": [[int(ts), int(side), float(qty), float(px)] for ts, side, qty, px in fills],
    }
    Path(path).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


# --------------------------------------------------------------------------------------------------
# Metrics.
