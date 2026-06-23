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
COPY python ./python
COPY config ./config
# The out-of-band supervisor lives in services/risk-monitor/main.py (its own RiskSupervisor; it does
# NOT import the coinext_risk protections package). Copy it in and put it on the import path.
COPY services/risk-monitor ./risk_monitor

# Bus (Envelope decode) + config + obs extras. No Rust extension required.
RUN uv pip install --system --no-cache \
      "redis>=5" "msgpack>=1" \
      "structlog>=24" "prometheus-client>=0.20" "opentelemetry-sdk>=1.25" \
      "pydantic>=2.7" "pyyaml>=6" "numpy>=2.0"

ENV PYTHONPATH=/app/python:/app/risk_monitor
ENV PYTHONUNBUFFERED=1

EXPOSE 9104
# services/risk-monitor/main.py runs the out-of-band watch loop (RiskSupervisor) and trips the global
# kill-switch on a breach; main() reads COINEXT__RISK__* / COINEXT__REDIS__URL env.
ENTRYPOINT ["python", "-m", "main"]
