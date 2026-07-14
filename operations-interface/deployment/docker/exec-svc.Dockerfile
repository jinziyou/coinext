# syntax=docker/dockerfile:1
# ----------------------------------------------------------------------------------------------
# exec-svc (Rust coinext-exec-svc) — OMS / execution service.
#
# Risk-gated order routing, Order FSM driving, and execution-report folding (ARCHITECTURE.md §5).
# SLO histogram: submit_to_ack_ns. Exposes Prometheus metrics on :9102 and a control/admin API on
# :8081 (e.g. kill-switch, reconcile triggers).
#
# Multi-stage cargo-chef build (deps cached separately from source) -> debian-slim runtime. We use
# debian-slim rather than distroless here so the control API healthcheck can shell out if needed.
# `coinext-exec-svc` is workspace-excluded, so the Docker build runs cargo-chef/cargo from the
# crate directory instead of the root workspace; the binary is still an OMS/risk wiring stub.
# ----------------------------------------------------------------------------------------------

FROM rust:1.97 AS chef
RUN cargo install cargo-chef --locked
WORKDIR /build

FROM chef AS planner
COPY . .
WORKDIR /build/execution-live/execution-service/rust/coinext-exec-svc
RUN cargo chef prepare --recipe-path /build/recipe.json

FROM chef AS builder
COPY --from=planner /build/recipe.json /build/recipe.json
WORKDIR /build/execution-live/execution-service/rust/coinext-exec-svc
RUN cargo chef cook --release --recipe-path /build/recipe.json
COPY . /build
WORKDIR /build/execution-live/execution-service/rust/coinext-exec-svc
# TODO(venue/IO): real venue order routing lives in market-data/venue-adapters/binance/rust/coinext-adapters-binance via market-data/network-transport/rust/coinext-network; the
# append-only OrderEvent store + reconciliation lives in operations-interface/persistence/rust/coinext-persistence (ARCHITECTURE.md §7).
RUN cargo build --release \
 && cp target/release/coinext-exec-svc /coinext-exec-svc

# --- runtime: debian-slim (small, has a shell + libssl for TLS to the venue) ---
FROM debian:bookworm-slim AS runtime
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates libssl3 \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=builder /coinext-exec-svc /app/coinext-exec-svc

EXPOSE 9102 8081
# Reads COINEXT__* env: COINEXT__REDIS__URL, COINEXT__POSTGRES__DSN, COINEXT__BINANCE__*, COINEXT__RISK__*,
# COINEXT__METRICS__PORT, COINEXT__CONTROL__PORT, COINEXT__OTEL__ENDPOINT.
ENTRYPOINT ["/app/coinext-exec-svc"]
CMD ["run"]
