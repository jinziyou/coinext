"""services/api — VeloxQuant control-plane HTTP/WebSocket API (FastAPI).

This is the *control plane* surface the UI (and operators) talk to. It does NOT run the hot path:
the deterministic Rust core (``qv_py``) runs inside the ``trader`` / ``ingestor`` / ``exec-svc``
processes (see ARCHITECTURE.md §6–§8). This service:

* reads run / position / fill / catalog state (today: stubs; later: Postgres + the data-lake
  catalog),
* triggers an *authoritative* backtest synchronously through the Rust kernel
  (``qv_backtest.run`` → ``qv_py.run_backtest`` with ``qv_strategy.SmaCross``),
* fans out live telemetry to the UI over a WebSocket by consuming the Redis-Streams bus
  (``qv_bus`` decoding the MessagePack ``Envelope`` — ARCHITECTURE.md §6),
* exposes the operator kill-switch control (publishes ``CtrlKillSwitch`` on the bus; the in-core
  ``qv-risk-engine`` and the out-of-band ``risk-monitor`` both honour it).

Canonical deployment (see the service/port table): built from ``deploy/docker/api.Dockerfile``,
listens on **:8000**, env via ``VQ__API__HOST`` / ``VQ__API__PORT`` / ``VQ__REDIS__URL``.

Design note: every heavy / native import (``qv_py``, ``qv_backtest``, ``qv_bus``, ``redis``) is
**lazy and guarded** so this module imports cleanly in a bare environment (e.g. for ``import``-time
unit tests, OpenAPI schema generation, or a UI dev box without the compiled extension). Endpoints
that need a missing dependency return HTTP 503 rather than 500.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

# --------------------------------------------------------------------------------------------------
# App + config
# --------------------------------------------------------------------------------------------------

app = FastAPI(
    title="VeloxQuant API",
    version="0.1.0",
    summary="Control-plane API: runs, positions, fills, catalog, backtests, controls, live stream.",
)

# Config follows the VQ__SECTION__KEY convention (see .env.example). We read lazily at call time so
# the module imports without any environment set.
REDIS_URL = os.environ.get("VQ__REDIS__URL", "redis://redis:6379/0")

# Bus stream / channel names. These MUST agree with the Rust publishers (qv-bus Envelope contract)
# and the Python qv_bus client. Centralised here as the api's view of the topology.
STREAM_CONTROL = "vq.control"  # CtrlKillSwitch and other operator commands flow here
STREAM_LIVE = "vq.live"        # position / PnL telemetry the UI subscribes to


# --------------------------------------------------------------------------------------------------
# Lazy, guarded imports of the native / heavy dependencies
# --------------------------------------------------------------------------------------------------


def _load_backtest() -> Any:
    """Import ``qv_backtest`` + ``qv_strategy`` lazily.

    Raises a 503 if the compiled ``qv_py`` extension (or the pure-Python packages) is unavailable,
    so the rest of the API stays usable without the Rust build.
    """
    try:
        import qv_backtest  # noqa: WPS433 - intentional lazy import
        import qv_strategy  # noqa: WPS433

        return qv_backtest, qv_strategy
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise HTTPException(
            status_code=503,
            detail=(
                "backtest engine unavailable: qv_py extension not built. Run "
                "`uvx maturin develop --manifest-path crates/qv-py/Cargo.toml --features python`."
            ),
        ) from exc


def _load_bus() -> Any:
    """Import the ``qv_bus`` Redis-Streams client lazily (None if unavailable).

    Callers degrade gracefully: the WebSocket falls back to a synthetic stub stream and the
    kill-switch endpoint reports that the bus is not wired.
    """
    try:
        import qv_bus  # noqa: WPS433 - intentional lazy import

        return qv_bus
    except ImportError:  # pragma: no cover - environment-dependent
        return None


# --------------------------------------------------------------------------------------------------
# Request / response models
# --------------------------------------------------------------------------------------------------


class BacktestRequest(BaseModel):
    """Parameters for a one-shot authoritative backtest of ``qv_strategy.SmaCross``."""

    symbol: str = "BTCUSDT"
    venue: str = "BINANCE"
    starting_balance: float = 100_000.0
    fast: int = 10
    slow: int = 30
    qty: float = 0.5
    # Number of synthetic bars to generate when no dataset id is supplied.
    # TODO: replace synthetic generation with a real dataset lookup via qv_data's catalog/HistoryReader.
    bars: int = 400


class KillSwitchRequest(BaseModel):
    """Operator kill-switch toggle. ``engaged=True`` halts all new order routing platform-wide."""

    engaged: bool = True
    reason: str = "manual operator action"


# --------------------------------------------------------------------------------------------------
# Read endpoints (stubs — to be backed by Postgres + the data-lake catalog)
# --------------------------------------------------------------------------------------------------


@app.get("/health")
def health() -> dict[str, Any]:
    """Liveness + a quick capability probe (does the native extension import?)."""
    have_native = False
    try:  # pragma: no cover - environment-dependent
        import qv_py  # noqa: F401, WPS433

        have_native = True
    except ImportError:
        have_native = False
    return {
        "status": "ok",
        "service": "api",
        "have_native": have_native,
        "redis_url": REDIS_URL,
    }


@app.get("/runs")
def list_runs() -> list[dict[str, Any]]:
    """List backtest / live runs.

    TODO: query the runs table in Postgres (VQ__POSTGRES__DSN). Returns a representative stub now.
    """
    return [
        {
            "run_id": "bt-0001",
            "kind": "backtest",
            "strategy": "SmaCross",
            "symbol": "BTCUSDT",
            "status": "completed",
            "created_at": "2026-06-05T00:00:00Z",
        },
    ]


@app.get("/positions")
def list_positions() -> list[dict[str, Any]]:
    """Current open positions.

    TODO: read from the live Cache snapshot (published on the bus) or the positions table. Stub now.
    """
    return [
        {
            "instrument_id": "BTCUSDT.BINANCE",
            "net_qty": 0.5,
            "avg_px": 50_000.0,
            "unrealized_pnl": 250.0,
            "realized_pnl": 0.0,
        },
    ]


@app.get("/fills")
def list_fills() -> list[dict[str, Any]]:
    """Recent fills (executions).

    TODO: read from the append-only OrderEvent store (qv-persistence). Stub now.
    """
    return [
        {
            "fill_id": "f-0001",
            "client_order_id": "SmaCross-00000000000000000001",
            "instrument_id": "BTCUSDT.BINANCE",
            "side": "buy",
            "qty": 0.5,
            "px": 50_000.0,
            "ts_event": 1_700_000_000_000_000_000,
        },
    ]


@app.get("/catalog")
def catalog() -> dict[str, Any]:
    """Data-lake catalog: available instruments / datasets for backtests.

    TODO: delegate to qv_data's DataLake catalog (Parquet on MinIO/S3, VQ__DATA__LAKE_ROOT). Stub now.
    """
    return {
        "lake_root": os.environ.get("VQ__DATA__LAKE_ROOT", "/data"),
        "instruments": [
            {"instrument_id": "BTCUSDT.BINANCE", "asset_class": "crypto", "bar_types": ["1m", "1h"]},
            {"instrument_id": "ETHUSDT.BINANCE", "asset_class": "crypto", "bar_types": ["1m", "1h"]},
        ],
    }


# --------------------------------------------------------------------------------------------------
# Backtest — authoritative event-driven run through the Rust kernel
# --------------------------------------------------------------------------------------------------


@app.post("/backtest")
def run_backtest(req: BacktestRequest) -> dict[str, Any]:
    """Run an authoritative backtest of ``SmaCross`` and return the tear-sheet metrics as JSON.

    This drives a Python ``Strategy`` through the SAME Rust engines + ``SimulatedExecutionClient``
    the live path uses (ARCHITECTURE.md §7), so the result is parity-valid — not a vectorized
    screen. Synchronous: a small backtest completes well within an HTTP request; large sweeps belong
    on a worker. TODO: offload long runs to a job queue and return a run_id to poll via ``/runs``.
    """
    qv_backtest, qv_strategy = _load_backtest()

    strategy = qv_strategy.SmaCross(fast=req.fast, slow=req.slow, qty=req.qty)
    bars = qv_backtest.synthetic_bars(n=req.bars)
    result = qv_backtest.run(
        strategy,
        symbol=req.symbol,
        venue=req.venue,
        starting_balance=req.starting_balance,
        bars=bars,
    )

    # Render the tear sheet via qv_analytics when present; always return the raw headline metrics so
    # the UI can chart them without parsing text.
    metrics: dict[str, Any] = {
        "starting_equity": getattr(result, "starting_equity", None),
        "final_equity": getattr(result, "final_equity", None),
        "realized_pnl": getattr(result, "realized_pnl", None),
        "orders_submitted": getattr(result, "orders_submitted", None),
        "orders_denied": getattr(result, "orders_denied", None),
        "fills": getattr(result, "fills", None),
    }
    try:  # pragma: no cover - optional dependency
        import qv_analytics  # noqa: WPS433

        m = qv_analytics.compute_metrics(list(result.equity_curve))
        metrics.update(
            {
                "total_return": m.total_return,
                "sharpe": m.sharpe,
                "sortino": m.sortino,
                "max_drawdown": m.max_drawdown,
                "volatility": m.volatility,
                "n_periods": m.n_periods,
            }
        )
        tear_sheet = qv_analytics.tear_sheet(result)
    except ImportError:
        tear_sheet = None

    return {
        "strategy": "SmaCross",
        "params": {"fast": req.fast, "slow": req.slow, "qty": req.qty},
        "symbol": req.symbol,
        "venue": req.venue,
        "metrics": metrics,
        "tear_sheet": tear_sheet,
    }


# --------------------------------------------------------------------------------------------------
# Controls — operator kill-switch
# --------------------------------------------------------------------------------------------------


@app.post("/control/killswitch")
def control_killswitch(req: KillSwitchRequest) -> dict[str, Any]:
    """Engage / release the platform-wide kill-switch by publishing ``CtrlKillSwitch`` on the bus.

    The atomic in-core gate (qv-risk-engine) and the out-of-band ``risk-monitor`` both honour it,
    so engaging it here stops new order routing across every ``trader`` process. Releasing it
    re-enables routing. This is defense-in-depth on top of the per-order RiskEngine.
    """
    bus = _load_bus()
    if bus is None:
        # No bus wired (e.g. dev box without redis/msgpack). Report rather than fail hard.
        return {
            "ok": False,
            "engaged": req.engaged,
            "published": False,
            "detail": "qv_bus unavailable; kill-switch NOT propagated. Install bus extras + run redis.",
        }

    # TODO: build and publish the real CtrlKillSwitch Envelope (MsgType.CTRL) via qv_bus once the
    # publisher API lands. Shape kept explicit so the contract is reviewable.
    payload = {
        "kind": "CtrlKillSwitch",
        "engaged": req.engaged,
        "reason": req.reason,
        "source": "api",
    }
    try:  # pragma: no cover - requires a running redis
        # Expected qv_bus surface: a Publisher that encodes a MsgType.CTRL Envelope onto STREAM_CONTROL.
        publisher = bus.Publisher(REDIS_URL)  # type: ignore[attr-defined]
        publisher.publish_control(STREAM_CONTROL, payload)
        published = True
        detail = "kill-switch command published on the control stream"
    except Exception as exc:  # noqa: BLE001 - surface bus/redis errors to the operator
        published = False
        detail = f"qv_bus present but publish failed: {exc}"

    return {"ok": published, "engaged": req.engaged, "published": published, "detail": detail}


# --------------------------------------------------------------------------------------------------
# Live WebSocket — streams stub position / PnL updates to the UI
# --------------------------------------------------------------------------------------------------


async def _bus_live_stream(ws: WebSocket) -> bool:
    """Try to stream real telemetry from the Redis bus. Returns False if the bus is unavailable.

    TODO: wire to qv_bus's async consumer of STREAM_LIVE, decode the MessagePack Envelope, and
    forward FILL / position-snapshot payloads. Kept as a guarded seam for now.
    """
    bus = _load_bus()
    if bus is None or not hasattr(bus, "AsyncConsumer"):
        return False
    # Placeholder: real consumer loop goes here once qv_bus exposes an async iterator.
    return False


async def _stub_live_stream(ws: WebSocket) -> None:
    """Fallback: emit a deterministic synthetic position/PnL tick once per second.

    Lets the UI build the live view before the bus path is wired. Replaced by ``_bus_live_stream``
    when ``qv_bus`` is available.
    """
    seq = 0
    pnl = 0.0
    while True:
        seq += 1
        # Deterministic sine-ish wobble so the UI shows motion without an RNG.
        pnl += 12.5 if seq % 4 else -25.0
        await ws.send_json(
            {
                "type": "position_update",
                "seq": seq,
                "instrument_id": "BTCUSDT.BINANCE",
                "net_qty": 0.5,
                "mark_px": 50_000.0 + (seq % 20) * 5.0,
                "unrealized_pnl": round(pnl, 2),
                "source": "stub",
            }
        )
        await asyncio.sleep(1.0)


@app.websocket("/ws/live")
async def ws_live(ws: WebSocket) -> None:
    """Stream live position / PnL updates to the UI.

    Prefers the real Redis bus consumer; falls back to a synthetic stub stream when ``qv_bus`` is
    not available so the UI is developable standalone.
    """
    await ws.accept()
    try:
        streamed = await _bus_live_stream(ws)
        if not streamed:
            await _stub_live_stream(ws)
    except WebSocketDisconnect:  # pragma: no cover - client-driven
        return


__all__ = ["app"]
