# operations-interface/api/service/api — Coinext control-plane API

A FastAPI app (`app.py`, exposing `app = FastAPI(...)`) that is the HTTP/WebSocket control plane the
UI and operators talk to. It is **not** on the hot path — the deterministic Rust core (`coinext_py`) runs
inside the `trader` / `ingestor` / `exec-svc` processes (see [`ARCHITECTURE.md`](../../../../ARCHITECTURE.md)
§3 and §7). This service reads state, triggers authoritative backtests through the Rust kernel, fans out
live telemetry from the Redis-Streams bus, and exposes operator controls.

## Endpoints

| Method | Path                  | Purpose                                                                 |
|--------|-----------------------|-------------------------------------------------------------------------|
| GET    | `/health`             | Liveness + native-extension capability probe.                           |
| GET    | `/runs`               | List backtest / live runs (stub → Postgres).                            |
| GET    | `/positions`          | Open positions (stub → live Cache snapshot / positions table).          |
| GET    | `/fills`              | Recent fills (stub → coinext-persistence OrderEvent store).                   |
| GET    | `/catalog`            | Data-lake catalog of instruments/datasets (stub → coinext_data DataLake).    |
| GET    | `/latency`            | Latency SLO histogram snapshot in ns (stub → Prometheus scrape).        |
| POST   | `/backtest`           | Run an authoritative `coinext_strategy.SmaCross` backtest; returns metrics.  |
| GET    | `/control/killswitch` | Current global kill-switch state (api's local projection).             |
| GET    | `/control/events`     | Recent projected control-stream events for the operator UI timeline.   |
| POST   | `/control/killswitch` | Engage/release the platform-wide kill-switch (`CtrlKillSwitch` on bus). |
| WS     | `/ws/live`            | Stream live telemetry from `coinext.live`; falls back to stub when bus is unavailable. |

`/backtest` drives a Python `Strategy` through the **same** Rust engines + `SimulatedExecutionClient`
the live path uses (ARCHITECTURE.md §1, §4), so the result is parity-valid — not a vectorized screen.

Mutating endpoints (`POST /backtest`, `POST /control/killswitch`) require `X-API-Key` to match
`COINEXT__API__KEY`. If the env var is unset, they fail closed with HTTP 503.

At startup, the API also consumes `coinext.control` when `coinext_bus`/Redis are available, so
risk-monitor-published `CtrlKillSwitch` breaches update the same `/control/killswitch` projection and
`/control/events` timeline that the UI polls. `/ws/live` consumes `coinext.live` through the same
Redis-Streams client and emits each decoded telemetry payload as JSON to WebSocket clients. Set
`COINEXT__API__CONTROL_CONSUMER=0` to disable only the background control projection in isolated dev
runs; `/ws/live` still attempts the live stream on each WebSocket connection.

## Service / port (canonical)

| Item        | Value                                                         |
|-------------|--------------------------------------------------------------|
| Build       | `operations-interface/deployment/docker/api.Dockerfile`                                |
| Listens     | `:8000` (`COINEXT__API__HOST` / `COINEXT__API__PORT`)                   |
| Bus         | `COINEXT__REDIS__URL` (default `redis://redis:6379/0`)             |

## Run (dev)

```bash
# from the repo root, with the coinext_* packages + compiled coinext_py on PYTHONPATH
pip install -r operations-interface/api/service/api/requirements.txt
uvicorn app:app --app-dir operations-interface/api/service/api --host 0.0.0.0 --port 8000 --reload
```

OpenAPI docs at <http://localhost:8000/docs>.

## Run (docker)

```bash
docker build -f operations-interface/deployment/docker/api.Dockerfile -t coinext/api .
docker run --rm -p 8000:8000 \
  -e COINEXT__REDIS__URL=redis://redis:6379/0 \
  -e COINEXT__API__KEY=change-me \
  coinext/api
```

(Usually started via `docker-compose` alongside `redis`, `postgres`, and the trading services.)

## Import safety

Every heavy / native import (`coinext_py`, `coinext_backtest`, `coinext_bus`, `redis`) is **lazy and guarded**, so
`app.py` imports cleanly without the compiled extension or a running Redis — endpoints that need a
missing dependency return HTTP 503 (or, for `/ws/live`, degrade to a clearly-labelled stub stream).
This keeps schema generation and unit tests dependency-light.

## Known gaps

- Back `/runs`, `/positions`, `/fills`, `/catalog` with Postgres + the coinext_data catalog + coinext-persistence.
- Exercise the native builder/callback producer against Binance testnet credentials; the API already
  consumes the resulting `coinext.live` payloads.
- Offload long backtests to a worker/job queue; return a `run_id` to poll.
