# syntax=docker/dockerfile:1
# ----------------------------------------------------------------------------------------------
# ingestor (Rust coinext-ingest) — market-data ingestion daemon.
#
# Normalizes Binance WS frames and republishes them on the Redis-Streams bus (ARCHITECTURE.md §7).
# SLO histogram: ingest_to_publish_ns. Exposes Prometheus metrics on :9101.
#
# Multi-stage, cargo-chef style: a dependency-planning stage and a cached build stage keep image
# rebuilds fast (only re-compile deps when Cargo.lock changes), then a tiny distroless runtime.
# NOTE: coinext-ingest is a workspace-excluded stub today (root Cargo.toml `exclude`); it is built here
# with its own manifest. This Dockerfile is intentionally valid-but-not-yet-buildable scaffolding.
# ----------------------------------------------------------------------------------------------

# --- chef: provides cargo-chef for dependency caching ---
FROM rust:1.96 AS chef
RUN cargo install cargo-chef --locked
WORKDIR /build

# --- planner: compute the dependency recipe (cache key) ---
FROM chef AS planner
COPY . .
# `prepare` writes recipe.json describing only the dependency graph (not our source).
RUN cargo chef prepare --recipe-path recipe.json

# --- builder: cook deps from the recipe (cached), then build the binary ---
FROM chef AS builder
COPY --from=planner /build/recipe.json recipe.json
# Cook just the dependencies first — this layer is reused until recipe.json changes.
RUN cargo chef cook --release --recipe-path recipe.json
COPY . .
# TODO(venue/IO): coinext-ingest's real WS/REST ingestion lives in coinext-network + coinext-adapters/binance.
RUN cargo build --release --bin coinext-ingest \
 && cp target/release/coinext-ingest /coinext-ingest

# --- runtime: distroless (no shell, minimal attack surface) ---
FROM gcr.io/distroless/cc-debian12 AS runtime
WORKDIR /app
COPY --from=builder /coinext-ingest /app/coinext-ingest

# Prometheus metrics endpoint (scraped by deploy/prometheus/prometheus.yml job `ingestor`).
EXPOSE 9101
# Config is supplied via COINEXT__* env (see .env / .env.example). The binary reads COINEXT__REDIS__URL,
# COINEXT__BINANCE__*, COINEXT__METRICS__PORT, COINEXT__OTEL__ENDPOINT, etc.
ENTRYPOINT ["/app/coinext-ingest"]
CMD ["run"]
