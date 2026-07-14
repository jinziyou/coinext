//! Port-driven sandbox/live kernel (DataClient + ExecutionClient).

use crate::{
    snapshot_portfolio, BacktestConfig, Environment, LiveKernelStopHandle, PortfolioSnapshot,
};
use coinext_bus::InProcBus;
use coinext_cache::Cache;
use coinext_core::{Clock, Currency};
use coinext_data_engine::DataEngine;
use coinext_exec_engine::ExecutionEngine;
use coinext_model::{MarketEvent, OrderEvent, StrategyId};
use coinext_portfolio::PortfolioState;
use coinext_ports::{BusMsg, MessageBus, Strategy, StrategyCommand, StrategyContext, Topic};
use coinext_risk_engine::RiskGate;
use std::cell::RefCell;
use std::rc::Rc;
use std::sync::Arc;

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
/// Port-driven sandbox/live kernel. The run loop is single-threaded (`Rc`/`RefCell`,
/// deterministic-by-design): it connects clients, takes their streams, and drains both — folding
/// reports through the same OMS `apply_report` and dispatching market events to the same
/// DataEngine + Strategy as backtest. Reconnect, crash-recovery reconcile, and out-of-band
/// kill-switch wiring remain live-ops concerns for `coinext-exec-svc` / the trader process.
pub struct LiveKernel {
    env: Environment,
    clock: Rc<dyn Clock>,
    /// Shared cache; `pub(crate)` so integration tests can inspect positions after a live drain.
    pub(crate) cache: Rc<RefCell<Cache>>,
    bus: InProcBus,
    data_engine: DataEngine,
    exec_engine: ExecutionEngine,
    risk: RiskGate,
    portfolio: PortfolioState,
    settle: Currency,
    strategy: Box<dyn Strategy>,
    ctx: StrategyContext,
    exec_client: Box<dyn coinext_ports::ExecutionClient>,
    data_client: Box<dyn coinext_ports::DataClient>,
    stop: LiveKernelStopHandle,
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
        let stop = LiveKernelStopHandle::default();
        LiveKernel {
            settle: config.settle,
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
            stop,
        }
    }

    /// The environment this kernel targets (`Sandbox` or `Live`).
    pub fn environment(&self) -> Environment {
        self.env
    }

    pub fn bus(&self) -> &InProcBus {
        &self.bus
    }

    /// Cloneable cancellation handle for the native live loop.
    pub fn stop_handle(&self) -> LiveKernelStopHandle {
        self.stop.clone()
    }

    /// Request graceful shutdown. The run loop wakes promptly and disconnects both ports.
    pub fn request_stop(&self) {
        self.stop.request_stop();
    }

    pub fn is_stop_requested(&self) -> bool {
        self.stop.is_stop_requested()
    }

    /// Native portfolio snapshot exported to Python telemetry adapters.
    pub fn portfolio_snapshot(&self) -> PortfolioSnapshot {
        let cache = self.cache.borrow();
        snapshot_portfolio(&cache, self.settle)
    }

    /// Connect the clients, take their streams, and drain the unified event loop until both streams
    /// close. Market events feed the DataEngine + Strategy; execution reports are folded through the
    /// SAME OMS `apply_report`; strategy order intents are routed through the RiskGate to the
    /// `ExecutionClient` port. Single-threaded by design (the core is `Rc`/`RefCell`).
    pub async fn run(&mut self) -> coinext_ports::PortResult<()> {
        self.run_inner::<fn(PortfolioSnapshot) -> coinext_ports::PortResult<()>>(None)
            .await
    }

    /// Run the live loop and call `on_portfolio` after each authoritative mark/fill fold.
    ///
    /// This is the native callback seam used by Python live telemetry adapters: the callback receives
    /// the same cache-derived [`PortfolioSnapshot`] as [`LiveKernel::portfolio_snapshot`]. The default
    /// [`LiveKernel::run`] path does NOT allocate snapshots because it passes no callback.
    pub async fn run_with_portfolio_callback<F>(
        &mut self,
        mut on_portfolio: F,
    ) -> coinext_ports::PortResult<()>
    where
        F: FnMut(PortfolioSnapshot),
    {
        self.try_run_with_portfolio_callback(|snapshot| {
            on_portfolio(snapshot);
            coinext_ports::PortResult::Ok(())
        })
        .await
    }

    /// Run the live loop and stop if the portfolio callback returns an error.
    pub async fn try_run_with_portfolio_callback<F, E>(
        &mut self,
        mut on_portfolio: F,
    ) -> coinext_ports::PortResult<()>
    where
        F: FnMut(PortfolioSnapshot) -> Result<(), E>,
        E: std::fmt::Display,
    {
        self.run_inner(Some(&mut |snapshot| {
            on_portfolio(snapshot).map_err(|e| {
                coinext_ports::PortError::Io(format!("portfolio callback failed: {e}"))
            })
        }))
        .await
    }

    async fn run_inner<F>(
        &mut self,
        mut on_portfolio: Option<&mut F>,
    ) -> coinext_ports::PortResult<()>
    where
        F: FnMut(PortfolioSnapshot) -> coinext_ports::PortResult<()>,
    {
        // Connect both ports, then take their (single-consumer) streams.
        self.data_client.connect().await?;
        self.exec_client.connect().await?;
        let mut market_rx = self.data_client.take_stream();
        let mut report_rx = self.exec_client.take_reports();
        let stop = self.stop.clone();

        // On startup reconcile venue truth into the OMS before accepting new flow (no-op for the sim).
        let now = self.clock.now_ns();
        for report in self.exec_client.reconcile().await? {
            for ev in self.exec_engine.apply_report(report, now) {
                self.notify_event(&ev);
            }
            self.emit_portfolio(&mut on_portfolio)?;
        }

        self.strategy.on_start(&mut self.ctx);
        self.route_outbox().await?;

        // Drain both inbound streams until both close. Reports are folded first (so a fill is visible
        // before the next strategy decision), then the market event drives the Strategy.
        loop {
            if stop.is_stop_requested() {
                break;
            }
            tokio::select! {
                biased;
                _ = stop.cancelled() => break,
                report = report_rx.recv() => match report {
                    Some(report) => {
                        let now = self.clock.now_ns();
                        for ev in self.exec_engine.apply_report(report, now) {
                            self.notify_event(&ev);
                        }
                        self.route_outbox().await?;
                        self.emit_portfolio(&mut on_portfolio)?;
                    }
                    None => break,
                },
                market = market_rx.recv() => match market {
                    Some(ev) => {
                        self.data_engine.process(&ev, &self.bus);
                        self.dispatch_market(&ev);
                        self.route_outbox().await?;
                        self.emit_portfolio(&mut on_portfolio)?;
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

    fn emit_portfolio<F>(&self, on_portfolio: &mut Option<&mut F>) -> coinext_ports::PortResult<()>
    where
        F: FnMut(PortfolioSnapshot) -> coinext_ports::PortResult<()>,
    {
        if let Some(callback) = on_portfolio.as_mut() {
            (**callback)(self.portfolio_snapshot())?;
        }
        Ok(())
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
                    // Cancel-replace on the live venue; not modeled on this path yet.
                }
            }
        }
        Ok(())
    }
}
