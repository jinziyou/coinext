//! `coinext-kernel` — the single place backtest vs live differs, and the deterministic synchronous core
//! loop. For backtest the [`BacktestKernel`] merge-sorts four event sources by timestamp — incoming
//! market data, due delayed execution reports from the sim's DelayedEventQueue, due timers from the
//! HistoricalClock, and due dated-contract expiry/settlement from the expiry schedule — and
//! dispatches each to the engines and the Strategy SYNCHRONOUSLY.
//!
//! For sandbox/live the [`LiveKernel`] runs the SAME engine set + Strategy, but driven by the
//! `coinext_ports::DataClient`/`ExecutionClient` PORTS (market events + execution reports arrive over
//! `tokio::mpsc` instead of the inherent sim queue). [`Environment`] selects which kernel is used;
//! only the Clock and the Data/Execution clients change — the OMS/Risk/Portfolio/Strategy above the
//! `ExecutionClient` seam are byte-for-byte identical (the parity invariant). NOTE: the live path is
//! a structural scaffold — it consumes the ports and folds reports through the shared engines, but
//! end-to-end live/sandbox trading against a real venue is not yet exercised.

use coinext_bus::InProcBus;
use coinext_cache::Cache;
use coinext_core::{Clock, Currency, HistoricalClock, Money, Price, UnixNanos};
use coinext_data_engine::DataEngine;
use coinext_exec_engine::ExecutionEngine;
use coinext_model::{
    AssetClass, ClientOrderId, Fill, Instrument, InstrumentId, LiquiditySide, MarketEvent,
    OrderEvent, OrderSide, PositionSide, StrategyId, TradeId, Venue, VenueOrderId,
};
use coinext_portfolio::PortfolioState;
use coinext_ports::{
    BusMsg, MessageBus, Portfolio, RiskLimits, Strategy, StrategyCommand, StrategyContext, Topic,
};
use coinext_risk_engine::RiskGate;
use coinext_sim::{BrokerageModel, DefaultBrokerageModel, SimulatedExecutionClient};
use rust_decimal::Decimal;
use std::cell::RefCell;
use std::rc::Rc;
use std::sync::Arc;

/// Environment the kernel targets. `Backtest` runs the deterministic [`BacktestKernel`];
/// `Sandbox`/`Live` run the [`LiveKernel`], which consumes the SAME engine set through the
/// `ExecutionClient`/`DataClient` ports (the shared seam).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Environment {
    Backtest,
    Sandbox,
    Live,
}

impl Environment {
    /// True for the port-driven live/sandbox path (everything except deterministic backtest).
    pub fn is_live(self) -> bool {
        matches!(self, Environment::Sandbox | Environment::Live)
    }
}

/// Backtest wiring configuration.
pub struct BacktestConfig {
    pub venue: Venue,
    pub instruments: Vec<Arc<dyn Instrument>>,
    pub settle: Currency,
    pub starting_balance: Money,
    pub risk: RiskLimits,
    pub brokerage: Box<dyn BrokerageModel>,
    pub start_ns: UnixNanos,
}

impl BacktestConfig {
    /// Construct with UNLIMITED risk limits — an explicit, test/example-friendly default. Production
    /// wiring should prefer [`BacktestConfig::with_env_risk`] (or set `.risk` explicitly) so the
    /// notional/position/gross/leverage limits are actually enforced rather than inert.
    pub fn new(
        venue: Venue,
        instruments: Vec<Arc<dyn Instrument>>,
        settle: Currency,
        starting_balance: Money,
    ) -> Self {
        BacktestConfig {
            venue,
            instruments,
            settle,
            starting_balance,
            risk: RiskLimits::unlimited(),
            brokerage: Box::new(DefaultBrokerageModel::default()),
            start_ns: UnixNanos::ZERO,
        }
    }

    /// Like [`BacktestConfig::new`] but populates the risk limits from the environment
    /// (`RiskLimits::from_env`), so configured notional/position/gross/leverage caps are enforced.
    pub fn with_env_risk(
        venue: Venue,
        instruments: Vec<Arc<dyn Instrument>>,
        settle: Currency,
        starting_balance: Money,
    ) -> Self {
        let mut cfg = Self::new(venue, instruments, settle, starting_balance);
        cfg.risk = RiskLimits::from_env(settle);
        cfg
    }
}

/// Result of a backtest run.
#[derive(Debug, Clone)]
pub struct RunResult {
    /// (ts_ns, equity) sampled once per processed bar — the input to the tear sheet.
    pub equity_curve: Vec<(u64, f64)>,
    pub fills: u64,
    /// Per-fill log `(ts_ns, symbol, side[+1 buy/-1 sell], qty, price)`. The `symbol` lets analytics
    /// reconstruct round-trip trades PER instrument (FIFO must not match across instruments); the
    /// parity gate compares these to bound realized-vs-simulated fill-price deviation.
    pub fills_log: Vec<(u64, String, i8, f64, f64)>,
    pub orders_submitted: u64,
    pub orders_denied: u64,
    pub starting_equity: f64,
    pub final_equity: f64,
    pub realized_pnl: f64,
}

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
            MarketEvent::Delta(_) => {}
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
                    // Modify is not modeled by the scaffold sim; live path handles it.
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

/// The live/sandbox kernel — the SAME engine set + Strategy as [`BacktestKernel`], driven by the
/// `ExecutionClient`/`DataClient` PORTS instead of the inherent sim API. This is the structural
/// enforcement of the parity seam: market data and execution reports flow over the ports, while the
/// DataEngine / ExecutionEngine / RiskGate / Portfolio / Strategy above them are byte-for-byte the
/// same code the backtest runs.
///
/// It is generic over no concrete venue (it holds `Box<dyn ExecutionClient>` + `Box<dyn DataClient>`),
/// so it compiles in the default workspace build WITHOUT pulling in the excluded venue/network crates;
/// a real venue (e.g. `coinext-adapters-binance`) is injected by the live service that owns those deps.
///
/// SCAFFOLD: the run loop is single-threaded (the core is `Rc`/`RefCell`, deterministic-by-design),
/// connects the clients, takes their streams, and drains BOTH — folding reports through the same OMS
/// `apply_report` and dispatching market events to the same DataEngine + Strategy. It does not yet
/// implement reconnect/reconcile or out-of-band kill-switch control; those are live-ops concerns
/// wired by `coinext-exec-svc`. Crucially, `Environment` is now MATCHED and the live path CONSUMES
/// the port, so the shared seam is structurally real rather than aspirational.
pub struct LiveKernel {
    env: Environment,
    clock: Rc<dyn Clock>,
    cache: Rc<RefCell<Cache>>,
    bus: InProcBus,
    data_engine: DataEngine,
    exec_engine: ExecutionEngine,
    risk: RiskGate,
    portfolio: PortfolioState,
    strategy: Box<dyn Strategy>,
    ctx: StrategyContext,
    exec_client: Box<dyn coinext_ports::ExecutionClient>,
    data_client: Box<dyn coinext_ports::DataClient>,
}

impl LiveKernel {
    /// Build a live/sandbox kernel. Panics if `env` is `Backtest` (use [`BacktestKernel`] for that —
    /// the two paths are intentionally distinct: backtest is deterministic-synchronous, live is
    /// port-driven). The clock is the wall clock; the clients are the injected venue ports.
    pub fn build(
        env: Environment,
        config: BacktestConfig,
        strategy_id: StrategyId,
        strategy: Box<dyn Strategy>,
        exec_client: Box<dyn coinext_ports::ExecutionClient>,
        data_client: Box<dyn coinext_ports::DataClient>,
    ) -> Self {
        assert!(
            env.is_live(),
            "LiveKernel requires Environment::Sandbox or ::Live; use BacktestKernel for Backtest"
        );
        let clock: Rc<dyn Clock> = Rc::new(coinext_core::SystemClock::new());
        let cache = Rc::new(RefCell::new(Cache::new()));
        {
            let mut c = cache.borrow_mut();
            for inst in &config.instruments {
                c.add_instrument(inst.clone());
            }
            let mut account = coinext_model::AccountState::new(
                coinext_model::AccountId::from("LIVE"),
                config.settle,
            );
            account.set_balance(config.starting_balance);
            c.set_account(account);
        }
        let risk = RiskGate::new(cache.clone(), config.risk);
        let portfolio = PortfolioState::new(cache.clone(), config.settle);
        let data_engine = DataEngine::new(cache.clone());
        let exec_engine = ExecutionEngine::new(cache.clone());
        let ctx = StrategyContext::new(strategy_id, clock.clone(), cache.clone());
        LiveKernel {
            env,
            clock,
            cache,
            bus: InProcBus::new(),
            data_engine,
            exec_engine,
            risk,
            portfolio,
            strategy,
            ctx,
            exec_client,
            data_client,
        }
    }

    /// The environment this kernel targets (`Sandbox` or `Live`).
    pub fn environment(&self) -> Environment {
        self.env
    }

    pub fn bus(&self) -> &InProcBus {
        &self.bus
    }

    /// Connect the clients, take their streams, and drain the unified event loop until both streams
    /// close. Market events feed the DataEngine + Strategy; execution reports are folded through the
    /// SAME OMS `apply_report`; strategy order intents are routed through the RiskGate to the
    /// `ExecutionClient` port. Single-threaded by design (the core is `Rc`/`RefCell`).
    pub async fn run(&mut self) -> coinext_ports::PortResult<()> {
        // Connect both ports, then take their (single-consumer) streams.
        self.data_client.connect().await?;
        self.exec_client.connect().await?;
        let mut market_rx = self.data_client.take_stream();
        let mut report_rx = self.exec_client.take_reports();

        // On startup reconcile venue truth into the OMS before accepting new flow (no-op for the sim).
        let now = self.clock.now_ns();
        for report in self.exec_client.reconcile().await? {
            for ev in self.exec_engine.apply_report(report, now) {
                self.notify_event(&ev);
            }
        }

        self.strategy.on_start(&mut self.ctx);
        self.route_outbox().await?;

        // Drain both inbound streams until both close. Reports are folded first (so a fill is visible
        // before the next strategy decision), then the market event drives the Strategy.
        loop {
            tokio::select! {
                biased;
                report = report_rx.recv() => match report {
                    Some(report) => {
                        let now = self.clock.now_ns();
                        for ev in self.exec_engine.apply_report(report, now) {
                            self.notify_event(&ev);
                        }
                        self.route_outbox().await?;
                    }
                    None => break,
                },
                market = market_rx.recv() => match market {
                    Some(ev) => {
                        self.data_engine.process(&ev, &self.bus);
                        self.dispatch_market(&ev);
                        self.route_outbox().await?;
                    }
                    None => break,
                },
            }
        }

        self.strategy.on_stop(&mut self.ctx);
        self.route_outbox().await?;
        self.data_client.disconnect().await?;
        self.exec_client.disconnect().await?;
        Ok(())
    }

    fn dispatch_market(&mut self, ev: &MarketEvent) {
        match ev {
            MarketEvent::Quote(q) => self.strategy.on_quote(q, &mut self.ctx),
            MarketEvent::Trade(t) => self.strategy.on_trade(t, &mut self.ctx),
            MarketEvent::Bar(b) => self.strategy.on_bar(b, &mut self.ctx),
            MarketEvent::Delta(_) => {}
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

    /// Drain the strategy outbox, running each intent through the SAME pre-trade RiskGate as backtest
    /// and routing approved orders/cancels to the `ExecutionClient` PORT (not the inherent sim API).
    async fn route_outbox(&mut self) -> coinext_ports::PortResult<()> {
        let cmds = self.ctx.drain_outbox();
        if cmds.is_empty() {
            return Ok(());
        }
        let now = self.clock.now_ns();
        for cmd in cmds {
            match cmd {
                StrategyCommand::Submit(mut order) => {
                    // Idempotency: a re-submit of an already-tracked order is a no-op (mirrors the OMS).
                    if self.cache.borrow().order(&order.client_order_id).is_some() {
                        continue;
                    }
                    let inst = self.cache.borrow().instrument(&order.instrument_id);
                    let Some(inst) = inst else { continue };
                    let decision = {
                        use coinext_ports::RiskEngine;
                        self.risk.check(&order, &self.portfolio, &*inst)
                    };
                    match decision {
                        coinext_ports::RiskDecision::Approved => {
                            let ev = OrderEvent::Submitted { ts: now };
                            let _ = order.apply(ev.clone());
                            self.cache.borrow_mut().add_order(order.clone());
                            self.notify_event(&ev);
                            self.exec_client
                                .submit_order(coinext_ports::SubmitOrder { order })
                                .await?;
                        }
                        coinext_ports::RiskDecision::Denied(reason) => {
                            let ev = OrderEvent::Denied {
                                reason: reason.to_string(),
                                ts: now,
                            };
                            let _ = order.apply(ev.clone());
                            self.cache.borrow_mut().add_order(order);
                            self.notify_event(&ev);
                        }
                    }
                }
                StrategyCommand::Cancel(coid) => {
                    let iid = self
                        .cache
                        .borrow()
                        .order(&coid)
                        .map(|o| o.instrument_id.clone());
                    if let Some(instrument_id) = iid {
                        let ev = OrderEvent::PendingCancel { ts: now };
                        let applied = self
                            .cache
                            .borrow_mut()
                            .order_mut(&coid)
                            .map(|o| o.apply(ev.clone()).is_ok())
                            .unwrap_or(false);
                        if applied {
                            self.notify_event(&ev);
                        }
                        self.exec_client
                            .cancel_order(coinext_ports::CancelOrder {
                                client_order_id: coid,
                                instrument_id,
                            })
                            .await?;
                    }
                }
                StrategyCommand::Modify { .. } => {
                    // Cancel-replace on the live venue; not modeled here in the scaffold.
                }
            }
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use coinext_core::{Price, Quantity};
    use coinext_model::{
        AggregationSource, Bar, BarAggregation, BarSpec, BarType, CurrencyPair, FuturesContract,
        InstrumentId, OptionContract, OptionRight, OrderSide, PriceType,
    };
    use rust_decimal_macros::dec;

    struct BuyOnceStrategy {
        iid: InstrumentId,
        bought: bool,
    }
    impl Strategy for BuyOnceStrategy {
        fn on_bar(&mut self, _b: &Bar, ctx: &mut StrategyContext) {
            if !self.bought {
                self.bought = true;
                ctx.submit_market(
                    self.iid.clone(),
                    OrderSide::Buy,
                    Quantity::from_decimal(dec!(1), 3).unwrap(),
                );
            }
        }
    }

    /// Buys one contract of a SPECIFIC instrument on that instrument's first bar (so its mark is set
    /// before the market order, even when other instruments share the timestamp).
    struct BuyContractOnce {
        target: InstrumentId,
        bought: bool,
    }
    impl Strategy for BuyContractOnce {
        fn on_bar(&mut self, b: &Bar, ctx: &mut StrategyContext) {
            if !self.bought && b.bar_type.instrument_id == self.target {
                self.bought = true;
                ctx.submit_market(
                    self.target.clone(),
                    OrderSide::Buy,
                    Quantity::from_decimal(dec!(1), 3).unwrap(),
                );
            }
        }
    }

    /// Buys one contract whenever flat, up to `max_buys` times — used to prove liquidation re-arms.
    struct BuyWhenFlat {
        iid: InstrumentId,
        buys: usize,
        max_buys: usize,
    }
    impl Strategy for BuyWhenFlat {
        fn on_bar(&mut self, _b: &Bar, ctx: &mut StrategyContext) {
            let flat = ctx
                .position(&self.iid)
                .map(|p| p.side == PositionSide::Flat)
                .unwrap_or(true);
            if flat && self.buys < self.max_buys {
                self.buys += 1;
                ctx.submit_market(
                    self.iid.clone(),
                    OrderSide::Buy,
                    Quantity::from_decimal(dec!(1), 3).unwrap(),
                );
            }
        }
    }

    fn opt_inst(
        strike: &str,
        right: OptionRight,
        expiry: u64,
        under: InstrumentId,
    ) -> Arc<dyn Instrument> {
        let usdt = Currency::new("USDT", 8).unwrap();
        Arc::new(OptionContract {
            id: InstrumentId::parse("BTCC.DERIBIT").unwrap(),
            underlying: under,
            quote: usdt,
            settlement: usdt,
            price_precision: 2,
            size_precision: 3,
            price_increment: Price::from_decimal(dec!(0.01), 2).unwrap(),
            size_increment: Quantity::from_decimal(dec!(0.001), 3).unwrap(),
            min_notional: None,
            multiplier: Quantity::from_raw(1, 0).unwrap(),
            maker_fee: dec!(0), // zero fees so settlement PnL is exact
            taker_fee: dec!(0),
            strike: Price::from_decimal(strike.parse().unwrap(), 2).unwrap(),
            right,
            expiry_ns: UnixNanos(expiry),
        })
    }

    fn fut_inst(expiry: u64) -> Arc<dyn Instrument> {
        let usdt = Currency::new("USDT", 8).unwrap();
        Arc::new(FuturesContract {
            id: InstrumentId::parse("BTCF.BINANCE").unwrap(),
            underlying: None,
            quote: usdt,
            settlement: usdt,
            price_precision: 2,
            size_precision: 3,
            price_increment: Price::from_decimal(dec!(0.01), 2).unwrap(),
            size_increment: Quantity::from_decimal(dec!(0.001), 3).unwrap(),
            min_notional: None,
            multiplier: Quantity::from_raw(1, 0).unwrap(),
            maker_fee: dec!(0),
            taker_fee: dec!(0),
            expiry_ns: UnixNanos(expiry),
        })
    }

    fn cfg(insts: Vec<Arc<dyn Instrument>>, venue: &str) -> BacktestConfig {
        let usdt = Currency::new("USDT", 8).unwrap();
        BacktestConfig::new(
            Venue::from(venue),
            insts,
            usdt,
            Money::from_decimal(dec!(100000), usdt).unwrap(),
        )
    }

    fn inst() -> Arc<dyn Instrument> {
        let usdt = Currency::new("USDT", 8).unwrap();
        let btc = Currency::new("BTC", 8).unwrap();
        Arc::new(CurrencyPair {
            id: InstrumentId::parse("BTCUSDT.BINANCE").unwrap(),
            base: btc,
            quote: usdt,
            price_precision: 2,
            size_precision: 3,
            price_increment: Price::from_decimal(dec!(0.01), 2).unwrap(),
            size_increment: Quantity::from_decimal(dec!(0.001), 3).unwrap(),
            min_notional: None,
            maker_fee: dec!(0.0002),
            taker_fee: dec!(0.0004),
        })
    }

    fn bar(iid: &InstrumentId, close: &str, ts: u64) -> MarketEvent {
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
            high: p(close),
            low: p(close),
            close: p(close),
            volume: Quantity::from_decimal(dec!(10), 3).unwrap(),
            ts_event: UnixNanos(ts),
            ts_init: UnixNanos(ts),
        })
    }

    #[test]
    fn backtest_runs_and_fills_market_order() {
        let usdt = Currency::new("USDT", 8).unwrap();
        let i = inst();
        let iid = i.id();
        let cfg = BacktestConfig::new(
            Venue::from("BINANCE"),
            vec![i],
            usdt,
            Money::from_decimal(dec!(100000), usdt).unwrap(),
        );
        let strat = Box::new(BuyOnceStrategy {
            iid: iid.clone(),
            bought: false,
        });
        let events = vec![
            bar(&iid, "50000", 1_000_000_000),
            bar(&iid, "51000", 2_000_000_000),
            bar(&iid, "52000", 3_000_000_000),
        ];
        let mut kernel = BacktestKernel::build(cfg, StrategyId::from("sma"), strat, events);
        let res = kernel.run();
        assert_eq!(res.orders_submitted, 1);
        assert_eq!(res.fills, 1);
        // Bought ~1 BTC at ~50000 then price rose to 52000 -> equity should exceed start.
        assert!(
            res.final_equity > res.starting_equity,
            "equity {} !> {}",
            res.final_equity,
            res.starting_equity
        );
        assert!(!res.equity_curve.is_empty());
    }

    #[test]
    fn option_settles_to_intrinsic_at_expiry_itm() {
        // Buy a 50000 call @ premium 1000; underlying at expiry is 54000 -> intrinsic 4000.
        let under = inst(); // BTCUSDT.BINANCE
        let under_iid = under.id();
        let opt = opt_inst("50000", OptionRight::Call, 2_500_000_000, under_iid.clone());
        let opt_iid = opt.id();
        let strat = Box::new(BuyContractOnce {
            target: opt_iid.clone(),
            bought: false,
        });
        let events = vec![
            bar(&opt_iid, "1000", 1_000_000_000), // premium; strategy decides to buy on this close
            bar(&under_iid, "52000", 1_000_000_000),
            bar(&opt_iid, "1500", 2_000_000_000), // no-look-ahead: the buy fills at THIS bar's open (1500)
            bar(&under_iid, "54000", 2_000_000_000), // last underlying mark before the 2.5e9 expiry
            bar(&under_iid, "55000", 3_000_000_000), // after expiry (option already settled)
        ];
        let mut kernel = BacktestKernel::build(
            cfg(vec![opt, under], "DERIBIT"),
            StrategyId::from("opt"),
            strat,
            events,
        );
        let res = kernel.run();
        assert_eq!(res.fills, 2, "one buy + one settlement fill");
        // No-look-ahead: the buy fills at the next opt bar's open (1500), not the decision close
        // (1000). Settled at intrinsic 4000 -> realized ~2500 (vs ~3000 under the old close-fill).
        assert!(
            (res.realized_pnl - 2500.0).abs() < 1.0,
            "realized {}",
            res.realized_pnl
        );
    }

    #[test]
    fn option_expires_worthless_when_out_of_the_money() {
        // Buy a 50000 call @ premium 1000; underlying stays at 48000 -> intrinsic 0 -> lose premium.
        let under = inst();
        let under_iid = under.id();
        let opt = opt_inst("50000", OptionRight::Call, 2_500_000_000, under_iid.clone());
        let opt_iid = opt.id();
        let strat = Box::new(BuyContractOnce {
            target: opt_iid.clone(),
            bought: false,
        });
        let events = vec![
            bar(&opt_iid, "1000", 1_000_000_000), // decide to buy on this close
            bar(&under_iid, "48000", 1_000_000_000),
            bar(&opt_iid, "1000", 2_000_000_000), // no-look-ahead: the buy fills at THIS open (1000)
            bar(&under_iid, "48000", 2_000_000_000),
        ];
        let mut kernel = BacktestKernel::build(
            cfg(vec![opt, under], "DERIBIT"),
            StrategyId::from("opt"),
            strat,
            events,
        );
        let res = kernel.run();
        assert_eq!(res.fills, 2);
        // Settled worthless (0), bought at ~1000 (next opt bar's open) -> realized ~ -1000.
        assert!(
            (res.realized_pnl + 1000.0).abs() < 1.0,
            "realized {}",
            res.realized_pnl
        );
    }

    #[test]
    fn account_is_liquidated_when_equity_breaches_maintenance() {
        // Start with 10k, buy 1 future @ ~50k (notional 50k). As the price falls the mark-to-market
        // equity drops; at 44k, equity (~4k) < maintenance (gross 44k x 0.1 = 4.4k) -> liquidate.
        let usdt = Currency::new("USDT", 8).unwrap();
        let fut = fut_inst(9_000_000_000_000_000_000); // far expiry -> no settlement during the test
        let fut_iid = fut.id();
        let mut config = BacktestConfig::new(
            Venue::from("BINANCE"),
            vec![fut],
            usdt,
            Money::from_decimal(dec!(10000), usdt).unwrap(),
        );
        config.risk.maintenance_margin_rate = Some(dec!(0.1));
        let strat = Box::new(BuyOnceStrategy {
            iid: fut_iid.clone(),
            bought: false,
        });
        // The price dips to 44k (breaching maintenance) THEN recovers to 50k — but liquidation is
        // irreversible, so the recovery does not save the account. No-look-ahead: the buy decided on
        // bar 1's close fills at bar 2's open (still 50k), so an extra 50k bar precedes the dip.
        let events = vec![
            bar(&fut_iid, "50000", 1_000_000_000), // decide to buy on this close
            bar(&fut_iid, "50000", 2_000_000_000), // entry fills at this open (50k)
            bar(&fut_iid, "44000", 3_000_000_000), // equity ~4k < maint ~4.4k -> liquidated
            bar(&fut_iid, "50000", 4_000_000_000), // recovers, but already flat
        ];
        let mut kernel = BacktestKernel::build(config, StrategyId::from("liq"), strat, events);
        let res = kernel.run();
        assert_eq!(res.fills, 2, "entry + liquidation close");
        assert!(res.realized_pnl < -5000.0, "realized {}", res.realized_pnl);
        assert!(res.final_equity < 5000.0, "final {}", res.final_equity);
    }

    #[test]
    fn liquidation_re_arms_after_a_prior_liquidation() {
        // After the first liquidation the strategy re-buys; the re-opened position must be liquidated
        // AGAIN on the next breach (the old one-shot latch left it unprotected = blow-through).
        let usdt = Currency::new("USDT", 8).unwrap();
        let fut = fut_inst(9_000_000_000_000_000_000);
        let fut_iid = fut.id();
        let mut config = BacktestConfig::new(
            Venue::from("BINANCE"),
            vec![fut],
            usdt,
            Money::from_decimal(dec!(10000), usdt).unwrap(),
        );
        config.risk.maintenance_margin_rate = Some(dec!(0.1));
        let strat = Box::new(BuyWhenFlat {
            iid: fut_iid.clone(),
            buys: 0,
            max_buys: 2,
        });
        // No-look-ahead: each buy decided on a 50k close fills at the NEXT bar's open, so every
        // entry is preceded by a second 50k bar before the dip that liquidates it.
        let events = vec![
            bar(&fut_iid, "50000", 1_000_000_000), // flat -> decide buy #1
            bar(&fut_iid, "50000", 2_000_000_000), // buy #1 fills at this open (50k)
            bar(&fut_iid, "44000", 3_000_000_000), // liquidate #1
            bar(&fut_iid, "50000", 4_000_000_000), // flat -> decide buy #2
            bar(&fut_iid, "50000", 5_000_000_000), // buy #2 fills at this open (50k)
            bar(&fut_iid, "44000", 6_000_000_000), // liquidate #2
        ];
        let mut kernel = BacktestKernel::build(config, StrategyId::from("liq2"), strat, events);
        let res = kernel.run();
        assert!(
            res.fills >= 4,
            "expected >=2 buys + 2 liquidations, got {} fills",
            res.fills
        );
    }

    #[test]
    fn no_liquidation_without_a_maintenance_rate() {
        // The IDENTICAL dip-then-recover, but no maintenance rate -> the position is NOT force-closed:
        // it rides the dip and recovers, settling ~flat at expiry (vs the locked-in loss above).
        let usdt = Currency::new("USDT", 8).unwrap();
        let fut = fut_inst(9_000_000_000_000_000_000); // far expiry
        let fut_iid = fut.id();
        let config = BacktestConfig::new(
            Venue::from("BINANCE"),
            vec![fut],
            usdt,
            Money::from_decimal(dec!(10000), usdt).unwrap(),
        );
        let strat = Box::new(BuyOnceStrategy {
            iid: fut_iid.clone(),
            bought: false,
        });
        let events = vec![
            bar(&fut_iid, "50000", 1_000_000_000),
            bar(&fut_iid, "44000", 2_000_000_000), // would breach, but no maintenance configured
            bar(&fut_iid, "50000", 3_000_000_000), // recovers
        ];
        let mut kernel = BacktestKernel::build(config, StrategyId::from("noliq"), strat, events);
        let res = kernel.run();
        // Survived the dip, recovered, settled ~flat at expiry (only entry slippage lost).
        assert!(res.realized_pnl > -100.0, "realized {}", res.realized_pnl);
        assert!(res.final_equity > 9000.0, "final {}", res.final_equity);
    }

    #[test]
    fn submitting_on_an_expired_contract_is_denied() {
        // The future already expired (500ms) before the first bar (1s); a buy on it is denied, so no
        // post-expiry position can be opened that settlement would then miss.
        let fut = fut_inst(500_000_000);
        let fut_iid = fut.id();
        let strat = Box::new(BuyOnceStrategy {
            iid: fut_iid.clone(),
            bought: false,
        });
        let events = vec![
            bar(&fut_iid, "50000", 1_000_000_000),
            bar(&fut_iid, "51000", 2_000_000_000),
        ];
        let mut kernel = BacktestKernel::build(
            cfg(vec![fut], "BINANCE"),
            StrategyId::from("fut"),
            strat,
            events,
        );
        let res = kernel.run();
        assert_eq!(res.orders_submitted, 1);
        assert_eq!(res.orders_denied, 1);
        assert_eq!(res.fills, 0, "expired-contract order must not fill");
    }

    #[test]
    fn future_cash_settles_to_mark_at_expiry() {
        // Buy a future @ 50000, price rises to 52000 by expiry -> cash-settle realizes ~2000.
        // No-look-ahead: the buy decided on bar 1's close fills at bar 2's open (still 50000), so a
        // third bar is needed to carry the price up to 52000 before the 3.5e9 expiry.
        let fut = fut_inst(3_500_000_000);
        let fut_iid = fut.id();
        let strat = Box::new(BuyOnceStrategy {
            iid: fut_iid.clone(),
            bought: false,
        });
        let events = vec![
            bar(&fut_iid, "50000", 1_000_000_000), // decide to buy on this close
            bar(&fut_iid, "50000", 2_000_000_000), // fills at this open (50000)
            bar(&fut_iid, "52000", 3_000_000_000), // last mark before the 3.5e9 expiry
        ];
        let mut kernel = BacktestKernel::build(
            cfg(vec![fut], "BINANCE"),
            StrategyId::from("fut"),
            strat,
            events,
        );
        let res = kernel.run();
        assert_eq!(res.fills, 2, "one buy + one settlement fill");
        // Settled at mark 52000; bought at ~50000 plus entry slippage -> realized just under 2000.
        assert!(
            (1990.0..2000.0).contains(&res.realized_pnl),
            "realized {}",
            res.realized_pnl
        );
    }

    // FIX 5: engaging the kill-switch on the kernel's RiskGate denies every order, so nothing fills.
    #[test]
    fn kill_switch_denies_all_orders_in_kernel() {
        let usdt = Currency::new("USDT", 8).unwrap();
        let i = inst();
        let iid = i.id();
        let cfg = BacktestConfig::new(
            Venue::from("BINANCE"),
            vec![i],
            usdt,
            Money::from_decimal(dec!(100000), usdt).unwrap(),
        );
        let strat = Box::new(BuyOnceStrategy {
            iid: iid.clone(),
            bought: false,
        });
        let events = vec![
            bar(&iid, "50000", 1_000_000_000),
            bar(&iid, "51000", 2_000_000_000),
            bar(&iid, "52000", 3_000_000_000),
        ];
        let mut kernel = BacktestKernel::build(cfg, StrategyId::from("kill"), strat, events);
        kernel.set_kill_switch(true);
        let res = kernel.run();
        assert_eq!(res.orders_submitted, 1);
        assert_eq!(res.orders_denied, 1, "kill-switch denies the order");
        assert_eq!(res.fills, 0, "no fills while killed");
    }

    // FIX 5: a configured max-order-notional limit (via BacktestConfig.risk) is enforced -> denied.
    #[test]
    fn configured_notional_limit_denies_oversized_order() {
        let usdt = Currency::new("USDT", 8).unwrap();
        let i = inst();
        let iid = i.id();
        let mut cfg = BacktestConfig::new(
            Venue::from("BINANCE"),
            vec![i],
            usdt,
            Money::from_decimal(dec!(100000), usdt).unwrap(),
        );
        // qty 1 @ ~50000 -> notional ~50000 > 40000 cap.
        cfg.risk.max_order_notional = Some(Money::from_decimal(dec!(40000), usdt).unwrap());
        let strat = Box::new(BuyOnceStrategy {
            iid: iid.clone(),
            bought: false,
        });
        let events = vec![
            bar(&iid, "50000", 1_000_000_000),
            bar(&iid, "51000", 2_000_000_000),
        ];
        let mut kernel = BacktestKernel::build(cfg, StrategyId::from("cap"), strat, events);
        let res = kernel.run();
        assert_eq!(res.orders_denied, 1, "over-notional order denied by config");
        assert_eq!(res.fills, 0);
    }

    // ---- LiveKernel (port-driven sandbox/live path) -------------------------------------------

    use coinext_model::{Fill, LiquiditySide, TradeId, VenueOrderId};
    use coinext_ports::{
        CancelOrder, DataClient, ExecutionClient, ExecutionReport, ModifyOrder, PortResult,
        SubmitOrder, Subscription,
    };
    use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering as AtomicOrdering};
    use tokio::sync::mpsc;

    /// A fake DataClient that replays a fixed list of market events, then closes the stream.
    struct ReplayDataClient {
        tx: Option<mpsc::Sender<MarketEvent>>,
        rx: Option<mpsc::Receiver<MarketEvent>>,
        events: Vec<MarketEvent>,
    }
    impl ReplayDataClient {
        fn new(events: Vec<MarketEvent>) -> Self {
            let (tx, rx) = mpsc::channel(64);
            ReplayDataClient {
                tx: Some(tx),
                rx: Some(rx),
                events,
            }
        }
    }
    #[async_trait::async_trait]
    impl DataClient for ReplayDataClient {
        async fn connect(&mut self) -> PortResult<()> {
            // Push all events now; dropping the sender afterwards closes the stream so `run` returns.
            let tx = self.tx.take().expect("connect called twice");
            for ev in self.events.drain(..) {
                tx.send(ev).await.ok();
            }
            Ok(())
        }
        async fn subscribe(&mut self, _sub: Subscription) -> PortResult<()> {
            Ok(())
        }
        async fn unsubscribe(&mut self, _sub: Subscription) -> PortResult<()> {
            Ok(())
        }
        async fn request_bars(
            &self,
            _bar_type: BarType,
            _start: UnixNanos,
            _end: UnixNanos,
        ) -> PortResult<Vec<Bar>> {
            Ok(Vec::new())
        }
        fn take_stream(&mut self) -> mpsc::Receiver<MarketEvent> {
            self.rx.take().expect("take_stream called twice")
        }
        async fn disconnect(&mut self) -> PortResult<()> {
            Ok(())
        }
    }

    /// A fake ExecutionClient that, on each submit, emits an Accepted then a Fill at the order's
    /// quantity and a fixed price — proving the LiveKernel routes intents to the PORT and folds the
    /// returned reports through the SAME OMS. Counts submits so the test can assert the port was used.
    struct InstantFillExec {
        tx: Option<mpsc::Sender<ExecutionReport>>,
        report_tx: mpsc::Sender<ExecutionReport>,
        rx: Option<mpsc::Receiver<ExecutionReport>>,
        submits: std::sync::Arc<AtomicUsize>,
        price: Price,
        settle: Currency,
        trade_seq: AtomicU64,
    }
    impl InstantFillExec {
        fn new(price: Price, settle: Currency, submits: std::sync::Arc<AtomicUsize>) -> Self {
            let (tx, rx) = mpsc::channel(64);
            InstantFillExec {
                report_tx: tx.clone(),
                tx: Some(tx),
                rx: Some(rx),
                submits,
                price,
                settle,
                trade_seq: AtomicU64::new(0),
            }
        }
    }
    #[async_trait::async_trait]
    impl ExecutionClient for InstantFillExec {
        fn venue(&self) -> Venue {
            Venue::from("BINANCE")
        }
        async fn connect(&mut self) -> PortResult<()> {
            // Keep a sender alive on `self` (report_tx); drop the extra so only the held one remains.
            self.tx.take();
            Ok(())
        }
        async fn submit_order(&self, cmd: SubmitOrder) -> PortResult<()> {
            self.submits.fetch_add(1, AtomicOrdering::SeqCst);
            let o = &cmd.order;
            self.report_tx
                .send(ExecutionReport::Accepted {
                    client_order_id: o.client_order_id.clone(),
                    venue_order_id: VenueOrderId::from("V-1"),
                })
                .await
                .ok();
            let seq = self.trade_seq.fetch_add(1, AtomicOrdering::SeqCst) + 1;
            let fill = Fill {
                trade_id: TradeId::from(format!("T-{seq}")),
                client_order_id: o.client_order_id.clone(),
                venue_order_id: VenueOrderId::from("V-1"),
                instrument_id: o.instrument_id.clone(),
                side: o.side,
                last_px: self.price,
                last_qty: o.quantity,
                fee: Money::zero(self.settle),
                liquidity: LiquiditySide::Taker,
                ts_event: UnixNanos(0),
                ts_init: UnixNanos(0),
            };
            self.report_tx.send(ExecutionReport::Fill(fill)).await.ok();
            Ok(())
        }
        async fn cancel_order(&self, _cmd: CancelOrder) -> PortResult<()> {
            Ok(())
        }
        async fn modify_order(&self, _cmd: ModifyOrder) -> PortResult<()> {
            Ok(())
        }
        async fn reconcile(&self) -> PortResult<Vec<ExecutionReport>> {
            Ok(Vec::new())
        }
        fn take_reports(&mut self) -> mpsc::Receiver<ExecutionReport> {
            self.rx.take().expect("take_reports called twice")
        }
        async fn disconnect(&mut self) -> PortResult<()> {
            Ok(())
        }
    }

    /// Records fills the Strategy is notified of, so the test can assert the live path delivered the
    /// port's reports all the way back up to the user Strategy (the shared seam end-to-end).
    struct CountingBuyStrategy {
        iid: InstrumentId,
        bought: bool,
        fills: std::sync::Arc<AtomicUsize>,
    }
    impl Strategy for CountingBuyStrategy {
        fn on_bar(&mut self, _b: &Bar, ctx: &mut StrategyContext) {
            if !self.bought {
                self.bought = true;
                ctx.submit_market(
                    self.iid.clone(),
                    OrderSide::Buy,
                    Quantity::from_decimal(dec!(1), 3).unwrap(),
                );
            }
        }
        fn on_order_filled(&mut self, _f: &Fill, _ctx: &mut StrategyContext) {
            self.fills.fetch_add(1, AtomicOrdering::SeqCst);
        }
    }

    #[test]
    fn live_kernel_consumes_ports_and_folds_fills_through_the_same_engines() {
        let usdt = Currency::new("USDT", 8).unwrap();
        let i = inst();
        let iid = i.id();
        let submits = std::sync::Arc::new(AtomicUsize::new(0));
        let strat_fills = std::sync::Arc::new(AtomicUsize::new(0));

        let exec = Box::new(InstantFillExec::new(
            Price::from_decimal(dec!(50000), 2).unwrap(),
            usdt,
            submits.clone(),
        ));
        let data = Box::new(ReplayDataClient::new(vec![
            bar(&iid, "50000", 1_000_000_000),
            bar(&iid, "51000", 2_000_000_000),
        ]));
        let strat = Box::new(CountingBuyStrategy {
            iid: iid.clone(),
            bought: false,
            fills: strat_fills.clone(),
        });

        let mut kernel = LiveKernel::build(
            Environment::Sandbox,
            BacktestConfig::new(
                Venue::from("BINANCE"),
                vec![i],
                usdt,
                Money::from_decimal(dec!(100000), usdt).unwrap(),
            ),
            StrategyId::from("live"),
            strat,
            exec,
            data,
        );
        assert_eq!(kernel.environment(), Environment::Sandbox);

        let rt = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap();
        let local = tokio::task::LocalSet::new();
        local.block_on(&rt, async move {
            kernel.run().await.unwrap();
            // The strategy submitted exactly one order over the PORT, and its Fill flowed back through
            // the SAME OMS `apply_report` into a long position the strategy was notified of.
            assert_eq!(
                submits.load(AtomicOrdering::SeqCst),
                1,
                "one order routed to the port"
            );
            assert_eq!(
                strat_fills.load(AtomicOrdering::SeqCst),
                1,
                "fill delivered to the Strategy"
            );
            let pos = kernel.cache.borrow().position(&iid).cloned();
            let pos = pos.expect("position opened from the port's fill");
            assert_eq!(pos.side, PositionSide::Long);
            assert_eq!(pos.quantity, Quantity::from_decimal(dec!(1), 3).unwrap());
        });
    }

    #[test]
    #[should_panic(expected = "LiveKernel requires")]
    fn live_kernel_rejects_backtest_environment() {
        let usdt = Currency::new("USDT", 8).unwrap();
        let i = inst();
        let submits = std::sync::Arc::new(AtomicUsize::new(0));
        let exec = Box::new(InstantFillExec::new(
            Price::from_decimal(dec!(50000), 2).unwrap(),
            usdt,
            submits,
        ));
        let data = Box::new(ReplayDataClient::new(vec![]));
        let _ = LiveKernel::build(
            Environment::Backtest,
            BacktestConfig::new(
                Venue::from("BINANCE"),
                vec![i],
                usdt,
                Money::from_decimal(dec!(100000), usdt).unwrap(),
            ),
            StrategyId::from("x"),
            Box::new(BuyOnceStrategy {
                iid: InstrumentId::parse("BTCUSDT.BINANCE").unwrap(),
                bought: false,
            }),
            exec,
            data,
        );
    }
}
