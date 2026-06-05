# VeloxQuant task runner. `just` (https://github.com/casey/just) wraps the common workflows.
set shell := ["bash", "-uc"]

# Default: list recipes
default:
    @just --list

# --- Rust core ---

# Run the Rust unit + property tests over the core workspace
test:
    cargo test

# Format + lint the Rust code
lint:
    cargo fmt --all
    cargo clippy --all-targets -- -D warnings

# Run the example SMA-crossover backtest
backtest:
    cargo run -p qv-example-backtest

# Build the optimized release binaries
build-release:
    cargo build --release

# --- Python control plane ---

# Create the venv and install the control-plane deps (research extras + dev tools)
py-setup:
    uv sync --extra research --group dev

# Build the qv_py PyO3 extension into the active venv (editable)
py-build:
    uvx maturin develop --manifest-path crates/qv-py/Cargo.toml --features python

# Run the Python tests (requires py-build first for qv_py)
py-test:
    uv run pytest

# Lint + format the Python code
py-lint:
    uv run ruff check python tests
    uv run ruff format python tests

# Run a backtest via the qv CLI
cli-backtest *ARGS:
    uv run qv backtest {{ARGS}}

# --- Ops ---

# Bring up the full dockerized stack (prod profile)
up:
    docker compose up -d --build

# Bring up local dev overrides + observability
up-dev:
    docker compose -f docker-compose.yml -f docker-compose.dev.yml -f docker-compose.obs.yml up -d --build

# Tear everything down
down:
    docker compose down -v

# Validate the compose topology without starting anything
compose-check:
    docker compose config -q && echo "compose OK"

# --- Everything ---

# Full local verification: rust tests + python tests + compose lint
verify: test compose-check
    @echo "core verified"
