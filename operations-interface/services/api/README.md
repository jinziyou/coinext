# api service

| | |
|---|---|
| **Status** | scaffold |
| **Source** | `operations-interface/services/api` |
| **Image** | `operations-interface/deployment/docker/api.Dockerfile` |
| **Port** | `8000` (`/health`, `/healthz`) |
| **Auth** | `X-API-Key` vs `COINEXT__API__KEY` on mutating routes |

Control-plane FastAPI app for the UI: runs, positions, catalog, backtest trigger, kill-switch, live WS.
Does **not** run the hot path. Lazy-imports heavy/native deps.

```bash
# local (after just py-setup && just py-build)
uv run uvicorn app:app --app-dir operations-interface/services/api --reload
```
