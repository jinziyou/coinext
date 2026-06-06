//! The hexagonal port traits. Async traits (`DataClient`/`ExecutionClient`/`InstrumentProvider`)
//! are the live-I/O seam; the sync traits (`Strategy`/`RiskEngine`/`Portfolio`) run on the
//! deterministic core thread. A Python Strategy subclass cannot implement an async Rust trait —
//! which is exactly why `Strategy` is synchronous and `PyStrategyAdapter` (qv-py) bridges it.

use crate::commands::{
    CancelOrder, ExecutionReport, ModifyOrder, RiskDecision, SubmitOrder, Subscription,
};
use crate::context::StrategyContext;
use crate::error::PortResult;
use async_trait::async_trait;
use qv_core::{TimerEvent, UnixNanos};
use qv_model::{
    Bar, BarType, Currency, Fill, Instrument, InstrumentId, MarketEvent, Money, Order, OrderEvent,
    Position, QuoteTick, TradeTick, Venue,
};
use std::sync::Arc;
use tokio::sync::mpsc;

/// Market-data port. The event stream is taken ONCE at wiring time (`take_stream`, single-consumer
/// mpsc). `request_bars` is async and serves warm-up + the backtest feed from the local
/// HistoryReader in BOTH modes (never live REST at handler time), so warm-up is identical.
#[async_trait]
pub trait DataClient: Send {
    async fn connect(&mut self) -> PortResult<()>;
    async fn subscribe(&mut self, sub: Subscription) -> PortResult<()>;
    async fn unsubscribe(&mut self, sub: Subscription) -> PortResult<()>;
    async fn request_bars(
        &self,
        bar_type: BarType,
        start: UnixNanos,
        end: UnixNanos,
    ) -> PortResult<Vec<Bar>>;
    /// Called once at Kernel build to take ownership of the inbound event stream.
    fn take_stream(&mut self) -> mpsc::Receiver<MarketEvent>;
    async fn disconnect(&mut self) -> PortResult<()>;
}

/// THE parity seam. `SimulatedExecutionClient` (backtest), the testnet variant (sandbox), and the
/// live `BinanceExecutionClient` all implement this identically; everything above it is the same.
/// `submit_order` is idempotent on `ClientOrderId`. Reports come back via `take_reports`.
#[async_trait]
pub trait ExecutionClient: Send {
    fn venue(&self) -> Venue;
    async fn connect(&mut self) -> PortResult<()>;
    async fn submit_order(&self, cmd: SubmitOrder) -> PortResult<()>;
    async fn cancel_order(&self, cmd: CancelOrder) -> PortResult<()>;
    async fn modify_order(&self, cmd: ModifyOrder) -> PortResult<()>;
    async fn reconcile(&self) -> PortResult<Vec<ExecutionReport>>;
    /// Called once at Kernel build to take ownership of the report stream.
    fn take_reports(&mut self) -> mpsc::Receiver<ExecutionReport>;
    async fn disconnect(&mut self) -> PortResult<()>;
}

/// Per-venue mapping from venue symbology to the shared `Instrument` model.
#[async_trait]
pub trait InstrumentProvider: Send + Sync {
    async fn load_all(&self) -> PortResult<Vec<Arc<dyn Instrument>>>;
    async fn load(&self, id: &InstrumentId) -> PortResult<Arc<dyn Instrument>>;
    fn find(&self, id: &InstrumentId) -> Option<Arc<dyn Instrument>>;
}

/// Pre-trade risk gate. Every order is checked synchronously on the core thread BEFORE reaching a
/// venue; on failure it becomes Denied and never leaves the process. Holds the kill-switch.
/// Interior mutability (atomics/cells) lets `&self` update rate counters.
pub trait RiskEngine {
    fn check(
        &self,
        order: &Order,
        portfolio: &dyn Portfolio,
        inst: &dyn Instrument,
    ) -> RiskDecision;
    fn set_kill_switch(&self, engaged: bool);
    fn is_killed(&self) -> bool;
}

/// Account/position/PnL analytics consumed via `self.portfolio`. Sources marks internally from the
/// Cache (so `unrealized_pnl` takes no mark argument). Returns owned values to avoid borrow tangles.
pub trait Portfolio {
    fn position(&self, id: &InstrumentId) -> Option<Position>;
    fn net_exposure(&self, id: &InstrumentId) -> Money;
    fn unrealized_pnl(&self, id: &InstrumentId) -> Money;
    fn realized_pnl(&self, id: &InstrumentId) -> Money;
    fn gross_exposure(&self) -> Money;
    fn balance(&self, ccy: &Currency) -> Money;
    /// Total account equity in the settlement currency = balance + realized + unrealized PnL.
    fn equity(&self) -> Money;
}

/// The ONE public user API. The SAME subclass runs in event-driven backtest, sandbox, and live.
/// Handlers are SYNCHRONOUS — called directly by the core's event loop. Default no-op bodies let a
/// strategy override only what it needs. (Native-Rust strategies implement this directly with no
/// GIL; Python strategies are bridged by qv-py's PyStrategyAdapter under the GIL.)
pub trait Strategy {
    fn on_start(&mut self, _ctx: &mut StrategyContext) {}
    fn on_quote(&mut self, _q: &QuoteTick, _ctx: &mut StrategyContext) {}
    fn on_trade(&mut self, _t: &TradeTick, _ctx: &mut StrategyContext) {}
    fn on_bar(&mut self, _b: &Bar, _ctx: &mut StrategyContext) {}
    fn on_order_event(&mut self, _e: &OrderEvent, _ctx: &mut StrategyContext) {}
    fn on_order_filled(&mut self, _f: &Fill, _ctx: &mut StrategyContext) {}
    fn on_timer(&mut self, _ev: &TimerEvent, _ctx: &mut StrategyContext) {}
    fn on_stop(&mut self, _ctx: &mut StrategyContext) {}
}
