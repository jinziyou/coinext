//! `ExecutionClient` port adapter over the !Send simulated exchange.

use crate::SimulatedExecutionClient;
use coinext_core::UnixNanos;
use coinext_model::Venue;
use coinext_ports::ExecutionReport;
use std::cell::RefCell;

/// Adapter that makes the simulated venue speak the SAME `coinext_ports::ExecutionClient` port the
/// live `BinanceExecutionClient` implements — so backtest and live share ONE trait at the seam.
///
/// ## Why an adapter (not a direct `impl` on `SimulatedExecutionClient`)
/// `SimulatedExecutionClient` holds `Rc<dyn Clock>` and `Rc<RefCell<Cache>>` (it is intentionally
/// single-threaded `!Send`), and the deterministic backtest kernel consumes it through the inherent
/// PULL API (`on_submit` / `on_market` / `drain_due` / `peek_due`) so reports interleave on the
/// time-frontier. The port is `Send` and PUSHES `ExecutionReport`s over a `tokio::mpsc` channel.
/// Bolting the push model onto the hot path (or making the sim `Send`) would perturb the
/// deterministic core, so the deterministic backtest keeps using the inherent API BY DESIGN and the
/// port is provided here as a separate, owned wrapper.
///
/// `SimDriver` is the small `Send` seam the adapter owns instead of the `!Send` sim: it carries the
/// exact knobs needed to build + step a sim (venue, brokerage, instruments, latency clock) on its
/// own thread/runtime. The adapter drains the sim's `DelayedEventQueue` after each command and
/// forwards every due `ExecutionReport` onto the report channel taken via [`take_reports`], so a live
/// runtime drains the SAME report stream shape whether the venue is the sim or Binance.
///
/// Port adapter: bridges the sim to `ExecutionClient` (same report stream shape as a real venue).
/// A full sandbox runtime that advances the clock from a live data feed is owned by `LiveKernel`
/// / the trader service — this type only proves and provides the sim side of the port.
pub struct SimExecutionClientPort {
    venue: Venue,
    /// Builds the `!Send` sim lazily on the runtime thread (`connect`), keeping the adapter `Send`.
    build: Box<dyn FnMut() -> SimulatedExecutionClient + Send>,
    /// Interior sim; `pub(crate)` for integration tests that step `on_market` after submit.
    pub(crate) sim: RefCell<Option<SimulatedExecutionClient>>,
    tx: tokio::sync::mpsc::Sender<ExecutionReport>,
    rx: Option<tokio::sync::mpsc::Receiver<ExecutionReport>>,
}

// SAFETY: the adapter only ever touches the `!Send` `SimulatedExecutionClient` from within its own
// async methods on the runtime thread that called `connect`; the sim is never shared or moved across
// threads. The `build` closure and the channel are `Send`, so the wrapper is sound to move between
// tasks before `connect`. The deterministic backtest does NOT use this type — it uses the inherent
// sync API directly — so this assertion never affects the hot path's determinism guarantees.
unsafe impl Send for SimExecutionClientPort {}
// SAFETY: see `Send` above — the adapter is only ever driven from a single runtime thread (the one
// that called `connect`), so the `!Sync` interior (the sim behind a `RefCell`) is never accessed
// concurrently. `Sync` is required because the port's `&self` async methods hold `&self` across an
// await point, which the `async_trait` desugaring requires to be `Send`.
unsafe impl Sync for SimExecutionClientPort {}

impl SimExecutionClientPort {
    /// Construct the adapter. `build` is invoked once on `connect` (on the runtime thread) to create
    /// the underlying sim, so the `!Send` sim is never constructed until it is pinned to that thread.
    pub fn new(
        venue: Venue,
        build: impl FnMut() -> SimulatedExecutionClient + Send + 'static,
    ) -> Self {
        let (tx, rx) = tokio::sync::mpsc::channel(2048);
        SimExecutionClientPort {
            venue,
            build: Box::new(build),
            sim: RefCell::new(None),
            tx,
            rx: Some(rx),
        }
    }

    /// Drain every report the sim has scheduled up to/through `frontier` and push it onto the report
    /// channel (the live-shaped seam). Best-effort: a closed receiver simply stops the forwarding.
    fn pump_reports(&self, frontier: UnixNanos) {
        let reports = match self.sim.borrow().as_ref() {
            Some(sim) => sim.drain_due(frontier),
            None => return,
        };
        for r in reports {
            if self.tx.try_send(r).is_err() {
                break;
            }
        }
    }
}

#[async_trait::async_trait]
impl coinext_ports::ExecutionClient for SimExecutionClientPort {
    fn venue(&self) -> Venue {
        self.venue.clone()
    }

    async fn connect(&mut self) -> coinext_ports::PortResult<()> {
        if self.sim.borrow().is_none() {
            let sim = (self.build)();
            *self.sim.borrow_mut() = Some(sim);
        }
        Ok(())
    }

    async fn submit_order(&self, cmd: coinext_ports::SubmitOrder) -> coinext_ports::PortResult<()> {
        match self.sim.borrow().as_ref() {
            Some(sim) => sim.on_submit(cmd.order),
            None => return Err(coinext_ports::PortError::NotConnected),
        }
        // Forward any immediately-due reports (e.g. an Accepted scheduled at now+latency once the
        // clock has advanced) onto the channel. Fills follow as the data feed steps the clock.
        let now = self
            .sim
            .borrow()
            .as_ref()
            .map(|s| s.now_ns())
            .unwrap_or(UnixNanos::ZERO);
        self.pump_reports(now);
        Ok(())
    }

    async fn cancel_order(&self, cmd: coinext_ports::CancelOrder) -> coinext_ports::PortResult<()> {
        match self.sim.borrow().as_ref() {
            Some(sim) => sim.on_cancel(cmd.client_order_id),
            None => return Err(coinext_ports::PortError::NotConnected),
        }
        let now = self
            .sim
            .borrow()
            .as_ref()
            .map(|s| s.now_ns())
            .unwrap_or(UnixNanos::ZERO);
        self.pump_reports(now);
        Ok(())
    }

    async fn modify_order(
        &self,
        _cmd: coinext_ports::ModifyOrder,
    ) -> coinext_ports::PortResult<()> {
        // The sim does not model order modify (the deterministic kernel skips it too); the live
        // path handles cancel-replace. Explicitly unsupported so callers fail fast.
        Err(coinext_ports::PortError::Unsupported(
            "SimulatedExecutionClient does not model order modify".into(),
        ))
    }

    async fn reconcile(&self) -> coinext_ports::PortResult<Vec<ExecutionReport>> {
        // The sim is the source of truth in-process; there is no external venue to diff against.
        Ok(Vec::new())
    }

    fn take_reports(&mut self) -> tokio::sync::mpsc::Receiver<ExecutionReport> {
        self.rx
            .take()
            .expect("SimExecutionClientPort::take_reports called more than once")
    }

    async fn disconnect(&mut self) -> coinext_ports::PortResult<()> {
        *self.sim.borrow_mut() = None;
        Ok(())
    }
}
