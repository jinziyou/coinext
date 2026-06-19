//! `coinext-sim` — the research-fidelity core. A SimulatedExchange parameterized by a
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

use coinext_cache::Cache;
use coinext_core::{Clock, Price, Quantity, UnixNanos};
use coinext_model::{
    Instrument, InstrumentId, LiquiditySide, MarketEvent, Order, OrderSide, OrderType, TradeId,
    Venue, VenueOrderId,
};
use coinext_ports::ExecutionReport;
use std::cell::RefCell;
use std::cmp::Ordering;
use std::collections::{BinaryHeap, HashMap};
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

/// Trailing-stop bookkeeping: the trailing distance and the favorable extreme seen so far. The
/// order's `trigger` is `extreme ∓ offset`, ratcheted in the favorable direction every bar.
struct TrailState {
    offset: Price,
    extreme: Price,
}

struct Resting {
    order: Order,
    /// Quantity still to fill (decremented by each partial fill; the order is removed at zero).
    remaining: Quantity,
    /// Estimated volume still resting AHEAD of this order in the queue at its price level. `None`
    /// until the order first becomes crossable (lazy-seeded from the BrokerageModel), then paid
    /// DOWN each bar that merely TOUCHES the level; a price that trades THROUGH the level zeroes it.
    queue_ahead: Option<Quantity>,
    /// The venue id allocated when the order was accepted — STABLE across partial fills (never the
    /// Vec index, which shifts as other orders are removed).
    venue_order_id: VenueOrderId,
    /// `Some` only for a `TrailingStopMarket`: its trailing distance + high/low-water mark.
    trail: Option<TrailState>,
}

struct SimState {
    queue: DelayedEventQueue,
    resting: Vec<Resting>,
    /// Last seen bar `(low, high, volume)` per instrument, for OHLC-aware MARKET-order slippage and
    /// volume participation in `on_submit` (which otherwise has no bar in scope).
    last_bar: HashMap<InstrumentId, (Price, Price, Quantity)>,
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
                last_bar: HashMap::new(),
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
        ExecutionReport::Fill(coinext_model::Fill {
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
            let last_bar = state.last_bar.get(&order.instrument_id).copied();
            let (fill_px, liquidity, first_chunk) = match order.order_type {
                OrderType::Market => {
                    // OHLC-aware slippage from the most recent bar's (low, high).
                    let bar_range = last_bar.map(|(lo, hi, _v)| (lo, hi));
                    let px = self.model.fill_price(
                        &order,
                        ref_px.unwrap_or_else(|| order.price.unwrap()),
                        bar_range,
                        &*inst,
                    );
                    // Volume participation: a large market order takes at most a share of the last
                    // bar's volume now; the rest rests as an aggressive remainder (filled over later
                    // bars). No bar/volume known yet -> fill in full (the pre-participation path).
                    let chunk = match last_bar {
                        Some((_, _, vol)) => self.model.fillable_qty(order.quantity, vol, &*inst),
                        None => order.quantity,
                    };
                    (px, LiquiditySide::Taker, chunk)
                }
                _ => (order.price.unwrap(), LiquiditySide::Taker, order.quantity),
            };
            let fill_at = now.saturating_add_ns(self.model.latency_ns(CommandKind::Submit));
            let fill = self.make_fill(
                &mut state,
                &order,
                venue_order_id.clone(),
                fill_px,
                first_chunk,
                liquidity,
                fill_at,
                &*inst,
            );
            state.queue.push(fill_at, fill);
            // A market order that couldn't fully fill this bar rests its remainder AGGRESSIVELY
            // (order_type Market, no price): it fills at each later bar's market price (taker),
            // capped by participation — never sits passively at a limit.
            if let Ok(remaining) = order.quantity.checked_sub(first_chunk) {
                if remaining.is_positive() {
                    state.resting.push(Resting {
                        order,
                        remaining,
                        queue_ahead: None,
                        venue_order_id,
                        trail: None,
                    });
                }
            }
        } else {
            // Passive resting order (limit / stop / trailing stop). Partial fills decrement
            // `remaining`; the queue-ahead is seeded lazily on the first crossing bar.
            let remaining = order.quantity;
            // A trailing stop seeds its high/low-water mark to the current mark and its trailing
            // distance to `|mark - trigger|` (the submit set `trigger = mark ∓ offset`). If there's
            // no mark yet it rests inert until one exists.
            let trail = if order.order_type == OrderType::TrailingStopMarket {
                match (
                    self.cache.borrow().mark(&order.instrument_id),
                    order.trigger,
                ) {
                    (Some(mark), Some(trigger)) => {
                        let offset = match order.side {
                            OrderSide::Sell => mark.checked_sub(trigger),
                            OrderSide::Buy => trigger.checked_sub(mark),
                        };
                        offset
                            .ok()
                            .filter(|o| o.raw() > 0)
                            .map(|offset| TrailState {
                                offset,
                                extreme: mark,
                            })
                    }
                    _ => None,
                }
            } else {
                None
            };
            state.resting.push(Resting {
                order,
                remaining,
                queue_ahead: None,
                venue_order_id,
                trail,
            });
        }
    }

    /// Cancel a resting order, scheduling a `Canceled` report.
    pub fn on_cancel(&self, client_order_id: coinext_model::ClientOrderId) {
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

    /// Match resting orders against an incoming market event and schedule fills. Passive limits
    /// cross at their price (capped per bar by volume participation + queue position); AGGRESSIVE
    /// market remainders (a market order that couldn't fully fill at submit) take liquidity at the
    /// bar's market price every bar (taker), also volume-capped. Caches the bar's `(low, high,
    /// volume)` for OHLC-aware market-order slippage + participation in `on_submit`.
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
        let Some((low, high, close)) = market_px else {
            return;
        };
        // The traded volume available to resting orders this event (only bars carry it; quotes/
        // trades cap nothing here -> `None` means fill the full remaining).
        let bar_volume = match ev {
            MarketEvent::Bar(b) => Some(b.volume),
            _ => None,
        };

        let mut state = self.state.borrow_mut();
        // Remember the bar (range + volume) for later market-order slippage + participation.
        if let MarketEvent::Bar(b) = ev {
            state.last_bar.insert(id.clone(), (low, high, b.volume));
        }

        // Phase 1: per resting order, decide (fill qty, fill price, liquidity, new queue-ahead),
        // immutably borrowing `resting`. `new_queue` is `Some(_)` only for a passive limit whose
        // queue must be written back. Tuple: (index, venue id, order, fill price, fill qty,
        // new queue-ahead, liquidity).
        #[allow(clippy::type_complexity)]
        let mut decisions: Vec<(
            usize,
            VenueOrderId,
            Order,
            Price,
            Quantity,
            Option<Quantity>,
            LiquiditySide,
        )> = Vec::new();
        // StopLimit orders whose trigger crossed this bar: convert them to resting limits.
        let mut to_activate: Vec<usize> = Vec::new();
        // TrailingStopMarket ratchets: (resting index, new extreme, new trigger) to write back.
        let mut to_trail: Vec<(usize, Price, Price)> = Vec::new();
        for (i, r) in state.resting.iter().enumerate() {
            if r.order.instrument_id != id {
                continue;
            }
            // Stop orders rest until the market crosses their trigger.
            //  * StopMarket / TrailingStopMarket -> take liquidity at the market (taker; ticks fill
            //    the full remaining, bars are volume-capped). A trailing stop's trigger first
            //    RATCHETS toward the favorable extreme each bar it is not yet hit.
            //  * StopLimit -> CONVERTS to a passive LIMIT at its price (filled from the next bar by
            //    the limit logic, so slippage is bounded by the limit).
            if !matches!(r.order.order_type, OrderType::Market | OrderType::Limit) {
                // A trailing stop with no trail state (no offset/mark at submit) is misconfigured ->
                // rest INERT rather than degrade into a static stop at its seed trigger.
                if r.order.order_type == OrderType::TrailingStopMarket && r.trail.is_none() {
                    continue;
                }
                let Some(trigger) = r.order.trigger else {
                    continue;
                };
                let triggered = match r.order.side {
                    OrderSide::Buy => high >= trigger, // price rose to the stop
                    OrderSide::Sell => low <= trigger, // price fell to the stop
                };
                let is_market_stop = matches!(
                    r.order.order_type,
                    OrderType::StopMarket | OrderType::TrailingStopMarket
                );
                if triggered && is_market_stop {
                    let fill_qty = match bar_volume {
                        Some(v) => self.model.fillable_qty(r.remaining, v, &*inst),
                        None => r.remaining,
                    };
                    if fill_qty.is_positive() {
                        // Stop out at the trigger, worsened to the bar if the price gapped past it
                        // (a buy stop fills no better than the bar low, a sell no better than the
                        // high), then slipped within the bar by the brokerage model.
                        let ref_px = match r.order.side {
                            OrderSide::Buy => trigger.max(low),
                            OrderSide::Sell => trigger.min(high),
                        };
                        let fill_px =
                            self.model
                                .fill_price(&r.order, ref_px, Some((low, high)), &*inst);
                        decisions.push((
                            i,
                            r.venue_order_id.clone(),
                            r.order.clone(),
                            fill_px,
                            fill_qty,
                            None,
                            LiquiditySide::Taker,
                        ));
                    }
                } else if triggered && r.order.order_type == OrderType::StopLimit {
                    // Activate: it becomes a resting Limit (handled by the limit branch next bar).
                    to_activate.push(i);
                } else if !triggered && r.order.order_type == OrderType::TrailingStopMarket {
                    // Not hit: ratchet the trail toward the favorable extreme (monotonic — the
                    // trigger only tightens, never loosens), for next bar.
                    if let Some(t) = &r.trail {
                        let new_extreme = match r.order.side {
                            OrderSide::Sell => t.extreme.max(high),
                            OrderSide::Buy => t.extreme.min(low),
                        };
                        let new_trigger = match r.order.side {
                            OrderSide::Sell => new_extreme.checked_sub(t.offset),
                            OrderSide::Buy => new_extreme.checked_add(t.offset),
                        };
                        if let Ok(nt) = new_trigger {
                            to_trail.push((i, new_extreme, nt));
                        }
                    }
                }
                continue;
            }
            let Some(limit) = r.order.price else {
                // AGGRESSIVE market remainder (no price): takes liquidity at the bar's market price,
                // capped by participation, no queue. It only participates on BARS (which carry the
                // volume to cap against); a quote/trade tick has no bar volume, so skip it rather
                // than dumping the whole remainder in one shot (which would defeat participation).
                if let Some(v) = bar_volume {
                    let fill_qty = self.model.fillable_qty(r.remaining, v, &*inst);
                    if fill_qty.is_positive() {
                        let fill_px =
                            self.model
                                .fill_price(&r.order, close, Some((low, high)), &*inst);
                        decisions.push((
                            i,
                            r.venue_order_id.clone(),
                            r.order.clone(),
                            fill_px,
                            fill_qty,
                            None,
                            LiquiditySide::Taker,
                        ));
                    }
                }
                continue;
            };
            // Passive limit. Split a cross into THROUGH (price traded strictly past the limit ->
            // level swept) vs TOUCH (price reached the limit exactly -> must wait behind the queue).
            let (through, touch) = match r.order.side {
                OrderSide::Buy => (low < limit, low == limit),
                OrderSide::Sell => (high > limit, high == limit),
            };
            if !(through || touch) {
                continue;
            }
            let Some(v) = bar_volume else {
                // Non-bar event (quote/trade): no volume model -> fill the full crossed remaining.
                if r.remaining.is_positive() {
                    decisions.push((
                        i,
                        r.venue_order_id.clone(),
                        r.order.clone(),
                        limit,
                        r.remaining,
                        None,
                        LiquiditySide::Maker,
                    ));
                }
                continue;
            };
            // The participation-capped per-bar budget (unchanged); it is the only volume the queue
            // logic may spend, so the participation cap is never inflated.
            let share = self.model.fillable_qty(r.remaining, v, &*inst);
            let prec = share.precision();
            let queue = r
                .queue_ahead
                .unwrap_or_else(|| self.model.initial_queue_ahead(v, &*inst));
            let (fill_qty, new_queue) = if through {
                // The whole level traded through: everyone ahead of us executed -> queue cleared.
                (share, Quantity::zero(prec))
            } else {
                // Touch only: our per-bar budget first pays down the queue; the excess fills us.
                let paid = share.as_decimal().min(queue.as_decimal());
                let paid =
                    Quantity::from_decimal(paid, prec).unwrap_or_else(|_| Quantity::zero(prec));
                let nq = queue
                    .checked_sub(paid)
                    .unwrap_or_else(|_| Quantity::zero(prec));
                let fq = share
                    .checked_sub(paid)
                    .unwrap_or_else(|_| Quantity::zero(prec));
                (fq, nq)
            };
            decisions.push((
                i,
                r.venue_order_id.clone(),
                r.order.clone(),
                limit,
                fill_qty,
                Some(new_queue),
                LiquiditySide::Maker,
            ));
        }

        // Activate triggered StopLimit orders -> they rest as plain Limits from here on.
        for &i in &to_activate {
            state.resting[i].order.order_type = OrderType::Limit;
        }
        // Write back trailing-stop ratchets (new high/low-water mark + tightened trigger).
        for (i, extreme, trigger) in to_trail {
            if let Some(t) = state.resting[i].trail.as_mut() {
                t.extreme = extreme;
            }
            state.resting[i].order.trigger = Some(trigger);
        }

        // Phase 2: schedule fills, decrement `remaining`, and write back queue-ahead (mutable).
        // Iteration order is ascending resting-index, so the `seq` assigned by the queue is stable.
        let mut to_remove: Vec<usize> = Vec::new();
        for (i, void, order, fill_px, fill_qty, new_queue, liquidity) in decisions {
            if fill_qty.is_positive() {
                let at = now.saturating_add_ns(self.model.latency_ns(CommandKind::Submit));
                let fill = self.make_fill(
                    &mut state, &order, void, fill_px, fill_qty, liquidity, at, &*inst,
                );
                state.queue.push(at, fill);
            }
            let r = &mut state.resting[i];
            if fill_qty.is_positive() {
                r.remaining = r
                    .remaining
                    .checked_sub(fill_qty)
                    .unwrap_or_else(|_| Quantity::zero(fill_qty.precision()));
            }
            if let Some(nq) = new_queue {
                r.queue_ahead = Some(nq);
            }
            if r.remaining.is_zero() {
                to_remove.push(i);
            }
        }
        // Remove fully-filled resting orders (highest index first so indices stay valid).
        to_remove.sort_unstable();
        for i in to_remove.into_iter().rev() {
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
    use coinext_core::HistoricalClock;
    use coinext_model::{CurrencyPair, OrderFlags, StrategyId, TimeInForce};
    use coinext_ports::OrderFactory;
    use rust_decimal::Decimal;
    use rust_decimal_macros::dec;
    use std::sync::Arc;

    fn setup() -> (
        Rc<HistoricalClock>,
        Rc<RefCell<Cache>>,
        InstrumentId,
        Arc<dyn Instrument>,
    ) {
        let usdt = coinext_core::Currency::new("USDT", 8).unwrap();
        let btc = coinext_core::Currency::new("BTC", 8).unwrap();
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

    fn bar(iid: &InstrumentId, low: &str, high: &str, close: &str, ts: u64) -> MarketEvent {
        use coinext_model::{AggregationSource, Bar, BarAggregation, BarSpec, BarType, PriceType};
        let p = |s: &str| Price::from_decimal(s.parse().unwrap(), 2).unwrap();
        MarketEvent::Bar(Bar {
            bar_type: BarType {
                instrument_id: iid.clone(),
                spec: BarSpec {
                    step: 1,
                    aggregation: BarAggregation::Minute,
                    price_type: PriceType::Last,
                },
                source: AggregationSource::External,
            },
            open: p(close),
            high: p(high),
            low: p(low),
            close: p(close),
            volume: Quantity::from_decimal(dec!(10), 3).unwrap(),
            ts_event: UnixNanos(ts),
            ts_init: UnixNanos(ts),
        })
    }

    fn limit_sim(id: &InstrumentId) -> (Rc<HistoricalClock>, SimulatedExecutionClient) {
        let (clock, cache, _id, _inst) = setup();
        let clock_dyn: Rc<dyn Clock> = clock.clone();
        let sim = SimulatedExecutionClient::new(
            Venue::from("BINANCE"),
            clock_dyn,
            cache,
            Box::new(DefaultBrokerageModel::default()),
        );
        let mut factory = OrderFactory::new(StrategyId::from("s1"));
        // A buy limit @ 49000 rests (ref mark is 50000, so not immediately marketable).
        let order = factory.limit(
            id.clone(),
            OrderSide::Buy,
            Quantity::from_decimal(dec!(1), 3).unwrap(),
            Price::from_decimal(dec!(49000), 2).unwrap(),
            TimeInForce::Gtc,
            OrderFlags::default(),
            UnixNanos(0),
        );
        sim.on_submit(order);
        // Drain the Accepted; the order is now resting (no fill yet).
        let reports = sim.drain_due(UnixNanos(10_000_000));
        assert!(reports
            .iter()
            .all(|r| !matches!(r, ExecutionReport::Fill(_))));
        (clock, sim)
    }

    #[test]
    fn resting_buy_limit_fills_when_bar_low_crosses() {
        let (_clock, cache, id, _inst) = setup();
        let _ = cache;
        let (clock, sim) = limit_sim(&id);
        // A bar whose LOW (48000) dips below the 49000 limit, though its CLOSE (50500) stays above.
        clock.advance_to(UnixNanos(1_000_000_000));
        sim.on_market(&bar(&id, "48000", "50600", "50500", 1_000_000_000));
        let reports = sim.drain_due(UnixNanos(2_000_000_000));
        let fills: Vec<_> = reports
            .iter()
            .filter_map(|r| match r {
                ExecutionReport::Fill(f) => Some(f),
                _ => None,
            })
            .collect();
        assert_eq!(fills.len(), 1, "low crossed the limit -> exactly one fill");
        assert_eq!(
            fills[0].last_px,
            Price::from_decimal(dec!(49000), 2).unwrap()
        );
    }

    #[test]
    fn resting_buy_limit_does_not_fill_when_low_stays_above() {
        let (_clock, cache, id, _inst) = setup();
        let _ = cache;
        let (clock, sim) = limit_sim(&id);
        // Close-only-like bar: low == close == 50500, never reaching the 49000 limit -> no fill.
        clock.advance_to(UnixNanos(1_000_000_000));
        sim.on_market(&bar(&id, "50500", "50600", "50500", 1_000_000_000));
        let reports = sim.drain_due(UnixNanos(2_000_000_000));
        assert!(
            reports
                .iter()
                .all(|r| !matches!(r, ExecutionReport::Fill(_))),
            "low stayed above the limit -> no fill"
        );
    }

    fn bar_vol(
        id: &InstrumentId,
        low: &str,
        high: &str,
        close: &str,
        vol: &str,
        ts: u64,
    ) -> MarketEvent {
        use coinext_model::{AggregationSource, Bar, BarAggregation, BarSpec, BarType, PriceType};
        let p = |s: &str| Price::from_decimal(s.parse().unwrap(), 2).unwrap();
        MarketEvent::Bar(Bar {
            bar_type: BarType {
                instrument_id: id.clone(),
                spec: BarSpec {
                    step: 1,
                    aggregation: BarAggregation::Minute,
                    price_type: PriceType::Last,
                },
                source: AggregationSource::External,
            },
            open: p(close),
            high: p(high),
            low: p(low),
            close: p(close),
            volume: Quantity::from_decimal(vol.parse().unwrap(), 3).unwrap(),
            ts_event: UnixNanos(ts),
            ts_init: UnixNanos(ts),
        })
    }

    #[test]
    fn resting_limit_partial_fills_over_bars_by_volume() {
        // A buy limit for qty 2.0 against bars of volume 4.0 at participation 0.25 fills 1.0/bar,
        // so it completes over TWO bars as two partial fills summing to exactly the order quantity.
        let (clock, cache, id, _inst) = setup();
        let clock_dyn: Rc<dyn Clock> = clock.clone();
        let sim = SimulatedExecutionClient::new(
            Venue::from("BINANCE"),
            clock_dyn,
            cache,
            Box::new(DefaultBrokerageModel::default()), // participation_rate = 0.25
        );
        let mut factory = OrderFactory::new(StrategyId::from("s1"));
        let order = factory.limit(
            id.clone(),
            OrderSide::Buy,
            Quantity::from_decimal(dec!(2), 3).unwrap(),
            Price::from_decimal(dec!(49000), 2).unwrap(),
            TimeInForce::Gtc,
            OrderFlags::default(),
            UnixNanos(0),
        );
        sim.on_submit(order);
        let _ = sim.drain_due(UnixNanos(10_000_000)); // Accepted; now resting

        let collect_fills = |reports: Vec<ExecutionReport>| -> Vec<Quantity> {
            reports
                .into_iter()
                .filter_map(|r| match r {
                    ExecutionReport::Fill(f) => Some(f.last_qty),
                    _ => None,
                })
                .collect()
        };

        // Bar 1: crosses (low 48000 <= 49000), volume 4.0 -> cap 1.0 -> one partial of 1.0.
        clock.advance_to(UnixNanos(1_000_000_000));
        sim.on_market(&bar_vol(&id, "48000", "50600", "50500", "4", 1_000_000_000));
        let f1 = collect_fills(sim.drain_due(UnixNanos(1_500_000_000)));
        assert_eq!(f1.len(), 1, "first bar: one partial fill");
        assert_eq!(f1[0], Quantity::from_decimal(dec!(1), 3).unwrap());

        // Bar 2: remaining 1.0, cap 1.0 -> completes; order removed from the book.
        clock.advance_to(UnixNanos(2_000_000_000));
        sim.on_market(&bar_vol(&id, "48000", "50600", "50500", "4", 2_000_000_000));
        let f2 = collect_fills(sim.drain_due(UnixNanos(2_500_000_000)));
        assert_eq!(f2.len(), 1, "second bar: completes the order");
        assert_eq!(f2[0], Quantity::from_decimal(dec!(1), 3).unwrap());

        // Bar 3: order fully filled and removed -> no further fills.
        clock.advance_to(UnixNanos(3_000_000_000));
        sim.on_market(&bar_vol(&id, "48000", "50600", "50500", "4", 3_000_000_000));
        let f3 = collect_fills(sim.drain_due(UnixNanos(3_500_000_000)));
        assert!(f3.is_empty(), "no fills after the order is complete");
    }

    #[test]
    fn thin_volume_cross_still_makes_progress_one_lot_minimum() {
        // A crossing bar whose participation share (0.25 * 0.003 = 0.00075) floors below one lot
        // (size_increment 0.001) must still fill the minimum lot — never stall a crossed order.
        let (clock, cache, id, _inst) = setup();
        let clock_dyn: Rc<dyn Clock> = clock.clone();
        let sim = SimulatedExecutionClient::new(
            Venue::from("BINANCE"),
            clock_dyn,
            cache,
            Box::new(DefaultBrokerageModel::default()),
        );
        let mut factory = OrderFactory::new(StrategyId::from("s1"));
        let order = factory.limit(
            id.clone(),
            OrderSide::Buy,
            Quantity::from_decimal(dec!(0.002), 3).unwrap(),
            Price::from_decimal(dec!(49000), 2).unwrap(),
            TimeInForce::Gtc,
            OrderFlags::default(),
            UnixNanos(0),
        );
        sim.on_submit(order);
        let _ = sim.drain_due(UnixNanos(10_000_000));

        // Bar 1: thin volume 0.003 -> fills the one-lot minimum 0.001.
        clock.advance_to(UnixNanos(1_000_000_000));
        sim.on_market(&bar_vol(
            &id,
            "48000",
            "50600",
            "50500",
            "0.003",
            1_000_000_000,
        ));
        let f1: Vec<_> = sim
            .drain_due(UnixNanos(1_500_000_000))
            .into_iter()
            .filter_map(|r| match r {
                ExecutionReport::Fill(f) => Some(f.last_qty),
                _ => None,
            })
            .collect();
        assert_eq!(f1.len(), 1, "thin bar still fills (no stall)");
        assert_eq!(f1[0], Quantity::from_decimal(dec!(0.001), 3).unwrap());
    }

    fn queue_sim(factor: Decimal, qty: &str) -> (Rc<HistoricalClock>, SimulatedExecutionClient) {
        let (clock, cache, id, _inst) = setup();
        let clock_dyn: Rc<dyn Clock> = clock.clone();
        let model = DefaultBrokerageModel {
            queue_ahead_factor: factor,
            ..DefaultBrokerageModel::default()
        };
        let sim = SimulatedExecutionClient::new(
            Venue::from("BINANCE"),
            clock_dyn,
            cache,
            Box::new(model),
        );
        let mut factory = OrderFactory::new(StrategyId::from("s1"));
        // Buy limit @ 49000 rests (mark 50000 > 49000).
        let order = factory.limit(
            id.clone(),
            OrderSide::Buy,
            Quantity::from_decimal(qty.parse().unwrap(), 3).unwrap(),
            Price::from_decimal(dec!(49000), 2).unwrap(),
            TimeInForce::Gtc,
            OrderFlags::default(),
            UnixNanos(0),
        );
        sim.on_submit(order);
        let _ = sim.drain_due(UnixNanos(10_000_000)); // Accepted; now resting
        (clock, sim)
    }

    fn drain_fill_count(sim: &SimulatedExecutionClient, frontier: u64) -> usize {
        sim.drain_due(UnixNanos(frontier))
            .into_iter()
            .filter(|r| matches!(r, ExecutionReport::Fill(_)))
            .count()
    }

    #[test]
    fn queue_position_touch_waits_then_fills() {
        // queue_ahead_factor 0.5 on volume-4 bars -> queue seeds to 2.0; each TOUCH bar (low ==
        // limit) pays down 1.0 (the participation share). So the qty-1 order fills only on the THIRD
        // touch bar (bars 1 and 2 pay down the queue, bar 3 fills).
        let id = InstrumentId::parse("BTCUSDT.BINANCE").unwrap();
        let (clock, sim) = queue_sim(dec!(0.5), "1");
        let touch = |ts: u64| bar_vol(&id, "49000", "50000", "49500", "4", ts); // low == limit
        for (n, ts) in [1u64, 2, 3].iter().map(|&n| (n, n * 1_000_000_000)) {
            clock.advance_to(UnixNanos(ts));
            sim.on_market(&touch(ts));
            let fills = drain_fill_count(&sim, ts + 500_000_000);
            if n < 3 {
                assert_eq!(fills, 0, "bar {n}: still waiting behind the queue");
            } else {
                assert_eq!(fills, 1, "bar {n}: queue cleared -> fills");
            }
        }
    }

    #[test]
    fn market_order_participation_fills_over_bars() {
        // A market BUY for qty 3.0 against volume-4 bars at participation 0.25 takes 1.0 now and
        // rests an aggressive remainder that fills 1.0/bar at the market price over the next bars.
        let (clock, cache, id, _inst) = setup();
        let clock_dyn: Rc<dyn Clock> = clock.clone();
        let sim = SimulatedExecutionClient::new(
            Venue::from("BINANCE"),
            clock_dyn,
            cache,
            Box::new(DefaultBrokerageModel::default()), // participation 0.25
        );
        let drain_qtys = |sim: &SimulatedExecutionClient, frontier: u64| -> Vec<Quantity> {
            sim.drain_due(UnixNanos(frontier))
                .into_iter()
                .filter_map(|r| match r {
                    ExecutionReport::Fill(f) => Some(f.last_qty),
                    _ => None,
                })
                .collect()
        };
        let one = Quantity::from_decimal(dec!(1), 3).unwrap();

        // Bar 1 seeds the volume cache (no resting orders yet -> no fills).
        clock.advance_to(UnixNanos(1_000_000_000));
        sim.on_market(&bar_vol(&id, "49900", "50100", "50000", "4", 1_000_000_000));
        assert!(drain_qtys(&sim, 1_100_000_000).is_empty());

        // Submit a market buy for 3.0: first chunk (cap 1.0) fills now; 2.0 rests aggressively.
        let mut factory = OrderFactory::new(StrategyId::from("s1"));
        let order = factory.market(
            id.clone(),
            OrderSide::Buy,
            Quantity::from_decimal(dec!(3), 3).unwrap(),
            UnixNanos(1_000_000_000),
        );
        sim.on_submit(order);
        assert_eq!(
            drain_qtys(&sim, 1_200_000_000),
            vec![one],
            "first chunk fills at submit"
        );

        // Bars 2 and 3 each fill 1.0 of the aggressive remainder.
        clock.advance_to(UnixNanos(2_000_000_000));
        sim.on_market(&bar_vol(&id, "49900", "50100", "50000", "4", 2_000_000_000));
        assert_eq!(
            drain_qtys(&sim, 2_200_000_000),
            vec![one],
            "bar 2 fills 1.0"
        );

        clock.advance_to(UnixNanos(3_000_000_000));
        sim.on_market(&bar_vol(&id, "49900", "50100", "50000", "4", 3_000_000_000));
        assert_eq!(
            drain_qtys(&sim, 3_200_000_000),
            vec![one],
            "bar 3 completes the order"
        );

        // Bar 4: nothing left to fill.
        clock.advance_to(UnixNanos(4_000_000_000));
        sim.on_market(&bar_vol(&id, "49900", "50100", "50000", "4", 4_000_000_000));
        assert!(
            drain_qtys(&sim, 4_200_000_000).is_empty(),
            "order complete -> no more fills"
        );
    }

    fn stop_sim(side: OrderSide, trigger: &str) -> (Rc<HistoricalClock>, SimulatedExecutionClient) {
        let (clock, cache, id, _inst) = setup(); // mark 50000
        let clock_dyn: Rc<dyn Clock> = clock.clone();
        let sim = SimulatedExecutionClient::new(
            Venue::from("BINANCE"),
            clock_dyn,
            cache,
            Box::new(DefaultBrokerageModel::default()),
        );
        let mut factory = OrderFactory::new(StrategyId::from("s1"));
        let order = factory.stop_market(
            id,
            side,
            Quantity::from_decimal(dec!(1), 3).unwrap(),
            Price::from_decimal(trigger.parse().unwrap(), 2).unwrap(),
            UnixNanos(0),
        );
        sim.on_submit(order);
        let _ = sim.drain_due(UnixNanos(10_000_000)); // Accepted; now resting
        (clock, sim)
    }

    fn drain_fills(reports: Vec<ExecutionReport>) -> Vec<(Price, Quantity)> {
        reports
            .into_iter()
            .filter_map(|r| match r {
                ExecutionReport::Fill(f) => Some((f.last_px, f.last_qty)),
                _ => None,
            })
            .collect()
    }

    #[test]
    fn stop_market_buy_triggers_above_the_stop() {
        let id = InstrumentId::parse("BTCUSDT.BINANCE").unwrap();
        let (clock, sim) = stop_sim(OrderSide::Buy, "51000"); // breakout buy stop above mark
                                                              // A bar that does NOT reach 51000 -> no trigger.
        clock.advance_to(UnixNanos(1_000_000_000));
        sim.on_market(&bar_vol(
            &id,
            "49000",
            "50500",
            "50000",
            "10",
            1_000_000_000,
        ));
        assert!(drain_fills(sim.drain_due(UnixNanos(1_500_000_000))).is_empty());
        // A bar whose HIGH reaches 51000 -> triggers and fills at/above the stop.
        clock.advance_to(UnixNanos(2_000_000_000));
        sim.on_market(&bar_vol(
            &id,
            "50000",
            "51200",
            "51000",
            "10",
            2_000_000_000,
        ));
        let fills = drain_fills(sim.drain_due(UnixNanos(2_500_000_000)));
        assert_eq!(fills.len(), 1);
        assert!(
            fills[0].0 >= Price::from_decimal(dec!(51000), 2).unwrap(),
            "buy stop fills >= trigger"
        );
    }

    fn stop_limit_sim(
        trigger: &str,
        limit: &str,
    ) -> (Rc<HistoricalClock>, SimulatedExecutionClient) {
        let (clock, cache, id, _inst) = setup();
        let clock_dyn: Rc<dyn Clock> = clock.clone();
        let sim = SimulatedExecutionClient::new(
            Venue::from("BINANCE"),
            clock_dyn,
            cache,
            Box::new(DefaultBrokerageModel::default()),
        );
        let mut factory = OrderFactory::new(StrategyId::from("s1"));
        // A SELL stop-limit below the mark (stop-loss with a price floor).
        let order = factory.stop_limit(
            id,
            OrderSide::Sell,
            Quantity::from_decimal(dec!(1), 3).unwrap(),
            Price::from_decimal(trigger.parse().unwrap(), 2).unwrap(),
            Price::from_decimal(limit.parse().unwrap(), 2).unwrap(),
            UnixNanos(0),
        );
        sim.on_submit(order);
        let _ = sim.drain_due(UnixNanos(10_000_000));
        (clock, sim)
    }

    #[test]
    fn stop_limit_triggers_then_fills_at_its_limit() {
        let id = InstrumentId::parse("BTCUSDT.BINANCE").unwrap();
        let (clock, sim) = stop_limit_sim("49000", "48900"); // sell stop @49000, limit floor 48900
                                                             // Bar 1: low 48800 crosses the 49000 stop -> converts to a sell limit @48900; bar high 48850
                                                             // is below the limit, so no fill this bar.
        clock.advance_to(UnixNanos(1_000_000_000));
        sim.on_market(&bar_vol(
            &id,
            "48800",
            "48850",
            "48820",
            "10",
            1_000_000_000,
        ));
        assert!(drain_fills(sim.drain_due(UnixNanos(1_500_000_000))).is_empty());
        // Bar 2: high 49000 >= the 48900 sell limit -> fills at the limit price.
        clock.advance_to(UnixNanos(2_000_000_000));
        sim.on_market(&bar_vol(
            &id,
            "48850",
            "49000",
            "48950",
            "10",
            2_000_000_000,
        ));
        let fills = drain_fills(sim.drain_due(UnixNanos(2_500_000_000)));
        assert_eq!(fills.len(), 1);
        assert_eq!(fills[0].0, Price::from_decimal(dec!(48900), 2).unwrap());
    }

    #[test]
    fn stop_limit_does_not_fill_below_its_limit() {
        // The price gaps through the stop AND the limit and never recovers -> no fill (the limit
        // floor protects against selling below 48900).
        let id = InstrumentId::parse("BTCUSDT.BINANCE").unwrap();
        let (clock, sim) = stop_limit_sim("49000", "48900");
        clock.advance_to(UnixNanos(1_000_000_000));
        sim.on_market(&bar_vol(
            &id,
            "48000",
            "48100",
            "48050",
            "10",
            1_000_000_000,
        )); // triggers
        clock.advance_to(UnixNanos(2_000_000_000));
        sim.on_market(&bar_vol(
            &id,
            "48000",
            "48200",
            "48100",
            "10",
            2_000_000_000,
        )); // stays below
        let fills = drain_fills(sim.drain_due(UnixNanos(2_500_000_000)));
        assert!(
            fills.is_empty(),
            "sell limit @48900 never crossed -> no fill"
        );
    }

    fn trailing_sim() -> (Rc<HistoricalClock>, SimulatedExecutionClient) {
        let (clock, cache, id, _inst) = setup(); // mark 50000
        let clock_dyn: Rc<dyn Clock> = clock.clone();
        let sim = SimulatedExecutionClient::new(
            Venue::from("BINANCE"),
            clock_dyn,
            cache,
            Box::new(DefaultBrokerageModel::default()),
        );
        let mut factory = OrderFactory::new(StrategyId::from("s1"));
        // SELL trailing stop with a 1000 offset: initial stop = mark(50000) - 1000 = 49000.
        let order = factory.trailing_stop(
            id,
            OrderSide::Sell,
            Quantity::from_decimal(dec!(1), 3).unwrap(),
            Price::from_decimal(dec!(49000), 2).unwrap(),
            UnixNanos(0),
        );
        sim.on_submit(order);
        let _ = sim.drain_due(UnixNanos(10_000_000));
        (clock, sim)
    }

    #[test]
    fn trailing_stop_ratchets_up_then_fills_on_pullback() {
        let id = InstrumentId::parse("BTCUSDT.BINANCE").unwrap();
        let (clock, sim) = trailing_sim();
        // Bar 1: price runs up to 52000 -> not hit; the stop ratchets to 52000-1000 = 51000.
        clock.advance_to(UnixNanos(1_000_000_000));
        sim.on_market(&bar_vol(
            &id,
            "51000",
            "52000",
            "51500",
            "10",
            1_000_000_000,
        ));
        assert!(drain_fills(sim.drain_due(UnixNanos(1_500_000_000))).is_empty());
        // Bar 2: pulls back to 50800, below the ratcheted 51000 stop -> fills near 51000.
        clock.advance_to(UnixNanos(2_000_000_000));
        sim.on_market(&bar_vol(
            &id,
            "50800",
            "51500",
            "51000",
            "10",
            2_000_000_000,
        ));
        let fills = drain_fills(sim.drain_due(UnixNanos(2_500_000_000)));
        assert_eq!(fills.len(), 1);
        // The trail locked in well above the 50000 entry (and far above the initial 49000 stop).
        assert!(fills[0].0 > Price::from_decimal(dec!(50000), 2).unwrap());
    }

    #[test]
    fn trailing_stop_does_not_fire_while_price_keeps_rising() {
        let id = InstrumentId::parse("BTCUSDT.BINANCE").unwrap();
        let (clock, sim) = trailing_sim();
        // Each bar makes a higher low -> the stop keeps trailing below the market, never hit.
        for (n, (lo, hi, c)) in [("51000", "52000", "51500"), ("52000", "53000", "52500")]
            .into_iter()
            .enumerate()
        {
            let t = (n as u64 + 1) * 1_000_000_000;
            clock.advance_to(UnixNanos(t));
            sim.on_market(&bar_vol(&id, lo, hi, c, "10", t));
            assert!(drain_fills(sim.drain_due(UnixNanos(t + 500_000_000))).is_empty());
        }
    }

    #[test]
    fn stop_market_sell_fills_through_a_gap_below_the_stop() {
        let id = InstrumentId::parse("BTCUSDT.BINANCE").unwrap();
        let (clock, sim) = stop_sim(OrderSide::Sell, "49000"); // stop-loss below mark
                                                               // A bar that GAPS below the 49000 stop (high 48500 < trigger) -> fills WORSE than the stop.
        clock.advance_to(UnixNanos(1_000_000_000));
        sim.on_market(&bar_vol(
            &id,
            "48000",
            "48500",
            "48200",
            "10",
            1_000_000_000,
        ));
        let fills = drain_fills(sim.drain_due(UnixNanos(1_500_000_000)));
        assert_eq!(fills.len(), 1);
        assert!(
            fills[0].0 < Price::from_decimal(dec!(49000), 2).unwrap(),
            "gap-down fills below stop"
        );
    }

    #[test]
    fn queue_position_through_cross_fills_immediately() {
        // Even with queue_ahead_factor 0.5, a price that trades THROUGH the level (low 48000 < limit
        // 49000) sweeps the book -> the order fills on the first crossing bar (no queue wait).
        let id = InstrumentId::parse("BTCUSDT.BINANCE").unwrap();
        let (clock, sim) = queue_sim(dec!(0.5), "1");
        clock.advance_to(UnixNanos(1_000_000_000));
        sim.on_market(&bar_vol(&id, "48000", "50000", "49500", "4", 1_000_000_000)); // low < limit
        assert_eq!(
            drain_fill_count(&sim, 1_500_000_000),
            1,
            "through-cross fills immediately"
        );
    }

    #[test]
    fn ohlc_market_slippage_scales_with_range_and_caps_at_extreme() {
        let (_clock, _cache, id, inst) = setup();
        let model = DefaultBrokerageModel::default();
        let mut factory = OrderFactory::new(StrategyId::from("s1"));
        let buy = factory.market(
            id.clone(),
            OrderSide::Buy,
            Quantity::from_decimal(dec!(1), 3).unwrap(),
            UnixNanos(0),
        );
        let refpx = Price::from_decimal(dec!(50000), 2).unwrap();

        // No bar range -> pure base-bps model (1 bp on 50000 = 5.0): 50005.
        let no_range = model.fill_price(&buy, refpx, None, &*inst);
        assert_eq!(no_range, Price::from_decimal(dec!(50005), 2).unwrap());

        // With a range, a buy slips UP (base + range component) but never above the bar high.
        let lo = Price::from_decimal(dec!(49000), 2).unwrap();
        let hi = Price::from_decimal(dec!(51000), 2).unwrap();
        let ranged = model.fill_price(&buy, refpx, Some((lo, hi)), &*inst);
        assert!(
            ranged > no_range,
            "range adds slippage: {ranged:?} !> {no_range:?}"
        );
        assert!(ranged <= hi, "buy fill capped at the bar high");

        // A high reference near the top forces the cap to bind exactly at the high.
        let near_top = Price::from_decimal(dec!(50950), 2).unwrap();
        let capped = model.fill_price(&buy, near_top, Some((lo, hi)), &*inst);
        assert_eq!(capped, hi, "buy fill price capped at the bar high");

        // Close AT the high: the range cap must NOT swallow the base slippage — a buy still pays
        // the base bps above the close (1 bp on 51000 = 5.10), i.e. just past the high.
        let at_high = model.fill_price(&buy, hi, Some((lo, hi)), &*inst);
        assert!(
            at_high > hi,
            "base slippage preserved even when close == high: {at_high:?}"
        );
        assert_eq!(at_high, Price::from_decimal(dec!(51005.10), 2).unwrap());
    }
}
