# syntax=docker/dockerfile:1
# ----------------------------------------------------------------------------------------------
# trader (Python coinext_live) — the live TradingNode.
#
# Runs the SAME engines as backtest; the Kernel injects a LiveClock + Binance Data/Exec clients
# (ARCHITECTURE.md §1/§7). The decision core is the compiled `coinext_py` extension (PyO3); the strategy
# handlers dispatch through it. SLO histogram: strategy_dispatch_ns. Metrics on :9103.
#
# Multi-stage:
#   1) rust builder  — compile the coinext_py extension with maturin (the Kernel + domain mirror).
#   2) python runtime — python:3.13-slim, deps installed with `uv`, coinext_py wheel dropped in.
# ----------------------------------------------------------------------------------------------

# --- stage 1: build the coinext_py PyO3 wheel (foundation/ffi-bridge/rust/coinext-py, --features python) ---
FROM rust:1.96-slim AS rust-builder
RUN apt-get update  && apt-get install -y --no-install-recommends python3 python3-pip python3-venv build-essential  && rm -rf /var/lib/apt/lists/*
# Install maturin to produce a wheel for coinext_py.
RUN pip install --break-system-packages "maturin>=1.7,<2"
WORKDIR /src
COPY . .
# `module-name = coinext_py`, `features = ["python"]` are set in foundation/ffi-bridge/rust/coinext-py/pyproject.toml.
RUN maturin build --release --manifest-path foundation/ffi-bridge/rust/coinext-py/Cargo.toml --out /wheels

# --- stage 2: python runtime with uv ---
FROM python:3.13-slim AS runtime
# uv: fast, reproducible dependency installs (ARCHITECTURE.md toolchain).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app

# Project metadata first for layer caching of the dependency install.
COPY pyproject.toml README.md ./
COPY foundation ./foundation
COPY market-data ./market-data
COPY strategy-research ./strategy-research
COPY backtesting-simulation ./backtesting-simulation
COPY analytics-optimization ./analytics-optimization
COPY risk-portfolio ./risk-portfolio
COPY execution-live ./execution-live
COPY operations-interface ./operations-interface
# The thin per-account live wrapper lives in execution-live/trader-service/service/trader/main.py
# (reads COINEXT__* env, builds one coinext_live.TradingNode). Copy it in and put it on the import path.
COPY execution-live/trader-service/service/trader ./trader

# Install the control-plane deps needed by the live runtime (bus + live + obs extras).
# `--system` installs into the image interpreter (no venv indirection in containers).
RUN uv pip install --system --no-cache       "redis>=5" "msgpack>=1.2.1" "anyio>=4"       "structlog>=24" "prometheus-client>=0.20" "opentelemetry-sdk>=1.25"       "pydantic>=2.7" "pyyaml>=6" "typer>=0.12" "numpy>=2.0"

# Drop in the compiled Rust core (coinext_py) built in stage 1.
COPY --from=rust-builder /wheels/*.whl /tmp/wheels/
RUN uv pip install --system --no-cache /tmp/wheels/*.whl && rm -rf /tmp/wheels

# Make the source packages importable (coinext_live, coinext_strategy, coinext_kernel, coinext_bus, ...)
# plus the trader wrapper.
ENV PYTHONPATH=/app/foundation/python-contracts/python:/app/foundation/runtime-config/python:/app/market-data/data-lake/python:/app/strategy-research/strategy-api/python:/app/strategy-research/indicators/python:/app/backtesting-simulation/kernel/python:/app/backtesting-simulation/runner/python:/app/backtesting-simulation/parity-gates/python:/app/analytics-optimization/analytics/python:/app/analytics-optimization/screening/python:/app/analytics-optimization/optimizer/python:/app/analytics-optimization/derivatives/python:/app/risk-portfolio/risk-facade/python:/app/risk-portfolio/portfolio-facade/python:/app/execution-live/live-runtime/python:/app/operations-interface/bus/python:/app/operations-interface/cli/python:/app/trader
ENV PYTHONUNBUFFERED=1

EXPOSE 9103
# TODO(venue/IO): coinext_live wires the Binance clients behind the ExecutionClient/DataClient ports.
# Run the per-account live node. main.py reads COINEXT__* env (account, env, symbol, redis, metrics
# port) — one container == one account; there is no --config flag (config is env-driven).
ENTRYPOINT ["python", "-m", "main"]
