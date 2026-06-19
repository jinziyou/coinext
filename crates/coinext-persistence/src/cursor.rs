//! [`SeqCursor`] — the persisted monotonic sequence behind deterministic `ClientOrderId`s.
//!
//! The OrderFactory mints ids as `{strategy_id}-{seq:020}` (architecture §5). In live, `seq` must be
//! **persisted** so that across process restarts ids keep advancing and never collide — that
//! stability is what makes submit idempotent and retries safe. Namespacing is per strategy (and,
//! per the open questions §11, potentially per account).
//!
//! [`SqliteSeqCursor`] persists the high-water `seq` per strategy in an embedded SQLite table so the
//! counter survives a crash/restart; [`InMemorySeqCursor`] is the non-durable variant for tests and
//! pure-backtest runs where restart-stability is irrelevant.

use crate::error::PersistResult;
use rusqlite::{params, Connection};
use std::collections::HashMap;
use std::sync::Mutex;

/// A durable, atomically-incrementing per-namespace counter.
pub trait SeqCursor: Send + Sync {
    /// Atomically reserve and return the next sequence value for `namespace` (e.g. a `StrategyId`).
    /// Must be monotonic and survive restarts.
    fn next(&self, namespace: &str) -> PersistResult<u64>;

    /// Peek the current (last-issued) value without advancing; `0` if the namespace is unseen.
    fn current(&self, namespace: &str) -> PersistResult<u64>;
}

/// An in-memory, NON-durable cursor for tests and pure-backtest runs (where restart-stability is
/// irrelevant). Live runs MUST use [`SqliteSeqCursor`] instead.
#[derive(Debug, Default)]
pub struct InMemorySeqCursor {
    counters: Mutex<HashMap<String, u64>>,
}

impl InMemorySeqCursor {
    pub fn new() -> Self {
        InMemorySeqCursor {
            counters: Mutex::new(HashMap::new()),
        }
    }
}

impl SeqCursor for InMemorySeqCursor {
    fn next(&self, namespace: &str) -> PersistResult<u64> {
        let mut g = self.counters.lock().expect("seq cursor mutex poisoned");
        let e = g.entry(namespace.to_string()).or_insert(0);
        *e += 1;
        Ok(*e)
    }

    fn current(&self, namespace: &str) -> PersistResult<u64> {
        let g = self.counters.lock().expect("seq cursor mutex poisoned");
        Ok(g.get(namespace).copied().unwrap_or(0))
    }
}

/// A SQLite-backed [`SeqCursor`]. The high-water `seq` per strategy lives in a `seq_cursor` table
/// keyed by `strategy_id`, so the deterministic `ClientOrderId` sequence survives a process
/// restart — the crux of crash-recovery determinism (no id reuse, no double-submit on retry).
pub struct SqliteSeqCursor {
    conn: Mutex<Connection>,
}

impl SqliteSeqCursor {
    /// Open (or create) the cursor store at `path`, creating the schema if absent. Pass
    /// `":memory:"` for an ephemeral (non-durable) store; pass a file path for crash recovery.
    pub fn new(path: &str) -> PersistResult<Self> {
        let conn = Connection::open(path)?;
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS seq_cursor (
                 strategy_id TEXT    PRIMARY KEY,
                 seq         INTEGER NOT NULL
             );",
        )?;
        Ok(SqliteSeqCursor {
            conn: Mutex::new(conn),
        })
    }

    /// Load the last-saved `seq` for `strategy_id` (`0` if unseen) — the value to resume minting from
    /// after a restart.
    pub fn load(&self, strategy_id: &str) -> PersistResult<u64> {
        let conn = self.conn.lock().expect("seq cursor mutex poisoned");
        let seq: Option<u64> = conn
            .query_row(
                "SELECT seq FROM seq_cursor WHERE strategy_id = ?1",
                params![strategy_id],
                |row| row.get(0),
            )
            .ok();
        Ok(seq.unwrap_or(0))
    }

    /// Durably persist `seq` as the high-water mark for `strategy_id` (UPSERT).
    pub fn save(&self, strategy_id: &str, seq: u64) -> PersistResult<()> {
        let conn = self.conn.lock().expect("seq cursor mutex poisoned");
        conn.execute(
            "INSERT INTO seq_cursor (strategy_id, seq) VALUES (?1, ?2)
             ON CONFLICT(strategy_id) DO UPDATE SET seq = excluded.seq",
            params![strategy_id, seq],
        )?;
        Ok(())
    }
}

impl SeqCursor for SqliteSeqCursor {
    fn next(&self, namespace: &str) -> PersistResult<u64> {
        let conn = self.conn.lock().expect("seq cursor mutex poisoned");
        // Atomic fetch-and-increment: bump the row (or insert at 1) and read it back. Wrapped in an
        // IMMEDIATE transaction so a concurrent `next` cannot interleave between the two statements.
        conn.execute_batch("BEGIN IMMEDIATE;")?;
        let res = (|| -> PersistResult<u64> {
            conn.execute(
                "INSERT INTO seq_cursor (strategy_id, seq) VALUES (?1, 1)
                 ON CONFLICT(strategy_id) DO UPDATE SET seq = seq + 1",
                params![namespace],
            )?;
            let seq: u64 = conn.query_row(
                "SELECT seq FROM seq_cursor WHERE strategy_id = ?1",
                params![namespace],
                |row| row.get(0),
            )?;
            Ok(seq)
        })();
        match &res {
            Ok(_) => conn.execute_batch("COMMIT;")?,
            Err(_) => {
                let _ = conn.execute_batch("ROLLBACK;");
            }
        }
        res
    }

    fn current(&self, namespace: &str) -> PersistResult<u64> {
        self.load(namespace)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn in_memory_cursor_is_monotonic_per_namespace() {
        let c = InMemorySeqCursor::new();
        assert_eq!(c.next("s1").unwrap(), 1);
        assert_eq!(c.next("s1").unwrap(), 2);
        assert_eq!(c.next("s2").unwrap(), 1);
        assert_eq!(c.current("s1").unwrap(), 2);
        assert_eq!(c.current("unseen").unwrap(), 0);
    }

    #[test]
    fn sqlite_cursor_survives_reopen_crash_recovery() {
        // Use a temp FILE db (not :memory:) so the value must round-trip through disk.
        let dir = std::env::temp_dir();
        let path = dir.join(format!("coinext_seq_cursor_{}.db", std::process::id()));
        let path_str = path.to_str().unwrap();
        let _ = std::fs::remove_file(&path);

        {
            let cur = SqliteSeqCursor::new(path_str).unwrap();
            cur.save("s1", 42).unwrap();
            assert_eq!(cur.load("s1").unwrap(), 42);
            // `cur` drops here, closing the connection — simulating a process exit.
        }

        // Reopen the same file: the saved seq must still be 42 (crash-recovery determinism).
        {
            let reopened = SqliteSeqCursor::new(path_str).unwrap();
            assert_eq!(reopened.load("s1").unwrap(), 42);
            assert_eq!(reopened.load("unseen").unwrap(), 0);
            // The next minted id resumes from the persisted high-water mark.
            assert_eq!(reopened.next("s1").unwrap(), 43);
        }

        let _ = std::fs::remove_file(&path);
    }
}
