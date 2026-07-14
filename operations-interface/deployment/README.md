# Coinext — Deployment

Deployment assets live in `operations-interface/deployment`, while root `docker-compose.yml`, `docker-compose.dev.yml`, and `docker-compose.obs.yml` remain the operator entrypoints.

The topology preserves the parity model: the same engines run in every environment; only Kernel-injected Clock, Cache, and Data/Execution clients differ by `COINEXT__ENV` and venue config.

## Services and ports

| Service | Functional source | Build asset | Runtime | Port(s) |
|---|---|---|---|---|
| `ingestor` | `market-data/crates/coinext-ingest` | `operations-interface/deployment/docker/ingestor.Dockerfile` | Rust `coinext-ingest` | metrics `9101` |
| `exec-svc` | `execution-live/crates/coinext-exec-svc` | `operations-interface/deployment/docker/exec-svc.Dockerfile` | Rust `coinext-exec-svc` | metrics `9102`, ctl `8081` |
| `trader` | `execution-live/services/trader` | `operations-interface/deployment/docker/trader.Dockerfile` | Python `coinext_live` | metrics `9103` |
| `risk-monitor` | `risk-portfolio/services/risk-monitor` | `operations-interface/deployment/docker/risk-monitor.Dockerfile` | Python supervisor | metrics `9104` |
| `api` | `operations-interface/services/api` | `operations-interface/deployment/docker/api.Dockerfile` | FastAPI | `8000` |
| `ui` | `operations-interface/services/ui` | `operations-interface/deployment/docker/ui.Dockerfile` | Node/Vite build → nginx | host `3000` → container `80` |

Backing services: Postgres (`event/audit store`), Redis (`Envelope` streams), and MinIO (`S3-compatible lake`). Runtime lake data remains rooted at `data/` locally and `/data` in containers.

## Prerequisites

```bash
cp .env.example .env
# fill COINEXT__BINANCE__* only for sandbox/live execution
```

All application services read `COINEXT__SECTION__KEY` env vars. The default YAML config directory is `foundation/config`.

## Bring-up

```bash
# Base topology
just up
# or directly:
docker compose up -d --build

# Dev + observability overlays
just up-dev
```

The dev overlay publishes ports to localhost, bind-mounts lifecycle sources, and runs the API with
`uvicorn --reload` watching `*/python` trees plus the API app. Python images install workspace
packages via `uv sync` (see Dockerfiles); only the thin service directory uses
`COINEXT_SERVICE_PYTHONPATH`. Rust services and `coinext_py` still require image rebuilds.

## Observability overlay

```bash
docker compose -f docker-compose.yml -f docker-compose.obs.yml up -d --build
```

The overlay mounts config from `operations-interface/deployment`:

- `otel-collector-config.yaml`
- `prometheus/prometheus.yml`
- `loki/loki-config.yml`
- `tempo/tempo-config.yml`
- `grafana/provisioning`
- `grafana/dashboards`

Open:

- Grafana: <http://localhost:3001> (`admin` / `admin` by default)
- Prometheus: <http://localhost:9090>
- MinIO console: <http://localhost:9001>

Signal flow:

```text
apps ──metrics(/metrics)──────────────► prometheus ──► grafana
apps ──OTLP(4317/4318)──► otel-collector ──┬─► tempo (traces) ──► grafana
                                           └─► loki  (logs)   ──► grafana
```

The Redis Envelope `trace_id` is the cross-service correlation key.

## Validate without starting containers

```bash
just compose-check
```

This validates base, dev, obs, and dev+obs compose topologies. It seeds a temporary `.env` from `.env.example` only when no `.env` exists.

## Tear down

```bash
docker compose down
docker compose down -v
just down
```

## Python image layout

1. `uv sync --frozen` installs root workspace members + service extras into `/opt/venv`.
2. `maturin` wheel (`coinext_py`) is layered on top (api/trader).
3. Entrypoint: `entrypoint-python.sh` only appends `COINEXT_SERVICE_PYTHONPATH` (service app dir).
4. `pythonpath.env` is a **fallback doc** for source-mount-only containers — not used by default images.

## Known gaps

- `coinext-ingest` and `coinext-exec-svc` are workspace-excluded daemons (partial).
- `trader`, `risk-monitor`, `api`, and `ui` are deployable scaffolds, not a proved live stack.
- Production secret management remains open; `.env` is for local/dev single-VPS operation.
