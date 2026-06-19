//! [`EventStore`] — the append-only `OrderEvent` log, the source of truth for event-sourced state.
//!
//! Orders/Positions are folds of an immutable event sequence (architecture §4). The store appends
//! every `OrderEvent` and can replay a stream to rebuild the aggregate on restart; this replay is
//! exactly what `ExecutionClient::reconcile()` diffs against venue truth (§7).
//!
//! [`SqliteEventStore`] backs the log with an embedded SQLite database (`rusqlite`, `bundled`):
//! a single append-only `order_events` table keyed by `(client_order_id, seq)` where `seq` is a
//! monotonic per-order counter, each event stored as a self-describing serde_json blob so the FSM
//! can evolve without a schema migration. [`NullEventStore`] is the no-op for pure-backtest runs
//! where durability is irrelevant.

use crate::error::PersistResult;
use coinext_core::UnixNanos;
use coinext_model::{ClientOrderId, OrderEvent, StrategyId};
use rusqlite::{params, Connection};
use std::sync::Mutex;

/// Append-only event log over the `OrderEvent` stream.
///
/// Implementors guarantee: appends are durable and ordered per `client_order_id`; [`replay`] returns
/// events in `seq` order so a naive `Order::apply` fold reconstructs the exact terminal state.
///
/// [`replay`]: EventStore::replay
pub trait EventStore: Send + Sync {
    /// Append `event` to the stream for `(strategy_id, client_order_id)` at append time `ts`,
    /// assigning the next monotonic per-order `seq`. Returns the `seq` written.
    fn append(
        &self,
        strategy_id: &StrategyId,
        client_order_id: &ClientOrderId,
        event: &OrderEvent,
        ts: UnixNanos,
    ) -> PersistResult<u64>;

    /// Replay all events for one order stream in `seq` order (for single-aggregate reconstruction).
    fn replay(&self, client_order_id: &ClientOrderId) -> PersistResult<Vec<OrderEvent>>;

    /// Replay every order belonging to `strategy_id`, ordered by `(client_order_id, seq)` — the
    /// full state rebuild on restart that `reconcile()` diffs against venue truth (§7).
    fn all_for_strategy(
        &self,
        strategy_id: &StrategyId,
    ) -> PersistResult<Vec<(ClientOrderId, OrderEvent)>>;
}

/// An embedded-SQLite-backed [`EventStore`]. Open with `:memory:` (tests) or a file path (live).
///
/// The connection is wrapped in a `Mutex` so the store is `Sync` and a single writer serializes
/// appends — the OMS drives this from the synchronous core, so there is no write contention on the
/// hot path; the lock only guards the rare reconcile-time reads racing an append.
pub struct SqliteEventStore {
    conn: Mutex<Connection>,
}

impl SqliteEventStore {
    /// Open (or create) the store at `path`, creating the schema if absent. Pass `":memory:"` for
    /// an ephemeral in-process store.
    pub fn new(path: &str) -> PersistResult<Self> {
        let conn = Connection::open(path)?;
        Self::init_schema(&conn)?;
        Ok(SqliteEventStore {
            conn: Mutex::new(conn),
        })
    }

    fn init_schema(conn: &Connection) -> PersistResult<()> {
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS order_events (
                 strategy_id     TEXT    NOT NULL,
                 client_order_id TEXT    NOT NULL,
                 seq             INTEGER NOT NULL,
                 ts              INTEGER NOT NULL,
                 event_json      TEXT    NOT NULL,
                 PRIMARY KEY (client_order_id, seq)
             );
             CREATE INDEX IF NOT EXISTS idx_order_events_order_seq
                 ON order_events (client_order_id, seq);
             CREATE INDEX IF NOT EXISTS idx_order_events_strategy
                 ON order_events (strategy_id, client_order_id, seq);",
        )?;
        Ok(())
    }
}

impl EventStore for SqliteEventStore {
    fn append(
        &self,
        strategy_id: &StrategyId,
        client_order_id: &ClientOrderId,
        event: &OrderEvent,
        ts: UnixNanos,
    ) -> PersistResult<u64> {
        let event_json = serde_json::to_string(event)?;
        let conn = self.conn.lock().expect("event store mutex poisoned");
        // Next monotonic per-order seq: one past the current max for this stream (0-based).
        let next_seq: u64 = conn.query_row(
            "SELECT COALESCE(MAX(seq) + 1, 0) FROM order_events WHERE client_order_id = ?1",
            params![client_order_id.as_str()],
            |row| row.get(0),
        )?;
        conn.execute(
            "INSERT INTO order_events (strategy_id, client_order_id, seq, ts, event_json)
             VALUES (?1, ?2, ?3, ?4, ?5)",
            params![
                strategy_id.as_str(),
                client_order_id.as_str(),
                next_seq,
                ts.as_u64(),
                event_json,
            ],
        )?;
        Ok(next_seq)
    }

    fn replay(&self, client_order_id: &ClientOrderId) -> PersistResult<Vec<OrderEvent>> {
        let conn = self.conn.lock().expect("event store mutex poisoned");
        let mut stmt = conn.prepare(
            "SELECT event_json FROM order_events
             WHERE client_order_id = ?1 ORDER BY seq ASC",
        )?;
        let rows = stmt.query_map(params![client_order_id.as_str()], |row| {
            row.get::<_, String>(0)
        })?;
        let mut out = Vec::new();
        for json in rows {
            let event: OrderEvent = serde_json::from_str(&json?)?;
            out.push(event);
        }
        Ok(out)
    }

    fn all_for_strategy(
        &self,
        strategy_id: &StrategyId,
    ) -> PersistResult<Vec<(ClientOrderId, OrderEvent)>> {
        let conn = self.conn.lock().expect("event store mutex poisoned");
        let mut stmt = conn.prepare(
            "SELECT client_order_id, event_json FROM order_events
             WHERE strategy_id = ?1 ORDER BY client_order_id ASC, seq ASC",
        )?;
        let rows = stmt.query_map(params![strategy_id.as_str()], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })?;
        let mut out = Vec::new();
        for row in rows {
            let (coid, json) = row?;
            let event: OrderEvent = serde_json::from_str(&json)?;
            out.push((ClientOrderId::from(coid), event));
        }
        Ok(out)
    }
}

/// A no-op [`EventStore`] for pure-backtest runs where the append-only audit log is not needed.
/// Appends are dropped; replays are empty. Live runs MUST use [`SqliteEventStore`] instead.
#[derive(Debug, Default)]
pub struct NullEventStore;

impl EventStore for NullEventStore {
    fn append(
        &self,
        _strategy_id: &StrategyId,
        _client_order_id: &ClientOrderId,
        _event: &OrderEvent,
        _ts: UnixNanos,
    ) -> PersistResult<u64> {
        Ok(0)
    }

    fn replay(&self, _client_order_id: &ClientOrderId) -> PersistResult<Vec<OrderEvent>> {
        Ok(Vec::new())
    }

    fn all_for_strategy(
        &self,
        _strategy_id: &StrategyId,
    ) -> PersistResult<Vec<(ClientOrderId, OrderEvent)>> {
        Ok(Vec::new())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use coinext_core::{Currency, Money, Price, Quantity};
    use coinext_model::{
        Fill, InstrumentId, LiquiditySide, OrderSide, OrderType, TimeInForce, TradeId, VenueOrderId,
    };
    use rust_decimal_macros::dec;

    fn iid() -> InstrumentId {
        InstrumentId::parse("BTCUSDT.BINANCE").unwrap()
    }

    fn fill(coid: &ClientOrderId, px: &str, qty: &str, tid: &str) -> Fill {
        let usdt = Currency::new("USDT", 8).unwrap();
        Fill {
            trade_id: TradeId::from(tid),
            client_order_id: coid.clone(),
            venue_order_id: VenueOrderId::from("V1"),
            instrument_id: iid(),
            side: OrderSide::Buy,
            last_px: Price::from_decimal(px.parse().unwrap(), 2).unwrap(),
            last_qty: Quantity::from_decimal(qty.parse().unwrap(), 3).unwrap(),
            fee: Money::zero(usdt),
            liquidity: LiquiditySide::Taker,
            ts_event: UnixNanos(3),
            ts_init: UnixNanos(3),
        }
    }

    #[test]
    fn append_replay_orders_and_filled_roundtrips() {
        let store = SqliteEventStore::new(":memory:").unwrap();
        let sid = StrategyId::from("s1");
        let coid = ClientOrderId::from("s1-00000000000000000001");

        let init = OrderEvent::Initialized {
            client_order_id: coid.clone(),
            instrument_id: iid(),
            side: OrderSide::Buy,
            order_type: OrderType::Limit,
            quantity: Quantity::from_decimal(dec!(1.0), 3).unwrap(),
            price: Some(Price::from_decimal(dec!(50000), 2).unwrap()),
            trigger: None,
            tif: TimeInForce::Gtc,
            flags: coinext_model::OrderFlags::default(),
            ts: UnixNanos(0),
        };
        let accepted = OrderEvent::Accepted {
            venue_order_id: VenueOrderId::from("V1"),
            ts: UnixNanos(2),
        };
        let filled = OrderEvent::Filled(fill(&coid, "50010", "1.0", "t1"));

        let s0 = store.append(&sid, &coid, &init, UnixNanos(10)).unwrap();
        let s1 = store.append(&sid, &coid, &accepted, UnixNanos(20)).unwrap();
        let s2 = store.append(&sid, &coid, &filled, UnixNanos(30)).unwrap();
        // Monotonic, 0-based seq.
        assert_eq!((s0, s1, s2), (0, 1, 2));

        let replayed = store.replay(&coid).unwrap();
        assert_eq!(replayed.len(), 3, "all three events present");
        // Ordered by seq.
        assert!(matches!(replayed[0], OrderEvent::Initialized { .. }));
        assert!(matches!(replayed[1], OrderEvent::Accepted { .. }));
        // The Filled event round-trips through serde byte-for-byte.
        assert_eq!(replayed[2], filled);
        match &replayed[2] {
            OrderEvent::Filled(f) => {
                assert_eq!(f.last_px.as_decimal(), dec!(50010.00));
                assert_eq!(f.last_qty.as_decimal(), dec!(1.000));
            }
            other => panic!("expected Filled, got {other:?}"),
        }
    }

    #[test]
    fn all_for_strategy_groups_orders() {
        let store = SqliteEventStore::new(":memory:").unwrap();
        let sid = StrategyId::from("s1");
        let other = StrategyId::from("s2");
        let a = ClientOrderId::from("s1-00000000000000000001");
        let b = ClientOrderId::from("s1-00000000000000000002");
        let c = ClientOrderId::from("s2-00000000000000000001");

        let ev = |ts| OrderEvent::Submitted { ts: UnixNanos(ts) };
        store.append(&sid, &a, &ev(1), UnixNanos(1)).unwrap();
        store.append(&sid, &b, &ev(2), UnixNanos(2)).unwrap();
        store.append(&other, &c, &ev(3), UnixNanos(3)).unwrap();

        let rows = store.all_for_strategy(&sid).unwrap();
        assert_eq!(rows.len(), 2, "only s1's orders");
        assert_eq!(rows[0].0, a);
        assert_eq!(rows[1].0, b);
    }

    #[test]
    fn null_store_is_noop() {
        let store = NullEventStore;
        let sid = StrategyId::from("s1");
        let coid = ClientOrderId::from("s1-00000000000000000001");
        store
            .append(&sid, &coid, &OrderEvent::Submitted { ts: UnixNanos(1) }, UnixNanos(1))
            .unwrap();
        assert!(store.replay(&coid).unwrap().is_empty());
        assert!(store.all_for_strategy(&sid).unwrap().is_empty());
    }
}
