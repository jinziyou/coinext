//! `coinext-persistence` — the audit-trail + crash-recovery source of truth behind the live runtime.
//!
//! Orders and Positions are **event-sourced** (architecture §4): their state is the fold of an
//! ordered, immutable `OrderEvent` sequence. This crate is where that sequence becomes **durable**,
//! and is the single place the live `exec-svc`/OMS reaches for two guarantees:
//!
//! - **Audit trail** — [`SqliteEventStore`] is an append-only log of every `OrderEvent` keyed by
//!   `(client_order_id, seq)`. The OMS appends on every FSM transition; on restart it replays the
//!   log to rebuild Orders/Positions, and that replayed state is exactly what
//!   `ExecutionClient::reconcile()` diffs against venue truth (§7). Events are stored as
//!   self-describing serde_json so the FSM can evolve without a schema migration.
//! - **Crash-recovery determinism** — [`SqliteSeqCursor`] persists the per-strategy high-water `seq`
//!   the OrderFactory uses to mint deterministic `ClientOrderId`s (`{strategy_id}-{seq:020}`, §5).
//!   Because the counter survives a restart, ids keep advancing and never collide, so submit stays
//!   idempotent and a retry after a crash never double-submits.
//!
//! [`ParquetWriter`] is the data-lake seam (§7): it materializes normalized [`coinext_model::Bar`]s as
//! Arrow/Parquet so the `coinext_data` catalog/HistoryReader can serve warm-up identically in backtest
//! and live. It is gated behind the default `parquet` feature so the event store can build lean.
//!
//! ## exec-svc / OMS wiring seam
//!
//! The OMS holds these as trait objects so backtest can inject the no-op variants and live the
//! durable ones, with no other code change (the parity invariant, §1):
//!
//! ```text
//! Backtest:  Box<dyn EventStore> = NullEventStore        SeqCursor = InMemorySeqCursor
//! Live:      Box<dyn EventStore> = SqliteEventStore(path) SeqCursor = SqliteSeqCursor(path)
//! ```
//!
//! On submit the OMS calls `cursor.next(strategy_id)` to mint the id, then `store.append(..)` on
//! every resulting `OrderEvent`; on startup it calls `store.all_for_strategy(..)`/`store.replay(..)`
//! to rebuild state before `reconcile()`.

#![allow(dead_code)]

mod cursor;
mod error;
mod event_store;
#[cfg(feature = "parquet")]
mod parquet;

pub use cursor::{InMemorySeqCursor, SeqCursor, SqliteSeqCursor};
pub use error::{PersistError, PersistResult, PersistenceError, PersistenceResult};
pub use event_store::{EventStore, NullEventStore, SqliteEventStore};
#[cfg(feature = "parquet")]
pub use parquet::ParquetWriter;
