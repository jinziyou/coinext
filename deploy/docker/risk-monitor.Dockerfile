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

# Bus (Envelope decode) + config + obs extras. No Rust extension required.
RUN uv pip install --system --no-cache \
      "redis>=5" "msgpack>=1" \
      "structlog>=24" "prometheus-client>=0.20" "opentelemetry-sdk>=1.25" \
      "pydantic>=2.7" "pyyaml>=6" "numpy>=2.0"

ENV PYTHONPATH=/app/python
ENV PYTHONUNBUFFERED=1

EXPOSE 9104
# TODO: coinext_risk.monitor implements the out-of-band watch loop + kill-switch trip over the control API.
ENTRYPOINT ["python", "-m", "coinext_risk.monitor"]
