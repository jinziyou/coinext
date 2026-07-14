# Coinext task runner. `just` (https://github.com/casey/just) wraps the common workflows.
set shell := ["bash", "-uc"]

# Shared target dir for workspace-excluded live-edge crates (avoids nested multi-GB target/).
export CARGO_TARGET_DIR_LIVE := justfile_directory() / "target" / "live-edge"

# Default: list recipes
default:
    @just --list

# --- Rust core ---

# Run the Rust unit + property tests over the core workspace
test:
    cargo test --workspace

# Test/build the workspace-excluded live-edge crates (artifacts under target/live-edge)
test-live-edge:
    CARGO_TARGET_DIR="{{CARGO_TARGET_DIR_LIVE}}" cargo test --manifest-path market-data/crates/coinext-network/Cargo.toml
    CARGO_TARGET_DIR="{{CARGO_TARGET_DIR_LIVE}}" cargo test --manifest-path market-data/crates/coinext-adapters-binance/Cargo.toml
    CARGO_TARGET_DIR="{{CARGO_TARGET_DIR_LIVE}}" cargo test --manifest-path operations-interface/crates/coinext-persistence/Cargo.toml
    CARGO_TARGET_DIR="{{CARGO_TARGET_DIR_LIVE}}" cargo build --manifest-path market-data/crates/coinext-ingest/Cargo.toml
    CARGO_TARGET_DIR="{{CARGO_TARGET_DIR_LIVE}}" cargo build --manifest-path execution-live/crates/coinext-exec-svc/Cargo.toml

# Run the partial exec-svc (SQLite + control :8081 + metrics :9102) until Ctrl-C
exec-svc:
    CARGO_TARGET_DIR="{{CARGO_TARGET_DIR_LIVE}}" cargo run --manifest-path execution-live/crates/coinext-exec-svc/Cargo.toml

# Offline ingest smoke (synthetic events → lake NDJSON + Parquet; optional REDIS URL)
ingest-smoke:
    CARGO_TARGET_DIR="{{CARGO_TARGET_DIR_LIVE}}" cargo run --manifest-path market-data/crates/coinext-ingest/Cargo.toml

# Live market-data ingest (requires network)
ingest-live:
    CARGO_TARGET_DIR="{{CARGO_TARGET_DIR_LIVE}}" cargo run --manifest-path market-data/crates/coinext-ingest/Cargo.toml --features live

# Format + lint the Rust code
lint:
    cargo fmt --all
    cargo clippy --all-targets --all-features -- -D warnings

# Run the example SMA-crossover backtest
backtest:
    cargo run -p coinext-example-backtest

# Build the optimized release binaries
build-release:
    cargo build --release

# Remove workspace + live-edge + any nested crate target/ dirs (local disk hygiene)
clean-targets:
    rm -rf target
    find . -type d -name target -not -path './.git/*' -prune -exec rm -rf {} +

# --- Python control plane ---

# Create the venv and install research/config/api/bus deps plus dev tools
py-setup:
    uv sync --extra research --extra config --extra api --extra bus --group dev

# Build the coinext_py PyO3 extension into the active venv (editable)
py-build:
    uvx maturin develop --manifest-path foundation/crates/coinext-py/Cargo.toml --features python

# Run the Python tests (requires py-build first for coinext_py)
py-test:
    uv run pytest

# Lint + format Python code under root workflow modules and tests
py-lint:
    uv run ruff check foundation market-data strategy-research backtesting-simulation analytics-optimization risk-portfolio execution-live operations-interface tests
    uv run ruff format foundation market-data strategy-research backtesting-simulation analytics-optimization risk-portfolio execution-live operations-interface tests

# CI-equivalent format check (does not rewrite files)
py-format-check:
    uv run ruff format --check foundation market-data strategy-research backtesting-simulation analytics-optimization risk-portfolio execution-live operations-interface tests

# Run a backtest via the coinext CLI
cli-backtest *ARGS:
    uv run coinext backtest {{ARGS}}

# --- Ops ---

# Bring up the base dockerized stack
up:
    docker compose up -d --build

# Bring up local dev overrides + observability
up-dev:
    docker compose -f docker-compose.yml -f docker-compose.dev.yml -f docker-compose.obs.yml up -d --build

# Tear everything down
down:
    docker compose down -v

# Validate root compose layers without starting anything. Works without shell-specific compose logic.
compose-check:
    python3 operations-interface/deployment/compose_check.py

# --- Everything ---

# Local verification: core workspace + live-edge crates + compose topology
verify: test test-live-edge compose-check
    @echo "coinext verified"
