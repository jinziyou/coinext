//! `coinext-risk-engine` — the pre-trade risk gate. Every order is checked synchronously on the core
//! thread BEFORE reaching a venue; on failure it becomes Denied and never leaves the process.
//! Holds the atomic global kill-switch. The SAME engine runs in backtest and live, so
//! risk-shaped behavior is reproducible.

use coinext_cache::Cache;
use coinext_model::{Instrument, Order, PositionSide};
use coinext_ports::{DenyReason, Portfolio, RiskDecision, RiskEngine, RiskLimits};
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

        // Signed current position and the order's signed delta — shared by the position-qty and
        // initial-margin (leverage) checks below.
        let cur = Self::signed_position_qty(portfolio, order);
        let delta = Decimal::from(order.side.sign()) * order.quantity.as_decimal();

        if let Some(maxq) = &self.limits.max_position_qty {
            let prospective = (cur + delta).abs();
            if prospective > maxq.as_decimal() {
                return RiskDecision::Denied(MaxPositionExceeded);
            }
        }

        if let Some(maxg) = &self.limits.max_gross_exposure {
            let gross = portfolio.gross_exposure();
            if gross.currency() == maxg.currency() && gross.amount() > maxg.amount() {
                return RiskDecision::Denied(MaxGrossExposureExceeded);
            }
        }

        // Initial-margin gate: an order that INCREASES exposure needs `added_notional / leverage` of
        // free equity (equity minus margin already locked up). A reducing/closing order frees margin
        // and is always allowed through this check. `leverage = None` = fully funded buying power.
        if let (Some(lev), Some(px)) = (self.limits.leverage, ref_px) {
            if lev > Decimal::ZERO {
                let added_qty = (cur + delta).abs() - cur.abs();
                if added_qty > Decimal::ZERO {
                    let added_notional = px.as_decimal() * added_qty * mult;
                    let equity = portfolio.equity().amount();
                    let used = portfolio.gross_exposure().amount() / lev;
                    let free = equity - used;
                    if added_notional / lev > free {
                        return RiskDecision::Denied(InsufficientMargin);
                    }
                }
            }
        }

        // (rate-limit throttle is a live-only concern; omitted in scaffold)
        RiskDecision::Approved
    }

    fn set_kill_switch(&self, engaged: bool) {
        self.killed.store(engaged, Ordering::SeqCst);
    }

    fn is_killed(&self) -> bool {
        self.killed.load(Ordering::SeqCst)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use coinext_core::{Currency, Money, Price, Quantity, UnixNanos};
    use coinext_model::{
        ClientOrderId, CurrencyPair, InstrumentId, OrderFlags, OrderSide, OrderType, Position,
        StrategyId, TimeInForce,
    };
    use coinext_ports::{DenyReason, RiskDecision};
    use rust_decimal_macros::dec;
    use std::sync::Arc;

    /// Flat portfolio with a configurable equity/gross — enough to exercise the leverage gate.
    struct StubPortfolio {
        settle: Currency,
        equity: Decimal,
        gross: Decimal,
    }
    impl Portfolio for StubPortfolio {
        fn position(&self, _id: &InstrumentId) -> Option<Position> {
            None
        }
        fn net_exposure(&self, _id: &InstrumentId) -> Money {
            Money::zero(self.settle)
        }
        fn unrealized_pnl(&self, _id: &InstrumentId) -> Money {
            Money::zero(self.settle)
        }
        fn realized_pnl(&self, _id: &InstrumentId) -> Money {
            Money::zero(self.settle)
        }
        fn gross_exposure(&self) -> Money {
            Money::from_decimal(self.gross, self.settle).unwrap()
        }
        fn balance(&self, ccy: &Currency) -> Money {
            Money::zero(*ccy)
        }
        fn equity(&self) -> Money {
            Money::from_decimal(self.equity, self.settle).unwrap()
        }
    }

    fn setup() -> (Rc<RefCell<Cache>>, Arc<dyn Instrument>, Currency) {
        let usdt = Currency::new("USDT", 8).unwrap();
        let btc = Currency::new("BTC", 8).unwrap();
        let id = InstrumentId::parse("BTCUSDT.BINANCE").unwrap();
        let inst: Arc<dyn Instrument> = Arc::new(CurrencyPair {
            id: id.clone(),
            base: btc,
            quote: usdt,
            price_precision: 2,
            size_precision: 3,
            price_increment: Price::from_decimal(dec!(0.01), 2).unwrap(),
            size_increment: Quantity::from_decimal(dec!(0.001), 3).unwrap(),
            min_notional: None,
            maker_fee: dec!(0.0002),
            taker_fee: dec!(0.0004),
        });
        let mut cache = Cache::new();
        cache.add_instrument(inst.clone());
        cache.set_mark(id, Price::from_decimal(dec!(50000), 2).unwrap());
        (Rc::new(RefCell::new(cache)), inst, usdt)
    }

    fn market(iid: &InstrumentId, qty: &str) -> Order {
        Order::new(
            StrategyId::from("s"),
            ClientOrderId::from("c"),
            iid.clone(),
            OrderSide::Buy,
            OrderType::Market,
            Quantity::from_decimal(qty.parse().unwrap(), 3).unwrap(),
            None,
            None,
            TimeInForce::Ioc,
            OrderFlags::default(),
            UnixNanos(0),
        )
    }

    // An over-notional order (vs the configured cap) is Denied; a within-cap one is Approved.
    #[test]
    fn over_notional_order_is_denied() {
        let (cache, inst, usdt) = setup();
        let mut limits = RiskLimits::unlimited();
        limits.max_order_notional = Some(Money::from_decimal(dec!(40000), usdt).unwrap());
        let gate = RiskGate::new(cache, limits);
        let pf = StubPortfolio {
            settle: usdt,
            equity: dec!(1_000_000),
            gross: dec!(0),
        };
        // qty 1 @ mark 50000 -> notional 50000 > 40000 cap.
        let denied = gate.check(&market(&inst.id(), "1"), &pf, &*inst);
        assert!(matches!(
            denied,
            RiskDecision::Denied(DenyReason::MaxOrderNotional)
        ));
        // qty 0.5 -> notional 25000 < cap -> approved.
        let ok = gate.check(&market(&inst.id(), "0.5"), &pf, &*inst);
        assert!(matches!(ok, RiskDecision::Approved));
    }

    // An order needing more initial margin than free equity (leverage gate) is Denied.
    #[test]
    fn over_leverage_order_is_denied() {
        let (cache, inst, usdt) = setup();
        let mut limits = RiskLimits::unlimited();
        limits.leverage = Some(dec!(2)); // 2x: need added_notional/2 of free equity
        let gate = RiskGate::new(cache, limits);
        // qty 1 @ 50000 -> added_notional 50000 -> needs 25000 free; only 1000 available -> denied.
        let pf = StubPortfolio {
            settle: usdt,
            equity: dec!(1000),
            gross: dec!(0),
        };
        let denied = gate.check(&market(&inst.id(), "1"), &pf, &*inst);
        assert!(matches!(
            denied,
            RiskDecision::Denied(DenyReason::InsufficientMargin)
        ));
        // With ample equity the same order is approved.
        let rich = StubPortfolio {
            settle: usdt,
            equity: dec!(1_000_000),
            gross: dec!(0),
        };
        assert!(matches!(
            gate.check(&market(&inst.id(), "1"), &rich, &*inst),
            RiskDecision::Approved
        ));
    }

    // The kill-switch denies EVERY order while engaged, and releasing it restores approval.
    #[test]
    fn kill_switch_denies_all_orders() {
        let (cache, inst, usdt) = setup();
        let gate = RiskGate::new(cache, RiskLimits::unlimited());
        let pf = StubPortfolio {
            settle: usdt,
            equity: dec!(1_000_000),
            gross: dec!(0),
        };
        assert!(matches!(
            gate.check(&market(&inst.id(), "1"), &pf, &*inst),
            RiskDecision::Approved
        ));
        gate.set_kill_switch(true);
        assert!(gate.is_killed());
        assert!(matches!(
            gate.check(&market(&inst.id(), "1"), &pf, &*inst),
            RiskDecision::Denied(DenyReason::KillSwitchEngaged)
        ));
        gate.set_kill_switch(false);
        assert!(matches!(
            gate.check(&market(&inst.id(), "1"), &pf, &*inst),
            RiskDecision::Approved
        ));
    }
}
