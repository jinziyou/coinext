# syntax=docker/dockerfile:1
# ----------------------------------------------------------------------------------------------
# exec-svc (Rust qv-exec-svc) — OMS / execution service.
#
# Risk-gated order routing, Order FSM driving, and execution-report folding (ARCHITECTURE.md §5).
# SLO histogram: submit_to_ack_ns. Exposes Prometheus metrics on :9102 and a control/admin API on
# :8081 (e.g. kill-switch, reconcile triggers).
#
# Multi-stage cargo-chef build (deps cached separately from source) -> debian-slim runtime. We use
# debian-slim rather than distroless here so the control API healthcheck can shell out if needed.
# NOTE: qv-exec-svc is a workspace-excluded stub today; this Dockerfile is valid scaffolding that
# is not expected to build until the crate is implemented (qv-network + adapters land first).
# ----------------------------------------------------------------------------------------------

FROM rust:1.95 AS chef
RUN cargo install cargo-chef --locked
WORKDIR /build

FROM chef AS planner
COPY . .
RUN cargo chef prepare --recipe-path recipe.json

FROM chef AS builder
COPY --from=planner /build/recipe.json recipe.json
RUN cargo chef cook --release --recipe-path recipe.json
COPY . .
# TODO(venue/IO): real venue order routing lives in qv-adapters/binance via qv-network; the
# append-only OrderEvent store + reconciliation lives in qv-persistence (ARCHITECTURE.md §7).
RUN cargo build --release --bin qv-exec-svc \
 && cp target/release/qv-exec-svc /qv-exec-svc

# --- runtime: debian-slim (small, has a shell + libssl for TLS to the venue) ---
FROM debian:bookworm-slim AS runtime
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates libssl3 \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=builder /qv-exec-svc /app/qv-exec-svc

EXPOSE 9102 8081
# Reads VQ__* env: VQ__REDIS__URL, VQ__POSTGRES__DSN, VQ__BINANCE__*, VQ__RISK__*,
# VQ__METRICS__PORT, VQ__CONTROL__PORT, VQ__OTEL__ENDPOINT.
ENTRYPOINT ["/app/qv-exec-svc"]
CMD ["run"]
