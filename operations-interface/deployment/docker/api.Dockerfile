# syntax=docker/dockerfile:1
# api — FastAPI control plane (:8000). Embeds coinext_py. Status: scaffold.
# Pure-Python packages installed via `uv sync` (workspace members); only the service app uses
# COINEXT_SERVICE_PYTHONPATH.

FROM rust:1.97-bookworm AS rust-builder
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
COPY operations-interface/services/api ./api
COPY operations-interface/deployment/docker/entrypoint-python.sh /entrypoint-python.sh
RUN chmod +x /entrypoint-python.sh

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    COINEXT_SERVICE_PYTHONPATH=/app/api

RUN uv venv /opt/venv \
 && uv sync --frozen --no-dev --extra api --extra bus --extra obs

COPY --from=rust-builder /wheels/*.whl /tmp/wheels/
RUN uv pip install --no-cache /tmp/wheels/*.whl && rm -rf /tmp/wheels

EXPOSE 8000
ENTRYPOINT ["/entrypoint-python.sh"]
CMD ["uvicorn", "app:app", "--app-dir", "/app/api", "--host", "0.0.0.0", "--port", "8000"]
