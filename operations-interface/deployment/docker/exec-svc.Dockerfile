# syntax=docker/dockerfile:1
# exec-svc — OMS/execution service (coinext-exec-svc). Metrics :9102, control :8081.
# Status: partial. Workspace-excluded crate; cargo-chef multi-stage → debian-slim.

FROM rust:1.97-bookworm AS chef
RUN cargo install cargo-chef --locked
WORKDIR /build
ENV CARGO_TARGET_DIR=/build/target

FROM chef AS planner
COPY . .
WORKDIR /build/execution-live/crates/coinext-exec-svc
RUN cargo chef prepare --recipe-path /build/recipe.json

FROM chef AS builder
COPY --from=planner /build/recipe.json /build/recipe.json
WORKDIR /build/execution-live/crates/coinext-exec-svc
RUN cargo chef cook --release --recipe-path /build/recipe.json
COPY . /build
WORKDIR /build/execution-live/crates/coinext-exec-svc
RUN cargo build --release \
 && cp /build/target/release/coinext-exec-svc /coinext-exec-svc

FROM debian:bookworm-slim AS runtime
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates libssl3 \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=builder /coinext-exec-svc /app/coinext-exec-svc

EXPOSE 9102 8081
ENTRYPOINT ["/app/coinext-exec-svc"]
CMD ["run"]
