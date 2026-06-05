//! `qv-adapters-binance` — the **reference venue adapter**, the live side of the parity seam.
//!
//! Per the architecture (`docs/ARCHITECTURE.md` §5, "The ExecutionClient port — the parity seam"),
//! the ONLY thing that differs between backtest and live is the Data/Execution clients behind the
//! byte-identical `qv-ports` traits. This crate provides the Binance spot implementations:
//!
//! - [`BinanceDataClient`]      — `impl qv_ports::DataClient`      (market-data WS + warm-up bars)
//! - [`BinanceExecutionClient`] — `impl qv_ports::ExecutionClient` (order flow + user-stream fills)
//! - [`BinanceInstrumentProvider`] — `impl qv_ports::InstrumentProvider` (exchangeInfo -> `Instrument`)
//!
//! The WS/REST transport is delegated to `qv-network`'s `WsClient`/`RestClient`/`RateLimiter`
//! (rustls TLS). Normalized results are handed to the deterministic core over the `tokio::mpsc`
//! seams the ports define (`take_stream` for market data, `take_reports` for execution), exactly as
//! `qv-sim` does in backtest — which is what makes the rest of the system identical across
//! environments.
//!
//! Two design pillars are realized here:
//!   1. **WS depth-diff resync** (see [`book`]): apply `@depth` diffs keyed by update ids with
//!      Binance gap detection (`U <= lastUpdateId+1 <= u` for the first diff; `pu == previous u`
//!      thereafter); a gap signals a resync.
//!   2. **Idempotent submit** (see [`exec`]): the deterministic `ClientOrderId` (architecture §5) is
//!      passed straight through as Binance `newClientOrderId`, so a retried submit is a no-op at the
//!      venue and `reconcile()` can diff venue truth against the local event log on restart.

#![allow(dead_code)]

pub mod book;
pub mod config;
pub mod data;
pub mod exec;
pub mod instruments;

pub use book::{ApplyOutcome, DepthUpdate, LocalOrderBook};
pub use config::BinanceConfig;
pub use data::BinanceDataClient;
pub use exec::BinanceExecutionClient;
pub use instruments::BinanceInstrumentProvider;

use qv_model::Venue;

/// The canonical `Venue` tag for this adapter (matches `InstrumentId` symbology `*.BINANCE`).
pub fn venue() -> Venue {
    Venue::from("BINANCE")
}
