# Coinext task runner. `just` (https://github.com/casey/just) wraps the common workflows.
set shell := ["bash", "-uc"]

# Default: list recipes
default:
    @just --list

# --- Rust core ---

# Run the Rust unit + property tests over the core workspace
test:
    cargo test --workspace

# Test/build the workspace-excluded live-edge crates the root workspace intentionally skips
test-live-edge:
    cargo test --manifest-path market-data/network-transport/rust/coinext-network/Cargo.toml
    cargo test --manifest-path market-data/venue-adapters/binance/rust/coinext-adapters-binance/Cargo.toml
    cargo test --manifest-path operations-interface/persistence/rust/coinext-persistence/Cargo.toml
    cargo build --manifest-path market-data/ingestion-service/rust/coinext-ingest/Cargo.toml
    cargo build --manifest-path execution-live/execution-service/rust/coinext-exec-svc/Cargo.toml

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

# --- Python control plane ---

# Create the venv and install research/config/api/bus deps plus dev tools
py-setup:
    uv sync --extra research --extra config --extra api --extra bus --group dev

# Build the coinext_py PyO3 extension into the active venv (editable)
py-build:
    uvx maturin develop --manifest-path foundation/ffi-bridge/rust/coinext-py/Cargo.toml --features python

# Run the Python tests (requires py-build first for coinext_py)
py-test:
    uv run pytest

# Lint + format Python code under root workflow modules and tests
py-lint:
    uv run ruff check foundation market-data strategy-research backtesting-simulation analytics-optimization risk-portfolio execution-live operations-interface tests
    uv run ruff format foundation market-data strategy-research backtesting-simulation analytics-optimization risk-portfolio execution-live operations-interface tests

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
