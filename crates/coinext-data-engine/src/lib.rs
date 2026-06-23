//! `coinext-data-engine` — consumes market data, maintains Cache state (quotes, marks; order-book and
//! bar aggregation are extension points), and does cache-THEN-publish so strategy handlers always
//! read fresh state. Venue-agnostic: it never imports an exchange.

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
                MarketEvent::Delta(_) => { /* order-book maintenance: extension point */ }
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
            MarketEvent::Delta(_) => {}
        }
    }
}
