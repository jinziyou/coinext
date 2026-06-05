//! Persistence error type. Wraps the storage backends (`rusqlite`, `serde_json`, IO) and the
//! domain [`ModelError`] (a replay can fold into an illegal transition).

use thiserror::Error;

/// Errors raised by the persistence backends (event store, SeqCursor, Parquet writer).
#[derive(Debug, Error)]
pub enum PersistError {
    /// SQLite-level failure (open, schema, statement, query).
    #[error("sqlite error: {0}")]
    Sqlite(#[from] rusqlite::Error),

    /// (De)serialization of a stored `OrderEvent` JSON payload failed.
    #[error("json codec error: {0}")]
    Json(#[from] serde_json::Error),

    /// Filesystem / object-store IO failure.
    #[error("io error: {0}")]
    Io(#[from] std::io::Error),

    /// A Parquet/Arrow encoding or decoding failure (only with the `parquet` feature).
    #[error("parquet error: {0}")]
    Parquet(String),

    /// A stored row failed an internal invariant (e.g. a non-UTF-8 id, a missing column).
    #[error("corrupt record: {0}")]
    Corrupt(String),
}

/// Convenience alias used throughout the crate.
pub type PersistResult<T> = Result<T, PersistError>;

/// Backwards-compatible alias — earlier scaffold code referred to `PersistenceError`/
/// `PersistenceResult`; keep them pointing at the canonical names so downstream `use`s keep working.
pub type PersistenceError = PersistError;
pub type PersistenceResult<T> = PersistResult<T>;
