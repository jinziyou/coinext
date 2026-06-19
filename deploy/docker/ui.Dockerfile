# syntax=docker/dockerfile:1
# ----------------------------------------------------------------------------------------------
# ui (Node 22 / Vite -> nginx) — the Coinext operations dashboard.
#
# Static React/Vite single-page app that talks to the `api` service. Vite inlines VITE_* env at
# BUILD time, so VITE_API_BASE is passed as a build arg (wired from COINEXT__UI__API_BASE in compose).
# The built bundle is served by nginx:alpine. Container listens on :80 (mapped to host :3000 in dev).
#
# NOTE: the UI source lives under services/ui/ (ARCHITECTURE.md §3). This is valid scaffolding; the
# SPA itself is created by the UI area.
# ----------------------------------------------------------------------------------------------

# --- stage 1: build the static bundle ---
FROM node:22-alpine AS builder
WORKDIR /app/ui

# Install deps first (cached until lockfile changes).
COPY services/ui/package.json services/ui/package-lock.json* ./
RUN npm ci || npm install

# Build-time API base baked into the bundle (Vite reads import.meta.env.VITE_API_BASE).
ARG VITE_API_BASE=http://localhost:8000
ENV VITE_API_BASE=${VITE_API_BASE}

COPY services/ui/ ./
# TODO: produces dist/ — the static SPA (positions, PnL, run control, SLO panels).
RUN npm run build

# --- stage 2: serve static files with nginx ---
FROM nginx:alpine AS runtime
# SPA-friendly nginx config (history-API fallback to index.html). Provided by the UI area;
# falls back to the image default if absent.
COPY services/ui/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/ui/dist /usr/share/nginx/html

EXPOSE 80
# nginx:alpine's default entrypoint runs nginx in the foreground.
