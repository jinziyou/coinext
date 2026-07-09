# syntax=docker/dockerfile:1
# ----------------------------------------------------------------------------------------------
# risk-monitor (Python) — out-of-band risk watcher.
#
# Independent of the trader process by design: tails all PnL/positions off the Redis bus and the
# Postgres event store, and can trip the GLOBAL kill-switch (ARCHITECTURE.md §7). SLO-relevant
# signal: risk_denials. Metrics on :9104.
#
# Pure-Python service — it does NOT need the compiled coinext_py Kernel (no strategy dispatch here); it
# only decodes the MessagePack Envelope via coinext_bus and reads positions/PnL. Single-stage slim image.
# ----------------------------------------------------------------------------------------------

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
# The out-of-band supervisor lives in risk-portfolio/risk-monitor/service/risk-monitor/main.py (its
# own RiskSupervisor; it does NOT import the coinext_risk protections package). Copy it in and put it
# on the import path.
COPY risk-portfolio/risk-monitor/service/risk-monitor ./risk_monitor

# Bus (Envelope decode) + config + obs extras. No Rust extension required.
RUN uv pip install --system --no-cache       "redis>=5" "msgpack>=1.2.1"       "structlog>=24" "prometheus-client>=0.20" "opentelemetry-sdk>=1.25"       "pydantic>=2.7" "pyyaml>=6" "numpy>=2.0"

ENV PYTHONPATH=/app/foundation/python-contracts/python:/app/foundation/runtime-config/python:/app/market-data/data-lake/python:/app/strategy-research/strategy-api/python:/app/strategy-research/indicators/python:/app/backtesting-simulation/kernel/python:/app/backtesting-simulation/runner/python:/app/backtesting-simulation/parity-gates/python:/app/analytics-optimization/analytics/python:/app/analytics-optimization/screening/python:/app/analytics-optimization/optimizer/python:/app/analytics-optimization/derivatives/python:/app/risk-portfolio/risk-facade/python:/app/risk-portfolio/portfolio-facade/python:/app/execution-live/live-runtime/python:/app/operations-interface/bus/python:/app/operations-interface/cli/python:/app/risk_monitor
ENV PYTHONUNBUFFERED=1

EXPOSE 9104
# risk-portfolio/risk-monitor/service/risk-monitor/main.py runs the out-of-band watch loop
# (RiskSupervisor) and trips the global kill-switch on a breach; main() reads COINEXT__RISK__* /
# COINEXT__REDIS__URL env.
ENTRYPOINT ["python", "-m", "main"]
