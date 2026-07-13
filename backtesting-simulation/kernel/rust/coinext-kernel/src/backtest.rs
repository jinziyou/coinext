//! Deterministic backtest kernel (HistoricalClock + SimulatedExecutionClient).

use crate::{
    snapshot_portfolio, BacktestConfig, PortfolioSnapshot, RunResult,
};
use coinext_bus::InProcBus;
use coinext_cache::Cache;
use coinext_core::{Clock, Currency, HistoricalClock, Money, Price, UnixNanos};
use coinext_data_engine::DataEngine;
use coinext_exec_engine::ExecutionEngine;
use coinext_model::{
    AssetClass, ClientOrderId, Fill, Instrument, InstrumentId, LiquiditySide, MarketEvent,
    OrderEvent, OrderSide, PositionSide, StrategyId, TradeId, VenueOrderId,
};
use coinext_portfolio::PortfolioState;
use coinext_ports::{
    BusMsg, MessageBus, Portfolio, Strategy, StrategyCommand, StrategyContext, Topic,
};
use coinext_risk_engine::RiskGate;
use coinext_sim::SimulatedExecutionClient;
use rust_decimal::Decimal;
use std::cell::RefCell;
use std::rc::Rc;
use std::sync::Arc;

/// The backtest kernel. Owns every component; the shared `Cache`/`Clock` are `Rc` so the
/// StrategyContext, Portfolio, Risk and Sim all see the same state in this single-threaded core.
pub struct BacktestKernel {
    clock: Rc<HistoricalClock>,
    cache: Rc<RefCell<Cache>>,
    bus: InProcBus,
    data_engine: DataEngine,
    exec_engine: ExecutionEngine,
    risk: RiskGate,
    portfolio: PortfolioState,
    settle: Currency,
    sim: SimulatedExecutionClient,
    strategy: Box<dyn Strategy>,
    ctx: StrategyContext,
    events: Vec<MarketEvent>,
    cursor: usize,
    /// Dated contracts to settle, sorted by `expiry_ns`; `expiry_cursor` is the next unsettled.
    expiries: Vec<(UnixNanos, InstrumentId)>,
    expiry_cursor: usize,
    /// Maintenance margin as a fraction of gross notional (from RiskLimits); `None` = no liquidation.
    maintenance_margin_rate: Option<Decimal>,
    starting_equity: f64,
    result: RunResult,
}

impl BacktestKernel {
    /// Build a backtest. `events` need NOT be pre-sorted — they are sorted by `ts_event` here.
    pub fn build(
        config: BacktestConfig,
        strategy_id: StrategyId,
        strategy: Box<dyn Strategy>,
        mut events: Vec<MarketEvent>,
    ) -> Self {
        events.sort_by_key(|e| e.ts_event());

        // Dated contracts (futures / options) settle at their expiry; collect + sort the schedule.
        let mut expiries: Vec<(UnixNanos, InstrumentId)> = config
            .instruments
            .iter()
            .filter_map(|i| i.expiry_ns().map(|e| (e, i.id())))
            .collect();
        expiries.sort_by_key(|(ts, _)| *ts);
        let maintenance_margin_rate = config.risk.maintenance_margin_rate;

        let clock = Rc::new(HistoricalClock::new(config.start_ns));
        let cache = Rc::new(RefCell::new(Cache::new()));
        {
            let mut c = cache.borrow_mut();
            for inst in &config.instruments {
                c.add_instrument(inst.clone());
            }
            let mut account = coinext_model::AccountState::new(
                coinext_model::AccountId::from("BACKTEST"),
                config.settle,
            );
            account.set_balance(config.starting_balance);
            c.set_account(account);
        }

        let clock_dyn: Rc<dyn coinext_core::Clock> = clock.clone();
        let sim = SimulatedExecutionClient::new(
            config.venue.clone(),
            clock_dyn.clone(),
            cache.clone(),
            config.brokerage,
        );
        let risk = RiskGate::new(cache.clone(), config.risk);
        let portfolio = PortfolioState::new(cache.clone(), config.settle);
        let data_engine = DataEngine::new(cache.clone());
        let exec_engine = ExecutionEngine::new(cache.clone());
        let ctx = StrategyContext::new(strategy_id, clock_dyn, cache.clone());

        let starting_equity = config.starting_balance.as_f64();
        BacktestKernel {
            settle: config.settle,
            clock,
            cache,
            bus: InProcBus::new(),
            data_engine,
            exec_engine,
            risk,
            portfolio,
            sim,
            strategy,
            ctx,
            events,
            cursor: 0,
            expiries,
            expiry_cursor: 0,
            maintenance_margin_rate,
            starting_equity,
            result: RunResult {
                equity_curve: Vec::new(),
                fills: 0,
                fills_log: Vec::new(),
                orders_submitted: 0,
                orders_denied: 0,
                starting_equity,
                final_equity: starting_equity,
                realized_pnl: 0.0,
            },
        }
    }

    /// Access the in-process bus (e.g. to subscribe an analytics/observer before running).
    pub fn bus(&self) -> &InProcBus {
        &self.bus
    }

    /// Native portfolio snapshot exported to Python telemetry adapters.
    pub fn portfolio_snapshot(&self) -> PortfolioSnapshot {
        let cache = self.cache.borrow();
        snapshot_portfolio(&cache, self.settle)
    }

    /// Engage (or release) the global kill-switch on the authoritative RiskGate. Once engaged,
    /// `RiskGate::check` denies EVERY subsequent order (`DenyReason::KillSwitchEngaged`) until it is
    /// released, halting new exposure in-process. (Cross-service bus/exec-svc propagation is a live
    /// concern handled where those stubs are wired.)
    pub fn set_kill_switch(&self, engaged: bool) {
        use coinext_ports::RiskEngine;
        self.risk.set_kill_switch(engaged);
    }

    /// Current portfolio equity = starting balance + realized + unrealized (settlement ccy, f64).
    fn equity(&self) -> f64 {
        let cache = self.cache.borrow();
        let mut realized = 0.0;
        let mut unreal = 0.0;
        for pos in cache.positions() {
            realized += pos.realized_pnl.as_f64();
            if let (Some(inst), Some(mark)) = (
                cache.instrument(&pos.instrument_id),
                cache.mark(&pos.instrument_id),
            ) {
                unreal += pos.unrealized_pnl(mark, &*inst).as_f64();
            }
        }
        self.starting_equity + realized + unreal
    }

    fn realized_total(&self) -> f64 {
        self.cache
            .borrow()
            .positions()
            .map(|p| p.realized_pnl.as_f64())
            .sum()
    }

    /// Run to completion and return the result.
    pub fn run(&mut self) -> RunResult {
        self.strategy.on_start(&mut self.ctx);
        self.process_outbox();

        loop {
            let next_market = self.events.get(self.cursor).map(|e| e.ts_event());
            let next_sim = self.sim.peek_due();
            let next_timer = self.clock.peek_next_timer();
            let next_expiry = self.expiries.get(self.expiry_cursor).map(|(ts, _)| *ts);

            let frontier = [next_market, next_sim, next_timer, next_expiry]
                .into_iter()
                .flatten()
                .min();
            let Some(frontier) = frontier else { break };
            self.clock.advance_to(frontier);

            // 1) Drain delayed execution reports due at/before the frontier.
            let reports = self.sim.drain_due(frontier);
            for r in reports {
                let now = self.clock.now_ns();
                let events = self.exec_engine.apply_report(r, now);
                for ev in &events {
                    if let OrderEvent::Filled(f) | OrderEvent::PartiallyFilled(f) = ev {
                        self.result.fills += 1;
                        self.result.fills_log.push((
                            f.ts_event.as_u64(),
                            f.instrument_id.symbol.as_str().to_string(),
                            f.side.sign() as i8,
                            f.last_qty.as_f64(),
                            f.last_px.as_f64(),
                        ));
                    }
                    self.notify_event(ev);
                }
            }
            self.process_outbox();

            // 2) Fire due timers.
            let timers = self.clock.pop_due(frontier);
            for t in timers {
                self.strategy.on_timer(&t, &mut self.ctx);
                self.bus.publish(
                    Topic::Timer(self.ctx.strategy_id.clone()),
                    BusMsg::Timer(Arc::new(t)),
                );
                self.process_outbox();
            }

            // 3) Process the market event at the frontier (if any).
            if next_market == Some(frontier) {
                let ev = self.events[self.cursor].clone();
                self.cursor += 1;
                self.data_engine.process(&ev, &self.bus);
                self.sim.on_market(&ev);
                self.dispatch_market(&ev);
                self.process_outbox();
                // Sample the equity curve at BAR cadence only. Quote/trade ticks (when fed) move the
                // mark intrabar but must not add sub-bar (often same-timestamp, zero-return) points
                // that would distort the per-bar annualized metrics. Bar-only backtests are
                // unaffected (every market event is a bar).
                if matches!(ev, MarketEvent::Bar(_)) {
                    let ts = frontier.as_u64();
                    let eq = self.equity();
                    self.result.equity_curve.push((ts, eq));
                }
            }

            // 4) Settle any dated contracts expiring at/before the frontier (AFTER the market event
            // at this ts, so the final mark / underlying spot is in the cache).
            self.settle_expiries(frontier);

            // 5) Mark-to-market maintenance-margin check: liquidate if equity has fallen below the
            // maintenance requirement (only when a leverage/maintenance model is configured).
            self.check_liquidation(frontier);
        }

        self.strategy.on_stop(&mut self.ctx);
        self.process_outbox();

        self.result.final_equity = self.equity();
        self.result.realized_pnl = self.realized_total();
        self.result.clone()
    }

    fn dispatch_market(&mut self, ev: &MarketEvent) {
        match ev {
            MarketEvent::Quote(q) => self.strategy.on_quote(q, &mut self.ctx),
            MarketEvent::Trade(t) => self.strategy.on_trade(t, &mut self.ctx),
            MarketEvent::Bar(b) => self.strategy.on_bar(b, &mut self.ctx),
            MarketEvent::Delta(d) => {
                // The DataEngine already folded this delta into the cached L2 book; hand the strategy
                // the maintained book (by reference — the cache borrow is held only over the handler,
                // which queues commands via the outbox and never re-borrows the cache mutably).
                let cache = self.cache.borrow();
                if let Some(book) = cache.order_book(&d.instrument_id) {
                    self.strategy.on_book(book, &mut self.ctx);
                }
            }
        }
    }

    fn notify_event(&mut self, ev: &OrderEvent) {
        match ev {
            OrderEvent::Filled(f) | OrderEvent::PartiallyFilled(f) => {
                self.strategy.on_order_filled(f, &mut self.ctx);
                self.strategy.on_order_event(ev, &mut self.ctx);
            }
            _ => self.strategy.on_order_event(ev, &mut self.ctx),
        }
        self.bus.publish(
            Topic::OrderEvent(self.ctx.strategy_id.clone()),
            BusMsg::Order(Arc::new(ev.clone())),
        );
    }

    fn process_outbox(&mut self) {
        let cmds = self.ctx.drain_outbox();
        if cmds.is_empty() {
            return;
        }
        let now = self.clock.now_ns();
        for cmd in cmds {
            match cmd {
                StrategyCommand::Submit(order) => {
                    self.result.orders_submitted += 1;
                    let events =
                        self.exec_engine
                            .submit(&self.risk, &self.portfolio, &self.sim, order, now);
                    for ev in &events {
                        if matches!(ev, OrderEvent::Denied { .. }) {
                            self.result.orders_denied += 1;
                        }
                        self.notify_event(ev);
                    }
                }
                StrategyCommand::Cancel(coid) => {
                    let events = self.exec_engine.cancel(&self.sim, coid, now);
                    for ev in &events {
                        self.notify_event(ev);
                    }
                }
                StrategyCommand::Modify { .. } => {
                    // Order modify is not modeled in the sim; live venue path handles cancel-replace.
                }
            }
        }
    }

    /// Settle every dated contract whose expiry is at/before `frontier` and not yet settled.
    fn settle_expiries(&mut self, frontier: UnixNanos) {
        while let Some((ts, iid)) = self.expiries.get(self.expiry_cursor).cloned() {
            if ts > frontier {
                break;
            }
            self.expiry_cursor += 1;
            self.settle_contract(&iid, frontier);
        }
    }

    /// Settle one expiring contract: close any open position at its settlement price (a future cash-
    /// settles to its final mark; an option settles to its intrinsic value vs the underlying spot,
    /// expiring worthless if out-of-the-money), then cancel any resting orders on the dead contract.
    fn settle_contract(&mut self, iid: &InstrumentId, now: UnixNanos) {
        // Resolve the instrument, its open position, and the settlement price (immutable borrow).
        let (inst, pos, settle_px) = {
            let cache = self.cache.borrow();
            let Some(inst) = cache.instrument(iid) else {
                return;
            };
            let pos = cache.position(iid).cloned();
            let settle_px = match inst.asset_class() {
                AssetClass::Option => {
                    // Intrinsic from the underlying's spot at expiry; fall back to the option's own
                    // last mark only if the underlying price is unavailable.
                    match (
                        inst.strike(),
                        inst.option_right(),
                        // The underlying's spot — but never the option's OWN mark (a self-referential
                        // underlying would read the premium as spot); fall back to own mark then.
                        inst.underlying()
                            .filter(|u| u != iid)
                            .and_then(|u| cache.mark(&u)),
                    ) {
                        (Some(k), Some(right), Some(spot)) => {
                            let intr = right.intrinsic(spot.as_decimal(), k.as_decimal());
                            Price::from_decimal(intr, inst.price_precision()).ok()
                        }
                        _ => cache.mark(iid),
                    }
                }
                // Futures (and any other dated contract) cash-settle to their final mark.
                _ => cache.mark(iid),
            };
            (inst, pos, settle_px)
        };

        // Close the open position with a synthetic settlement fill at the settlement price.
        let _ = (inst, pos); // resolved above only to gate settlement; close re-reads the cache.
        if let Some(settle_px) = settle_px {
            self.close_position_at(iid, settle_px, now, "SETTLE");
        }

        // The contract is dead: cancel any of the strategy's resting orders on it.
        let open: Vec<ClientOrderId> = {
            let cache = self.cache.borrow();
            cache
                .orders()
                .filter(|o| &o.instrument_id == iid && !o.status.is_terminal())
                .map(|o| o.client_order_id.clone())
                .collect()
        };
        for coid in open {
            let events = self.exec_engine.cancel(&self.sim, coid, now);
            for ev in &events {
                self.notify_event(ev);
            }
        }
    }

    /// Close any open position in `iid` at price `px` with a synthetic fill (used by expiry
    /// settlement and liquidation). `tag_suffix` distinguishes the source (`SETTLE` / `LIQ`).
    fn close_position_at(
        &mut self,
        iid: &InstrumentId,
        px: Price,
        now: UnixNanos,
        tag_suffix: &str,
    ) {
        let (inst, pos) = {
            let cache = self.cache.borrow();
            (cache.instrument(iid), cache.position(iid).cloned())
        };
        let (Some(inst), Some(mut pos)) = (inst, pos) else {
            return;
        };
        let close_side = match pos.side {
            PositionSide::Long => OrderSide::Sell,
            PositionSide::Short => OrderSide::Buy,
            PositionSide::Flat => return,
        };
        let qty = pos.quantity;
        let tag = format!("{iid}-{tag_suffix}");
        let fill = Fill {
            trade_id: TradeId::from(tag.as_str()),
            client_order_id: ClientOrderId::from(tag.as_str()),
            venue_order_id: VenueOrderId::from(tag.as_str()),
            instrument_id: iid.clone(),
            side: close_side,
            last_px: px,
            last_qty: qty,
            fee: Money::zero(inst.settlement_currency()),
            liquidity: LiquiditySide::Taker,
            ts_event: now,
            ts_init: now,
        };
        {
            let mut cache = self.cache.borrow_mut();
            let _ = pos.apply_fill(&fill, &*inst);
            cache.upsert_position(pos);
        }
        self.result.fills += 1;
        self.result.fills_log.push((
            now.as_u64(),
            iid.symbol.as_str().to_string(),
            close_side.sign() as i8,
            qty.as_f64(),
            px.as_f64(),
        ));
        self.notify_event(&OrderEvent::Filled(fill));
        self.process_outbox();
    }

    /// Liquidate the account if mark-to-market equity has fallen below the maintenance requirement
    /// (`gross notional × maintenance_margin_rate`): force-flatten every open position at its mark.
    /// Runs CONTINUOUSLY each bar (it naturally no-ops once flat, since `gross` is then 0), so a
    /// position re-opened after a prior liquidation is still protected. No-op unless a maintenance
    /// margin rate is configured.
    fn check_liquidation(&mut self, now: UnixNanos) {
        let Some(rate) = self.maintenance_margin_rate else {
            return;
        };
        let equity = self.portfolio.equity().amount();
        let gross = self.portfolio.gross_exposure().amount();
        if gross <= Decimal::ZERO || equity >= gross * rate {
            return;
        }
        let iids: Vec<InstrumentId> = {
            let cache = self.cache.borrow();
            cache
                .positions()
                .filter(|p| p.side != PositionSide::Flat)
                .map(|p| p.instrument_id.clone())
                .collect()
        };
        for iid in iids {
            let mark = self.cache.borrow().mark(&iid);
            if let Some(mark) = mark {
                self.close_position_at(&iid, mark, now, "LIQ");
            }
        }
    }
}

