# syntax=docker/dockerfile:1
# risk-monitor — out-of-band watcher + kill-switch. Metrics :9104. Status: scaffold.
# Pure-Python; workspace packages via `uv sync` (no coinext_py).

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
COPY risk-portfolio/services/risk-monitor ./risk_monitor
COPY operations-interface/deployment/docker/entrypoint-python.sh /entrypoint-python.sh
RUN chmod +x /entrypoint-python.sh

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    COINEXT_SERVICE_PYTHONPATH=/app/risk_monitor

RUN uv venv /opt/venv \
 && uv sync --frozen --no-dev --extra bus --extra obs

EXPOSE 9104
ENTRYPOINT ["/entrypoint-python.sh"]
CMD ["python", "-m", "main"]
