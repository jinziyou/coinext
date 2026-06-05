# syntax=docker/dockerfile:1
# ----------------------------------------------------------------------------------------------
# api (Python FastAPI) — control-plane REST/WS for the UI.
#
# Exposes run management, positions/PnL, and a live event stream (decoded from the Redis Envelope)
# to the dashboard. Embeds the compiled `qv_py` extension so it can speak the integer-precision
# domain types (Price/Quantity/Money) without re-deriving them in Python (ARCHITECTURE.md §4/§6).
# Serves on :8000.
#
# Multi-stage:
#   1) rust builder  — maturin builds the qv_py wheel from crates/qv-py (--features python).
#   2) python runtime — python:3.13-slim, uv installs FastAPI/uvicorn + the wheel.
# ----------------------------------------------------------------------------------------------

# --- stage 1: build the qv_py PyO3 wheel ---
FROM rust:1.95-slim AS rust-builder
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-pip python3-venv build-essential \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --break-system-packages "maturin>=1.7,<2"
WORKDIR /src
COPY . .
RUN maturin build --release --manifest-path crates/qv-py/Cargo.toml --out /wheels

# --- stage 2: python runtime with uv + uvicorn ---
FROM python:3.13-slim AS runtime
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app

COPY pyproject.toml README.md ./
COPY python ./python
COPY config ./config

# API extras (fastapi + uvicorn) + bus (Envelope decode) + config + obs.
RUN uv pip install --system --no-cache \
      "fastapi>=0.110" "uvicorn>=0.29" \
      "redis>=5" "msgpack>=1" \
      "structlog>=24" "prometheus-client>=0.20" "opentelemetry-sdk>=1.25" \
      "pydantic>=2.7" "pyyaml>=6" "numpy>=2.0"

# Install the compiled Rust core (qv_py) built in stage 1.
COPY --from=rust-builder /wheels/*.whl /tmp/wheels/
RUN uv pip install --system --no-cache /tmp/wheels/*.whl && rm -rf /tmp/wheels

ENV PYTHONPATH=/app/python
ENV PYTHONUNBUFFERED=1

EXPOSE 8000
# TODO: qv_api.main:app exposes /healthz, /metrics, and the run/positions/PnL routes consumed by ui.
# Prod runs a single uvicorn worker (the control plane is light); scale horizontally if needed.
ENTRYPOINT ["uvicorn", "qv_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
