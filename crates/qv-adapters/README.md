# qv-adapters — venue adapters (the live side of the parity seam)

This directory holds the per-venue adapters. Each adapter is a small crate that implements the
hexagonal ports defined in `qv-ports` for one venue. Per `docs/ARCHITECTURE.md` §5, the
`ExecutionClient` port is **the single seam where backtest vs live differ** — everything above it
(OMS, Risk, Portfolio, Strategy) is byte-for-byte identical across environments.

## The adapter pattern

An adapter translates between a venue's wire protocol/symbology and the venue-agnostic domain
(`qv-model`), behind three port traits:

| Port (`qv-ports`)    | Sync? | What the adapter does                                                            |
|----------------------|-------|----------------------------------------------------------------------------------|
| `DataClient`         | async | WS market-data frames -> normalized `MarketEvent`, pushed over a `tokio::mpsc`    |
| `ExecutionClient`    | async | order commands -> signed REST; acks/fills -> `ExecutionReport` over a `tokio::mpsc`|
| `InstrumentProvider` | async | venue `exchangeInfo` -> shared `Instrument` (tick/lot/min-notional as increments) |

Key properties every adapter must preserve:

- **The mpsc seam.** Inbound streams are taken ONCE at Kernel wiring (`take_stream` /
  `take_reports`). Async WS/REST tasks run on Tokio and hand normalized values to the deterministic
  synchronous core over channels — the same shape `qv-sim` uses in backtest. This is *why* the core
  is identical across environments.
- **Idempotent submit.** `ClientOrderId` is assigned once by the OrderFactory and passed straight
  through to the venue (e.g. `newClientOrderId`), so retries never double-submit and `reconcile()`
  can diff venue truth against the local event log by id on restart (§5, §7).
- **Warm-up from the local HistoryReader, never live REST.** `request_bars` does NOT hit a venue
  klines endpoint at handler time — warm-up is served from the local data lake in both backtest and
  live, so indicators are byte-identical across environments (§7).
- **Shared economics.** Instrument fees/increments feed the SAME `BrokerageModel` the simulator
  uses, so backtest and live agree on venue economics, not just order flow.

Adapters build on `qv-network` (shared `WsClient` / `RestClient` / `RateLimiter`: reconnect,
heartbeat, retry/backoff, auth signing, weight limiting) so each adapter only encodes the venue's
*format and symbology*, not its own transport machinery.

## Crates

- [`binance/`](./binance) — the **reference adapter** (`qv-adapters-binance`). Currently a stub:
  ports are implemented with `PortError::Unsupported` bodies and the WS depth-diff resync + idempotent
  submit plans are documented inline as TODOs.

## Adding a new venue

1. Create `qv-adapters/<venue>/` with its own `Cargo.toml` (these crates are **excluded** from the
   root workspace, so use explicit dependency versions + path deps — not `.workspace = true`).
2. Implement `DataClient`, `ExecutionClient`, `InstrumentProvider`, reusing `qv-network`.
3. Normalize all timestamps to `ts_event` (venue time) + `ts_init` (receipt time) so the
   time-frontier ordering and latency metrics hold.
4. Wire it in the live Kernel build behind the same ports — nothing above the seam changes.
