# Coinext Architecture

This is the canonical architecture document for Coinext. Everything below is grounded in the
current source tree (`crates/`, `python/`, `config/`); where a subsystem is a stub it is marked as
such.

---

## 1. Overview

Coinext is a multi-asset, venue-agnostic platform for quantitative **research and execution**. The
hot path — market-data ingestion and order execution — is **Rust 1.95 on Tokio**; the control plane
— strategy authoring, research, analytics, and ops — is **Python 3.13**. The two are bridged **only**
by PyO3/maturin (`crates/coinext-py`), with no second binding layer.

The whole design turns on one invariant — **backtest↔live parity**:

> **ONE** Strategy API, **ONE** set of engines (Data / Execution / Risk / Portfolio), **ONE**
> deterministic synchronous core loop. Only the **Kernel** swaps three things between
> `Backtest` / `Sandbox` / `Live`: the **Clock** (`HistoricalClock` vs `SystemClock`/live), the
> **Cache** contents, and the **Data/Execution clients** behind byte-identical ports. Every design
> conflict is tie-broken in favor of parity.

**Status of the invariant (honest scope).** Parity is **enforced structurally**, not yet **verified
end-to-end**. Structurally: there is one `Strategy` trait, one engine set, one core loop, and the
`ExecutionClient` port is implemented by BOTH the simulated venue (`coinext-sim`'s
`SimExecutionClientPort`) and the live `BinanceExecutionClient`; the `Environment` enum is matched by
the kernel, and the `LiveKernel` drives the same engines over the ports. What is **not** yet done: the
`LiveKernel` is a compiling, unit-tested scaffold that has never run against a real venue, and the
"mandatory sandbox-vs-backtest gate" currently compares the backtest to a *perturbed copy of the
backtest* — it does not yet compare live/sandbox fills to backtest fills. So the strong, well-tested
artifact today is the **deterministic backtest core**; live parity is a structurally-enforced design
intent awaiting a real venue run.

A separate vectorized research screen (`python/coinext_screen`) exists for fast parameter sweeps,
but it is **explicitly non-authoritative**: it does not pass through the engines and never validates
a strategy for promotion. Only the event-driven runner is parity-valid.

## 2. Domain model

The domain lives in two Rust crates and is mirrored to Python through PyO3 with the **same integer
representation**.

**Value types (`coinext-core::value`) — the integer invariant.** All prices, quantities, and money
are fixed-precision, integer-backed value types: `Price { raw: i64, precision }`,
`Quantity { raw: i64, precision }` (non-negative), `Money { amount: i128, currency }`, and
`Currency`. Time is `UnixNanos(u64)` (`coinext-core::time`). **There is no `f64` in the domain** —
`as_f64()` is display-only, and the Python mirror exposes decimals via `as_decimal()` / `amount()`
methods, never as fields. This eliminates float drift in PnL and matching.

**Typed identifiers (`coinext-model::identifiers`).** Every identifier is a distinct newtype, so a
`ClientOrderId` can never be passed where a `VenueOrderId` is expected: `Symbol`, `Venue`,
`ClientOrderId`, `VenueOrderId`, `StrategyId`, `TraderId`, `AccountId`, `PositionId`, and the
composite `InstrumentId` (`Symbol` + `Venue`, displayed `SYMBOL.VENUE`, e.g. `BTCUSDT.BINANCE`).
`ClientOrderId` is deterministic/idempotent — assigned once by the OrderFactory.

**Instrument (`coinext-model::instrument`).** A venue-agnostic `Instrument` trait with concrete
spot, perpetual, `Equity`, `FuturesContract` (linear + `expiry_ns` + underlying), and
`OptionContract` (`strike` / `right` / `expiry_ns` / `underlying` / `multiplier`) types. Default
trait accessors (`expiry_ns` / `strike` / `option_right` / `underlying`) return `None`, so spot/perp
are unchanged; PnL scales by `multiplier` / `is_inverse`, so all instrument kinds trade through the
same kernel.

**Event-sourced Order FSM and Position (`coinext-model::order`, `::position`).** `Order` and
`Position` state is the **fold of an immutable event sequence**, giving an audit trail and trivial
reconciliation. The Order FSM has a complete transition table including the modify path
(`PendingUpdate → Updated → Accepted/PartiallyFilled`, `PendingCancel → Canceled`); illegal
transitions fail fast.

**Fill, account, market data (`coinext-model::{fill,account,market_data}`).** `Fill` records
realized executions; `market_data` defines the venue-agnostic event types (quote tick / trade tick /
bar, plus book deltas) that adapters normalize into.

## 3. Components & boundaries

The architecture is **hexagonal**: a sync-only domain (`coinext-core` + `coinext-model`) plus a set
of port traits, with all concrete drivers (sim, venue adapters, clients) living behind those ports.

**Ports (`coinext-ports`)** — the single crate that owns every hexagonal boundary and `async-trait`,
which keeps `coinext-model` sync-only:

| Port trait         | Sync? | Role |
|--------------------|-------|------|
| `DataClient`       | async | source of `MarketEvent`s (sim or venue WS), handed to the core over `tokio::mpsc` |
| `ExecutionClient`  | async | order commands → venue/sim; acks/fills → `ExecutionReport` over `tokio::mpsc` — **the parity seam** |
| `InstrumentProvider` | async | venue `exchangeInfo` → shared `Instrument` |
| `RiskEngine`       | sync  | pre-trade gate + atomic kill-switch |
| `Portfolio`        | sync  | balances, realized/unrealized PnL, exposure |
| `Strategy`         | sync  | user logic: `on_start` / `on_quote` / `on_trade` / `on_bar` / `on_order_event` / `on_order_filled` / `on_timer` / `on_stop` |
| `MessageBus`       | —     | event fan-out (in-proc typed `Arc`, or Redis Envelope across processes) |

**Engines** implement the work above the ports and are wired identically in every environment:

- `coinext-data-engine` — cache-then-publish market data, mark maintenance, bar aggregation.
- `coinext-exec-engine` — the OMS: risk-gated routing, FSM driving, report folding.
- `coinext-risk-engine` — pre-trade risk gate + atomic kill-switch (margin/leverage aware).
- `coinext-portfolio` — balances, realized/unrealized PnL, exposure (marks sourced from the Cache).
- `coinext-cache` — in-memory object store (instruments / quotes / marks / orders / positions / account).
- `coinext-bus` — in-process bus (typed `Arc`, zero serialization) + the Redis `Envelope` contract.
- `coinext-indicators` — streaming SMA / EMA / RSI / ATR / MACD / Bollinger / VWAP (same code warm-up + live).
- `coinext-sim` — the `SimulatedExchange`: a `BrokerageModel` + matching + `DelayedEventQueue`.
- `coinext-derivatives` — Black-Scholes price/greeks/implied-vol (pure-`f64`, zero-dep pricing math).

**Kernel (`coinext-kernel`).** Owns the `Environment` enum (`Backtest` / `Sandbox` / `Live`) and two
runtimes that share one engine set: the `BacktestKernel` (deterministic synchronous core loop, §4,
fed by the inherent sim API on the time-frontier) and the `LiveKernel` (sandbox/live, the SAME
engines + Strategy driven over the `DataClient`/`ExecutionClient` PORTS via `tokio::mpsc`). The
`Environment` is matched to pick the runtime and the Clock (`HistoricalClock` vs `SystemClock`); the
clients are injected. The `LiveKernel` is a working scaffold (unit-tested with in-memory fake clients
and the sim port adapter); it does not yet run against a real venue or implement live-ops reconnect /
out-of-band kill-switch control (those live in `coinext-exec-svc`).

**Rust hot path vs Python control plane.** The Rust crates are the **source of truth** for the
domain and the engines. The Python packages under `python/` build research, strategy authoring,
analytics, and ops **on top of** the compiled core:

- `coinext_contracts` (port Protocols + Envelope schema), `coinext_strategy` (Strategy ABC +
  OrderFactory + examples), `coinext_kernel` (thin wrapper over `coinext_py`).
- `coinext_backtest` (authoritative runner) and `coinext_screen` (advisory vectorized screen).
- `coinext_data` (DataLake catalog + HistoryReader), `coinext_analytics` (metrics + bias detectors),
  `coinext_optimize` (walk-forward), `coinext_indicators` / `coinext_derivatives` (bridged Rust math).
- `coinext_parity` (the parity/cross-check gates), `coinext_risk` / `coinext_portfolio` (config
  facades), `coinext_bus` (Redis-Streams client), `coinext_live` (TradingNode, stub),
  `coinext_config` (layered pydantic config), `coinext_cli` (the `coinext` Typer CLI).

**The PyO3 bridge (`coinext-py`).** PyO3 + maturin is the **only** in-process binding (no Cython). A
Python `Strategy` subclass is bridged by **`PyStrategyAdapter`**: it holds a `Py<PyAny>`, implements
the synchronous Rust `Strategy` trait, and for each event acquires the GIL and calls the matching
Python method. The trait is synchronous because a Python object **cannot** implement an async Rust
trait — a sync trait plus a GIL-bridging adapter is the only coherent way to have ONE Strategy API
driven by a single deterministic core. Per-event GIL cost is treated as a **first-class, budgeted
cost** (tracked as the SLO histogram `strategy_dispatch_ns`), and **native-Rust strategies** (the
Rust trait directly, no GIL — e.g. `examples/backtest-sma`) are a fully supported, latency-sensitive
path.

For cross-service / UI fan-out, Python never imports the Rust bus in-process; it consumes a
**Redis-Streams** bus via `coinext_bus`, decoding a versioned MessagePack `Envelope`
(`{schema_version, msg_type, trace_id, ts_init, payload}`). The in-process bus passes typed `Arc`
payloads with **zero serialization** (the hot path); only Redis serializes.

## 4. Data flow

The hot path is Rust on Tokio, but the **decision core is a single-threaded synchronous event
loop**. All async I/O (WebSocket, REST, persistence) runs on Tokio tasks and hands results to the
core over MPSC channels; the core drains events and calls synchronous component handlers — including
the Strategy.

In backtest, `coinext-kernel::BacktestKernel::run` merge-sorts event sources by timestamp on the
time-frontier — incoming `MarketEvent`s (from the HistoryReader-backed feed), due delayed execution
reports from the sim's `DelayedEventQueue`, due `TimerEvent`s from the `HistoricalClock`, and dated
contract **expiries** — then processes each in order. This is what makes a delayed fill (scheduled
at `now + latency`) interleave correctly with subsequent market data: a structural guarantee against
look-ahead.

```
                         ┌──────────────────── Kernel (per Environment) ─────────────────────┐
                         │  injects: Clock  +  Cache  +  Data/Exec clients (behind ports)     │
                         └────────────────────────────────────────────────────────────────────┘

  BACKTEST                                   SANDBOX / LIVE
  HistoricalClock                            SystemClock / LiveClock
  HistoryReader feed                         Binance WS (ingestor) + local HistoryReader warm-up
  SimulatedExecutionClient                   testnet / BinanceExecutionClient
        │                                            │   (async Tokio I/O ── tokio::mpsc ──┐)
        ▼                                            ▼                                      │
  ┌──────────────────────── deterministic synchronous core loop ───────────────────────┐  │
  │  advance clock to next time-frontier =                                              │  │
  │     min(next market event, due sim reports, due timers, due expiries)               │◄─┘
  │       │                                                                             │
  │       ├─ 1. drain due delayed exec reports  → fold into Order/Position FSM          │
  │       ├─ 2. fire due TimerEvents            → Strategy.on_timer                      │
  │       ├─ 3. process market event            → DataEngine cache-then-publish         │
  │       │                                       → Strategy.on_bar/on_quote/on_trade    │
  │       │                                       → orders → RiskEngine gate (sync)      │
  │       │                                       → ExecutionClient (sim enqueues        │
  │       │                                         ack/fill at now+latency)             │
  │       ├─ 4. settle dated-contract expiries  → synthetic settlement fills            │
  │       └─ 5. mark-to-market maintenance      → liquidate if equity < gross × rate    │
  └─────────────────────────────────────────────────────────────────────────────────────┘
        │                                            │
        ▼                                            ▼
  tear sheet + bias screens (coinext_analytics)  Redis Envelope fan-out → trader / api / risk-monitor
```

By construction the differences between the columns are the three Kernel-injected things — Clock,
Cache, and clients — and the engines/Strategy above the ports are the same code in both the
`BacktestKernel` and the `LiveKernel`. The live column above is the **intended** topology; today the
right-hand side is a scaffold: the `LiveKernel` consumes the ports and folds reports through the
shared engines (unit-tested with fakes), but the standalone `ingestor`, the `reconcile()`-on-restart
flow, and the out-of-band `risk-monitor` kill-switch are not yet wired against a live venue. Warm-up
is served from the **local HistoryReader** (never live REST at handler time) so indicators are
identical to backtest — this part is real in both paths.

## 5. Tech stack & key tradeoffs

| Layer | Choice | Why |
|-------|--------|-----|
| Hot path | **Rust 1.95 (stable)** on **Tokio** | deterministic, no-GC latency; async I/O kept off the decision core via MPSC |
| Control plane | **Python 3.13**, managed by **uv** | research/analytics ergonomics; the ecosystem (numpy/polars/optuna/matplotlib) |
| Bridge | **PyO3 + maturin only** | one in-process binding, no Cython dual-binding tax; Python `Strategy` → same Rust kernel |
| Cross-process bus | **Redis Streams** (not Kafka) | a clean horizontal-scale / telemetry seam; the in-proc bus is the default |
| Numerics | **integer-backed value types** | no `f64` in the domain → no float drift in PnL/matching |
| Concurrency | **single-threaded synchronous core** | determinism + ONE Strategy API; async lives only at the edges |

Key tradeoffs made explicit:

- **Synchronous Strategy trait + `PyStrategyAdapter`** — a Python object cannot implement an async
  Rust trait; the sync trait + GIL-bridging adapter is the only coherent ONE-API design, with
  native-Rust strategies as the no-GIL escape hatch.
- **Per-event GIL cost is budgeted, not hidden** — measured via `strategy_dispatch_ns`; the
  "deterministic / C-like-latency" claim is scoped to the Rust pipeline and to backtest
  reproducibility.
- **The vectorized screen is explicitly non-authoritative** — it skips Risk/Exec/Brokerage, so it is
  a fast filter, never a parity surface; the cross-check is an advisory drift warning.
- **In-proc bus passes typed `Arc` (zero serialization); only Redis serializes** — resolves the
  zero-serialization claim on the hot path while still enabling cross-service fan-out.

## 6. Key invariants & constraints

1. **Backtest↔live parity above all.** ONE Strategy API, ONE engine set, ONE core loop; only the
   Kernel swaps Clock/Cache/clients. Every conflict is tie-broken toward parity.
2. **No `f64` in the domain.** Prices/quantities/money are integer-backed in both Rust and the
   Python mirror; floats are display-only.
3. **The `ExecutionClient` port is the only backtest-vs-live seam.** The simulated venue
   (`coinext-sim`: the deterministic backtest uses its inherent pull API; `SimExecutionClientPort`
   wraps it to implement the port) and `BinanceExecutionClient` (live) both implement the ONE
   `coinext_ports::ExecutionClient` trait; everything above it (OMS/Risk/Portfolio/Strategy) is the
   same code. The `SimulatedExchange` is parameterized by a **`BrokerageModel`** (fees / slippage /
   fill / latency) **shared with live config**, so backtest and live agree on venue *economics*, not
   just order flow. Caveat: this seam is structurally in place and unit-tested, but has not yet been
   exercised against a live venue, so end-to-end byte-identical live-vs-backtest fills are an
   intent, not a measured result.
4. **One history path.** Warm-up is always served from the local `HistoryReader`, in both backtest
   and live — never live REST at handler time — so streaming indicators are byte-identical across
   environments.
5. **Single `ClientOrderId` owner.** The OrderFactory assigns it deterministically
   (`{strategy_id}-{seq:020}`, persisted SeqCursor in live) at construction, so it is stable before
   submit and idempotent retries never double-submit; the ExecutionEngine tracks/dedupes by it.
6. **Event-sourced Orders/Positions with the full modify FSM; fail-fast numerics; Clock timers
   deliver `TimerEvent`s with cancel.** Delayed fills are scheduled on the time-frontier and
   merge-sorted with market data — without that, latency is cosmetic and fills mis-interleave.

## 7. Deployment forms

The runnable units are thin wrappers (`services/`) over the Rust crates and Python packages, each
packaged with one Dockerfile under `deploy/docker/`. The topology preserves parity: the SAME engines
run everywhere; only the Kernel-injected Clock/Cache/clients differ (selected via `COINEXT__ENV` and
the Binance section of `.env`).

| Service        | Kind                       | Port(s)                |
|----------------|----------------------------|------------------------|
| `ingestor`     | Rust (`coinext-ingest`)    | metrics `9101`         |
| `exec-svc`     | Rust (`coinext-exec-svc`)  | metrics `9102`, ctl `8081` |
| `trader`       | Python (`coinext_live`), one process per account | metrics `9103` |
| `risk-monitor` | Python (out-of-band global supervisor) | metrics `9104` |
| `api`          | Python FastAPI             | `8000`                 |
| `ui`           | Node 22 / Vite → nginx     | `3000`                 |

Backing services: `postgres:16` (event/audit store), `redis:7` (Redis-Streams bus), `minio` (S3 data
lake). The stack runs on a single VPS via docker-compose with `prod` / `dev` / `obs` profiles.

**Observability (`deploy/*`, `docker-compose.obs.yml`).** Prometheus (metrics) + Grafana
(dashboards-as-code) + Loki (logs) + Tempo (traces) via an OpenTelemetry Collector. The `trace_id`
in the Redis `Envelope` propagates across `ingestor → trader → exec-svc`, so Grafana correlates
metrics ↔ logs ↔ traces. SLO histograms: `ingest_to_publish_ns`, `submit_to_ack_ns`,
`strategy_dispatch_ns`, `book_gaps`, `ws_reconnects`, `risk_denials`. The Rust service crates and the
Python live entrypoints are scaffolded stubs today; the Dockerfiles are valid and reference the
agreed module names. See [`deploy/README.md`](deploy/README.md) and [`services/README.md`](services/README.md).

## 8. Doc map

- [`README.md`](README.md) — what it is, the parity invariant, quick starts, repo layout.
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — what is done/verified, what is next (research), what is
  deferred (live/ops), and open questions.
- [`docs/TESTNET.md`](docs/TESTNET.md) — the end-to-end testnet runbook and the parity promotion gate.
- [`tests/parity/README.md`](tests/parity/README.md) — the two parity checks (advisory cross-check +
  the sandbox-vs-backtest gate, which currently compares backtest-vs-perturbed-backtest, not live).
- Sub-package READMEs: [`crates/coinext-adapters`](crates/coinext-adapters/README.md),
  [`services/*`](services/README.md), [`deploy/`](deploy/README.md),
  [`notebooks/`](notebooks/README.md), [`data/sample/`](data/sample/README.md).
