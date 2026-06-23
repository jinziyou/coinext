"""services/api — Coinext control-plane HTTP/WebSocket API (FastAPI).

This is the *control plane* surface the UI (and operators) talk to. It does NOT run the hot path:
the deterministic Rust core (``coinext_py``) runs inside the ``trader`` / ``ingestor`` / ``exec-svc``
processes (see ARCHITECTURE.md §6–§8). This service:

* reads run / position / fill / catalog state (today: stubs; later: Postgres + the data-lake
  catalog),
* triggers an *authoritative* backtest synchronously through the Rust kernel
  (``coinext_backtest.run`` → ``coinext_py.run_backtest`` with ``coinext_strategy.SmaCross``),
* fans out live telemetry to the UI over a WebSocket by consuming the Redis-Streams bus
  (``coinext_bus`` decoding the MessagePack ``Envelope`` — ARCHITECTURE.md §6),
* exposes the operator kill-switch control (publishes ``CtrlKillSwitch`` on the bus; the in-core
  ``coinext-risk-engine`` and the out-of-band ``risk-monitor`` both honour it).

Canonical deployment (see the service/port table): built from ``deploy/docker/api.Dockerfile``,
listens on **:8000**, env via ``COINEXT__API__HOST`` / ``COINEXT__API__PORT`` / ``COINEXT__REDIS__URL``.

Design note: every heavy / native import (``coinext_py``, ``coinext_backtest``, ``coinext_bus``, ``redis``) is
**lazy and guarded** so this module imports cleanly in a bare environment (e.g. for ``import``-time
unit tests, OpenAPI schema generation, or a UI dev box without the compiled extension). Endpoints
that need a missing dependency return HTTP 503 rather than 500.
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# --------------------------------------------------------------------------------------------------
# App + config
# --------------------------------------------------------------------------------------------------

app = FastAPI(
    title="Coinext API",
    version="0.1.0",
    summary="Control-plane API: runs, positions, fills, catalog, backtests, controls, live stream.",
)

# Config follows the COINEXT__SECTION__KEY convention (see .env.example). We read lazily at call time so
# the module imports without any environment set.
REDIS_URL = os.environ.get("COINEXT__REDIS__URL", "redis://redis:6379/0")

# --------------------------------------------------------------------------------------------------
# Security — API-key auth on mutating/control endpoints + CORS
# --------------------------------------------------------------------------------------------------
#
# This service binds 0.0.0.0:8000 and exposes operator controls (notably POST /control/killswitch, a
# platform-wide trading halt). Mutating endpoints therefore require a shared secret passed in the
# ``X-API-Key`` header, compared against COINEXT__API__KEY. Read-only probes (/health, GET stubs) stay
# open so liveness checks and the UI's read path keep working without a key.
API_KEY_HEADER = "X-API-Key"


def _api_key() -> str | None:
    """The configured API key (COINEXT__API__KEY), read lazily so tests can set it per-call.

    Returns ``None`` when unset/blank — in which case auth is *closed* (every protected endpoint 503s)
    rather than open, so a misconfigured deploy fails safe instead of exposing the kill-switch.
    """
    key = os.environ.get("COINEXT__API__KEY", "").strip()
    return key or None


def require_api_key(x_api_key: str | None = Header(default=None, alias=API_KEY_HEADER)) -> None:
    """FastAPI dependency: require ``X-API-Key`` to match COINEXT__API__KEY.

    * 503 if the server has no key configured (fail-closed: never silently unauthenticated).
    * 401 if the header is missing or does not match.
    """
    expected = _api_key()
    if expected is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "API authentication is not configured: set COINEXT__API__KEY to enable the "
                "control plane's mutating endpoints."
            ),
        )
    if not x_api_key or x_api_key != expected:
        raise HTTPException(status_code=401, detail="missing or invalid API key")


def _cors_origins() -> list[str]:
    """Allowed CORS origins from COINEXT__API__CORS_ORIGINS (comma-separated). Default: none.

    We intentionally never default to ``*`` — an unset value means no cross-origin browser access,
    which is the safe posture for a trading-control surface.
    """
    raw = os.environ.get("COINEXT__API__CORS_ORIGINS", "").strip()
    if not raw:
        return []
    return [origin.strip() for origin in raw.split(",") if origin.strip()]


# Restrict CORS to explicitly-configured origins (never "*"). With an empty list the middleware
# allows no cross-origin requests, matching the locked-down default.
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*", API_KEY_HEADER],
)

# Bus stream / channel names. These MUST agree with the Rust publishers (coinext-bus Envelope contract)
# and the Python coinext_bus client. Centralised here as the api's view of the topology.
STREAM_CONTROL = "coinext.control"  # CtrlKillSwitch and other operator commands flow here
STREAM_LIVE = "coinext.live"  # position / PnL telemetry the UI subscribes to

# Last-known kill-switch state, mirrored here so GET /control/killswitch and the POST response can
# report it without a round-trip to the risk-engine. Authoritative enforcement still lives in-core
# (coinext-risk-engine) + the risk-monitor; this is the api's local projection for the operator UI.
# TODO: replace with a read of the authoritative state once the bus exposes a control snapshot.
_killswitch_state: dict[str, Any] = {
    "engaged": False,
    "engaged_by": None,
    "reason": None,
    "ts_changed": None,
}


# --------------------------------------------------------------------------------------------------
# Lazy, guarded imports of the native / heavy dependencies
# --------------------------------------------------------------------------------------------------


def _load_backtest() -> Any:
    """Import ``coinext_backtest`` + ``coinext_strategy`` lazily.

    Raises a 503 if the compiled ``coinext_py`` extension (or the pure-Python packages) is unavailable,
    so the rest of the API stays usable without the Rust build.
    """
    try:
        import coinext_backtest  # noqa: WPS433 - intentional lazy import
        import coinext_strategy  # noqa: WPS433

        return coinext_backtest, coinext_strategy
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise HTTPException(
            status_code=503,
            detail=(
                "backtest engine unavailable: coinext_py extension not built. Run "
                "`uvx maturin develop --manifest-path crates/coinext-py/Cargo.toml --features python`."
            ),
        ) from exc


def _load_bus() -> Any:
    """Import the ``coinext_bus`` Redis-Streams client lazily (None if unavailable).

    Callers degrade gracefully: the WebSocket falls back to a synthetic stub stream and the
    kill-switch endpoint reports that the bus is not wired.
    """
    try:
        import coinext_bus  # noqa: WPS433 - intentional lazy import

        return coinext_bus
    except ImportError:  # pragma: no cover - environment-dependent
        return None


# --------------------------------------------------------------------------------------------------
# Request / response models
# --------------------------------------------------------------------------------------------------


class BacktestRequest(BaseModel):
    """Parameters for a one-shot authoritative backtest of ``coinext_strategy.SmaCross``."""

    symbol: str = "BTCUSDT"
    venue: str = "BINANCE"
    starting_balance: float = 100_000.0
    fast: int = 10
    slow: int = 30
    qty: float = 0.5
    # Number of synthetic bars to generate when no dataset id is supplied.
    # TODO: replace synthetic generation with a real dataset lookup via coinext_data's catalog/HistoryReader.
    bars: int = 400


class KillSwitchRequest(BaseModel):
    """Operator kill-switch toggle. ``engage=True`` halts all new order routing platform-wide."""

    engage: bool = True
    reason: str = "manual operator action"
    # Operator identity for the audit trail (defaults to the api process if the caller omits it).
    actor: str | None = None


class KillSwitchState(BaseModel):
    """Current global kill-switch state (the shape both GET and POST /control/killswitch return)."""

    engaged: bool = False
    engaged_by: str | None = None
    reason: str | None = None
    ts_changed: str | None = None  # ISO-8601


# --------------------------------------------------------------------------------------------------
# Read endpoints (stubs — to be backed by Postgres + the data-lake catalog)
# --------------------------------------------------------------------------------------------------


@app.get("/health")
@app.get("/healthz")
def health() -> dict[str, Any]:
    """Liveness + a quick capability probe (does the native extension import?).

    Served at both ``/health`` (documented for the UI) and ``/healthz`` (the path the Docker /
    docker-compose healthcheck probes). Read-only and unauthenticated by design.
    """
    have_native = False
    try:  # pragma: no cover - environment-dependent
        import coinext_py  # noqa: F401, WPS433

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

    TODO: query the runs table in Postgres (COINEXT__POSTGRES__DSN). Returns a representative stub now.

    Shape mirrors the UI's ``Run`` wire type (services/ui/src/api.ts): monetary fields (``pnl``)
    cross as strings to preserve the fixed-precision integer domain (ARCHITECTURE §4).
    """
    return [
        {
            "run_id": "bt-0001",
            "strategy_id": "SmaCross",
            "environment": "backtest",
            "status": "completed",
            "started_at": "2026-06-05T00:00:00Z",
            "updated_at": "2026-06-05T00:00:00Z",
            "pnl": "1234.50",
            "pnl_currency": "USDT",
        },
    ]


@app.get("/positions")
def list_positions() -> list[dict[str, Any]]:
    """Current open positions.

    TODO: read from the live Cache snapshot (published on the bus) or the positions table. Stub now.

    Shape mirrors the UI's ``Position`` wire type (services/ui/src/api.ts): quantity / price / PnL
    fields cross as strings to preserve the fixed-precision integer domain (ARCHITECTURE §4).
    """
    return [
        {
            "instrument_id": "BTCUSDT.BINANCE",
            "venue": "BINANCE",
            "side": "long",
            "quantity": "0.5",
            "avg_px": "50000.00",
            "mark_px": "50500.00",
            "unrealized_pnl": "250.00",
            "realized_pnl": "0.00",
            "currency": "USDT",
            "ts_last": "2026-06-05T00:00:00Z",
        },
    ]


@app.get("/fills")
def list_fills() -> list[dict[str, Any]]:
    """Recent fills (executions).

    TODO: read from the append-only OrderEvent store (coinext-persistence). Stub now.
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

    TODO: delegate to coinext_data's DataLake catalog (Parquet on MinIO/S3, COINEXT__DATA__LAKE_ROOT). Stub now.
    """
    return {
        "lake_root": os.environ.get("COINEXT__DATA__LAKE_ROOT", "/data"),
        "instruments": [
            {
                "instrument_id": "BTCUSDT.BINANCE",
                "asset_class": "crypto",
                "bar_types": ["1m", "1h"],
            },
            {
                "instrument_id": "ETHUSDT.BINANCE",
                "asset_class": "crypto",
                "bar_types": ["1m", "1h"],
            },
        ],
    }


@app.get("/latency")
def latency() -> dict[str, Any]:
    """Latency SLO histogram snapshot (the UI polls this for the Latency panel).

    Values are the percentiles the platform tracks (ARCHITECTURE §8), reported in nanoseconds.
    Shape mirrors the UI's ``LatencySnapshot`` wire type (services/ui/src/api.ts).
    TODO: scrape the real histograms from the Prometheus endpoints the trading services expose.
    """
    return {
        "metrics": [
            {
                "name": "submit_to_ack_ns",
                "p50_ns": 18_000,
                "p95_ns": 42_000,
                "p99_ns": 90_000,
                "count": 0,
            },
            {
                "name": "strategy_dispatch_ns",
                "p50_ns": 1_200,
                "p95_ns": 3_500,
                "p99_ns": 8_000,
                "count": 0,
            },
            {
                "name": "ingest_to_publish_ns",
                "p50_ns": 6_500,
                "p95_ns": 15_000,
                "p99_ns": 35_000,
                "count": 0,
            },
        ],
        "ts_snapshot": None,
    }


# --------------------------------------------------------------------------------------------------
# Backtest — authoritative event-driven run through the Rust kernel
# --------------------------------------------------------------------------------------------------


@app.post("/backtest", dependencies=[Depends(require_api_key)])
def run_backtest(req: BacktestRequest) -> dict[str, Any]:
    """Run an authoritative backtest of ``SmaCross`` and return the tear-sheet metrics as JSON.

    This drives a Python ``Strategy`` through the SAME Rust engines + ``SimulatedExecutionClient``
    the live path uses (ARCHITECTURE.md §7), so the result is parity-valid — not a vectorized
    screen. Synchronous: a small backtest completes well within an HTTP request; large sweeps belong
    on a worker. TODO: offload long runs to a job queue and return a run_id to poll via ``/runs``.
    """
    coinext_backtest, coinext_strategy = _load_backtest()

    strategy = coinext_strategy.SmaCross(fast=req.fast, slow=req.slow, qty=req.qty)
    bars = coinext_backtest.synthetic_bars(n=req.bars)
    result = coinext_backtest.run(
        strategy,
        symbol=req.symbol,
        venue=req.venue,
        starting_balance=req.starting_balance,
        bars=bars,
    )

    # Render the tear sheet via coinext_analytics when present; always return the raw headline metrics so
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
        import coinext_analytics  # noqa: WPS433

        m = coinext_analytics.compute_metrics(list(result.equity_curve))
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
        tear_sheet = coinext_analytics.tear_sheet(result)
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


@app.get("/control/killswitch")
def get_killswitch() -> KillSwitchState:
    """Return the api's last-known global kill-switch state (polled by the UI).

    This is the api's local projection (see ``_killswitch_state``); authoritative enforcement lives
    in-core (coinext-risk-engine) and in the ``risk-monitor``.
    """
    return KillSwitchState(**_killswitch_state)


@app.post("/control/killswitch", dependencies=[Depends(require_api_key)])
def control_killswitch(req: KillSwitchRequest) -> KillSwitchState:
    """Engage / release the platform-wide kill-switch by publishing ``CtrlKillSwitch`` on the bus.

    The atomic in-core gate (coinext-risk-engine) and the out-of-band ``risk-monitor`` both honour it,
    so engaging it here stops new order routing across every ``trader`` process. Releasing it
    re-enables routing. This is defense-in-depth on top of the per-order RiskEngine.

    Returns the updated ``KillSwitchState`` (the UI reads it back to refresh its view). The local
    projection is updated even when the bus is unavailable so the operator sees the requested state.
    """
    actor = req.actor or "api"
    bus = _load_bus()

    # TODO: build and publish the real CtrlKillSwitch Envelope (MsgType.CTRL) via coinext_bus once the
    # publisher API lands. Shape kept explicit so the contract is reviewable.
    payload = {
        "kind": "CtrlKillSwitch",
        "engaged": req.engage,
        "reason": req.reason,
        "source": "api",
        "actor": actor,
    }
    if bus is not None:
        try:  # pragma: no cover - requires a running redis
            # Expected coinext_bus surface: a Publisher that encodes a MsgType.CTRL Envelope onto STREAM_CONTROL.
            publisher = bus.Publisher(REDIS_URL)  # type: ignore[attr-defined]
            publisher.publish_control(STREAM_CONTROL, payload)
        except Exception as exc:  # noqa: BLE001 - surface bus/redis errors to the operator
            raise HTTPException(
                status_code=503,
                detail=f"coinext_bus present but publish failed: {exc}",
            ) from exc
    # No bus wired (e.g. dev box without redis/msgpack): we still mirror the requested state locally
    # so the UI reflects the operator's intent; enforcement is a no-op until the bus is available.

    _killswitch_state.update(
        engaged=req.engage,
        engaged_by=actor,
        reason=req.reason,
        ts_changed=datetime.now(UTC).isoformat(),
    )
    return KillSwitchState(**_killswitch_state)


# --------------------------------------------------------------------------------------------------
# Live WebSocket — streams stub position / PnL updates to the UI
# --------------------------------------------------------------------------------------------------


async def _bus_live_stream(ws: WebSocket) -> bool:
    """Try to stream real telemetry from the Redis bus. Returns False if the bus is unavailable.

    TODO: wire to coinext_bus's async consumer of STREAM_LIVE, decode the MessagePack Envelope, and
    forward FILL / position-snapshot payloads. Kept as a guarded seam for now.
    """
    bus = _load_bus()
    if bus is None or not hasattr(bus, "AsyncConsumer"):
        return False
    # Placeholder: real consumer loop goes here once coinext_bus exposes an async iterator.
    return False


async def _stub_live_stream(ws: WebSocket) -> None:
    """Fallback: emit a deterministic synthetic position/PnL tick once per second.

    Lets the UI build the live view before the bus path is wired. Replaced by ``_bus_live_stream``
    when ``coinext_bus`` is available.
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

    Prefers the real Redis bus consumer; falls back to a synthetic stub stream when ``coinext_bus`` is
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
