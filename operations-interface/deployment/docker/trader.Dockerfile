# syntax=docker/dockerfile:1
# trader — live TradingNode wrapper (one process per account). Metrics :9103.
# Status: scaffold. Multi-stage: maturin wheel → python:3.13-slim + uv.

FROM rust:1.95-bookworm AS rust-builder
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-pip python3-venv build-essential \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --break-system-packages "maturin>=1.7,<2"
WORKDIR /src
COPY . .
RUN maturin build --release --manifest-path foundation/crates/coinext-py/Cargo.toml \
      --features python --out /wheels

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
COPY execution-live/services/trader ./trader

RUN uv pip install --system --no-cache \
      "redis>=5" "msgpack>=1.2.1" "anyio>=4" \
      "structlog>=24" "prometheus-client>=0.20" "opentelemetry-sdk>=1.25" \
      "pydantic>=2.7" "pyyaml>=6" "typer>=0.12" "numpy>=2.0"

COPY --from=rust-builder /wheels/*.whl /tmp/wheels/
RUN uv pip install --system --no-cache /tmp/wheels/*.whl && rm -rf /tmp/wheels

ENV PYTHONPATH=/app/foundation/python:/app/market-data/python:/app/strategy-research/python:/app/backtesting-simulation/python:/app/analytics-optimization/python:/app/risk-portfolio/python:/app/execution-live/python:/app/operations-interface/python:/app/trader
ENV PYTHONUNBUFFERED=1

EXPOSE 9103
ENTRYPOINT ["python", "-m", "main"]
