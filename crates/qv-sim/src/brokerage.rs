//! The BrokerageModel — venue ECONOMICS separated from venue CONNECTION (LEAN's split). The exact
//! same fee/slippage/latency model used in backtest is registered for the venue in live config, so
//! backtest and live agree on economics, not just on order flow.

use qv_model::{Instrument, LiquiditySide, Money, Order, OrderSide, Price, Quantity};
use rust_decimal::Decimal;

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
    fn fill_price(&self, order: &Order, ref_px: Price, inst: &dyn Instrument) -> Price;
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

/// A reasonable default: fixed slippage in basis points (adverse to the order side), maker/taker
/// fees from the instrument, and constant latency.
#[derive(Debug, Clone)]
pub struct DefaultBrokerageModel {
    pub slippage_bps: Decimal,
    pub latency_ns: u64,
}

impl Default for DefaultBrokerageModel {
    fn default() -> Self {
        DefaultBrokerageModel {
            slippage_bps: Decimal::new(1, 0),
            latency_ns: 1_000_000,
        }
    }
}

impl BrokerageModel for DefaultBrokerageModel {
    fn latency_ns(&self, _kind: CommandKind) -> u64 {
        self.latency_ns
    }

    fn fill_price(&self, order: &Order, ref_px: Price, inst: &dyn Instrument) -> Price {
        let slip = ref_px.as_decimal() * self.slippage_bps / Decimal::from(10_000);
        let adjusted = match order.side {
            OrderSide::Buy => ref_px.as_decimal() + slip,
            OrderSide::Sell => ref_px.as_decimal() - slip,
        };
        inst.make_price(adjusted).unwrap_or(ref_px)
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
        let notional =
            fill_px.as_decimal() * fill_qty.as_decimal() * inst.multiplier().as_decimal();
        Money::from_decimal(notional * rate, inst.settlement_currency())
            .unwrap_or_else(|_| Money::zero(inst.settlement_currency()))
    }
}
