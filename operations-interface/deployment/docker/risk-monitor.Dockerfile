# syntax=docker/dockerfile:1
# risk-monitor — out-of-band PnL/position watcher + kill-switch. Metrics :9104.
# Status: scaffold. Pure-Python (no coinext_py).

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
COPY operations-interface/deployment/docker/pythonpath.env /etc/coinext/pythonpath.env
COPY operations-interface/deployment/docker/entrypoint-python.sh /entrypoint-python.sh
RUN chmod +x /entrypoint-python.sh

RUN uv pip install --system --no-cache \
      "redis>=5" "msgpack>=1.2.1" \
      "structlog>=24" "prometheus-client>=0.20" "opentelemetry-sdk>=1.25" \
      "pydantic>=2.7" "pyyaml>=6" "numpy>=2.0"

ENV PYTHONUNBUFFERED=1
ENV COINEXT_SERVICE_PYTHONPATH=/app/risk_monitor

EXPOSE 9104
ENTRYPOINT ["/entrypoint-python.sh"]
CMD ["python", "-m", "main"]
