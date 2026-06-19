# services/api — Coinext control-plane API

A FastAPI app (`app.py`, exposing `app = FastAPI(...)`) that is the HTTP/WebSocket control plane the
UI and operators talk to. It is **not** on the hot path — the deterministic Rust core (`coinext_py`) runs
inside the `trader` / `ingestor` / `exec-svc` processes (see [`docs/ARCHITECTURE.md`](../../docs/ARCHITECTURE.md)
§6–§8). This service reads state, triggers authoritative backtests through the Rust kernel, fans out
live telemetry from the Redis-Streams bus, and exposes operator controls.

## Endpoints

| Method | Path                  | Purpose                                                                 |
|--------|-----------------------|-------------------------------------------------------------------------|
| GET    | `/health`             | Liveness + native-extension capability probe.                           |
| GET    | `/runs`               | List backtest / live runs (stub → Postgres).                            |
| GET    | `/positions`          | Open positions (stub → live Cache snapshot / positions table).          |
| GET    | `/fills`              | Recent fills (stub → coinext-persistence OrderEvent store).                   |
| GET    | `/catalog`            | Data-lake catalog of instruments/datasets (stub → coinext_data DataLake).    |
| POST   | `/backtest`           | Run an authoritative `coinext_strategy.SmaCross` backtest; returns metrics.  |
| POST   | `/control/killswitch` | Engage/release the platform-wide kill-switch (`CtrlKillSwitch` on bus). |
| WS     | `/ws/live`            | Stream stub position/PnL updates (→ Redis bus consumer).                |

`/backtest` drives a Python `Strategy` through the **same** Rust engines + `SimulatedExecutionClient`
the live path uses (ARCHITECTURE.md §1, §7), so the result is parity-valid — not a vectorized screen.

## Service / port (canonical)

| Item        | Value                                                         |
|-------------|--------------------------------------------------------------|
| Build       | `deploy/docker/api.Dockerfile`                                |
| Listens     | `:8000` (`COINEXT__API__HOST` / `COINEXT__API__PORT`)                   |
| Bus         | `COINEXT__REDIS__URL` (default `redis://redis:6379/0`)             |

## Run (dev)

```bash
# from the repo root, with the coinext_* packages + compiled coinext_py on PYTHONPATH
pip install -r services/api/requirements.txt
uvicorn app:app --app-dir services/api --host 0.0.0.0 --port 8000 --reload
```

OpenAPI docs at <http://localhost:8000/docs>.

## Run (docker)

```bash
docker build -f deploy/docker/api.Dockerfile -t coinext/api .
docker run --rm -p 8000:8000 \
  -e COINEXT__REDIS__URL=redis://redis:6379/0 \
  coinext/api
```

(Usually started via `docker-compose` alongside `redis`, `postgres`, and the trading services.)

## Import safety

Every heavy / native import (`coinext_py`, `coinext_backtest`, `coinext_bus`, `redis`) is **lazy and guarded**, so
`app.py` imports cleanly without the compiled extension or a running Redis — endpoints that need a
missing dependency return HTTP 503 (or, for the live WS / kill-switch, degrade to a clearly-labelled
stub). This keeps schema generation and unit tests dependency-light.

## TODOs

- Back `/runs`, `/positions`, `/fills`, `/catalog` with Postgres + the coinext_data catalog + coinext-persistence.
- Wire `/ws/live` to a real `coinext_bus` async consumer of the live telemetry stream.
- Publish a real `CtrlKillSwitch` Envelope (`MsgType.CTRL`) from `/control/killswitch`.
- Offload long backtests to a worker/job queue; return a `run_id` to poll.
