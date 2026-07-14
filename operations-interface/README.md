# operations-interface

Bus, CLI, API, UI, deployment assets, persistence.

| Path | Role | Status |
|---|---|---|
| `crates/coinext-bus` | In-proc bus + Redis Envelope contract | verified |
| `crates/coinext-persistence` | Event store / SeqCursor (workspace-excluded) | partial |
| `python/coinext_bus` | Python bus client | verified |
| `python/coinext_cli` | `coinext` CLI | verified |
| `services/api` | FastAPI control plane | scaffold |
| `services/ui` | React/Vite dashboard | scaffold |
| `deployment/` | Dockerfiles, compose overlays, observability | partial |
