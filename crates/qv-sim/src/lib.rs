//! `qv-sim` — the research-fidelity core. A SimulatedExchange parameterized by a
//! [`BrokerageModel`] (fees/slippage/latency) that is SHARED with live config, so backtest and
//! live share venue economics. Acks/fills are scheduled at `now + latency` in a
//! [`DelayedEventQueue`] and drained by the kernel as the HistoricalClock advances, so delayed
//! executions interleave deterministically with market data — this is what makes a backtest
//! faithful rather than cosmetic.
//!
//! State is behind `RefCell` so methods take `&self`, matching the single-threaded deterministic
//! core. The kernel uses the inherent synchronous API (`on_submit`/`on_market`/`drain_due`); the
//! same client is conceptually behind the identical `ExecutionClient` port the live adapter
//! implements (the parity seam).

use qv_cache::Cache;
use qv_core::{Clock, Price, Quantity, UnixNanos};
use qv_model::{
    Instrument, InstrumentId, LiquiditySide, MarketEvent, Order, OrderSide, OrderType, TradeId,
    Venue, VenueOrderId,
};
use qv_ports::ExecutionReport;
use std::cell::RefCell;
use std::cmp::Ordering;
use std::collections::BinaryHeap;
use std::rc::Rc;

mod brokerage;
pub use brokerage::{BrokerageModel, CommandKind, DefaultBrokerageModel};

/// A report scheduled to become due at `due`. Ordered so a `BinaryHeap` pops the EARLIEST due
/// first (ties broken by insertion `seq` for determinism).
struct Scheduled {
    due: UnixNanos,
    seq: u64,
    report: ExecutionReport,
}
impl PartialEq for Scheduled {
    fn eq(&self, o: &Self) -> bool {
        self.due == o.due && self.seq == o.seq
    }
}
impl Eq for Scheduled {}
impl Ord for Scheduled {
    fn cmp(&self, o: &Self) -> Ordering {
        // Reversed: earliest due is "greatest" so the max-heap yields it first.
        o.due.cmp(&self.due).then(o.seq.cmp(&self.seq))
    }
}
impl PartialOrd for Scheduled {
    fn partial_cmp(&self, o: &Self) -> Option<Ordering> {
        Some(self.cmp(o))
    }
}

/// Min-heap (by due time) of pending execution reports.
#[derive(Default)]
pub struct DelayedEventQueue {
    heap: BinaryHeap<Scheduled>,
    seq: u64,
}
impl DelayedEventQueue {
    fn push(&mut self, due: UnixNanos, report: ExecutionReport) {
        self.seq += 1;
        self.heap.push(Scheduled {
            due,
            seq: self.seq,
            report,
        });
    }
    fn peek_due(&self) -> Option<UnixNanos> {
        self.heap.peek().map(|s| s.due)
    }
    fn drain_due(&mut self, frontier: UnixNanos) -> Vec<ExecutionReport> {
        let mut out = Vec::new();
        while let Some(top) = self.heap.peek() {
            if top.due > frontier {
                break;
            }
            out.push(self.heap.pop().unwrap().report);
        }
        out
    }
}

struct Resting {
    order: Order,
}

struct SimState {
    queue: DelayedEventQueue,
    resting: Vec<Resting>,
    venue_seq: u64,
    trade_seq: u64,
}

/// Simulated execution venue. Construct with a shared `Clock` (the kernel's HistoricalClock) and
/// `Cache` (for marks/instruments) plus a `BrokerageModel`.
pub struct SimulatedExecutionClient {
    venue: Venue,
    clock: Rc<dyn Clock>,
    cache: Rc<RefCell<Cache>>,
    model: Box<dyn BrokerageModel>,
    state: RefCell<SimState>,
}

impl SimulatedExecutionClient {
    pub fn new(
        venue: Venue,
        clock: Rc<dyn Clock>,
        cache: Rc<RefCell<Cache>>,
        model: Box<dyn BrokerageModel>,
    ) -> Self {
        SimulatedExecutionClient {
            venue,
            clock,
            cache,
            model,
            state: RefCell::new(SimState {
                queue: DelayedEventQueue::default(),
                resting: Vec::new(),
                venue_seq: 0,
                trade_seq: 0,
            }),
        }
    }

    pub fn venue(&self) -> Venue {
        self.venue.clone()
    }

    /// The reference price for `instrument`: prefer the side-appropriate quote, else the mark.
    fn reference_price(&self, id: &InstrumentId, side: OrderSide) -> Option<Price> {
        let cache = self.cache.borrow();
        if let Some(q) = cache.quote(id) {
            return Some(match side {
                OrderSide::Buy => q.ask,
                OrderSide::Sell => q.bid,
            });
        }
        cache.mark(id)
    }

    fn next_venue_id(state: &mut SimState) -> VenueOrderId {
        state.venue_seq += 1;
        VenueOrderId::from(format!("SIM-{:020}", state.venue_seq))
    }
    fn next_trade_id(state: &mut SimState) -> TradeId {
        state.trade_seq += 1;
        TradeId::from(format!("SIM-T-{:020}", state.trade_seq))
    }

    #[allow(clippy::too_many_arguments)]
    fn make_fill(
        &self,
        state: &mut SimState,
        order: &Order,
        venue_order_id: VenueOrderId,
        fill_px: Price,
        fill_qty: Quantity,
        liquidity: LiquiditySide,
        ts: UnixNanos,
        inst: &dyn Instrument,
    ) -> ExecutionReport {
        let fee = self.model.fee(order, fill_px, fill_qty, liquidity, inst);
        ExecutionReport::Fill(qv_model::Fill {
            trade_id: Self::next_trade_id(state),
            client_order_id: order.client_order_id.clone(),
            venue_order_id,
            instrument_id: order.instrument_id.clone(),
            side: order.side,
            last_px: fill_px,
            last_qty: fill_qty,
            fee,
            liquidity,
            ts_event: ts,
            ts_init: ts,
        })
    }

    /// Submit an order. Schedules an `Accepted` and (for marketable orders) a `Fill` at
    /// `now + latency`. Non-marketable limit orders rest until a future market event crosses them.
    pub fn on_submit(&self, order: Order) {
        let now = self.clock.now_ns();
        let inst = match self.cache.borrow().instrument(&order.instrument_id) {
            Some(i) => i,
            None => return, // unknown instrument: silently ignore (kernel validates upstream)
        };
        let mut state = self.state.borrow_mut();
        let venue_order_id = Self::next_venue_id(&mut state);
        let ack_at = now.saturating_add_ns(self.model.latency_ns(CommandKind::Submit));
        state.queue.push(
            ack_at,
            ExecutionReport::Accepted {
                client_order_id: order.client_order_id.clone(),
                venue_order_id: venue_order_id.clone(),
            },
        );

        let ref_px = self.reference_price(&order.instrument_id, order.side);
        let marketable = match order.order_type {
            OrderType::Market => true,
            OrderType::Limit => match (order.price, ref_px) {
                (Some(limit), Some(rp)) => match order.side {
                    OrderSide::Buy => rp <= limit,
                    OrderSide::Sell => rp >= limit,
                },
                _ => false,
            },
            _ => false, // stop/trailing not modeled in the scaffold
        };

        if marketable {
            let (fill_px, liquidity) = match order.order_type {
                OrderType::Market => (
                    self.model.fill_price(
                        &order,
                        ref_px.unwrap_or_else(|| order.price.unwrap()),
                        &*inst,
                    ),
                    LiquiditySide::Taker,
                ),
                _ => (order.price.unwrap(), LiquiditySide::Taker),
            };
            let fill_at = now.saturating_add_ns(self.model.latency_ns(CommandKind::Submit));
            let fill = self.make_fill(
                &mut state,
                &order,
                venue_order_id,
                fill_px,
                order.quantity,
                liquidity,
                fill_at,
                &*inst,
            );
            state.queue.push(fill_at, fill);
        } else {
            state.resting.push(Resting { order });
        }
    }

    /// Cancel a resting order, scheduling a `Canceled` report.
    pub fn on_cancel(&self, client_order_id: qv_model::ClientOrderId) {
        let now = self.clock.now_ns();
        let mut state = self.state.borrow_mut();
        if let Some(pos) = state
            .resting
            .iter()
            .position(|r| r.order.client_order_id == client_order_id)
        {
            state.resting.remove(pos);
            let at = now.saturating_add_ns(self.model.latency_ns(CommandKind::Cancel));
            state
                .queue
                .push(at, ExecutionReport::Canceled { client_order_id });
        }
    }

    /// Match resting limit orders against an incoming market event; schedule fills that cross.
    pub fn on_market(&self, ev: &MarketEvent) {
        let now = self.clock.now_ns();
        let id = ev.instrument_id().clone();
        let inst = match self.cache.borrow().instrument(&id) {
            Some(i) => i,
            None => return,
        };
        let market_px = match ev {
            MarketEvent::Bar(b) => Some((b.low, b.high, b.close)),
            MarketEvent::Trade(t) => Some((t.price, t.price, t.price)),
            MarketEvent::Quote(q) => Some((q.bid, q.ask, q.mid())),
            MarketEvent::Delta(_) => None,
        };
        let Some((low, high, _close)) = market_px else {
            return;
        };

        let mut state = self.state.borrow_mut();
        let mut filled_idx = Vec::new();
        // Collect crossings first (avoid borrow conflicts), then schedule.
        let mut fills: Vec<(VenueOrderId, Order, Price)> = Vec::new();
        for (i, r) in state.resting.iter().enumerate() {
            if r.order.instrument_id != id {
                continue;
            }
            if let Some(limit) = r.order.price {
                let crossed = match r.order.side {
                    OrderSide::Buy => low <= limit,   // price dipped to/below our bid
                    OrderSide::Sell => high >= limit, // price rose to/above our ask
                };
                if crossed {
                    let void = VenueOrderId::from(format!("SIM-R-{}", i));
                    fills.push((void, r.order.clone(), limit));
                    filled_idx.push(i);
                }
            }
        }
        for (void, order, limit) in fills {
            let at = now.saturating_add_ns(self.model.latency_ns(CommandKind::Submit));
            let fill = self.make_fill(
                &mut state,
                &order,
                void,
                limit,
                order.quantity,
                LiquiditySide::Maker,
                at,
                &*inst,
            );
            state.queue.push(at, fill);
        }
        // Remove filled resting orders (highest index first).
        filled_idx.sort_unstable();
        for i in filled_idx.into_iter().rev() {
            state.resting.remove(i);
        }
    }

    /// The next due execution-report time, for the kernel time-frontier merge.
    pub fn peek_due(&self) -> Option<UnixNanos> {
        self.state.borrow().queue.peek_due()
    }

    /// Drain all reports due at or before `frontier`.
    pub fn drain_due(&self, frontier: UnixNanos) -> Vec<ExecutionReport> {
        self.state.borrow_mut().queue.drain_due(frontier)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use qv_core::HistoricalClock;
    use qv_model::{CurrencyPair, OrderFlags, StrategyId, TimeInForce};
    use qv_ports::OrderFactory;
    use rust_decimal_macros::dec;
    use std::sync::Arc;

    fn setup() -> (
        Rc<HistoricalClock>,
        Rc<RefCell<Cache>>,
        InstrumentId,
        Arc<dyn Instrument>,
    ) {
        let usdt = qv_core::Currency::new("USDT", 8).unwrap();
        let btc = qv_core::Currency::new("BTC", 8).unwrap();
        let id = InstrumentId::parse("BTCUSDT.BINANCE").unwrap();
        let inst: Arc<dyn Instrument> = Arc::new(CurrencyPair {
            id: id.clone(),
            base: btc,
            quote: usdt,
            price_precision: 2,
            size_precision: 3,
            price_increment: Price::from_decimal(dec!(0.01), 2).unwrap(),
            size_increment: Quantity::from_decimal(dec!(0.001), 3).unwrap(),
            min_notional: None,
            maker_fee: dec!(0.0002),
            taker_fee: dec!(0.0004),
        });
        let mut cache = Cache::new();
        cache.add_instrument(inst.clone());
        cache.set_mark(id.clone(), Price::from_decimal(dec!(50000), 2).unwrap());
        (
            Rc::new(HistoricalClock::new(UnixNanos(0))),
            Rc::new(RefCell::new(cache)),
            id,
            inst,
        )
    }

    #[test]
    fn market_order_fills_after_latency() {
        let (clock, cache, id, _inst) = setup();
        let clock_dyn: Rc<dyn Clock> = clock.clone();
        let sim = SimulatedExecutionClient::new(
            Venue::from("BINANCE"),
            clock_dyn,
            cache,
            Box::new(DefaultBrokerageModel::default()),
        );
        let mut factory = OrderFactory::new(StrategyId::from("s1"));
        let order = factory.market(
            id,
            OrderSide::Buy,
            Quantity::from_decimal(dec!(1), 3).unwrap(),
            UnixNanos(0),
        );
        let _ = OrderFlags::default();
        let _ = TimeInForce::Gtc;
        sim.on_submit(order);
        // Nothing due at t=0 (latency > 0).
        assert!(sim.drain_due(UnixNanos(0)).is_empty());
        // Advance well past latency -> Accepted + Fill drain.
        let reports = sim.drain_due(UnixNanos(10_000_000));
        assert_eq!(reports.len(), 2);
        assert!(matches!(reports[0], ExecutionReport::Accepted { .. }));
        assert!(matches!(reports[1], ExecutionReport::Fill(_)));
    }
}
