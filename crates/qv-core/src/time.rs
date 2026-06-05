//! Time primitives. The whole platform timestamps on `UnixNanos` (u64 nanoseconds since the
//! Unix epoch). `ts_event` (venue time) drives the deterministic time-frontier ordering that
//! structurally prevents look-ahead; `ts_init` (ingest time) enables latency measurement.

use chrono::{DateTime, TimeZone, Utc};
use serde::{Deserialize, Serialize};

/// Nanoseconds since the Unix epoch. Copy, totally ordered — the merge-sort key for the
/// backtest time-frontier (market data + delayed sim fills + timers all order by this).
#[derive(
    Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize, Default,
)]
pub struct UnixNanos(pub u64);

impl UnixNanos {
    pub const ZERO: UnixNanos = UnixNanos(0);

    #[inline]
    pub fn as_u64(self) -> u64 {
        self.0
    }

    /// Saturating add of a nanosecond duration — used to schedule `now + latency_ns`.
    #[inline]
    pub fn saturating_add_ns(self, ns: u64) -> UnixNanos {
        UnixNanos(self.0.saturating_add(ns))
    }

    /// Convert to a `chrono` UTC datetime (display/analytics only).
    pub fn to_datetime(self) -> DateTime<Utc> {
        let secs = (self.0 / 1_000_000_000) as i64;
        let nanos = (self.0 % 1_000_000_000) as u32;
        Utc.timestamp_opt(secs, nanos)
            .single()
            .unwrap_or_else(Utc::now)
    }

    pub fn from_datetime(dt: DateTime<Utc>) -> UnixNanos {
        let secs = dt.timestamp().max(0) as u64;
        let nanos = dt.timestamp_subsec_nanos() as u64;
        UnixNanos(secs.saturating_mul(1_000_000_000).saturating_add(nanos))
    }
}

impl From<u64> for UnixNanos {
    fn from(v: u64) -> Self {
        UnixNanos(v)
    }
}

impl std::fmt::Display for UnixNanos {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.0)
    }
}
