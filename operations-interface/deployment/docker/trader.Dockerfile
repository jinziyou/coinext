# syntax=docker/dockerfile:1
# trader — per-account live TradingNode. Metrics :9103. Status: scaffold.
# Workspace packages via `uv sync`; service app on COINEXT_SERVICE_PYTHONPATH.

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

COPY pyproject.toml README.md uv.lock ./
COPY foundation ./foundation
COPY market-data ./market-data
COPY strategy-research ./strategy-research
COPY backtesting-simulation ./backtesting-simulation
COPY analytics-optimization ./analytics-optimization
COPY risk-portfolio ./risk-portfolio
COPY execution-live ./execution-live
COPY operations-interface ./operations-interface
COPY execution-live/services/trader ./trader
COPY operations-interface/deployment/docker/entrypoint-python.sh /entrypoint-python.sh
RUN chmod +x /entrypoint-python.sh

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    COINEXT_SERVICE_PYTHONPATH=/app/trader

RUN uv venv /opt/venv \
 && uv sync --frozen --no-dev --extra bus --extra live --extra obs --extra cli

COPY --from=rust-builder /wheels/*.whl /tmp/wheels/
RUN uv pip install --no-cache /tmp/wheels/*.whl && rm -rf /tmp/wheels

EXPOSE 9103
ENTRYPOINT ["/entrypoint-python.sh"]
CMD ["python", "-m", "main"]
