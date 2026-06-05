# VeloxQuant Architecture

> 本文为 VeloxQuant 平台的权威架构说明。核心不变量是 **回测↔实盘一致性（backtest↔live parity）**：
> 同一份 Strategy 代码、同一套引擎、同一个确定性核心，在回测 / 沙盒 / 实盘中原样运行；只有 Kernel 注入的
> 三样东西不同 —— 时钟（Clock）、缓存（Cache）内容、数据/执行客户端（behind identical ports）。

This document is the canonical description of the VeloxQuant platform. It was synthesized from a
study of NautilusTrader (the closest prior art — also Python+Rust), QuantConnect LEAN, Freqtrade,
Hummingbot, and Rust trading engines (barter-rs), then designed from three competing angles
(research-first, execution-first, platform-ops-first), merged, critiqued, and revised.

---

## 1. The invariant: backtest↔live parity

Everything is tie-broken in favor of one property:

> **ONE** Strategy API, **ONE** set of engines (Data / Execution / Risk / Portfolio), **ONE**
> deterministic synchronous core loop. Only the **Kernel** swaps three things between
> `Backtest` / `Sandbox` / `Live`:
>
> 1. the **Clock** — `HistoricalClock` (advances on the data time-frontier, no sleeping) vs
>    `LiveClock` (wall clock + real timers);
> 2. the **Cache** contents;
> 3. the **Data/Execution clients** — `SimulatedExecutionClient` vs `BinanceExecutionClient`,
>    behind byte-identical ports.

A separate vectorized `populate_*` research screen exists for fast sweeps but is explicitly
**non-authoritative**: it does not pass through the engines and never validates a strategy for
promotion. Only the event-driven runner is parity-valid.

## 2. The synchronous deterministic core

The hot path is **Rust 1.95 on Tokio**, but the decision core is a **single-threaded synchronous
event loop**. All async I/O (WebSocket, REST, persistence) runs on Tokio tasks and hands results to
the core over MPSC channels (the barter-rs / NautilusTrader model). The core drains events and, for
each, calls **synchronous** component handlers — including the Strategy.

Why synchronous? Because a Python `Strategy` subclass **cannot implement a Rust `#[async_trait]`**.
A synchronous `Strategy` trait plus an adapter is the only coherent way to have ONE Strategy API
invoked by a single deterministic core. See §6.

In backtest, the core merge-sorts **three event sources by timestamp**:

- incoming `MarketEvent`s (from the HistoryReader-backed data feed),
- due delayed execution reports from the sim's `DelayedEventQueue`,
- due `TimerEvent`s from the `HistoricalClock`.

This merge-sort on `ts_event` is what makes a delayed fill (scheduled at `now + latency`) interleave
correctly with subsequent market data — the concrete mechanism behind research fidelity, and a
structural guarantee against look-ahead. (Implemented in `qv-kernel::BacktestKernel::run`.)

## 3. Monorepo layout

```
veloxquant/
├── crates/                      # RUST: hot path + shared domain (source of truth)
│   ├── qv-core/                 # fixed-precision Price/Quantity/Money/Currency, UnixNanos, Clock+timers
│   ├── qv-model/                # typed IDs, Instrument, event-sourced Order FSM, Fill, Position, market data
│   ├── qv-ports/                # ALL hexagonal port traits + command/report value types
│   ├── qv-bus/                  # in-proc bus (typed Arc, zero-serialization) + Redis Envelope contract
│   ├── qv-cache/                # in-memory object store (instruments/quotes/marks/orders/positions/account)
│   ├── qv-data-engine/          # cache-then-publish market data; mark maintenance; bar aggregation
│   ├── qv-exec-engine/          # OMS: risk-gated routing, FSM driving, report folding
│   ├── qv-risk-engine/          # pre-trade risk gate + atomic kill-switch
│   ├── qv-portfolio/            # balances, realized/unrealized PnL, exposure (mark-sourced from Cache)
│   ├── qv-sim/                  # SimulatedExchange: BrokerageModel + matching + DelayedEventQueue
│   ├── qv-kernel/               # the synchronous core loop; Environment; backtest wiring
│   ├── qv-indicators/           # streaming SMA/EMA/RSI/ATR (same code warm-up + live)
│   ├── qv-testkit/              # sample instruments + synthetic data generators
│   ├── qv-adapters/binance/     # reference venue adapter (Data/Exec/InstrumentProvider) [stub]
│   ├── qv-network/              # shared WS/REST framework (rate limit, auth, retry) [stub]
│   ├── qv-persistence/          # append-only OrderEvent store + Parquet writer [stub]
│   ├── qv-ingest/               # BIN: market-data ingestion daemon [stub]
│   ├── qv-exec-svc/             # BIN: OMS/execution service [stub]
│   └── qv-py/                   # PyO3 crate: domain + Kernel + PyStrategyAdapter dispatch shim
│
├── python/                      # PYTHON: control plane
│   ├── qv_contracts/            # re-exports qv_py stubs; port Protocols; Envelope schema
│   ├── qv_strategy/             # Strategy ABC (sync handlers) + OrderFactory + example strategies
│   ├── qv_kernel/               # thin wrapper over qv_py: build/run Kernel
│   ├── qv_backtest/             # BacktestNode (authoritative) + vectorized screen (advisory)
│   ├── qv_data/                 # DataLake catalog + HistoryReader [stub]
│   ├── qv_analytics/            # returns/Sharpe/drawdown, tear sheets, bias detectors
│   ├── qv_optimize/             # Optuna walk-forward optimization [stub]
│   ├── qv_risk/  qv_portfolio/  # Python-side config facades
│   ├── qv_bus/                  # Redis-Streams client + MessagePack Envelope decode
│   ├── qv_live/                 # TradingNode (live runtime) [stub]
│   ├── qv_config/               # layered config (CLI > env > files > defaults), pydantic
│   └── qv_cli/                  # `qv` CLI (Typer)
│
├── services/                    # deployable wrappers: ingestor, trader, risk-monitor, api, ui
├── deploy/                      # Dockerfiles + prometheus/grafana/loki/tempo/otel
├── config/                      # base.yaml + {backtest,sandbox,live}.yaml
├── examples/backtest-sma/       # runnable example
└── tests/{parity,regression}/   # golden parity + pinned-statistics regression
```

## 4. Domain model — the integer invariant

All prices/quantities/money are **fixed-precision integer-backed** value types (`qv-core::value`):
`Price { raw: i64, precision }`, `Quantity { raw: i64, precision }` (non-negative),
`Money { amount: i128, currency }`. **No `f64` in the domain** — `as_f64()` is display-only; the
Python mirror keeps the same integer representation and exposes decimals only via `as_decimal()` /
`amount()` methods, never as fields. This eliminates float drift in PnL/matching. All identifiers are
distinct newtypes (`qv-model::identifiers`) so ID categories can never be mixed.

`Order` and `Position` are **event-sourced** (`qv-model::order`, `qv-model::position`): state is the
fold of an immutable event sequence, giving an audit trail and trivial reconciliation. The Order FSM
has a complete transition table including the modify path
(`PendingUpdate → Updated → Accepted/PartiallyFilled`, `PendingCancel → Canceled`). Illegal
transitions fail-fast.

## 5. The ExecutionClient port — the parity seam

`qv-ports::ExecutionClient` is the single seam where backtest vs live differ.
`SimulatedExecutionClient` (backtest), a testnet variant (sandbox), and `BinanceExecutionClient`
(live) all implement it identically; the OMS/Risk/Strategy above it is byte-for-byte the same.

The `SimulatedExchange` (`qv-sim`) is parameterized by a **`BrokerageModel`** (fees / slippage /
fill / latency) that is **shared with live config** — backtest and live agree on venue *economics*,
not just order flow (LEAN's keystone). Simulated acks/fills are scheduled at `now + latency_ns` in a
`DelayedEventQueue` and merge-sorted with market data on the HistoricalClock time-frontier.

`ClientOrderId` has a single owner: the **OrderFactory** assigns it deterministically
(`{strategy_id}-{seq:020}`, persisted SeqCursor in live) at construction, so it is stable before
submit and retries never double-submit. The ExecutionEngine only tracks/dedupes by it.

## 6. The Rust↔Python boundary

PyO3 + maturin is the **only** in-process binding (no Cython — avoiding NautilusTrader's
dual-binding tax). A Python `Strategy` subclass is bridged by **`PyStrategyAdapter`**: it holds a
`Py<PyAny>`, implements the synchronous Rust `Strategy` trait, and for each event acquires the GIL
and calls the corresponding Python method.

Per-event GIL acquisition is treated as a **first-class, budgeted cost** (tracked as an SLO
histogram `strategy_dispatch_ns`), not hidden. Two consequences are made explicit:

- **Native-Rust strategies** (implementing the Rust trait directly, no GIL) are a fully supported
  first-class path for latency-sensitive users. The example (`examples/backtest-sma`) is one.
- The "deterministic / C-like-latency" claim is scoped to the **Rust engine pipeline** and to
  backtest reproducibility — a Python handler adds a bounded, measured per-event cost.

For cross-service / UI fan-out, Python never imports the Rust bus crate in-process. It consumes a
**Redis-Streams** bus via the `qv_bus` package, decoding a versioned MessagePack `Envelope`
(`{schema_version, msg_type, trace_id, ts_init, payload}`). The in-process bus, by contrast, passes
typed `Arc` payloads with **zero serialization** (the hot path).

## 7. Data flow

**Backtest.** `qv_backtest`/CLI builds a `RunConfig` with `Environment::Backtest`; the Kernel injects
a `HistoricalClock`, a HistoryReader-backed data feed, and a `SimulatedExecutionClient`. The core
loop drains merge-sorted CoreEvents; the DataEngine does cache-then-publish; the Strategy handler
fires synchronously; orders pass the synchronous RiskEngine gate, then the SimulatedExecutionClient
applies the BrokerageModel and **enqueues** ack/fill reports at `now + latency`; as the clock
advances, those reports fold into the event-sourced Order/Position at the correct interleaved time.
Analytics produces a tear sheet and runs lookahead/recursion bias detectors.

**Live.** `qv_live` builds the SAME RunConfig with `Environment::Live`; the Kernel injects a
`LiveClock`, the `BinanceDataClient`, and the `BinanceExecutionClient` — nothing else changes. A
standalone Rust `ingestor` normalizes Binance WS frames and republishes on the Redis bus; the
`trader` process's DataEngine consumes them; warm-up is served from the LOCAL HistoryReader
(identical to backtest). Fills/acks arrive via the WS user-stream (fast) + a REST poll loop
(fallback); on restart, `reconcile()` replays the event log and diffs venue truth. An out-of-band
`risk-monitor` watches all PnL/positions and can trip the global kill-switch.

## 8. Observability & deployment

Dockerized multi-service stack deployable to a single VPS via docker-compose (prod/dev/obs
profiles). Observability: Prometheus (metrics) + Grafana (dashboards-as-code) + Loki (logs) + Tempo
(traces) via an OpenTelemetry Collector; `trace_id` propagates through the Redis Envelope. SLO
histograms include `ingest_to_publish_ns`, `submit_to_ack_ns`, `strategy_dispatch_ns`, `book_gaps`,
`ws_reconnects`, `risk_denials`. The stack starts with the in-process bus + single-process node
topology (deterministic, simple); Redis Streams is a clean horizontal-scale / telemetry seam.

## 9. Build order

1. Bootstrap workspace + CI + empty compose with redis/postgres/observability.
2. `qv-core` (value types, clock, timers) — property tested. ✅
3. `qv-model` (IDs, Instrument, Order FSM, Fill, Position, market data). ✅
4. `qv-ports` (all port traits + command/report types). ✅
5. `qv-py` PyO3 + PyStrategyAdapter dispatch shim. 🚧
6. `qv-bus` + `qv-cache` (in-proc bus, indexed cache). ✅
7. Engines on the ports (data/exec/risk/portfolio) wired in `qv-kernel`. ✅
8. `qv-sim` (matching + BrokerageModel + DelayedEventQueue) — first proof of parity. ✅
9. Data lake foundation (`qv_data`). 🚧
10. `qv_strategy` + `qv_backtest` (authoritative runner + advisory screen). 🚧
11. `qv_analytics` (metrics + bias detectors). 🚧
12. `qv_optimize` (Optuna walk-forward). 🚧
13. `qv-network` + `qv-adapters/binance`. 🚧
14. `qv-persistence` + reconciliation. 🚧
15. Python bus client + standalone services. 🚧
16. Observability wiring. 🚧
17. `qv_live` + api + ui + risk-monitor. 🚧
18. Ops + hardening + sandbox-vs-backtest parity acceptance gate. 🚧

## 10. Key decisions (with rationale)

- **Synchronous Strategy trait + PyStrategyAdapter** — a Python object cannot implement an async
  Rust trait; a sync trait + GIL-bridging adapter is the only coherent ONE-API design. Native-Rust
  strategies are the no-GIL escape hatch.
- **Per-event GIL cost is budgeted, not hidden** — measured via `strategy_dispatch_ns`; the latency
  claim is scoped to the Rust pipeline.
- **Vectorized path is explicitly non-authoritative** — it skips Risk/Exec/Brokerage, so it is a
  fast screen, never a parity surface; the cross-check is an advisory drift warning.
- **Single ClientOrderId owner (OrderFactory)** — stable before submit; idempotent retries.
- **All ports in one `qv-ports` crate owning `async-trait`** — keeps `qv-model` sync-only; fixes the
  non-compiling `&self -> Receiver` (now `take_*(&mut self)` once at wiring); adds the modify
  report + `disconnect()`.
- **In-proc bus passes typed Arc (zero serialization); only Redis serializes** — resolves the
  contradiction with the zero-serialization claim; Python consumes via `qv_bus`.
- **Delayed-fill scheduling on the time-frontier** — without it, latency is cosmetic and fills
  mis-interleave; merge-sort makes delayed execution deterministic.
- **Event-sourced Orders/Positions with the full modify FSM; integer domain even in Python; fail-
  fast numerics; Clock timers deliver TimerEvents with cancel.**
- **Rust+Tokio core + Python control plane, PyO3-only; in-proc bus default, Redis Streams (not
  Kafka) as the scale seam; per-node injectable runtime (no global singletons); out-of-band
  risk-monitor.**
- **Warm-up always from the local HistoryReader** — never live REST at handler time, so indicators
  are identical in backtest and live.

## 11. Open questions

Tracked for later: multi-node sharding & ordered replay; heavy per-event Strategy compute beyond the
GIL baseline; concrete cross-check & sandbox-vs-backtest parity thresholds per asset class;
BrokerageModel queue/partial-fill fidelity ceiling; prod secrets management (SOPS/Vault);
asset-class roadmap (inverse perps, futures-with-expiry, options, equities) and settlement-PnL
validation against venue statements; data-lake retention/downsampling; reconciliation edge cases
(WS vs REST disagreement, modify-then-fill races); SeqCursor namespacing across accounts.
