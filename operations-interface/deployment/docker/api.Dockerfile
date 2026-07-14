# syntax=docker/dockerfile:1
# api — FastAPI control plane (:8000). Embeds coinext_py for domain types.
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
COPY operations-interface/deployment/docker/pythonpath.env /etc/coinext/pythonpath.env
COPY operations-interface/deployment/docker/entrypoint-python.sh /entrypoint-python.sh
RUN chmod +x /entrypoint-python.sh

RUN uv pip install --system --no-cache \
      "fastapi>=0.110" "starlette>=1.3.1" "uvicorn>=0.29" \
      "redis>=5" "msgpack>=1.2.1" \
      "structlog>=24" "prometheus-client>=0.20" "opentelemetry-sdk>=1.25" \
      "pydantic>=2.7" "pyyaml>=6" "numpy>=2.0"

COPY --from=rust-builder /wheels/*.whl /tmp/wheels/
RUN uv pip install --system --no-cache /tmp/wheels/*.whl && rm -rf /tmp/wheels

ENV PYTHONUNBUFFERED=1
ENV COINEXT_SERVICE_PYTHONPATH=/app/api

EXPOSE 8000
ENTRYPOINT ["/entrypoint-python.sh"]
CMD ["uvicorn", "app:app", "--app-dir", "/app/api", "--host", "0.0.0.0", "--port", "8000"]
