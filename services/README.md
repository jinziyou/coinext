# services/ — deployable service wrappers

Thin deployment wrappers around the VeloxQuant core. The load-bearing logic lives in the Rust crates
(`crates/`) and the Python control-plane packages (`python/`); these directories just package a unit
for deployment (one Dockerfile each, under `deploy/docker/`). See [`docs/ARCHITECTURE.md`](../docs/ARCHITECTURE.md)
§7–§8.

| Service        | Dir                  | Kind                  | Build                                 | Ports                  |
|----------------|----------------------|-----------------------|---------------------------------------|------------------------|
| `ingestor`     | [`ingestor/`](ingestor/)         | Rust (`qv-ingest`)    | `deploy/docker/ingestor.Dockerfile`     | metrics `9101`         |
| `exec-svc`     | (Rust `qv-exec-svc`) | Rust                  | `deploy/docker/exec-svc.Dockerfile`     | metrics `9102`, ctrl `8081` |
| `trader`       | [`trader/`](trader/)             | Python (`qv_live`)    | `deploy/docker/trader.Dockerfile`       | metrics `9103`         |
| `risk-monitor` | [`risk-monitor/`](risk-monitor/) | Python                | `deploy/docker/risk-monitor.Dockerfile` | metrics `9104`         |
| `api`          | [`api/`](api/)                   | Python (FastAPI)      | `deploy/docker/api.Dockerfile`          | `8000`                 |
| `ui`           | `../ui` (separate)   | Node 22 / Vite        | `deploy/docker/ui.Dockerfile`           | `3000`                 |

Notes:

- **`ingestor`** and **`exec-svc`** are Rust binaries — only the `ingestor/` wrapper here carries a
  README; both binaries' code lives in `crates/` (`qv-ingest`, `qv-exec-svc`).
- **`trader`**, **`risk-monitor`**, **`api`** are pure-Python wrappers. Every heavy / native import
  (`qv_py`, `qv_live`, `qv_bus`, `redis`, `fastapi`) is **lazy and guarded** so each module imports
  in a bare environment; each ships a `requirements.txt` mirroring the relevant root extras.
- Config everywhere follows the `VQ__SECTION__KEY` convention (see [`.env.example`](../.env.example)).
