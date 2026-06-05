//! Token-bucket rate limiter — the gate every outbound request passes through so adapters never
//! trip a venue's weight ban. Venues bill requests by *weight*, so `acquire` takes a cost.
//!
//! Backed by [`governor`]'s GCRA limiter. Binance publishes a REQUEST_WEIGHT pool ("1200 weight /
//! minute" on spot) and a separate ORDER pool ("50 orders / 10s, 160000 / day"); a [`RateLimiter`]
//! models a single pool and an adapter holds one per pool. `acquire(cost)` waits until `cost`
//! weight cells are available (the governor crate clamps `until_n_ready` internally), giving async
//! backpressure rather than unbounded memory growth.

use crate::error::NetError;
use crate::error::NetResult;
use governor::clock::DefaultClock;
use governor::state::{InMemoryState, NotKeyed};
use governor::{Quota, RateLimiter as Governor};
use std::num::NonZeroU32;
use std::sync::Arc;
use std::time::Duration;

/// A per-venue (or per-endpoint-group) weight limiter. Cloneable (shares the underlying governor
/// state via `Arc`) so the REST client and any background poll loops charge the SAME budget.
#[derive(Clone)]
pub struct RateLimiter {
    capacity: u32,
    inner: Arc<Governor<NotKeyed, InMemoryState, DefaultClock>>,
}

impl RateLimiter {
    /// Build a limiter that replenishes `capacity` weight cells per `period`. The bucket also bursts
    /// up to `capacity` cells. For Binance spot REST use `per_minute(1200)`.
    pub fn new(capacity: u32, period: Duration) -> Self {
        let cap = NonZeroU32::new(capacity.max(1)).expect("capacity >= 1");
        // `with_period` sets the replenish interval for ONE cell; `allow_burst` sets bucket size so
        // the steady-state rate is `capacity` cells per `period`.
        let cell_period = period / capacity.max(1);
        let quota = Quota::with_period(cell_period)
            .expect("non-zero cell period")
            .allow_burst(cap);
        RateLimiter {
            capacity,
            inner: Arc::new(Governor::direct(quota)),
        }
    }

    /// Binance REQUEST_WEIGHT convenience: `weight` cells per minute (spot default is 1200).
    pub fn per_minute(weight: u32) -> Self {
        RateLimiter::new(weight, Duration::from_secs(60))
    }

    /// Binance ORDER pool convenience: `orders` cells per `secs` seconds (spot default 50 / 10s).
    pub fn per_secs(orders: u32, secs: u64) -> Self {
        RateLimiter::new(orders, Duration::from_secs(secs.max(1)))
    }

    /// Configured bucket capacity (max burst weight).
    pub fn capacity(&self) -> u32 {
        self.capacity
    }

    /// Acquire `cost` weight cells, awaiting replenishment if the bucket is short. A `cost` larger
    /// than the bucket capacity can never be satisfied, so it fails fast with [`NetError::Auth`]-
    /// adjacent `RateLimited` rather than deadlocking.
    pub async fn acquire(&self, cost: u32) -> NetResult<()> {
        let n = match NonZeroU32::new(cost) {
            Some(n) => n,
            None => return Ok(()), // zero-weight request: nothing to charge.
        };
        if cost > self.capacity {
            return Err(NetError::RateLimited {
                retry_after_ms: 0,
            });
        }
        self.inner
            .until_n_ready(n)
            .await
            .map_err(|_| NetError::RateLimited { retry_after_ms: 0 })
    }

    /// Non-blocking attempt to take `cost` cells; `false` if the bucket is currently short.
    pub fn try_acquire(&self, cost: u32) -> bool {
        match NonZeroU32::new(cost) {
            None => true,
            Some(n) => self.inner.check_n(n).map(|r| r.is_ok()).unwrap_or(false),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fresh_bucket_admits_a_burst_up_to_capacity() {
        let rl = RateLimiter::per_minute(1200);
        assert_eq!(rl.capacity(), 1200);
        // A fresh GCRA bucket is full, so a single sub-capacity charge passes immediately.
        assert!(rl.try_acquire(10));
    }

    #[test]
    fn try_acquire_zero_is_free() {
        let rl = RateLimiter::per_secs(50, 10);
        assert!(rl.try_acquire(0));
    }

    #[test]
    fn over_capacity_try_acquire_is_refused() {
        let rl = RateLimiter::new(5, Duration::from_secs(1));
        // Asking for more than the whole bucket can never succeed.
        assert!(!rl.try_acquire(6));
    }

    #[tokio::test]
    async fn acquire_over_capacity_fails_fast() {
        let rl = RateLimiter::new(5, Duration::from_secs(1));
        let err = rl.acquire(6).await.unwrap_err();
        assert!(matches!(err, NetError::RateLimited { .. }));
    }

    #[tokio::test]
    async fn acquire_zero_is_immediate() {
        let rl = RateLimiter::per_minute(1200);
        rl.acquire(0).await.unwrap();
    }
}
