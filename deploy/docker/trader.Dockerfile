# syntax=docker/dockerfile:1
# ----------------------------------------------------------------------------------------------
# trader (Python qv_live) — the live TradingNode.
#
# Runs the SAME engines as backtest; the Kernel injects a LiveClock + Binance Data/Exec clients
# (ARCHITECTURE.md §1/§7). The decision core is the compiled `qv_py` extension (PyO3); the strategy
# handlers dispatch through it. SLO histogram: strategy_dispatch_ns. Metrics on :9103.
#
# Multi-stage:
#   1) rust builder  — compile the qv_py extension with maturin (the Kernel + domain mirror).
#   2) python runtime — python:3.13-slim, deps installed with `uv`, qv_py wheel dropped in.
# ----------------------------------------------------------------------------------------------

# --- stage 1: build the qv_py PyO3 wheel (crates/qv-py, --features python) ---
FROM rust:1.95-slim AS rust-builder
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-pip python3-venv build-essential \
 && rm -rf /var/lib/apt/lists/*
# Install maturin to produce a wheel for qv_py.
RUN pip install --break-system-packages "maturin>=1.7,<2"
WORKDIR /src
COPY . .
# `module-name = qv_py`, `features = ["python"]` are set in crates/qv-py/pyproject.toml.
RUN maturin build --release --manifest-path crates/qv-py/Cargo.toml --out /wheels

# --- stage 2: python runtime with uv ---
FROM python:3.13-slim AS runtime
# uv: fast, reproducible dependency installs (ARCHITECTURE.md toolchain).
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app

# Project metadata first for layer caching of the dependency install.
COPY pyproject.toml README.md ./
COPY python ./python
COPY config ./config

# Install the control-plane deps needed by the live runtime (bus + live + obs extras).
# `--system` installs into the image interpreter (no venv indirection in containers).
RUN uv pip install --system --no-cache \
      "redis>=5" "msgpack>=1" "anyio>=4" \
      "structlog>=24" "prometheus-client>=0.20" "opentelemetry-sdk>=1.25" \
      "pydantic>=2.7" "pyyaml>=6" "typer>=0.12" "numpy>=2.0"

# Drop in the compiled Rust core (qv_py) built in stage 1.
COPY --from=rust-builder /wheels/*.whl /tmp/wheels/
RUN uv pip install --system --no-cache /tmp/wheels/*.whl && rm -rf /tmp/wheels

# Make the source packages importable (qv_live, qv_strategy, qv_kernel, qv_bus, ...).
ENV PYTHONPATH=/app/python
ENV PYTHONUNBUFFERED=1

EXPOSE 9103
# TODO(venue/IO): qv_live wires the Binance clients behind the ExecutionClient/DataClient ports.
# Launches the live node via the `qv` CLI (entrypoint declared in pyproject [project.scripts]).
ENTRYPOINT ["qv"]
CMD ["live", "run", "--config", "/app/config/live.yaml"]
