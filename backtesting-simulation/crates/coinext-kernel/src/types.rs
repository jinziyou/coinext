//! Shared kernel types: environment, config, run results, portfolio snapshots.

use coinext_cache::Cache;
use coinext_core::{Currency, Money, UnixNanos};
use coinext_model::{Instrument, PositionSide, Venue};
use coinext_ports::RiskLimits;
use coinext_sim::{BrokerageModel, DefaultBrokerageModel};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use tokio::sync::Notify;

/// Environment the kernel targets. `Backtest` runs the deterministic [`crate::BacktestKernel`];
/// `Sandbox`/`Live` run the [`crate::LiveKernel`].
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

/// Shared cancellation handle for the native sandbox/live kernel.
///
/// The kernel remains single-threaded, but the stop signal is thread-safe so Python/runtime control
/// code can request shutdown without owning the `LiveKernel` while its run loop is active.
#[derive(Clone)]
pub struct LiveKernelStopHandle {
    requested: Arc<AtomicBool>,
    notify: Arc<Notify>,
}

impl Default for LiveKernelStopHandle {
    fn default() -> Self {
        LiveKernelStopHandle {
            requested: Arc::new(AtomicBool::new(false)),
            notify: Arc::new(Notify::new()),
        }
    }
}

impl LiveKernelStopHandle {
    pub fn request_stop(&self) {
        self.requested.store(true, Ordering::SeqCst);
        self.notify.notify_waiters();
    }

    pub fn is_stop_requested(&self) -> bool {
        self.requested.load(Ordering::SeqCst)
    }

    pub(crate) async fn cancelled(&self) {
        if self.is_stop_requested() {
            return;
        }
        self.notify.notified().await;
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

/// One display-ready native position row exported for Python portfolio telemetry.
#[derive(Debug, Clone)]
pub struct PositionSnapshot {
    pub symbol: String,
    pub venue: String,
    pub net_qty: f64,
    pub avg_price: f64,
    pub mark_price: f64,
    pub realized_pnl: f64,
    pub unrealized_pnl: f64,
    pub notional: f64,
}

/// Display-ready account/portfolio state exported to Python.
#[derive(Debug, Clone)]
pub struct PortfolioSnapshot {
    pub cash_balance: f64,
    pub realized_pnl: f64,
    pub equity: f64,
    pub gross_exposure: f64,
    pub net_exposure: f64,
    pub unrealized_pnl: f64,
    pub positions: Vec<PositionSnapshot>,
}

pub(crate) fn snapshot_portfolio(cache: &Cache, settle: Currency) -> PortfolioSnapshot {
    let cash_balance = cache
        .account()
        .map(|a| a.balance(&settle).as_f64())
        .unwrap_or(0.0);
    let mut positions = Vec::new();
    let mut realized_pnl = 0.0;
    let mut unrealized_pnl = 0.0;
    let mut gross_exposure = 0.0;
    let mut net_exposure = 0.0;
    for pos in cache.positions() {
        let sign = match pos.side {
            PositionSide::Long => 1.0,
            PositionSide::Short => -1.0,
            PositionSide::Flat => 0.0,
        };
        let net_qty = sign * pos.quantity.as_f64();
        let realized = pos.realized_pnl.as_f64();
        let avg_price = pos.avg_px_open.as_f64();
        let (mark_price, unrealized, notional) = match (
            cache.instrument(&pos.instrument_id),
            cache.mark(&pos.instrument_id),
        ) {
            (Some(inst), Some(mark)) => (
                mark.as_f64(),
                pos.unrealized_pnl(mark, &*inst).as_f64(),
                pos.notional(mark, &*inst).as_f64(),
            ),
            _ => (0.0, 0.0, 0.0),
        };
        realized_pnl += realized;
        unrealized_pnl += unrealized;
        gross_exposure += notional.abs();
        net_exposure += sign * notional;
        positions.push(PositionSnapshot {
            symbol: pos.instrument_id.symbol.as_str().to_string(),
            venue: pos.instrument_id.venue.as_str().to_string(),
            net_qty,
            avg_price,
            mark_price,
            realized_pnl: realized,
            unrealized_pnl: unrealized,
            notional,
        });
    }
    PortfolioSnapshot {
        cash_balance,
        realized_pnl,
        equity: cash_balance + realized_pnl + unrealized_pnl,
        gross_exposure,
        net_exposure,
        unrealized_pnl,
        positions,
    }
}
