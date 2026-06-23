//! The BrokerageModel — venue ECONOMICS separated from venue CONNECTION (LEAN's split). The exact
//! same fee/slippage/latency model used in backtest is registered for the venue in live config, so
//! backtest and live agree on economics, not just on order flow.

use coinext_model::{Instrument, LiquiditySide, Money, Order, OrderSide, Price, Quantity};
use rust_decimal::{Decimal, RoundingStrategy};

/// Which command's latency is being asked for (submit→ack differs from cancel/modify).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CommandKind {
    Submit,
    Cancel,
    Modify,
}

/// Pluggable venue economics. Implementors decide fills, fees, slippage, and latency.
pub trait BrokerageModel {
    /// Latency from command to its first report, in nanoseconds.
    fn latency_ns(&self, kind: CommandKind) -> u64;
    /// The realized fill price for a marketable order given a reference price (applies slippage).
    ///
    /// `bar_range` is the current bar's `(low, high)` when one is known (OHLC-aware slippage); pass
    /// `None` (or `low == high`) to fall back to a pure reference-price ± fixed-bps model.
    fn fill_price(
        &self,
        order: &Order,
        ref_px: Price,
        bar_range: Option<(Price, Price)>,
        inst: &dyn Instrument,
    ) -> Price;
    /// How much of a resting order may fill against THIS bar/event, given the order's remaining
    /// (`leaves`) quantity and the event's traded `bar_volume`. The default is no cap (fill all
    /// `leaves`); a volume-participation model caps a single bar's fill to a share of its volume so a
    /// large order fills over multiple bars. MUST return `<= leaves` (never over-fill).
    fn fillable_qty(
        &self,
        leaves: Quantity,
        _bar_volume: Quantity,
        _inst: &dyn Instrument,
    ) -> Quantity {
        leaves
    }
    /// Estimated volume resting AHEAD of a freshly-placed limit order at its price level — the queue
    /// it must wait behind before filling (bar-based estimate; there is no real L2 book). Seeded the
    /// first bar the order becomes crossable. The default is `0` (no queue → fill on first cross,
    /// the pre-queue behavior).
    fn initial_queue_ahead(&self, bar_volume: Quantity, _inst: &dyn Instrument) -> Quantity {
        Quantity::zero(bar_volume.precision())
    }
    /// The fee charged for a fill, as first-class `Money` in the settlement currency.
    fn fee(
        &self,
        order: &Order,
        fill_px: Price,
        fill_qty: Quantity,
        liquidity: LiquiditySide,
        inst: &dyn Instrument,
    ) -> Money;
}

/// A reasonable default: fixed base slippage in basis points plus an intrabar-range component
/// (adverse to the order side, capped at the bar's extreme), maker/taker fees from the instrument,
/// constant latency, and **volume-participation partial fills** (a resting order fills at most
/// `participation_rate` of each bar's volume).
#[derive(Debug, Clone)]
pub struct DefaultBrokerageModel {
    /// Fixed base slippage on the reference price, in basis points (1 = 0.01%).
    pub slippage_bps: Decimal,
    /// Extra market-order slippage as a fraction of the bar's `(high - low)` range, capped so the
    /// fill is never worse than the bar's high (buy) / low (sell). `0` disables the range component.
    pub range_impact: Decimal,
    /// Max share of a bar's volume one resting order may take per bar (`0` or volume `0` = no cap →
    /// fill fully). A large order vs thin volume then fills across several bars.
    pub participation_rate: Decimal,
    /// Queue-position estimate: a freshly-placed limit assumes `queue_ahead_factor` × the crossing
    /// bar's volume rests ahead of it, which must trade through before it fills (a TOUCH of the
    /// level pays the queue down; a price that trades THROUGH the level sweeps it). `0` = no queue =
    /// fill on first cross (the pre-queue behavior). Most existing fills are through-crosses, so a
    /// positive value only delays orders the price merely *touches*.
    pub queue_ahead_factor: Decimal,
    pub latency_ns: u64,
}

impl Default for DefaultBrokerageModel {
    fn default() -> Self {
        DefaultBrokerageModel {
            slippage_bps: Decimal::new(1, 0),        // 1 bp base
            range_impact: Decimal::new(1, 1),        // 0.1 of the bar range
            participation_rate: Decimal::new(25, 2), // 0.25 of a bar's volume
            queue_ahead_factor: Decimal::ZERO,       // queue modeling OFF by default (opt-in)
            latency_ns: 1_000_000,
        }
    }
}

impl BrokerageModel for DefaultBrokerageModel {
    fn latency_ns(&self, _kind: CommandKind) -> u64 {
        self.latency_ns
    }

    fn fill_price(
        &self,
        order: &Order,
        ref_px: Price,
        bar_range: Option<(Price, Price)>,
        inst: &dyn Instrument,
    ) -> Price {
        let ref_d = ref_px.as_decimal();
        let base_slip = ref_d * self.slippage_bps / Decimal::from(10_000);
        // Range component only when a real intrabar range is known.
        let real_range = bar_range.filter(|(lo, hi)| hi.as_decimal() > lo.as_decimal());
        let range_slip = match real_range {
            Some((lo, hi)) => (hi.as_decimal() - lo.as_decimal()) * self.range_impact,
            None => Decimal::ZERO,
        };
        // Base slippage (spread/impact) always applies; only the RANGE component is bounded by the
        // bar extreme, so a bar that closes at its high/low can't swallow the base slippage.
        let adjusted = match order.side {
            OrderSide::Buy => {
                let base = ref_d + base_slip;
                let full = base + range_slip;
                match real_range {
                    Some((_, hi)) => full.min(hi.as_decimal()).max(base),
                    None => full,
                }
            }
            OrderSide::Sell => {
                let base = ref_d - base_slip;
                let full = base - range_slip;
                match real_range {
                    Some((lo, _)) => full.max(lo.as_decimal()).min(base),
                    None => full,
                }
            }
        };
        inst.make_price(adjusted).unwrap_or(ref_px)
    }

    fn fillable_qty(
        &self,
        leaves: Quantity,
        bar_volume: Quantity,
        inst: &dyn Instrument,
    ) -> Quantity {
        // No cap when participation is disabled or the bar carries no volume (e.g. a close-only
        // series): fill the whole remaining quantity, preserving the pre-participation behavior.
        if self.participation_rate <= Decimal::ZERO || bar_volume.as_decimal() <= Decimal::ZERO {
            return leaves;
        }
        let prec = leaves.precision();
        // Floor the participation share to a whole lot (ToZero) so we never round UP past the cap.
        let floored = (self.participation_rate * bar_volume.as_decimal())
            .round_dp_with_strategy(prec as u32, RoundingStrategy::ToZero);
        // Forward-progress floor: a genuine cross with real volume fills at LEAST one lot, so a
        // thin-volume bar (share rounds below a lot) can't stall a crossing order forever.
        let one_lot = inst.size_increment().as_decimal();
        let fillable = floored.max(one_lot).min(leaves.as_decimal());
        // `fillable <= leaves` (already at `prec`), so from_decimal cannot round it above leaves.
        Quantity::from_decimal(fillable, prec).unwrap_or(leaves)
    }

    fn initial_queue_ahead(&self, bar_volume: Quantity, _inst: &dyn Instrument) -> Quantity {
        if self.queue_ahead_factor <= Decimal::ZERO {
            return Quantity::zero(bar_volume.precision());
        }
        let prec = bar_volume.precision();
        // Floor to a whole lot (ToZero) so the queue is lot-aligned and depletes to exactly zero.
        let q = (self.queue_ahead_factor * bar_volume.as_decimal())
            .round_dp_with_strategy(prec as u32, RoundingStrategy::ToZero);
        Quantity::from_decimal(q, prec).unwrap_or_else(|_| Quantity::zero(prec))
    }

    fn fee(
        &self,
        _order: &Order,
        fill_px: Price,
        fill_qty: Quantity,
        liquidity: LiquiditySide,
        inst: &dyn Instrument,
    ) -> Money {
        let rate = match liquidity {
            LiquiditySide::Maker => inst.maker_fee(),
            LiquiditySide::Taker => inst.taker_fee(),
        };
        // Fees are charged on the fill NOTIONAL in the settlement currency. Linear: `px*qty*mult`
        // (quote ccy). Inverse (coin-margined): `qty*mult/px` (coin ccy) — charging the linear
        // notional on an inverse perp would denominate the fee in the wrong currency/magnitude.
        let px = fill_px.as_decimal();
        let mult = inst.multiplier().as_decimal();
        let qty = fill_qty.as_decimal();
        let notional = if inst.is_inverse() {
            if px.is_zero() {
                Decimal::ZERO
            } else {
                qty * mult / px
            }
        } else {
            px * qty * mult
        };
        Money::from_decimal(notional * rate, inst.settlement_currency())
            .unwrap_or_else(|_| Money::zero(inst.settlement_currency()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use coinext_model::{
        ClientOrderId, CryptoPerpetual, CurrencyPair, InstrumentId, OrderType, StrategyId,
        TimeInForce, VenueOrderId,
    };
    use coinext_model::{Currency, OrderFlags, UnixNanos};
    use rust_decimal_macros::dec;

    fn order(side: OrderSide, iid: &InstrumentId) -> Order {
        Order::new(
            StrategyId::from("s"),
            ClientOrderId::from("c"),
            iid.clone(),
            side,
            OrderType::Market,
            Quantity::from_decimal(dec!(100), 0).unwrap(),
            None,
            None,
            TimeInForce::Ioc,
            OrderFlags::default(),
            UnixNanos(0),
        )
    }

    // Linear fee = px*qty*mult*rate, in the quote/settle currency.
    #[test]
    fn linear_fee_uses_quote_notional() {
        let usdt = Currency::new("USDT", 8).unwrap();
        let btc = Currency::new("BTC", 8).unwrap();
        let id = InstrumentId::parse("BTCUSDT.BINANCE").unwrap();
        let inst = CurrencyPair {
            id: id.clone(),
            base: btc,
            quote: usdt,
            price_precision: 2,
            size_precision: 0,
            price_increment: Price::from_decimal(dec!(0.01), 2).unwrap(),
            size_increment: Quantity::from_decimal(dec!(1), 0).unwrap(),
            min_notional: None,
            maker_fee: dec!(0.0002),
            taker_fee: dec!(0.0004),
        };
        let m = DefaultBrokerageModel::default();
        let fee = m.fee(
            &order(OrderSide::Buy, &id),
            Price::from_decimal(dec!(50000), 2).unwrap(),
            Quantity::from_decimal(dec!(100), 0).unwrap(),
            LiquiditySide::Taker,
            &inst,
        );
        // 50000*100*1 * 0.0004 = 2000 USDT.
        assert_eq!(fee.currency(), usdt);
        assert_eq!(fee.amount(), dec!(2000));
    }

    // Inverse fee = (qty*mult/px)*rate, in the COIN (settle) currency — NOT px*qty*mult.
    #[test]
    fn inverse_fee_uses_coin_notional() {
        let usdt = Currency::new("USDT", 8).unwrap();
        let btc = Currency::new("BTC", 8).unwrap();
        let id = InstrumentId::parse("BTCUSD.BINANCE").unwrap();
        let inst = CryptoPerpetual {
            id: id.clone(),
            base: btc,
            quote: usdt,
            settlement: btc,
            price_precision: 1,
            size_precision: 0,
            price_increment: Price::from_decimal(dec!(0.1), 1).unwrap(),
            size_increment: Quantity::from_decimal(dec!(1), 0).unwrap(),
            min_notional: None,
            multiplier: Quantity::from_raw(1, 0).unwrap(),
            maker_fee: dec!(0.0002),
            taker_fee: dec!(0.0004),
            is_inverse: true,
            funding_interval_ns: 0,
        };
        let m = DefaultBrokerageModel::default();
        let _ = VenueOrderId::from("v"); // keep the import used regardless of feature flags
        let fee = m.fee(
            &order(OrderSide::Buy, &id),
            Price::from_decimal(dec!(50000), 1).unwrap(),
            Quantity::from_decimal(dec!(100), 0).unwrap(),
            LiquiditySide::Taker,
            &inst,
        );
        // coin notional = 100/50000 = 0.002 BTC; fee = 0.002 * 0.0004 = 0.0000008 BTC.
        assert_eq!(fee.currency(), btc);
        assert_eq!(fee.amount(), dec!(0.0000008));
    }
}
