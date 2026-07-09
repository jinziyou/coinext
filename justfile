# Coinext task runner. `just` (https://github.com/casey/just) wraps the common workflows.
set shell := ["bash", "-uc"]

# Default: list recipes
default:
    @just --list

# --- Rust core ---

# Run the Rust unit + property tests over the core workspace
test:
    cargo test

# Test/build the workspace-excluded live-edge crates the root workspace intentionally skips
test-live-edge:
    cargo test --manifest-path crates/coinext-network/Cargo.toml
    cargo test --manifest-path crates/coinext-adapters/binance/Cargo.toml
    cargo test --manifest-path crates/coinext-persistence/Cargo.toml
    cargo build --manifest-path crates/coinext-ingest/Cargo.toml
    cargo build --manifest-path crates/coinext-exec-svc/Cargo.toml

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
    uvx maturin develop --manifest-path crates/coinext-py/Cargo.toml --features python

# Run the Python tests (requires py-build first for coinext_py)
py-test:
    uv run pytest

# Lint + format Python code, service wrappers, and tests
py-lint:
    uv run ruff check python services tests
    uv run ruff format python services tests

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

# Validate all compose layers without starting anything. Seeds .env from .env.example only if absent.
compose-check:
    @set -e; tmp_created=0; if [ ! -f .env ]; then cp .env.example .env; tmp_created=1; fi; trap 'if [ "$tmp_created" = 1 ]; then rm -f .env; fi' EXIT; docker compose -f docker-compose.yml config -q; docker compose -f docker-compose.yml -f docker-compose.dev.yml config -q; docker compose -f docker-compose.yml -f docker-compose.obs.yml config -q; docker compose -f docker-compose.yml -f docker-compose.dev.yml -f docker-compose.obs.yml config -q; echo "compose OK (base, dev, obs, dev+obs)"

# --- Everything ---

# Local verification: core workspace + live-edge crates + compose topology
verify: test test-live-edge compose-check
    @echo "coinext verified"
