//! `coinext-portfolio` — read-only account/position/PnL analytics over the Cache. Sources marks
//! internally from the Cache (last quote mid for spot; the mark feed for perps), so
//! `unrealized_pnl` takes no mark argument. Returns owned `Money`/`Position` to avoid borrow
//! tangles with the rest of the single-threaded core.

use coinext_cache::Cache;
use coinext_core::{Currency, Money, Price};
use coinext_model::{Instrument, InstrumentId, Position};
use coinext_ports::Portfolio;
use rust_decimal::Decimal;
use std::cell::RefCell;
use std::rc::Rc;

/// Convert a per-instrument `Money` value (denominated in the instrument's settlement currency)
/// into the portfolio's `settle` currency, returning the amount as a `Decimal`.
///
/// - Same currency: pass through.
/// - Inverse (coin-margined) perp whose value is in the COIN (base) but whose QUOTE matches the
///   portfolio settle: convert with the mark (coin value × mark, since mark is quote-per-coin). This
///   is what makes an inverse position's exposure/PnL visible to gross-exposure and the liquidation
///   check (otherwise it is silently dropped).
/// - Otherwise the rate is genuinely unavailable: return `None` so the caller can account for the
///   omission EXPLICITLY rather than silently swallowing it.
fn convert_to_settle(
    value: Money,
    inst: &dyn Instrument,
    mark: Price,
    settle: Currency,
) -> Option<Decimal> {
    if value.currency() == settle {
        return Some(value.amount());
    }
    if inst.is_inverse()
        && inst.quote_currency() == settle
        && value.currency() == inst.base_currency()
    {
        return Some(value.amount() * mark.as_decimal());
    }
    None
}

/// Portfolio facade. `settle` is the common settlement currency used for aggregate figures
/// (gross exposure etc.); per-instrument figures use the instrument's settlement currency.
pub struct PortfolioState {
    cache: Rc<RefCell<Cache>>,
    settle: Currency,
}

impl PortfolioState {
    pub fn new(cache: Rc<RefCell<Cache>>, settle: Currency) -> Self {
        PortfolioState { cache, settle }
    }
}

impl Portfolio for PortfolioState {
    fn position(&self, id: &InstrumentId) -> Option<Position> {
        self.cache.borrow().position(id).cloned()
    }

    fn net_exposure(&self, id: &InstrumentId) -> Money {
        let cache = self.cache.borrow();
        match (cache.position(id), cache.instrument(id), cache.mark(id)) {
            (Some(pos), Some(inst), Some(mark)) => pos.notional(mark, &*inst),
            _ => Money::zero(self.settle),
        }
    }

    fn unrealized_pnl(&self, id: &InstrumentId) -> Money {
        let cache = self.cache.borrow();
        match (cache.position(id), cache.instrument(id), cache.mark(id)) {
            (Some(pos), Some(inst), Some(mark)) => pos.unrealized_pnl(mark, &*inst),
            _ => Money::zero(self.settle),
        }
    }

    fn realized_pnl(&self, id: &InstrumentId) -> Money {
        self.cache
            .borrow()
            .position(id)
            .map(|p| p.realized_pnl)
            .unwrap_or_else(|| Money::zero(self.settle))
    }

    fn gross_exposure(&self) -> Money {
        let cache = self.cache.borrow();
        let mut total = Decimal::ZERO;
        for pos in cache.positions() {
            if let (Some(inst), Some(mark)) = (
                cache.instrument(&pos.instrument_id),
                cache.mark(&pos.instrument_id),
            ) {
                let notional = pos.notional(mark, &*inst);
                // Convert into the settle currency so inverse (coin-margined) exposure is NOT
                // silently dropped — it must be visible to the liquidation check. Positions with no
                // known conversion are skipped (the rate is genuinely unavailable).
                if let Some(amt) = convert_to_settle(notional, &*inst, mark, self.settle) {
                    total += amt;
                }
            }
        }
        Money::from_decimal(total, self.settle).unwrap_or_else(|_| Money::zero(self.settle))
    }

    fn balance(&self, ccy: &Currency) -> Money {
        self.cache
            .borrow()
            .account()
            .map(|a| a.balance(ccy))
            .unwrap_or_else(|| Money::zero(*ccy))
    }

    fn equity(&self) -> Money {
        let cache = self.cache.borrow();
        let mut total = cache
            .account()
            .map(|a| a.balance(&self.settle).amount())
            .unwrap_or(Decimal::ZERO);
        for pos in cache.positions() {
            let inst = cache.instrument(&pos.instrument_id);
            let mark = cache.mark(&pos.instrument_id);
            // Realized PnL: convert into settle (inverse-perp realized is in the coin currency) so it
            // is not dropped. Falls back to the raw amount only when its currency already is settle.
            if pos.realized_pnl.currency() == self.settle {
                total += pos.realized_pnl.amount();
            } else if let (Some(inst), Some(mark)) = (inst.as_ref(), mark) {
                if let Some(amt) = convert_to_settle(pos.realized_pnl, &**inst, mark, self.settle) {
                    total += amt;
                }
            }
            if let (Some(inst), Some(mark)) = (inst, mark) {
                let unreal = pos.unrealized_pnl(mark, &*inst);
                if let Some(amt) = convert_to_settle(unreal, &*inst, mark, self.settle) {
                    total += amt;
                }
            }
        }
        Money::from_decimal(total, self.settle).unwrap_or_else(|_| Money::zero(self.settle))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use coinext_core::{Price, Quantity, UnixNanos};
    use coinext_model::{
        ClientOrderId, CryptoPerpetual, Fill, Instrument, InstrumentId, LiquiditySide, OrderSide,
        Position, PositionId, TradeId, VenueOrderId,
    };
    use rust_decimal_macros::dec;
    use std::sync::Arc;

    fn inverse_perp() -> Arc<dyn Instrument> {
        let usdt = Currency::new("USDT", 8).unwrap();
        let btc = Currency::new("BTC", 8).unwrap();
        Arc::new(CryptoPerpetual {
            id: InstrumentId::parse("BTCUSD.BINANCE").unwrap(),
            base: btc,
            quote: usdt, // quote == portfolio settle -> conversion via the mark is available
            settlement: btc,
            price_precision: 1,
            size_precision: 0,
            price_increment: Price::from_decimal(dec!(0.1), 1).unwrap(),
            size_increment: Quantity::from_decimal(dec!(1), 0).unwrap(),
            min_notional: None,
            multiplier: Quantity::from_raw(1, 0).unwrap(),
            maker_fee: dec!(0),
            taker_fee: dec!(0),
            is_inverse: true,
            funding_interval_ns: 0,
        })
    }

    // FIX 4: an inverse (coin-margined) position settled in BTC must be VISIBLE to gross_exposure
    // (converted into the USDT settle currency), not silently dropped.
    #[test]
    fn inverse_exposure_is_visible_to_gross_exposure() {
        let usdt = Currency::new("USDT", 8).unwrap();
        let inst = inverse_perp();
        let id = inst.id();

        let mut cache = Cache::new();
        cache.add_instrument(inst.clone());
        let mark = Price::from_decimal(dec!(50000), 1).unwrap();
        cache.set_mark(id.clone(), mark);

        // Build a long 100-contract inverse position via a fill.
        let mut pos = Position::flat(
            PositionId::from("P"),
            id.clone(),
            inst.price_precision(),
            inst.size_precision(),
            inst.settlement_currency(),
        );
        let f = Fill {
            trade_id: TradeId::from("T"),
            client_order_id: ClientOrderId::from("C"),
            venue_order_id: VenueOrderId::from("V"),
            instrument_id: id.clone(),
            side: OrderSide::Buy,
            last_px: mark,
            last_qty: Quantity::from_decimal(dec!(100), 0).unwrap(),
            fee: Money::zero(inst.settlement_currency()),
            liquidity: LiquiditySide::Taker,
            ts_event: UnixNanos(0),
            ts_init: UnixNanos(0),
        };
        pos.apply_fill(&f, &*inst).unwrap();
        cache.upsert_position(pos);

        let pf = PortfolioState::new(Rc::new(RefCell::new(cache)), usdt);
        // coin notional = 100/50000 = 0.002 BTC; converted = 0.002 * 50000 = 100 USDT.
        let gross = pf.gross_exposure();
        assert_eq!(gross.currency(), usdt);
        assert_eq!(
            gross.amount(),
            dec!(100),
            "inverse exposure converted into settle and not dropped"
        );
    }
}
