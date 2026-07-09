# syntax=docker/dockerfile:1
# ----------------------------------------------------------------------------------------------
# api (Python FastAPI) — control-plane REST/WS for the UI.
#
# Exposes run management, positions/PnL, and a live event stream (decoded from the Redis Envelope)
# to the dashboard. Embeds the compiled `coinext_py` extension so it can speak the integer-precision
# domain types (Price/Quantity/Money) without re-deriving them in Python (ARCHITECTURE.md §4/§6).
# Serves on :8000.
#
# Multi-stage:
#   1) rust builder  — maturin builds the coinext_py wheel from foundation/ffi-bridge/rust/coinext-py (--features python).
#   2) python runtime — python:3.13-slim, uv installs FastAPI/uvicorn + the wheel.
# ----------------------------------------------------------------------------------------------

# --- stage 1: build the coinext_py PyO3 wheel ---
FROM rust:1.95-slim AS rust-builder
RUN apt-get update  && apt-get install -y --no-install-recommends python3 python3-pip python3-venv build-essential  && rm -rf /var/lib/apt/lists/*
RUN pip install --break-system-packages "maturin>=1.7,<2"
WORKDIR /src
COPY . .
RUN maturin build --release --manifest-path foundation/ffi-bridge/rust/coinext-py/Cargo.toml --out /wheels

# --- stage 2: python runtime with uv + uvicorn ---
FROM python:3.13-slim AS runtime
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app

COPY pyproject.toml README.md ./
COPY foundation ./foundation
COPY market-data ./market-data
COPY strategy-research ./strategy-research
COPY backtesting-simulation ./backtesting-simulation
COPY analytics-optimization ./analytics-optimization
COPY risk-portfolio ./risk-portfolio
COPY execution-live ./execution-live
COPY operations-interface ./operations-interface
# The FastAPI app lives in operations-interface/api/service/api/app.py (exposes `app`); it imports
# the coinext_* packages lazily. Copy it in and put it on the import path.
COPY operations-interface/api/service/api ./api

# API extras (fastapi + uvicorn) + bus (Envelope decode) + config + obs.
RUN uv pip install --system --no-cache       "fastapi>=0.110" "starlette>=1.3.1" "uvicorn>=0.29"       "redis>=5" "msgpack>=1.2.1"       "structlog>=24" "prometheus-client>=0.20" "opentelemetry-sdk>=1.25"       "pydantic>=2.7" "pyyaml>=6" "numpy>=2.0"

# Install the compiled Rust core (coinext_py) built in stage 1.
COPY --from=rust-builder /wheels/*.whl /tmp/wheels/
RUN uv pip install --system --no-cache /tmp/wheels/*.whl && rm -rf /tmp/wheels

ENV PYTHONPATH=/app/foundation/python-contracts/python:/app/foundation/runtime-config/python:/app/market-data/data-lake/python:/app/strategy-research/strategy-api/python:/app/strategy-research/indicators/python:/app/backtesting-simulation/kernel/python:/app/backtesting-simulation/runner/python:/app/backtesting-simulation/parity-gates/python:/app/analytics-optimization/analytics/python:/app/analytics-optimization/screening/python:/app/analytics-optimization/optimizer/python:/app/analytics-optimization/derivatives/python:/app/risk-portfolio/risk-facade/python:/app/risk-portfolio/portfolio-facade/python:/app/execution-live/live-runtime/python:/app/operations-interface/bus/python:/app/operations-interface/cli/python:/app/api
ENV PYTHONUNBUFFERED=1

EXPOSE 8000
# operations-interface/api/service/api/app.py exposes `app` (FastAPI) with /health, /runs,
# /positions, /backtest, /control/killswitch, /control/events, and /ws/live for the UI.
# Prod runs a single uvicorn worker (the control plane is light); scale horizontally if needed.
ENTRYPOINT ["uvicorn", "app:app", "--app-dir", "/app/api", "--host", "0.0.0.0", "--port", "8000"]
