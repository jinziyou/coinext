//! `coinext-data-engine` — consumes market data, maintains Cache state (quotes, marks, and the L2
//! order book folded from `OrderBookDelta`s; bar aggregation is still an extension point), and does
//! cache-THEN-publish so strategy handlers always read fresh state. Venue-agnostic: it never imports
//! an exchange.

use coinext_cache::Cache;
use coinext_model::MarketEvent;
use coinext_ports::{BusMsg, MessageBus, Topic};
use std::cell::RefCell;
use std::rc::Rc;
use std::sync::Arc;

pub struct DataEngine {
    cache: Rc<RefCell<Cache>>,
}

impl DataEngine {
    pub fn new(cache: Rc<RefCell<Cache>>) -> Self {
        DataEngine { cache }
    }

    /// Update the Cache from a market event, then publish it on `bus` (cache-then-publish, so
    /// strategy handlers read fresh state).
    pub fn process(&self, ev: &MarketEvent, bus: &dyn MessageBus) {
        {
            let mut cache = self.cache.borrow_mut();
            match ev {
                MarketEvent::Quote(q) => {
                    cache.add_quote(q.clone()); // also refreshes the mark to the mid
                }
                MarketEvent::Trade(t) => {
                    cache.set_mark(t.instrument_id.clone(), t.price);
                }
                MarketEvent::Bar(b) => {
                    // The mark (for valuation/PnL) is the bar close. NOTE: this does NOT set the
                    // fill price — a marketable order decided on this close fills at the NEXT bar's
                    // OPEN in the sim (no intra-bar look-ahead), not at this close.
                    cache.set_mark(b.bar_type.instrument_id.clone(), b.close);
                }
                MarketEvent::Delta(d) => {
                    // Fold the delta into the per-instrument L2 book. `BookAction::Clear` (a snapshot
                    // boundary from a resync) wipes the book; the following `Add`s rebuild it.
                    cache.apply_book_delta(d);
                }
            }
        }
        // cache-then-publish
        match ev {
            MarketEvent::Quote(q) => bus.publish(
                Topic::Quote(q.instrument_id.clone()),
                BusMsg::Quote(Arc::new(q.clone())),
            ),
            MarketEvent::Trade(t) => bus.publish(
                Topic::Trade(t.instrument_id.clone()),
                BusMsg::Trade(Arc::new(t.clone())),
            ),
            MarketEvent::Bar(b) => bus.publish(
                Topic::Bar(b.bar_type.clone()),
                BusMsg::Bar(Arc::new(b.clone())),
            ),
            MarketEvent::Delta(d) => bus.publish(
                Topic::Delta(d.instrument_id.clone()),
                BusMsg::Delta(Arc::new(d.clone())),
            ),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use coinext_core::{Price, Quantity, UnixNanos};
    use coinext_model::{BookAction, InstrumentId, OrderBookDelta, OrderSide};
    use coinext_ports::{BoxedHandler, HandlerId};
    use std::cell::Cell;

    /// Minimal bus that counts published deltas (the consumer under test is the Cache; this just
    /// confirms cache-then-publish still fans the delta out).
    #[derive(Default)]
    struct CountingBus {
        deltas: Cell<u32>,
    }
    impl MessageBus for CountingBus {
        fn publish(&self, _topic: Topic, msg: BusMsg) {
            if matches!(msg, BusMsg::Delta(_)) {
                self.deltas.set(self.deltas.get() + 1);
            }
        }
        fn subscribe(&self, _topic: Topic, _handler: BoxedHandler) -> HandlerId {
            HandlerId(0)
        }
        fn unsubscribe(&self, _id: HandlerId) {}
    }

    fn delta(action: BookAction, side: OrderSide, px: i64, sz: i64, seq: u64) -> OrderBookDelta {
        OrderBookDelta {
            instrument_id: InstrumentId::parse("BTCUSDT.BINANCE").unwrap(),
            action,
            side,
            price: Price::from_raw(px, 2).unwrap(),
            size: Quantity::from_raw(sz, 3).unwrap(),
            sequence: seq,
            ts_event: UnixNanos(seq),
            ts_init: UnixNanos(seq),
        }
    }

    #[test]
    fn process_delta_maintains_cached_l2_book_and_publishes() {
        let cache = Rc::new(RefCell::new(Cache::new()));
        let engine = DataEngine::new(cache.clone());
        let bus = CountingBus::default();
        let iid = InstrumentId::parse("BTCUSDT.BINANCE").unwrap();

        // Snapshot boundary (Clear) then a rebuild from fresh levels — the resync loop's output.
        for ev in [
            delta(BookAction::Clear, OrderSide::Buy, 0, 0, 100),
            delta(BookAction::Add, OrderSide::Buy, 10_000, 5, 100),
            delta(BookAction::Add, OrderSide::Sell, 10_010, 7, 100),
            delta(BookAction::Update, OrderSide::Buy, 10_000, 9, 101),
        ] {
            engine.process(&MarketEvent::Delta(ev), &bus);
        }

        let c = cache.borrow();
        let book = c.order_book(&iid).expect("book maintained from deltas");
        assert_eq!(book.best_bid().unwrap().0.raw(), 10_000);
        assert_eq!(book.best_bid().unwrap().1.raw(), 9); // the Update applied
        assert_eq!(book.best_ask().unwrap().0.raw(), 10_010);
        assert_eq!(bus.deltas.get(), 4); // every delta fanned out
    }
}
