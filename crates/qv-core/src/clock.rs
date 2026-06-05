//! Clock abstraction — the second parity seam. `HistoricalClock` advances on the data
//! time-frontier with no sleeping (backtest); `SystemClock` reads the wall clock (live).
//! Timers fire a `TimerEvent` back into the core, where the kernel dispatches it to
//! `Strategy::on_timer`. In backtest, timers are due-checked against the SAME frontier as
//! market data and simulated fills, so a timer set for `now + 60s` fires at the right point
//! in the historical stream.

use crate::time::UnixNanos;
use chrono::{DateTime, Utc};
use std::cell::{Cell, RefCell};
use std::cmp::Reverse;
use std::collections::{BinaryHeap, HashSet};

/// Opaque timer handle returned by `set_timer`, used to cancel.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, PartialOrd, Ord)]
pub struct TimerId(pub u64);

/// Delivered to `Strategy::on_timer` when a timer fires.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct TimerEvent {
    pub id: TimerId,
    pub name: String,
    pub ts_event: UnixNanos,
}

/// Time + timers. Not `Send + Sync`: the deterministic core is single-threaded, so the clock
/// uses cheap interior mutability. The live runtime wraps a shared clock behind its own sync.
pub trait Clock {
    fn now_ns(&self) -> UnixNanos;
    fn timestamp(&self) -> DateTime<Utc> {
        self.now_ns().to_datetime()
    }
    /// Register a timer that fires a `TimerEvent` into the core at `at`. Returns its id.
    fn set_timer(&self, name: &str, at: UnixNanos) -> TimerId;
    fn cancel_timer(&self, id: TimerId);
    /// The next pending (non-cancelled) timer's fire time — used by the kernel time-frontier merge.
    fn peek_next_timer(&self) -> Option<UnixNanos>;
}

/// Backtest clock. The kernel calls `advance_to` as the frontier moves and `pop_due` to fire
/// timers in timestamp order, interleaved with market data and simulated fills.
pub struct HistoricalClock {
    now: Cell<UnixNanos>,
    heap: RefCell<BinaryHeap<Reverse<(UnixNanos, TimerId)>>>,
    cancelled: RefCell<HashSet<TimerId>>,
    names: RefCell<std::collections::HashMap<TimerId, String>>,
    next_id: Cell<u64>,
}

impl HistoricalClock {
    pub fn new(start: UnixNanos) -> Self {
        HistoricalClock {
            now: Cell::new(start),
            heap: RefCell::new(BinaryHeap::new()),
            cancelled: RefCell::new(HashSet::new()),
            names: RefCell::new(std::collections::HashMap::new()),
            next_id: Cell::new(1),
        }
    }

    /// Advance the clock to `ts` (monotonic; never moves backwards).
    pub fn advance_to(&self, ts: UnixNanos) {
        if ts > self.now.get() {
            self.now.set(ts);
        }
    }

    /// Pop and return all timers due at or before `frontier`, in timestamp order, as
    /// `TimerEvent`s. Cancelled timers are silently dropped.
    pub fn pop_due(&self, frontier: UnixNanos) -> Vec<TimerEvent> {
        let mut out = Vec::new();
        let mut heap = self.heap.borrow_mut();
        let mut cancelled = self.cancelled.borrow_mut();
        let mut names = self.names.borrow_mut();
        while let Some(Reverse((ts, id))) = heap.peek().copied() {
            if ts > frontier {
                break;
            }
            heap.pop();
            if cancelled.remove(&id) {
                names.remove(&id);
                continue;
            }
            let name = names.remove(&id).unwrap_or_default();
            out.push(TimerEvent {
                id,
                name,
                ts_event: ts,
            });
        }
        out
    }
}

impl Clock for HistoricalClock {
    fn now_ns(&self) -> UnixNanos {
        self.now.get()
    }

    fn set_timer(&self, name: &str, at: UnixNanos) -> TimerId {
        let id = TimerId(self.next_id.get());
        self.next_id.set(self.next_id.get() + 1);
        self.heap.borrow_mut().push(Reverse((at, id)));
        self.names.borrow_mut().insert(id, name.to_string());
        id
    }

    fn cancel_timer(&self, id: TimerId) {
        self.cancelled.borrow_mut().insert(id);
    }

    fn peek_next_timer(&self) -> Option<UnixNanos> {
        let heap = self.heap.borrow();
        let cancelled = self.cancelled.borrow();
        // Skip cancelled timers at the top lazily.
        heap.iter()
            .filter(|Reverse((_, id))| !cancelled.contains(id))
            .map(|Reverse((ts, _))| *ts)
            .min()
    }
}

/// Live wall-clock. Timer delivery in live is handled by the runtime (Tokio); for the scaffold
/// this records timers and reports the next due time, leaving firing to the live node.
pub struct SystemClock {
    heap: RefCell<BinaryHeap<Reverse<(UnixNanos, TimerId)>>>,
    next_id: Cell<u64>,
}

impl Default for SystemClock {
    fn default() -> Self {
        SystemClock::new()
    }
}

impl SystemClock {
    pub fn new() -> Self {
        SystemClock {
            heap: RefCell::new(BinaryHeap::new()),
            next_id: Cell::new(1),
        }
    }
}

impl Clock for SystemClock {
    fn now_ns(&self) -> UnixNanos {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos() as u64)
            .unwrap_or(0);
        UnixNanos(now)
    }

    fn set_timer(&self, _name: &str, at: UnixNanos) -> TimerId {
        let id = TimerId(self.next_id.get());
        self.next_id.set(self.next_id.get() + 1);
        self.heap.borrow_mut().push(Reverse((at, id)));
        id
    }

    fn cancel_timer(&self, _id: TimerId) {}

    fn peek_next_timer(&self) -> Option<UnixNanos> {
        self.heap.borrow().peek().map(|Reverse((ts, _))| *ts)
    }
}
