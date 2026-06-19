# Coinext

A multi-asset, venue-agnostic quantitative **research & execution** platform — **Rust hot path +
Python control plane**, with **backtest↔live parity** as the one core invariant.

The hot path (market-data ingestion + order execution) is **Rust 1.95 on Tokio**; the control plane
(strategy authoring, research, analytics, ops) is **Python 3.13** (managed by uv). They are bridged
**only** by PyO3/maturin.

The whole design turns on one invariant — **backtest↔live parity**:

> ONE Strategy API, ONE set of engines (Data / Execution / Risk / Portfolio), ONE deterministic
> synchronous core loop. Only the **Kernel** swaps three things between Backtest / Sandbox / Live:
> the **Clock** (`HistoricalClock` vs live), the **Cache** contents, and the **Data/Execution
> clients** behind byte-identical ports. Every design conflict is tie-broken in favor of parity.

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the full design and rationale.

## Status

This is a **scaffold**: the full monorepo is laid out, the shared contracts are defined once in
Rust and mirrored to Python, and a vertical slice runs **end-to-end in pure Rust** today.

| Layer | Crate / package | State |
|---|---|---|
| Value types (fixed-precision, no `f64` in domain) | `coinext-core` | ✅ implemented + tested |
| Domain hub (typed IDs, Instrument, event-sourced Order FSM, Fill, Position, market data) | `coinext-model` | ✅ implemented + tested |
| Hexagonal ports (Data/Exec/Strategy/Risk/Portfolio/Bus traits + value types) | `coinext-ports` | ✅ implemented |
| In-memory store | `coinext-cache` | ✅ implemented |
| In-process bus (zero-serialization hot path) | `coinext-bus` | ✅ implemented + tested |
| Streaming indicators (SMA/EMA/RSI/ATR/MACD/Bollinger/VWAP), bridged to Python | `coinext-indicators`, `coinext-py` | ✅ implemented + tested |
| Option pricing (Black-Scholes price/greeks/IV) | `coinext-derivatives` | ✅ implemented + tested |
| Pre-trade risk gate + kill-switch (margin/leverage aware) | `coinext-risk-engine` | ✅ implemented |
| Portfolio analytics (PnL, exposure, linear/inverse perps) | `coinext-portfolio` | ✅ implemented |
| Data + execution engines (OMS, FSM driver, report folding) | `coinext-data-engine`, `coinext-exec-engine` | ✅ implemented |
| **Simulated exchange** (BrokerageModel: OHLC limit matching, volume-participation partial fills, queue position, stop orders, range-scaled slippage; DelayedEventQueue) | `coinext-sim` | ✅ implemented + tested |
| **Backtest kernel** (deterministic synchronous core loop; expiry settlement; liquidation) | `coinext-kernel` | ✅ implemented + tested |
| Runnable SMA-crossover backtest | `examples/backtest-sma` | ✅ runs |
| PyO3 bridge (Python `Strategy` → same Rust kernel; OHLC + multi-instrument; parity proof) | `coinext-py` | ✅ implemented + tested |
| Research control plane (backtest, data lake, parity gate, vectorized screen) | `python/coinext_{backtest,data,parity,screen}` | ✅ implemented + tested |
| Analytics (trade stats, bias screens, tear sheet + plots) | `python/coinext_analytics` | ✅ implemented + tested |
| Walk-forward optimization (rolling/anchored, OOS degradation, grid/Optuna) | `python/coinext_optimize` | ✅ implemented + tested |
| Binance adapter, network, persistence, ingest/exec services | `coinext-adapters/*`, `coinext-network`, … | 🚧 interface stubs |
| FastAPI control plane + React dashboard + docker-compose + observability | `services/*`, `deploy/*` | 🚧 scaffolded |

## Quick start (Rust core)

```bash
# Run the unit + property tests across the core workspace
cargo test          # or: just test

# Run the example SMA-crossover backtest end-to-end
cargo run -p coinext-example-backtest   # or: just backtest
```

Expected output is a tear-sheet-style summary (orders, fills, equity, return, Sharpe, max drawdown)
produced by running a native-Rust `Strategy` through the `SimulatedExecutionClient` with realistic
fees, slippage, and **delayed fills interleaved on the time-frontier** — the same `Strategy` trait a
Python strategy implements and the same path that runs live.

## Quick start (research: real data, reproducible backtests)

The Python control plane downloads REAL Binance history (public REST, no API key) into a local
**Parquet data lake**, then backtests over the SAME bytes repeatedly — reproducible research.

```bash
# One-time: create the venv, then build the Rust core into it
just py-setup     # uv sync --extra research --group dev
just py-build     # maturin develop (compiles crates/coinext-py)

# Download + backtest from the lake
uv run coinext download --symbols BTCUSDT,ETHUSDT --interval 1m --days 30   # paginated -> Parquet lake
uv run coinext catalog                                                      # coverage (rows + UTC span)
uv run coinext backtest --from-lake --symbol BTCUSDT                        # reproducible SMA backtest
uv run coinext optimize --from-lake --mode anchored                         # walk-forward, OOS degradation
```

The `backtest` tear sheet reports trade-level stats (win rate, profit factor, exposure, turnover)
and runs the look-ahead / overfitting **bias screens** inline. `optimize` does a genuine
walk-forward — params are chosen IN-SAMPLE per fold and re-scored OUT-of-sample, so its headline is
the **OOS degradation** that guards against overfitting (grid search by default; `--optuna` for TPE).

```bash
uv run coinext backtest --strategy limit-maker          # rests LIMIT orders -> OHLC-aware (high/low) fills
uv run coinext backtest-multi --symbols BTCUSDT,ETHUSDT,SOLUSDT   # a portfolio through ONE kernel
uv run coinext screen --from-lake --symbol BTCUSDT      # fast vectorized sweep + advisory cross-check
```

`--strategy limit-maker` posts resting limit orders that fill on a bar's **intrabar high/low**, not
just its close — the bridge passes full OHLC to the Rust sim. `backtest-multi` runs a per-symbol SMA
portfolio across many instruments in a single deterministic kernel; positions stay isolated and a
portfolio run is exactly the union of the per-symbol standalone runs. `screen` ranks a parameter
grid in milliseconds with a **vectorized** (numpy) backtest — fast but NON-authoritative (no
fees/slippage/latency/queue) — then runs `coinext_parity.cross_check` to warn if the best params
**drift** from the event-driven runner; narrow the space with `screen`, then confirm survivors with
the parity-valid `coinext backtest`.

The end-to-end research loop (screen → optimize → backtest → indicators → portfolio → ticks) is a
single runnable script: `uv run python notebooks/research_loop.py` (synthetic by default; flip
`USE_LAKE = True` to run over the real lake — see [`notebooks/README.md`](notebooks/README.md)).

The downloader pages past Binance's 1000-bar request limit; the lake is partitioned
(`bars/venue=…/symbol=…/interval=…/{YYYYMM}.parquet`) and deduped/idempotent, so re-downloads only
extend coverage. `HistoryReader` reads it back for the backtest — and, in live, for indicator
warm-up — the ONE history path that keeps indicators identical across backtest and live.

## Repository layout

```
crates/      Rust: hot path + shared domain (the source of truth)
python/      Python control plane: research, strategy authoring, analytics, ops
services/    Deployable service wrappers (ingestor, trader, risk-monitor, api, ui)
deploy/      Dockerfiles + observability config (prometheus/grafana/loki/tempo/otel)
config/      Layered config (base + backtest/sandbox/live)
examples/    Runnable example strategies
notebooks/   Research notebooks (py:percent scripts)
tests/       Parity + regression suites
docs/        Roadmap + testnet runbook
```

## Documentation map

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — the canonical design: domain model, hexagonal ports, the
  deterministic core loop, invariants, deployment, tradeoffs.
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — what is done/verified, what is next (research), what is
  deferred (live/ops), and open questions.
- [`docs/TESTNET.md`](docs/TESTNET.md) — the end-to-end testnet runbook + the parity promotion gate.
- [`tests/parity/README.md`](tests/parity/README.md) — the two parity checks (advisory cross-check +
  mandatory sandbox-vs-backtest gate).
- [`deploy/README.md`](deploy/README.md) / [`services/README.md`](services/README.md) — the
  dockerized multi-service stack and observability overlay.

## Toolchain

Rust 1.95 (stable), Python 3.13 (uv), Node 22 (dashboard), Docker. See
[`ARCHITECTURE.md`](ARCHITECTURE.md) and the [`justfile`](justfile) for tasks.

## License

MIT.
