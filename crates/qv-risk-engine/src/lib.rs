//! `qv-risk-engine` — the pre-trade risk gate. Every order is checked synchronously on the core
//! thread BEFORE reaching a venue; on failure it becomes Denied and never leaves the process.
//! Holds the atomic global kill-switch. The SAME engine runs in backtest and live, so
//! risk-shaped behavior is reproducible.

use qv_cache::Cache;
use qv_model::{Instrument, Order, OrderSide, PositionSide};
use qv_ports::{DenyReason, Portfolio, RiskDecision, RiskEngine, RiskLimits};
use rust_decimal::Decimal;
use std::cell::RefCell;
use std::rc::Rc;
use std::sync::atomic::{AtomicBool, Ordering};

pub struct RiskGate {
    cache: Rc<RefCell<Cache>>,
    limits: RiskLimits,
    killed: AtomicBool,
}

impl RiskGate {
    pub fn new(cache: Rc<RefCell<Cache>>, limits: RiskLimits) -> Self {
        RiskGate {
            cache,
            limits,
            killed: AtomicBool::new(false),
        }
    }

    fn signed_position_qty(portfolio: &dyn Portfolio, order: &Order) -> Decimal {
        match portfolio.position(&order.instrument_id) {
            Some(p) => match p.side {
                PositionSide::Long => p.quantity.as_decimal(),
                PositionSide::Short => -p.quantity.as_decimal(),
                PositionSide::Flat => Decimal::ZERO,
            },
            None => Decimal::ZERO,
        }
    }
}

impl RiskEngine for RiskGate {
    fn check(
        &self,
        order: &Order,
        portfolio: &dyn Portfolio,
        inst: &dyn Instrument,
    ) -> RiskDecision {
        use DenyReason::*;
        if self.is_killed() {
            return RiskDecision::Denied(KillSwitchEngaged);
        }

        let mult = inst.multiplier().as_decimal();
        // Reference price: the limit price, else the current mark from the Cache.
        let ref_px = order
            .price
            .or_else(|| self.cache.borrow().mark(&order.instrument_id));

        if let Some(px) = ref_px {
            let notional = px.as_decimal() * order.quantity.as_decimal() * mult;
            if let Some(min) = inst.min_notional() {
                if notional < min.amount() {
                    return RiskDecision::Denied(MinNotional);
                }
            }
            if let Some(maxn) = &self.limits.max_order_notional {
                if notional > maxn.amount() {
                    return RiskDecision::Denied(MaxOrderNotional);
                }
            }
        }

        if let Some(maxq) = &self.limits.max_position_qty {
            let cur = Self::signed_position_qty(portfolio, order);
            let delta = Decimal::from(order.side.sign()) * order.quantity.as_decimal();
            let prospective = (cur + delta).abs();
            if prospective > maxq.as_decimal() {
                return RiskDecision::Denied(MaxPositionExceeded);
            }
        }

        if let Some(maxg) = &self.limits.max_gross_exposure {
            let gross = portfolio.gross_exposure();
            if gross.currency() == maxg.currency() && gross.amount() > maxg.amount() {
                return RiskDecision::Denied(MaxNotionalExceeded);
            }
        }

        let _ = OrderSide::Buy; // (rate-limit throttle is a live-only concern; omitted in scaffold)
        RiskDecision::Approved
    }

    fn set_kill_switch(&self, engaged: bool) {
        self.killed.store(engaged, Ordering::SeqCst);
    }

    fn is_killed(&self) -> bool {
        self.killed.load(Ordering::SeqCst)
    }
}
