//! `qv-kernel` — the single place backtest vs live differs, and the deterministic synchronous core
//! loop. For backtest it merge-sorts three event sources by timestamp — incoming market data, due
//! delayed execution reports from the sim's DelayedEventQueue, and due timers from the
//! HistoricalClock — and dispatches each to the engines and the Strategy SYNCHRONOUSLY. The same
//! engines, Strategy, RiskEngine and Cache are used in live; only the Clock and Data/Execution
//! clients are swapped (the parity invariant).

use qv_bus::InProcBus;
use qv_cache::Cache;
use qv_core::{Clock, Currency, HistoricalClock, Money, UnixNanos};
use qv_data_engine::DataEngine;
use qv_exec_engine::ExecutionEngine;
use qv_model::{Instrument, MarketEvent, OrderEvent, StrategyId, Venue};
use qv_portfolio::PortfolioState;
use qv_ports::{BusMsg, MessageBus, RiskLimits, Strategy, StrategyCommand, StrategyContext, Topic};
use qv_risk_engine::RiskGate;
use qv_sim::{BrokerageModel, DefaultBrokerageModel, SimulatedExecutionClient};
use std::cell::RefCell;
use std::rc::Rc;
use std::sync::Arc;

/// Which environment the kernel runs. Only this selects the Clock + Data/Exec clients.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Environment {
    Backtest,
    Sandbox,
    Live,
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
}

/// Result of a backtest run.
#[derive(Debug, Clone)]
pub struct RunResult {
    /// (ts_ns, equity) sampled once per processed bar/quote — the input to the tear sheet.
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

        let clock = Rc::new(HistoricalClock::new(config.start_ns));
        let cache = Rc::new(RefCell::new(Cache::new()));
        {
            let mut c = cache.borrow_mut();
            for inst in &config.instruments {
                c.add_instrument(inst.clone());
            }
            let mut account =
                qv_model::AccountState::new(qv_model::AccountId::from("BACKTEST"), config.settle);
            account.set_balance(config.starting_balance);
            c.set_account(account);
        }

        let clock_dyn: Rc<dyn qv_core::Clock> = clock.clone();
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

            let frontier = [next_market, next_sim, next_timer]
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
                let ts = frontier.as_u64();
                let eq = self.equity();
                self.result.equity_curve.push((ts, eq));
            }
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
}

#[cfg(test)]
mod tests {
    use super::*;
    use qv_core::{Price, Quantity};
    use qv_model::{
        AggregationSource, Bar, BarAggregation, BarSpec, BarType, CurrencyPair, InstrumentId,
        OrderSide, PriceType,
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
}
