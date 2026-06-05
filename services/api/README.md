# services/api — VeloxQuant control-plane API

A FastAPI app (`app.py`, exposing `app = FastAPI(...)`) that is the HTTP/WebSocket control plane the
UI and operators talk to. It is **not** on the hot path — the deterministic Rust core (`qv_py`) runs
inside the `trader` / `ingestor` / `exec-svc` processes (see [`docs/ARCHITECTURE.md`](../../docs/ARCHITECTURE.md)
§6–§8). This service reads state, triggers authoritative backtests through the Rust kernel, fans out
live telemetry from the Redis-Streams bus, and exposes operator controls.

## Endpoints

| Method | Path                  | Purpose                                                                 |
|--------|-----------------------|-------------------------------------------------------------------------|
| GET    | `/health`             | Liveness + native-extension capability probe.                           |
| GET    | `/runs`               | List backtest / live runs (stub → Postgres).                            |
| GET    | `/positions`          | Open positions (stub → live Cache snapshot / positions table).          |
| GET    | `/fills`              | Recent fills (stub → qv-persistence OrderEvent store).                   |
| GET    | `/catalog`            | Data-lake catalog of instruments/datasets (stub → qv_data DataLake).    |
| POST   | `/backtest`           | Run an authoritative `qv_strategy.SmaCross` backtest; returns metrics.  |
| POST   | `/control/killswitch` | Engage/release the platform-wide kill-switch (`CtrlKillSwitch` on bus). |
| WS     | `/ws/live`            | Stream stub position/PnL updates (→ Redis bus consumer).                |

`/backtest` drives a Python `Strategy` through the **same** Rust engines + `SimulatedExecutionClient`
the live path uses (ARCHITECTURE.md §1, §7), so the result is parity-valid — not a vectorized screen.

## Service / port (canonical)

| Item        | Value                                                         |
|-------------|--------------------------------------------------------------|
| Build       | `deploy/docker/api.Dockerfile`                                |
| Listens     | `:8000` (`VQ__API__HOST` / `VQ__API__PORT`)                   |
| Bus         | `VQ__REDIS__URL` (default `redis://redis:6379/0`)             |

## Run (dev)

```bash
# from the repo root, with the qv_* packages + compiled qv_py on PYTHONPATH
pip install -r services/api/requirements.txt
uvicorn app:app --app-dir services/api --host 0.0.0.0 --port 8000 --reload
```

OpenAPI docs at <http://localhost:8000/docs>.

## Run (docker)

```bash
docker build -f deploy/docker/api.Dockerfile -t veloxquant/api .
docker run --rm -p 8000:8000 \
  -e VQ__REDIS__URL=redis://redis:6379/0 \
  veloxquant/api
```

(Usually started via `docker-compose` alongside `redis`, `postgres`, and the trading services.)

## Import safety

Every heavy / native import (`qv_py`, `qv_backtest`, `qv_bus`, `redis`) is **lazy and guarded**, so
`app.py` imports cleanly without the compiled extension or a running Redis — endpoints that need a
missing dependency return HTTP 503 (or, for the live WS / kill-switch, degrade to a clearly-labelled
stub). This keeps schema generation and unit tests dependency-light.

## TODOs

- Back `/runs`, `/positions`, `/fills`, `/catalog` with Postgres + the qv_data catalog + qv-persistence.
- Wire `/ws/live` to a real `qv_bus` async consumer of the live telemetry stream.
- Publish a real `CtrlKillSwitch` Envelope (`MsgType.CTRL`) from `/control/killswitch`.
- Offload long backtests to a worker/job queue; return a `run_id` to poll.
