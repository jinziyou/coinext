# Coinext UI — operator dashboard

A minimal Vite + React + TypeScript operator cockpit for Coinext. It is a
**read-only-by-default** dashboard over the `api` service (FastAPI), with one
guarded mutating action: the global **kill-switch**.

See `docs/ARCHITECTURE.md` §8 (observability & deployment) for where this fits.

## Panels

| Panel                | Source endpoint            | Notes                                              |
| -------------------- | -------------------------- | -------------------------------------------------- |
| Runs                 | `GET /runs`                | runs across `backtest` / `sandbox` / `live`        |
| Live Positions / PnL | `GET /positions`           | mark-sourced unrealized PnL (from the Cache)       |
| Fills                | `GET /fills`               | recent execution fills                             |
| Latency (SLO)        | `GET /latency`             | SLO histograms (`submit_to_ack_ns`, …) in ns       |
| Kill-Switch          | `GET/POST /control/killswitch` | guarded confirm dialog; trips `coinext-risk-engine` |

All monetary / quantity / price fields cross the wire as **strings** to preserve
the fixed-precision integer domain (no `f64`; see ARCHITECTURE §4). The UI treats
them as opaque display strings and does not do float math on them.

## Run locally

```bash
npm install
npm run dev          # http://localhost:3000
```

Point it at a running `api` service with `VITE_API_BASE` (defaults to
`http://localhost:8000`):

```bash
VITE_API_BASE=http://localhost:8000 npm run dev
```

Or route through the dev proxy (avoids CORS) by setting the base to `/api`:

```bash
VITE_API_BASE=/api VITE_API_TARGET=http://localhost:8000 npm run dev
```

## Scripts

- `npm run dev` — Vite dev server on port 3000 (canonical `ui` port).
- `npm run build` — type-check (`tsc --noEmit`) then `vite build` into `dist/`.
- `npm run preview` — serve the production build.
- `npm run typecheck` — type-check only.

## Build / deploy

Built and shipped via `deploy/docker/ui.Dockerfile` (Node 22). In the compose
stack the UI is served on port **3000** and talks to `api` on **8000**.

## Status

This is a **scaffold**. Panels render real fetched data, but UX is intentionally
minimal. TODOs in the source mark where richer operator tooling (per-run drill
down, order ladder, charting, auth) will land.
