# syntax=docker/dockerfile:1
# ----------------------------------------------------------------------------------------------
# ingestor (Rust coinext-ingest) — market-data ingestion daemon.
#
# Normalizes Binance WS frames and republishes them on the Redis-Streams bus (ARCHITECTURE.md §7).
# SLO histogram: ingest_to_publish_ns. Exposes Prometheus metrics on :9101.
#
# Multi-stage, cargo-chef style: a dependency-planning stage and a cached build stage keep image
# rebuilds fast, then a tiny distroless runtime. `coinext-ingest` is workspace-excluded, so the
# cargo-chef and cargo build steps run from `market-data/ingestion-service/rust/coinext-ingest` instead of the root workspace.
# ----------------------------------------------------------------------------------------------

# --- chef: provides cargo-chef for dependency caching ---
FROM rust:1.96 AS chef
RUN cargo install cargo-chef --locked
WORKDIR /build

# --- planner: compute the dependency recipe (cache key) ---
FROM chef AS planner
COPY . .
WORKDIR /build/market-data/ingestion-service/rust/coinext-ingest
# `prepare` writes recipe.json for the excluded crate's dependency graph (not app source).
RUN cargo chef prepare --recipe-path /build/recipe.json

# --- builder: cook deps from the recipe (cached), then build the binary ---
FROM chef AS builder
COPY --from=planner /build/recipe.json /build/recipe.json
WORKDIR /build/market-data/ingestion-service/rust/coinext-ingest
# Cook just the dependencies first — this layer is reused until recipe.json changes.
RUN cargo chef cook --release --recipe-path /build/recipe.json
COPY . /build
WORKDIR /build/market-data/ingestion-service/rust/coinext-ingest
# TODO(venue/IO): coinext-ingest's real WS/REST ingestion lives in market-data/network-transport/rust/coinext-network + market-data/venue-adapters/binance/rust/coinext-adapters-binance.
RUN cargo build --release \
 && cp target/release/coinext-ingest /coinext-ingest

# --- runtime: distroless (no shell, minimal attack surface) ---
FROM gcr.io/distroless/cc-debian12 AS runtime
WORKDIR /app
COPY --from=builder /coinext-ingest /app/coinext-ingest

# Prometheus metrics endpoint (scraped by operations-interface/deployment/prometheus/prometheus.yml job `ingestor`).
EXPOSE 9101
# Config is supplied via COINEXT__* env (see .env / .env.example). The binary reads COINEXT__REDIS__URL,
# COINEXT__BINANCE__*, COINEXT__METRICS__PORT, COINEXT__OTEL__ENDPOINT, etc.
ENTRYPOINT ["/app/coinext-ingest"]
CMD ["run"]
