# Coinext service index

Deployable service wrappers are grouped by lifecycle module. Dockerfiles stay under
`operations-interface/deployment/docker`; compose files stay at the repository root.

| Service | Lifecycle module | Source path | Dockerfile | Status |
|---|---|---|---|---|
| `ingestor` | `market-data/` | `market-data/crates/coinext-ingest` (+ optional `market-data/services/ingestor` notes) | `operations-interface/deployment/docker/ingestor.Dockerfile` | partial |
| `exec-svc` | `execution-live/` | `execution-live/crates/coinext-exec-svc` | `operations-interface/deployment/docker/exec-svc.Dockerfile` | partial |
| `trader` | `execution-live/` | `execution-live/services/trader` | `operations-interface/deployment/docker/trader.Dockerfile` | scaffold |
| `risk-monitor` | `risk-portfolio/` | `risk-portfolio/services/risk-monitor` | `operations-interface/deployment/docker/risk-monitor.Dockerfile` | scaffold |
| `api` | `operations-interface/` | `operations-interface/services/api` | `operations-interface/deployment/docker/api.Dockerfile` | scaffold |
| `ui` | `operations-interface/` | `operations-interface/services/ui` | `operations-interface/deployment/docker/ui.Dockerfile` | scaffold |

Notes:

- Rust service daemons are workspace-excluded; verify with `just test-live-edge` (artifacts under `target/live-edge`).
- Python wrappers keep heavy/native imports lazy so modules import in dependency-light tests.
- Config uses `COINEXT__SECTION__KEY` and defaults to `foundation/config`.
- Runtime data lake mounts stay at root `data/` locally and `/data` in containers.
