# Coinext Architecture

This is the canonical architecture document for Coinext. The current source tree is organized as root-level lifecycle modules (`foundation/`, `market-data/`, `strategy-research/`, `backtesting-simulation/`, `analytics-optimization/`, `risk-portfolio/`, `execution-live/`, `operations-interface/`) plus root control files (`Cargo.toml`, `pyproject.toml`, `docker-compose*.yml`, `tests/`, `docs/`, `data/`). There is no `modules/` wrapper directory.

## 1. Overview

Coinext is a multi-asset, venue-agnostic quant platform covering data ingestion, strategy research, backtesting, analytics/optimization, risk/portfolio controls, live execution, and operations UI/API. The hot path is **Rust 1.95 on Tokio**; the control plane is **Python 3.13**. Rust and Python are bridged only by PyO3/maturin at `foundation/ffi-bridge/rust/coinext-py`.

The core invariant is **backtest↔live parity**:

> ONE Strategy API, ONE set of Data / Execution / Risk / Portfolio engines, ONE deterministic synchronous core loop. Backtest / Sandbox / Live swap only Clock, Cache contents, and Data/Execution clients behind byte-identical ports.

Status: the deterministic backtest core and Python bridge are tested; the `LiveKernel`, `ingestor`, `exec-svc`, service wrappers, and UI are scaffold/stub paths; true venue end-to-end parity has not yet been measured.

A vectorized research screen exists for fast sweeps, but it is advisory only. Promotion decisions must use the event-driven runner and parity gates.

## 2. Root lifecycle modules

| Module | Boundary |
|---|---|
| `foundation/` | Value types, domain model, hexagonal ports, cache, Python contracts, runtime config, PyO3 bridge, testkit. |
| `market-data/` | Data engine, local data lake, REST/WS transport, venue adapters, ingestion daemon wrapper. |
| `strategy-research/` | Python strategy API, indicators, research notebooks. |
| `backtesting-simulation/` | Kernel, simulated exchange, authoritative Python runner, parity gates, Rust examples. |
| `analytics-optimization/` | Tear sheets, bias screens, vectorized screen, walk-forward optimizer, derivatives pricing. |
| `risk-portfolio/` | Pre-trade risk, margin/liquidation, portfolio PnL/exposure, risk monitor wrapper. |
| `execution-live/` | OMS/execution engine, live runtime, trader wrapper, execution service daemon. |
| `operations-interface/` | In-proc/Redis bus, CLI, FastAPI, UI, deployment assets, persistence. |

## 3. Domain model

The domain is Rust-first and mirrored to Python through `coinext_py` with the same integer representation.

- `coinext-core` (`foundation/primitives/rust/coinext-core`) owns fixed-precision `Price`, `Quantity`, `Money`, `Currency`, and `UnixNanos`. Domain numerics do not use `f64`; float conversion is display-only.
- `coinext-model` (`foundation/domain-model/rust/coinext-model`) owns typed IDs, `Instrument`, event-sourced `Order` FSM, `Fill`, `Position`, account, and market-data events.
- `coinext-ports` (`foundation/ports/rust/coinext-ports`) owns the hexagonal port traits: `DataClient`, `ExecutionClient`, `InstrumentProvider`, `RiskEngine`, `Portfolio`, `Strategy`, and `MessageBus`.

`ClientOrderId` is assigned once by the order factory, stays stable before submit, and makes retries idempotent. Orders and positions are folds of immutable event sequences, which gives an audit trail and deterministic replay.

## 4. Components and boundaries

Engines sit above the port traits and are wired identically in every environment:

- Data engine: `market-data/data-engine/rust/coinext-data-engine`.
- Execution engine / OMS: `execution-live/execution-engine/rust/coinext-exec-engine`.
- Risk engine: `risk-portfolio/risk-engine/rust/coinext-risk-engine`.
- Portfolio engine: `risk-portfolio/portfolio-engine/rust/coinext-portfolio`.
- State cache: `foundation/state-cache/rust/coinext-cache`.
- In-process bus and Redis Envelope contract: `operations-interface/bus/rust/coinext-bus` plus `operations-interface/bus/python`.
- Simulated exchange: `backtesting-simulation/simulated-exchange/rust/coinext-sim`.
- Kernel: `backtesting-simulation/kernel/rust/coinext-kernel` and Python wrapper under `backtesting-simulation/kernel/python`.
- Venue adapter and transport: `market-data/venue-adapters/binance/rust/coinext-adapters-binance` and `market-data/network-transport/rust/coinext-network`.
- Persistence: `operations-interface/persistence/rust/coinext-persistence`.

Python packages keep their import names (`coinext_backtest`, `coinext_strategy`, `coinext_data`, `coinext_analytics`, `coinext_optimize`, `coinext_live`, `coinext_cli`, etc.) while their parent directories live under lifecycle modules' `python` directories. `pyproject.toml` is the single source for pytest discovery.

The PyO3 bridge is the only in-process binding. A Python `Strategy` subclass is bridged by `PyStrategyAdapter`, which implements the synchronous Rust `Strategy` trait and calls Python handlers under the GIL. Native Rust strategies remain the low-latency/no-GIL path.

Cross-service fan-out uses a Redis Streams Envelope (`schema_version`, `msg_type`, `trace_id`, `ts_init`, `payload`) through `coinext_bus`. The in-process hot path uses typed `Arc` payloads with no serialization.

## 5. Data flow

Backtest is a deterministic single-threaded synchronous event loop. Async I/O stays outside the decision core; live/sandbox drivers hand market and execution events to the core through Tokio channels.

```text
BACKTEST                                           SANDBOX / LIVE
HistoricalClock                                    SystemClock / LiveClock
HistoryReader feed                                 venue WS + local HistoryReader warm-up
SimulatedExecutionClient                           testnet/live ExecutionClient
        │                                                   │
        └────────────── same deterministic Kernel loop ◄────┘
                          1. drain due execution reports
                          2. fire timers
                          3. process market event
                          4. Strategy handler submits orders
                          5. RiskEngine gates orders
                          6. ExecutionClient accepts/fills/cancels
                          7. settle dated contracts
                          8. mark-to-market maintenance / liquidation
```

Only Clock, Cache, and clients differ by environment. Historical warm-up always comes from the local `HistoryReader`, not ad-hoc live REST calls inside strategy handlers.

## 6. Key invariants

1. Backtest↔live parity wins every design conflict.
2. Domain prices, quantities, and money are integer-backed.
3. `ExecutionClient` is the only backtest-vs-live order-flow seam.
4. The local HistoryReader is the single historical warm-up path.
5. Order and position state is event-sourced and replayable.
6. Strategy handlers are synchronous; async work remains at the edges.
7. The vectorized screen is non-authoritative.

## 7. Deployment forms

Root `docker-compose.yml` stays the operator entrypoint. Dockerfiles and observability assets live under `operations-interface/deployment/`.

| Service | Functional module | Build asset | Port(s) | Status |
|---|---|---|---|---|
| `ingestor` | `market-data/ingestion-service` | `operations-interface/deployment/docker/ingestor.Dockerfile` | metrics `9101` | stub daemon |
| `exec-svc` | `execution-live/execution-service` | `operations-interface/deployment/docker/exec-svc.Dockerfile` | metrics `9102`, ctl `8081` | stub daemon |
| `trader` | `execution-live/trader-service` | `operations-interface/deployment/docker/trader.Dockerfile` | metrics `9103` | scaffold wrapper |
| `risk-monitor` | `risk-portfolio/risk-monitor` | `operations-interface/deployment/docker/risk-monitor.Dockerfile` | metrics `9104` | scaffold supervisor |
| `api` | `operations-interface/api` | `operations-interface/deployment/docker/api.Dockerfile` | `8000` | scaffold FastAPI |
| `ui` | `operations-interface/ui` | `operations-interface/deployment/docker/ui.Dockerfile` | host `3000` | scaffold dashboard |

Backing services: Postgres for event/audit state, Redis for cross-process Envelope streams, and MinIO/S3 for the data lake. Observability overlay (`docker-compose.obs.yml`) mounts configs from `operations-interface/deployment` and provides OpenTelemetry Collector, Prometheus, Grafana, Loki, and Tempo.

## 8. Doc map

Full index: [`docs/README.md`](docs/README.md).

- [`README.md`](README.md) — platform positioning, status, quick starts, root module layout.
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — status snapshot, verified work, next research, deferred live/ops.
- [`docs/TESTNET.md`](docs/TESTNET.md) — Binance public data + spot testnet runbook.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — build order + open questions (stub → this doc).
- [`tests/backtesting-simulation/parity/README.md`](tests/backtesting-simulation/parity/README.md) — advisory cross-check and sandbox gate notes.
- [`operations-interface/deployment/README.md`](operations-interface/deployment/README.md) — compose/deployment/observability.
- [`operations-interface/deployment/services.md`](operations-interface/deployment/services.md) — service index.
- [`strategy-research/research-notebooks/notebooks/README.md`](strategy-research/research-notebooks/notebooks/README.md) — research scripts.
- [`data/sample/README.md`](data/sample/README.md) — sample data retained at the repo root runtime data-lake mount.
