# syntax=docker/dockerfile:1
# ingestor — market-data daemon (coinext-ingest). Metrics :9101.
# Status: partial. Workspace-excluded crate; cargo-chef multi-stage → distroless.

FROM rust:1.97-bookworm AS chef
RUN cargo install cargo-chef --locked
WORKDIR /build
ENV CARGO_TARGET_DIR=/build/target

FROM chef AS planner
COPY . .
WORKDIR /build/market-data/crates/coinext-ingest
RUN cargo chef prepare --recipe-path /build/recipe.json

FROM chef AS builder
COPY --from=planner /build/recipe.json /build/recipe.json
WORKDIR /build/market-data/crates/coinext-ingest
RUN cargo chef cook --release --recipe-path /build/recipe.json
COPY . /build
WORKDIR /build/market-data/crates/coinext-ingest
RUN cargo build --release \
 && cp /build/target/release/coinext-ingest /coinext-ingest

FROM gcr.io/distroless/cc-debian12 AS runtime
WORKDIR /app
COPY --from=builder /coinext-ingest /app/coinext-ingest

EXPOSE 9101
ENTRYPOINT ["/app/coinext-ingest"]
CMD ["run"]
