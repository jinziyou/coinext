# ui service

| | |
|---|---|
| **Status** | scaffold |
| **Source** | `operations-interface/services/ui` |
| **Image** | `operations-interface/deployment/docker/ui.Dockerfile` |
| **Port** | host `3000` → container `80` (dev overlay) |
| **API** | same-origin `/api` (nginx proxy) or `VITE_API_BASE` |

React/Vite operator dashboard. Local HMR:

```bash
cd operations-interface/services/ui && npm install && npm run dev
```

Compose serves the production nginx bundle; stop `ui` before host HMR on :3000.
