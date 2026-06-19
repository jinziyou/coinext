# Coinext — Deployment

Dockerized multi-service stack for a single VPS, with `prod` / `dev` / `obs` compose profiles
([`ARCHITECTURE.md`](../ARCHITECTURE.md) §7). The topology preserves the parity invariant: the SAME engines run
everywhere; only the Kernel-injected Clock / Cache / clients differ between backtest, sandbox, and
live (selected via `COINEXT__ENV` and the Binance section of `.env`).

## Services & ports

| Service        | Build                                   | Runtime              | Port(s)              |
|----------------|-----------------------------------------|----------------------|----------------------|
| `ingestor`     | `deploy/docker/ingestor.Dockerfile`     | Rust `coinext-ingest`     | metrics 9101         |
| `exec-svc`     | `deploy/docker/exec-svc.Dockerfile`     | Rust `coinext-exec-svc`   | metrics 9102, ctl 8081 |
| `trader`       | `deploy/docker/trader.Dockerfile`       | Python `coinext_live`     | metrics 9103         |
| `risk-monitor` | `deploy/docker/risk-monitor.Dockerfile` | Python               | metrics 9104         |
| `api`          | `deploy/docker/api.Dockerfile`          | Python FastAPI       | 8000                 |
| `ui`           | `deploy/docker/ui.Dockerfile`           | Node 22 build → nginx| 3000 (dev → :80)     |
| `postgres`     | image `postgres:16`                     | event/audit store    | 5432                 |
| `redis`        | image `redis:7`                         | Redis-Streams bus    | 6379                 |
| `minio`        | image `minio/minio`                     | S3 data lake         | 9000 (S3), 9001 (console) |

Observability overlay (`docker-compose.obs.yml`):

| Service          | Image                                       | Port(s)        |
|------------------|---------------------------------------------|----------------|
| `otel-collector` | `otel/opentelemetry-collector-contrib`      | 4317, 4318     |
| `prometheus`     | `prom/prometheus`                           | 9090           |
| `grafana`        | `grafana/grafana`                           | 3001 (→ :3000) |
| `loki`           | `grafana/loki`                              | 3100           |
| `tempo`          | `grafana/tempo`                             | 3200           |

## Prerequisites

```bash
cp .env.example .env      # then fill in COINEXT__BINANCE__* for sandbox/live
```

All services read configuration from `.env` using the `COINEXT__SECTION__KEY` convention.

## Bring-up

### Production topology

```bash
# From the repo root:
docker compose up -d --build
# equivalently:
just up
```

This starts the app services plus `postgres`, `redis`, and `minio`. Internal service-to-service
traffic uses the docker network by name (e.g. `redis:6379`); in prod, app ports are NOT published
to the host.

### With the observability overlay

```bash
docker compose -f docker-compose.yml -f docker-compose.obs.yml up -d --build
```

Adds Prometheus + Grafana + Loki + Tempo + the OpenTelemetry Collector. Grafana auto-provisions the
Prometheus/Loki/Tempo datasources and the **Coinext — SLOs & PnL** dashboard from
`deploy/grafana/`.

Open:
- Grafana   → http://localhost:3001  (default `admin` / `admin`, override via `COINEXT__GRAFANA__*`)
- Prometheus→ http://localhost:9090
- MinIO     → http://localhost:9001

### Local development

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
# with observability too:
docker compose -f docker-compose.yml -f docker-compose.dev.yml -f docker-compose.obs.yml up -d --build
# equivalently:
just up-dev
```

The `dev` overlay publishes every port to `localhost`, bind-mounts `python/`, `config/`, and `data/`
into the Python containers, and runs the `api` under `uvicorn --reload`. See the header of
`docker-compose.dev.yml` for hot-reload caveats (Rust services and the compiled `coinext_py` extension
require an image rebuild; pure-Python edits reload live).

## Validate the topology (no containers started)

```bash
docker compose config -q && echo OK
# or:
just compose-check
```

## Tear down

```bash
docker compose down            # keep volumes
docker compose down -v         # also drop postgres/redis/minio/data volumes
# equivalently:
just down
```

## Observability wiring (how signals flow)

```
apps ──metrics(/metrics)──────────────► prometheus ──► grafana
apps ──OTLP(4317/4318)──► otel-collector ──┬─► tempo (traces) ──► grafana
                                           └─► loki  (logs)   ──► grafana
```

The `trace_id` carried in the Redis `Envelope` propagates across services, so a single trace can
span `ingestor → trader → exec-svc`, and Grafana correlates metrics ↔ logs ↔ traces via that id.
SLO histograms surfaced on the dashboard: `strategy_dispatch_ns`, `submit_to_ack_ns`,
`ingest_to_publish_ns`, `risk_denials`, and PnL.

## Notes & TODOs

- The Rust service crates (`coinext-ingest`, `coinext-exec-svc`) are workspace-excluded **stubs** today; their
  Dockerfiles are valid scaffolding and are not expected to build until the venue adapters
  (`coinext-network`, `coinext-adapters/binance`) and persistence land.
- The Python service entrypoints (`coinext_live`, `coinext_api`, `coinext_risk.monitor`) and the UI source
  (`services/ui/`) are scaffolded by their respective areas; the Dockerfiles reference the agreed
  module/entrypoint names.
- Secrets management (SOPS/Vault) is an open question (`docs/ARCHITECTURE.md`); for now secrets come
  from `.env` (git-ignored).
