# VeloxQuant

> 多资产、面向场所无关（venue-agnostic）的量化研究与执行平台 —— **Rust 热路径 + Python 控制平面**，
> 以「回测↔实盘一致性（backtest↔live parity）」为唯一核心不变量。

A multi-asset, venue-agnostic quantitative trading **research & execution** platform. The hot path
(market-data ingestion + order execution) is **Rust 1.95 on Tokio**; the control plane (strategy
authoring, research, analytics, ops) is **Python 3.13**. They are bridged **only** by PyO3/maturin.

The whole design turns on one invariant — **backtest↔live parity**:

> ONE Strategy API, ONE set of engines (Data / Execution / Risk / Portfolio), ONE deterministic
> synchronous core loop. Only the **Kernel** swaps three things between Backtest / Sandbox / Live:
> the **Clock** (`HistoricalClock` vs `LiveClock`), the **Cache** contents, and the
> **Data/Execution clients** behind identical ports. Every design conflict is tie-broken in favor
> of parity.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full design and rationale.

## Status

This is a **scaffold**: the full monorepo is laid out, the shared contracts are defined once in
Rust and mirrored to Python, and a vertical slice runs **end-to-end in pure Rust** today.

| Layer | Crate / package | State |
|---|---|---|
| Value types (fixed-precision, no `f64` in domain) | `qv-core` | ✅ implemented + tested |
| Domain hub (typed IDs, Instrument, event-sourced Order FSM, Fill, Position, market data) | `qv-model` | ✅ implemented + tested |
| Hexagonal ports (Data/Exec/Strategy/Risk/Portfolio/Bus traits + value types) | `qv-ports` | ✅ implemented |
| In-memory store | `qv-cache` | ✅ implemented |
| In-process bus (zero-serialization hot path) | `qv-bus` | ✅ implemented + tested |
| Streaming indicators (SMA/EMA/RSI/ATR) | `qv-indicators` | ✅ implemented + tested |
| Pre-trade risk gate + kill-switch | `qv-risk-engine` | ✅ implemented |
| Portfolio analytics (PnL, exposure, linear/inverse perps) | `qv-portfolio` | ✅ implemented |
| Data + execution engines (OMS, FSM driver, report folding) | `qv-data-engine`, `qv-exec-engine` | ✅ implemented |
| **Simulated exchange** (BrokerageModel + DelayedEventQueue + matching) | `qv-sim` | ✅ implemented + tested |
| **Backtest kernel** (deterministic synchronous core loop) | `qv-kernel` | ✅ implemented + tested |
| Runnable SMA-crossover backtest | `examples/backtest-sma` | ✅ runs |
| PyO3 bindings + Python control plane | `qv-py`, `python/*` | 🚧 scaffolded |
| Binance adapter, network, persistence, ingest/exec services | `qv-adapters/*`, `qv-network`, … | 🚧 interface stubs |
| FastAPI control plane + React dashboard + docker-compose + observability | `services/*`, `deploy/*` | 🚧 scaffolded |

## Quick start (Rust core)

```bash
# Run the unit + property tests across the core workspace
cargo test

# Run the example SMA-crossover backtest end-to-end
cargo run -p qv-example-backtest
```

Expected output is a tear-sheet-style summary (orders, fills, equity, return, Sharpe, max drawdown)
produced by running a native-Rust `Strategy` through the SimulatedExecutionClient with realistic
fees, slippage, and **delayed fills interleaved on the time-frontier** — the same `Strategy` trait a
Python strategy implements and the same path that runs live.

## Quick start (research: real data, reproducible backtests)

The Python control plane downloads REAL Binance history (public REST, no API key) into a local
**Parquet data lake**, then backtests over the SAME bytes repeatedly — reproducible research.

```bash
# Build the Rust core into the venv (once), then download + backtest from the lake
just py-build
uv run qv download --symbols BTCUSDT,ETHUSDT --interval 1m --days 30   # paginated -> Parquet lake
uv run qv catalog                                                      # coverage (rows + UTC span)
uv run qv backtest --from-lake --symbol BTCUSDT                        # reproducible SMA backtest
```

The downloader pages past Binance's 1000-bar request limit; the lake is partitioned
(`bars/venue=…/symbol=…/interval=…/{YYYYMM}.parquet`) and deduped/idempotent, so re-downloads only
extend coverage. `HistoryReader` reads it back for the backtest (and, in live, for indicator
warm-up — the ONE history path that keeps indicators identical across backtest and live).

## Repository layout

```
crates/      Rust: hot path + shared domain (the source of truth)
python/      Python control plane: research, strategy authoring, analytics, ops
services/    Deployable service wrappers (ingestor, trader, risk-monitor, api, ui)
deploy/      Dockerfiles + observability config (prometheus/grafana/loki/tempo/otel)
config/      Layered config (base + backtest/sandbox/live)
examples/    Runnable example strategies
notebooks/   Research notebooks
tests/       Parity + regression suites
```

## Toolchain

Rust 1.95 (stable), Python 3.13 (uv), Node 22 (dashboard), Docker. See
[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) and the `justfile` for tasks.

## License

MIT.
