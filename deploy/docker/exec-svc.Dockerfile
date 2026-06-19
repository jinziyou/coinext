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
# NOTE: coinext-exec-svc is a workspace-excluded stub today; this Dockerfile is valid scaffolding that
# is not expected to build until the crate is implemented (coinext-network + adapters land first).
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
# TODO(venue/IO): real venue order routing lives in coinext-adapters/binance via coinext-network; the
# append-only OrderEvent store + reconciliation lives in coinext-persistence (ARCHITECTURE.md §7).
RUN cargo build --release --bin coinext-exec-svc \
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
