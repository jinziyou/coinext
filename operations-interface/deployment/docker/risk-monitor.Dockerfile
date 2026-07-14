# syntax=docker/dockerfile:1
# risk-monitor — out-of-band PnL/position watcher + global kill-switch. Metrics :9104.
# Status: scaffold. Pure-Python (no coinext_py).

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
COPY risk-portfolio/services/risk-monitor ./risk_monitor

RUN uv pip install --system --no-cache \
      "redis>=5" "msgpack>=1.2.1" \
      "structlog>=24" "prometheus-client>=0.20" "opentelemetry-sdk>=1.25" \
      "pydantic>=2.7" "pyyaml>=6" "numpy>=2.0"

ENV PYTHONPATH=/app/foundation/python:/app/market-data/python:/app/strategy-research/python:/app/backtesting-simulation/python:/app/analytics-optimization/python:/app/risk-portfolio/python:/app/execution-live/python:/app/operations-interface/python:/app/risk_monitor
ENV PYTHONUNBUFFERED=1

EXPOSE 9104
ENTRYPOINT ["python", "-m", "main"]
