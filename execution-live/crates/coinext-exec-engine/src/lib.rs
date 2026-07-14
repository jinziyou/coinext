//! `coinext-exec-engine` — the OMS. Routes strategy order intents through the pre-trade RiskEngine to
//! the ExecutionClient, and folds `ExecutionReport`s back into the event-sourced Order FSM and the
//! Position. It TRACKS the OrderFactory-assigned `ClientOrderId` (never mints one). Backtest injects
//! `SimulatedExecutionClient`; sandbox/live swap a venue client behind the same `ExecutionClient` port.
//!
//! **A-share T+1:** for cash equities on SSE/SZSE, shares bought on calendar day D (UTC date of the
//! fill) cannot be sold until day D+1. The ledger is updated on buy fills and enforced at submit
//! (DenyReason::TPlusOne). US/HK equities are unaffected (day-trading allowed).
//!
//! **A-share 涨跌停:** for the same instruments, order prices (limit, or mark for market) must stay
//! within ±limit of the previous UTC day's last mark (DenyReason::PriceLimit). Main board ±10%,
//! ChiNext/STAR (300/688) ±20%, ST names ±5%.

use coinext_cache::Cache;
use coinext_core::UnixNanos;
use coinext_model::{
    AssetClass, InstrumentId, Order, OrderEvent, OrderSide, OrderType, Position, PositionId,
    PositionSide, TradeId,
};
use coinext_ports::{DenyReason, ExecutionReport, Portfolio, RiskDecision, RiskEngine};
use coinext_sim::SimulatedExecutionClient;
use rust_decimal::Decimal;
use std::cell::RefCell;
use std::collections::{HashMap, HashSet};
use std::rc::Rc;

pub struct ExecutionEngine {
    cache: Rc<RefCell<Cache>>,
    /// Trade ids whose Fill has already been folded into the Position — guards against a duplicate /
    /// replayed Fill double-counting size & realized PnL.
    seen_trade_ids: RefCell<HashSet<TradeId>>,
    /// A-share T+1 ledger: (instrument, yyyymmdd UTC) → qty bought that day (not yet free to sell).
    bought_today: RefCell<HashMap<(InstrumentId, u32), Decimal>>,
    /// Session-day last marks: (instrument, session_day_key) → last mark that day.
    /// Prev close for 涨跌停 = latest entry with day_key < today (skips empty weekend/holiday keys).
    close_by_day: RefCell<HashMap<(InstrumentId, u32), Decimal>>,
}

impl ExecutionEngine {
    pub fn new(cache: Rc<RefCell<Cache>>) -> Self {
        ExecutionEngine {
            cache,
            seen_trade_ids: RefCell::new(HashSet::new()),
            bought_today: RefCell::new(HashMap::new()),
            close_by_day: RefCell::new(HashMap::new()),
        }
    }

    /// SSE / SZSE cash equities: T+1 + daily price limits.
    fn is_ashare_equity(inst: &dyn coinext_model::Instrument) -> bool {
        if inst.asset_class() != AssetClass::Equity {
            return false;
        }
        matches!(inst.id().venue.as_str(), "SSE" | "SZSE")
    }

    fn is_t_plus_one(inst: &dyn coinext_model::Instrument) -> bool {
        Self::is_ashare_equity(inst)
    }

    fn day_key_utc(now: UnixNanos) -> u32 {
        (now.as_u64() / 1_000_000_000 / 86_400) as u32
    }

    /// Session day key for A-share rules: **Asia/Shanghai** (UTC+8, no DST).
    /// Matches `coinext_data.calendar.session_date(..., "SSE")`.
    fn day_key_shanghai(now: UnixNanos) -> u32 {
        let secs = now.as_u64() / 1_000_000_000 + 8 * 3600;
        (secs / 86_400) as u32
    }

    fn day_key_for_inst(inst: &dyn coinext_model::Instrument, now: UnixNanos) -> u32 {
        if Self::is_ashare_equity(inst) {
            Self::day_key_shanghai(now)
        } else {
            Self::day_key_utc(now)
        }
    }

    /// Limit fraction: ST 5%, ChiNext/STAR 20%, else main/ETF 10%.
    fn price_limit_pct(symbol: &str) -> Decimal {
        let s = symbol.to_ascii_uppercase();
        if s.starts_with("*ST") || s.starts_with("ST") {
            return Decimal::new(5, 2); // 0.05
        }
        let digits: String = s.chars().filter(|c| c.is_ascii_digit()).collect();
        if digits.len() >= 6 {
            let code = &digits[digits.len() - 6..];
            if code.starts_with("300")
                || code.starts_with("301")
                || code.starts_with("688")
                || code.starts_with("689")
            {
                return Decimal::new(20, 2); // 0.20
            }
        }
        Decimal::new(10, 2) // 0.10
    }

    /// Observe the cache mark for `id` at `now` (store session-day close).
    fn observe_mark(&self, id: &InstrumentId, now: UnixNanos) {
        let Some(mark) = self.cache.borrow().mark(id) else {
            return;
        };
        let day = {
            let cache = self.cache.borrow();
            if let Some(inst) = cache.instrument(id) {
                Self::day_key_for_inst(&*inst, now)
            } else {
                Self::day_key_shanghai(now)
            }
        };
        let px = mark.as_decimal();
        self.close_by_day.borrow_mut().insert((id.clone(), day), px);
    }

    /// Last mark on a session day strictly before ``day`` (walk back ≤ 40 day-keys).
    /// Empty keys (weekends / holidays with no prints) are skipped automatically.
    fn prev_close_for(&self, id: &InstrumentId, day: u32) -> Option<Decimal> {
        let map = self.close_by_day.borrow();
        for back in 1u32..=40 {
            if day < back {
                break;
            }
            if let Some(px) = map.get(&(id.clone(), day - back)) {
                if *px > Decimal::ZERO {
                    return Some(*px);
                }
            }
        }
        None
    }

    fn price_limit_violation(
        &self,
        inst: &dyn coinext_model::Instrument,
        order: &Order,
        now: UnixNanos,
    ) -> Option<String> {
        if !Self::is_ashare_equity(inst) {
            return None;
        }
        self.observe_mark(&order.instrument_id, now);
        let day = Self::day_key_for_inst(inst, now);
        let prev = self.prev_close_for(&order.instrument_id, day)?;
        if prev <= Decimal::ZERO {
            return None;
        }
        let pct = Self::price_limit_pct(order.instrument_id.symbol.as_str());
        // Round band to 0.01 (A-share tick) like coinext_broker.LimitBand.
        let up = (prev * (Decimal::ONE + pct)).round_dp(2);
        let down = (prev * (Decimal::ONE - pct)).round_dp(2);
        // Market: check mark; limit: check limit price.
        let ref_px = match order.order_type {
            OrderType::Limit | OrderType::StopLimit => order.price.map(|p| p.as_decimal()),
            _ => self
                .cache
                .borrow()
                .mark(&order.instrument_id)
                .map(|p| p.as_decimal()),
        }?;
        let ok = match order.side {
            OrderSide::Buy => ref_px <= up,
            OrderSide::Sell => ref_px >= down,
        };
        if ok {
            None
        } else {
            Some(format!(
                "{}: px {} outside [{}, {}] (prev_close={}, pct={})",
                DenyReason::PriceLimit,
                ref_px,
                down,
                up,
                prev,
                pct
            ))
        }
    }

    fn sellable_qty(
        &self,
        portfolio: &dyn Portfolio,
        id: &InstrumentId,
        now: UnixNanos,
        inst: &dyn coinext_model::Instrument,
    ) -> Decimal {
        let long = match portfolio.position(id) {
            Some(p) if p.side == PositionSide::Long => p.quantity.as_decimal(),
            _ => Decimal::ZERO,
        };
        let day = Self::day_key_for_inst(inst, now);
        let bought = self
            .bought_today
            .borrow()
            .get(&(id.clone(), day))
            .copied()
            .unwrap_or(Decimal::ZERO);
        (long - bought).max(Decimal::ZERO)
    }

    /// Submit an order: run the pre-trade risk gate, then route to the sim (or deny). Returns the
    /// order events applied (for strategy notification + bus publish).
    pub fn submit(
        &self,
        risk: &dyn RiskEngine,
        portfolio: &dyn Portfolio,
        sim: &SimulatedExecutionClient,
        mut order: Order,
        now: UnixNanos,
    ) -> Vec<OrderEvent> {
        // Idempotency on ClientOrderId: a re-submit of an already-tracked order must NOT clobber the
        // cached FSM state nor allocate a second venue order in the sim. Treat it as a no-op.
        if self.cache.borrow().order(&order.client_order_id).is_some() {
            return Vec::new();
        }
        let inst = match self.cache.borrow().instrument(&order.instrument_id) {
            Some(i) => i,
            None => return Vec::new(),
        };
        // A dated contract is dead once the clock reaches its expiry: deny any new order on it so a
        // post-expiry position can't be opened that the kernel's expiry settlement would then miss.
        if let Some(expiry) = inst.expiry_ns() {
            if now >= expiry {
                let ev = OrderEvent::Denied {
                    reason: "instrument expired".to_string(),
                    ts: now,
                };
                let _ = order.apply(ev.clone());
                self.cache.borrow_mut().add_order(order);
                return vec![ev];
            }
        }
        // A-share T+1: sell qty must be ≤ long − same-session-day buys (Shanghai date).
        if order.side == OrderSide::Sell && Self::is_t_plus_one(&*inst) {
            let sellable = self.sellable_qty(portfolio, &order.instrument_id, now, &*inst);
            if order.quantity.as_decimal() > sellable {
                let ev = OrderEvent::Denied {
                    reason: format!(
                        "{}: sellable {} < {}",
                        DenyReason::TPlusOne,
                        sellable,
                        order.quantity.as_decimal()
                    ),
                    ts: now,
                };
                let _ = order.apply(ev.clone());
                self.cache.borrow_mut().add_order(order);
                return vec![ev];
            }
        }
        // A-share 涨跌停 vs previous day's close.
        if let Some(reason) = self.price_limit_violation(&*inst, &order, now) {
            let ev = OrderEvent::Denied { reason, ts: now };
            let _ = order.apply(ev.clone());
            self.cache.borrow_mut().add_order(order);
            return vec![ev];
        }
        match risk.check(&order, portfolio, &*inst) {
            RiskDecision::Approved => {
                let ev = OrderEvent::Submitted { ts: now };
                let _ = order.apply(ev.clone());
                self.cache.borrow_mut().add_order(order.clone());
                sim.on_submit(order); // sim schedules Accepted + Fill on the delayed queue
                vec![ev]
            }
            RiskDecision::Denied(reason) => {
                let ev = OrderEvent::Denied {
                    reason: reason.to_string(),
                    ts: now,
                };
                let _ = order.apply(ev.clone());
                self.cache.borrow_mut().add_order(order);
                vec![ev]
            }
        }
    }

    /// Request cancellation of a resting order.
    pub fn cancel(
        &self,
        sim: &SimulatedExecutionClient,
        client_order_id: coinext_model::ClientOrderId,
        now: UnixNanos,
    ) -> Vec<OrderEvent> {
        let mut applied = Vec::new();
        if let Some(o) = self.cache.borrow_mut().order_mut(&client_order_id) {
            let ev = OrderEvent::PendingCancel { ts: now };
            if o.apply(ev.clone()).is_ok() {
                applied.push(ev);
            }
        }
        sim.on_cancel(client_order_id);
        applied
    }

    /// Fold an execution report into the cached order (FSM) and Position. Returns the order events
    /// applied so the kernel can notify the strategy and publish them.
    pub fn apply_report(&self, report: ExecutionReport, now: UnixNanos) -> Vec<OrderEvent> {
        let mut cache = self.cache.borrow_mut();
        let mut applied = Vec::new();
        match report {
            ExecutionReport::Accepted {
                client_order_id,
                venue_order_id,
            } => {
                if let Some(o) = cache.order_mut(&client_order_id) {
                    let ev = OrderEvent::Accepted {
                        venue_order_id,
                        ts: now,
                    };
                    if o.apply(ev.clone()).is_ok() {
                        applied.push(ev);
                    }
                }
            }
            ExecutionReport::Fill(fill) => {
                // Dedupe by trade_id: a replayed/duplicate Fill must not double-count into the
                // Position. A trade_id seen before is dropped entirely (FSM + Position).
                if !self
                    .seen_trade_ids
                    .borrow_mut()
                    .insert(fill.trade_id.clone())
                {
                    return applied;
                }
                let iid = fill.instrument_id.clone();
                let inst = cache.instrument(&iid);
                // 1) Fold into the order FSM (Filled vs PartiallyFilled by remaining qty).
                let fsm_ok = if let Some(o) = cache.order_mut(&fill.client_order_id) {
                    let full = fill.last_qty.as_decimal() >= o.leaves_qty().as_decimal();
                    let ev = if full {
                        OrderEvent::Filled(fill.clone())
                    } else {
                        OrderEvent::PartiallyFilled(fill.clone())
                    };
                    if o.apply(ev.clone()).is_ok() {
                        applied.push(ev);
                        true
                    } else {
                        false
                    }
                } else {
                    // Unknown order (never tracked) — the FSM rejected it; do not touch the Position.
                    false
                };
                // 2) Fold into the Position ONLY if the FSM accepted the fill. A late/duplicate fill
                // on an already-terminal or unknown order is dropped by the FSM and must NOT
                // double-count size & realized PnL here.
                if fsm_ok {
                    if let Some(inst) = inst {
                        // T+1 ledger: record same-day buy fills for SSE/SZSE equities.
                        if fill.side == OrderSide::Buy && Self::is_ashare_equity(&*inst) {
                            let day = Self::day_key_for_inst(&*inst, fill.ts_event);
                            let mut ledger = self.bought_today.borrow_mut();
                            let e = ledger.entry((iid.clone(), day)).or_insert(Decimal::ZERO);
                            *e += fill.last_qty.as_decimal();
                        }
                        let mut pos = cache.position(&iid).cloned().unwrap_or_else(|| {
                            Position::flat(
                                PositionId::from(format!("{iid}-POS")),
                                iid.clone(),
                                inst.price_precision(),
                                inst.size_precision(),
                                inst.settlement_currency(),
                            )
                        });
                        let _ = pos.apply_fill(&fill, &*inst);
                        cache.upsert_position(pos);
                    }
                }
            }
            ExecutionReport::Rejected {
                client_order_id,
                reason,
            } => {
                if let Some(o) = cache.order_mut(&client_order_id) {
                    let ev = OrderEvent::Rejected { reason, ts: now };
                    if o.apply(ev.clone()).is_ok() {
                        applied.push(ev);
                    }
                }
            }
            ExecutionReport::Canceled { client_order_id } => {
                if let Some(o) = cache.order_mut(&client_order_id) {
                    let ev = OrderEvent::Canceled { ts: now };
                    if o.apply(ev.clone()).is_ok() {
                        applied.push(ev);
                    }
                }
            }
            ExecutionReport::Expired { client_order_id } => {
                if let Some(o) = cache.order_mut(&client_order_id) {
                    let ev = OrderEvent::Expired { ts: now };
                    if o.apply(ev.clone()).is_ok() {
                        applied.push(ev);
                    }
                }
            }
            ExecutionReport::PendingUpdate { client_order_id } => {
                if let Some(o) = cache.order_mut(&client_order_id) {
                    let ev = OrderEvent::PendingUpdate { ts: now };
                    if o.apply(ev.clone()).is_ok() {
                        applied.push(ev);
                    }
                }
            }
            ExecutionReport::Modified {
                client_order_id,
                quantity,
                price,
            } => {
                if let Some(o) = cache.order_mut(&client_order_id) {
                    let ev = OrderEvent::Updated {
                        quantity,
                        price,
                        ts: now,
                    };
                    if o.apply(ev.clone()).is_ok() {
                        applied.push(ev);
                    }
                }
            }
            ExecutionReport::PendingCancel { client_order_id } => {
                if let Some(o) = cache.order_mut(&client_order_id) {
                    let ev = OrderEvent::PendingCancel { ts: now };
                    if o.apply(ev.clone()).is_ok() {
                        applied.push(ev);
                    }
                }
            }
        }
        applied
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use coinext_core::{Clock, Currency, HistoricalClock, Money, Price, Quantity};
    use coinext_model::{
        ClientOrderId, CurrencyPair, Fill, Instrument, InstrumentId, LiquiditySide, OrderSide,
        Position, PositionSide, StrategyId, TradeId,
    };
    use coinext_ports::{OrderFactory, Portfolio, RiskDecision, RiskEngine};
    use rust_decimal_macros::dec;
    use std::sync::Arc;

    /// Risk engine stub that always approves (kill-switch inert) — isolates the OMS logic under test.
    struct AlwaysApprove;
    impl RiskEngine for AlwaysApprove {
        fn check(&self, _o: &Order, _p: &dyn Portfolio, _i: &dyn Instrument) -> RiskDecision {
            RiskDecision::Approved
        }
        fn set_kill_switch(&self, _engaged: bool) {}
        fn is_killed(&self) -> bool {
            false
        }
    }

    /// Portfolio stub: flat everywhere (the risk stub ignores it anyway).
    struct FlatPortfolio(Currency);
    impl Portfolio for FlatPortfolio {
        fn position(&self, _id: &InstrumentId) -> Option<Position> {
            None
        }
        fn net_exposure(&self, _id: &InstrumentId) -> Money {
            Money::zero(self.0)
        }
        fn unrealized_pnl(&self, _id: &InstrumentId) -> Money {
            Money::zero(self.0)
        }
        fn realized_pnl(&self, _id: &InstrumentId) -> Money {
            Money::zero(self.0)
        }
        fn gross_exposure(&self) -> Money {
            Money::zero(self.0)
        }
        fn balance(&self, ccy: &Currency) -> Money {
            Money::zero(*ccy)
        }
        fn equity(&self) -> Money {
            Money::zero(self.0)
        }
    }

    fn setup() -> (Rc<RefCell<Cache>>, InstrumentId, Currency) {
        let usdt = Currency::new("USDT", 8).unwrap();
        let btc = Currency::new("BTC", 8).unwrap();
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
        cache.add_instrument(inst);
        cache.set_mark(id.clone(), Price::from_decimal(dec!(50000), 2).unwrap());
        (Rc::new(RefCell::new(cache)), id, usdt)
    }

    fn sim_for(cache: Rc<RefCell<Cache>>) -> SimulatedExecutionClient {
        let clock: Rc<dyn Clock> = Rc::new(HistoricalClock::new(UnixNanos(0)));
        SimulatedExecutionClient::new(
            coinext_model::Venue::from("BINANCE"),
            clock,
            cache,
            Box::new(coinext_sim::DefaultBrokerageModel::default()),
        )
    }

    fn fill(
        iid: &InstrumentId,
        coid: &ClientOrderId,
        trade: &str,
        side: OrderSide,
        px: &str,
        qty: &str,
        settle: Currency,
    ) -> Fill {
        Fill {
            trade_id: TradeId::from(trade),
            client_order_id: coid.clone(),
            venue_order_id: coinext_model::VenueOrderId::from("V-1"),
            instrument_id: iid.clone(),
            side,
            last_px: Price::from_decimal(px.parse().unwrap(), 2).unwrap(),
            last_qty: Quantity::from_decimal(qty.parse().unwrap(), 3).unwrap(),
            fee: Money::zero(settle),
            liquidity: LiquiditySide::Taker,
            ts_event: UnixNanos(0),
            ts_init: UnixNanos(0),
        }
    }

    // FIX 1: a duplicate submit on the same ClientOrderId is a no-op (one venue order, FSM intact).
    #[test]
    fn duplicate_submit_is_idempotent() {
        let (cache, id, usdt) = setup();
        let eng = ExecutionEngine::new(cache.clone());
        let sim = sim_for(cache.clone());
        let risk = AlwaysApprove;
        let pf = FlatPortfolio(usdt);
        let mut factory = OrderFactory::new(StrategyId::from("s1"));
        let order = factory.market(
            id.clone(),
            OrderSide::Buy,
            Quantity::from_decimal(dec!(1), 3).unwrap(),
            UnixNanos(0),
        );
        let coid = order.client_order_id.clone();

        let first = eng.submit(&risk, &pf, &sim, order.clone(), UnixNanos(0));
        assert_eq!(first.len(), 1, "first submit applies a Submitted event");

        // Re-submit the SAME order: must be a no-op (no clobber, no second venue order).
        let second = eng.submit(&risk, &pf, &sim, order, UnixNanos(0));
        assert!(second.is_empty(), "duplicate submit returns no events");

        // Drain the sim: exactly ONE venue order -> one Accepted + one Fill (not two of each).
        let reports = sim.drain_due(UnixNanos(10_000_000));
        let accepted = reports
            .iter()
            .filter(|r| matches!(r, ExecutionReport::Accepted { .. }))
            .count();
        assert_eq!(accepted, 1, "exactly one venue order allocated");

        // The cached FSM is still in the post-Submit state (not reset to a fresh Initialized order).
        let c = cache.borrow();
        let o = c.order(&coid).unwrap();
        assert!(
            !matches!(o.status, coinext_model::OrderStatus::Initialized),
            "FSM not clobbered back to Initialized"
        );
    }

    // FIX 2a: a duplicate trade_id folds into the Position exactly once.
    #[test]
    fn duplicate_trade_id_applied_once() {
        let (cache, id, usdt) = setup();
        let eng = ExecutionEngine::new(cache.clone());
        let sim = sim_for(cache.clone());
        let risk = AlwaysApprove;
        let pf = FlatPortfolio(usdt);
        let mut factory = OrderFactory::new(StrategyId::from("s1"));
        let order = factory.market(
            id.clone(),
            OrderSide::Buy,
            Quantity::from_decimal(dec!(2), 3).unwrap(),
            UnixNanos(0),
        );
        let coid = order.client_order_id.clone();
        let _ = eng.submit(&risk, &pf, &sim, order, UnixNanos(0));
        // Accept the order so the FSM will admit a fill.
        let _ = eng.apply_report(
            ExecutionReport::Accepted {
                client_order_id: coid.clone(),
                venue_order_id: coinext_model::VenueOrderId::from("V-1"),
            },
            UnixNanos(0),
        );

        let f = fill(&id, &coid, "T-1", OrderSide::Buy, "50000", "1", usdt);
        let _ = eng.apply_report(ExecutionReport::Fill(f.clone()), UnixNanos(0));
        // Replay the SAME trade_id — must be ignored.
        let again = eng.apply_report(ExecutionReport::Fill(f), UnixNanos(0));
        assert!(again.is_empty(), "duplicate trade_id produces no events");

        let c = cache.borrow();
        let pos = c.position(&id).unwrap();
        assert_eq!(
            pos.quantity,
            Quantity::from_decimal(dec!(1), 3).unwrap(),
            "position size counted the fill exactly once"
        );
        assert_eq!(pos.side, PositionSide::Long);
    }

    // FIX 2b: a fill for an UNKNOWN order does not mutate the Position (FSM rejects -> no fold).
    #[test]
    fn fill_for_unknown_order_does_not_touch_position() {
        let (cache, id, usdt) = setup();
        let eng = ExecutionEngine::new(cache.clone());
        let unknown = ClientOrderId::from("never-tracked");
        let f = fill(&id, &unknown, "T-99", OrderSide::Buy, "50000", "1", usdt);
        let ev = eng.apply_report(ExecutionReport::Fill(f), UnixNanos(0));
        assert!(ev.is_empty(), "no FSM event for an unknown order");
        assert!(
            cache.borrow().position(&id).is_none(),
            "position must remain untouched for an unknown order's fill"
        );
    }

    /// A-share T+1: same-day sell of newly bought shares is denied on SSE equities.
    #[test]
    fn ashare_t_plus_one_denies_same_day_sell() {
        use coinext_model::Equity;

        let cny = Currency::new("CNY", 2).unwrap();
        let id = InstrumentId::parse("600519.SSE").unwrap();
        let inst: Arc<dyn Instrument> = Arc::new(Equity {
            id: id.clone(),
            currency: cny,
            price_precision: 2,
            size_precision: 0,
            price_increment: Price::from_decimal(dec!(0.01), 2).unwrap(),
            size_increment: Quantity::from_decimal(dec!(1), 0).unwrap(),
            min_notional: None,
            maker_fee: dec!(0.00025),
            taker_fee: dec!(0.00075),
        });
        let mut raw = Cache::new();
        raw.add_instrument(inst);
        raw.set_mark(id.clone(), Price::from_decimal(dec!(1700), 2).unwrap());
        let cache = Rc::new(RefCell::new(raw));
        let eng = ExecutionEngine::new(cache.clone());
        let sim = SimulatedExecutionClient::new(
            coinext_model::Venue::from("SSE"),
            Rc::new(HistoricalClock::new(UnixNanos(0))) as Rc<dyn Clock>,
            cache.clone(),
            Box::new(coinext_sim::DefaultBrokerageModel::default()),
        );
        let risk = AlwaysApprove;
        let day0 = UnixNanos(86_400 * 1_000_000_000);

        let mut factory = OrderFactory::new(StrategyId::from("s1"));
        let buy = factory.market(
            id.clone(),
            OrderSide::Buy,
            Quantity::from_decimal(dec!(100), 0).unwrap(),
            day0,
        );
        let coid = buy.client_order_id.clone();
        let _ = eng.submit(&risk, &FlatPortfolio(cny), &sim, buy, day0);
        let _ = eng.apply_report(
            ExecutionReport::Accepted {
                client_order_id: coid.clone(),
                venue_order_id: coinext_model::VenueOrderId::from("V-1"),
            },
            day0,
        );
        let mut buy_fill = fill(&id, &coid, "T-buy", OrderSide::Buy, "1700", "100", cny);
        buy_fill.last_qty = Quantity::from_decimal(dec!(100), 0).unwrap();
        buy_fill.ts_event = day0;
        buy_fill.ts_init = day0;
        let _ = eng.apply_report(ExecutionReport::Fill(buy_fill), day0);
        assert!(cache.borrow().position(&id).is_some());

        struct CachePf(Rc<RefCell<Cache>>, Currency);
        impl Portfolio for CachePf {
            fn position(&self, id: &InstrumentId) -> Option<Position> {
                self.0.borrow().position(id).cloned()
            }
            fn net_exposure(&self, _id: &InstrumentId) -> Money {
                Money::zero(self.1)
            }
            fn unrealized_pnl(&self, _id: &InstrumentId) -> Money {
                Money::zero(self.1)
            }
            fn realized_pnl(&self, _id: &InstrumentId) -> Money {
                Money::zero(self.1)
            }
            fn gross_exposure(&self) -> Money {
                Money::zero(self.1)
            }
            fn balance(&self, ccy: &Currency) -> Money {
                Money::zero(*ccy)
            }
            fn equity(&self) -> Money {
                Money::zero(self.1)
            }
        }
        let pf = CachePf(cache.clone(), cny);

        let sell = factory.market(
            id.clone(),
            OrderSide::Sell,
            Quantity::from_decimal(dec!(100), 0).unwrap(),
            day0,
        );
        let denied = eng.submit(&risk, &pf, &sim, sell, day0);
        assert!(
            matches!(denied.as_slice(), [OrderEvent::Denied { reason, .. }] if reason.contains("TPlusOne")),
            "same-day sell denied: {denied:?}"
        );

        let day1 = UnixNanos(86_400 * 2 * 1_000_000_000);
        let sell2 = factory.market(
            id.clone(),
            OrderSide::Sell,
            Quantity::from_decimal(dec!(100), 0).unwrap(),
            day1,
        );
        let ok = eng.submit(&risk, &pf, &sim, sell2, day1);
        assert!(
            matches!(ok.as_slice(), [OrderEvent::Submitted { .. }]),
            "next-day sell allowed: {ok:?}"
        );
    }

    /// 涨跌停: limit buy above +10% of previous day's close is denied.
    #[test]
    fn ashare_price_limit_denies_limit_above_band() {
        use coinext_model::{Equity, OrderFlags, TimeInForce};

        let cny = Currency::new("CNY", 2).unwrap();
        let id = InstrumentId::parse("600519.SSE").unwrap();
        let inst: Arc<dyn Instrument> = Arc::new(Equity {
            id: id.clone(),
            currency: cny,
            price_precision: 2,
            size_precision: 0,
            price_increment: Price::from_decimal(dec!(0.01), 2).unwrap(),
            size_increment: Quantity::from_decimal(dec!(1), 0).unwrap(),
            min_notional: None,
            maker_fee: dec!(0.00025),
            taker_fee: dec!(0.00075),
        });
        let mut raw = Cache::new();
        raw.add_instrument(inst);
        let day0 = UnixNanos(86_400 * 1_000_000_000);
        let day1 = UnixNanos(86_400 * 2 * 1_000_000_000);
        raw.set_mark(id.clone(), Price::from_decimal(dec!(100), 2).unwrap());
        let cache = Rc::new(RefCell::new(raw));
        let eng = ExecutionEngine::new(cache.clone());
        let sim = SimulatedExecutionClient::new(
            coinext_model::Venue::from("SSE"),
            Rc::new(HistoricalClock::new(UnixNanos(0))) as Rc<dyn Clock>,
            cache.clone(),
            Box::new(coinext_sim::DefaultBrokerageModel::default()),
        );
        let risk = AlwaysApprove;
        let pf = FlatPortfolio(cny);
        let mut factory = OrderFactory::new(StrategyId::from("s1"));

        let seed = factory.limit(
            id.clone(),
            OrderSide::Buy,
            Quantity::from_decimal(dec!(1), 0).unwrap(),
            Price::from_decimal(dec!(100), 2).unwrap(),
            TimeInForce::Gtc,
            OrderFlags::default(),
            day0,
        );
        let _ = eng.submit(&risk, &pf, &sim, seed, day0); // seeds last_mark day0 @ 100

        cache
            .borrow_mut()
            .set_mark(id.clone(), Price::from_decimal(dec!(100), 2).unwrap());
        let bad = factory.limit(
            id.clone(),
            OrderSide::Buy,
            Quantity::from_decimal(dec!(100), 0).unwrap(),
            Price::from_decimal(dec!(111), 2).unwrap(),
            TimeInForce::Gtc,
            OrderFlags::default(),
            day1,
        );
        let denied = eng.submit(&risk, &pf, &sim, bad, day1);
        assert!(
            matches!(denied.as_slice(), [OrderEvent::Denied { reason, .. }] if reason.contains("PriceLimit")),
            "limit above band denied: {denied:?}"
        );

        let ok = factory.limit(
            id.clone(),
            OrderSide::Buy,
            Quantity::from_decimal(dec!(100), 0).unwrap(),
            Price::from_decimal(dec!(110), 2).unwrap(),
            TimeInForce::Gtc,
            OrderFlags::default(),
            day1,
        );
        let allowed = eng.submit(&risk, &pf, &sim, ok, day1);
        assert!(
            matches!(allowed.as_slice(), [OrderEvent::Submitted { .. }]),
            "at-limit buy allowed: {allowed:?}"
        );
    }
}
