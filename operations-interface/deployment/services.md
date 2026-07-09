# Coinext service index

Deployable service wrappers are grouped by lifecycle module. Dockerfiles stay centralized under `operations-interface/deployment/docker`; compose files stay at the repository root.

| Service | Lifecycle module | Source path | Dockerfile | Status |
|---|---|---|---|---|
| `ingestor` | `market-data/` | `market-data/ingestion-service/service/ingestor` + Rust crate `market-data/ingestion-service/rust/coinext-ingest` | `operations-interface/deployment/docker/ingestor.Dockerfile` | stub daemon |
| `exec-svc` | `execution-live/` | Rust crate `execution-live/execution-service/rust/coinext-exec-svc` | `operations-interface/deployment/docker/exec-svc.Dockerfile` | stub daemon |
| `trader` | `execution-live/` | `execution-live/trader-service/service/trader` | `operations-interface/deployment/docker/trader.Dockerfile` | scaffold wrapper around `coinext_live` |
| `risk-monitor` | `risk-portfolio/` | `risk-portfolio/risk-monitor/service/risk-monitor` | `operations-interface/deployment/docker/risk-monitor.Dockerfile` | scaffold out-of-band supervisor |
| `api` | `operations-interface/` | `operations-interface/api/service/api` | `operations-interface/deployment/docker/api.Dockerfile` | scaffold FastAPI control plane |
| `ui` | `operations-interface/` | `operations-interface/ui/service/ui` | `operations-interface/deployment/docker/ui.Dockerfile` | scaffold React/Vite dashboard |

Notes:

- Rust service daemons are intentionally workspace-excluded and verified by explicit manifest-path commands in `just test-live-edge` and CI.
- Python wrappers keep heavy/native imports lazy so modules import in dependency-light tests.
- Config uses the `COINEXT__SECTION__KEY` convention and defaults to `foundation/runtime-config/config`.
- Runtime data lake mounts stay at root `data/` locally and `/data` in containers.
