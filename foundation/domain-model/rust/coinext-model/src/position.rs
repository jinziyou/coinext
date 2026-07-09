//! Net position aggregating fills; tracks average entry and realized/unrealized PnL with
//! instrument precision. Linear vs inverse perp PnL is handled per the instrument family
//! (`is_inverse()`); `apply_funding()` applies a funding charge as a realized-PnL adjustment —
//! intended to be driven by the kernel at funding intervals, but not yet wired in.
//! The mark is supplied by the caller (the Portfolio sources it from the Cache).

use crate::enums::PositionSide;
use crate::fill::Fill;
use crate::identifiers::{InstrumentId, PositionId};
use crate::instrument::Instrument;
use coinext_core::{Currency, Money, Price, Quantity};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Position {
    pub position_id: PositionId,
    pub instrument_id: InstrumentId,
    pub side: PositionSide,
    /// Absolute size.
    pub quantity: Quantity,
    pub avg_px_open: Price,
    pub realized_pnl: Money,
    size_precision: u8,
    price_precision: u8,
}

impl Position {
    pub fn flat(
        position_id: PositionId,
        instrument_id: InstrumentId,
        price_precision: u8,
        size_precision: u8,
        settlement: Currency,
    ) -> Position {
        Position {
            position_id,
            instrument_id,
            side: PositionSide::Flat,
            quantity: Quantity::zero(size_precision),
            avg_px_open: Price::zero(price_precision),
            realized_pnl: Money::zero(settlement),
            size_precision,
            price_precision,
        }
    }

    /// Signed position size (+long / -short) as a Decimal.
    fn signed(&self) -> Decimal {
        match self.side {
            PositionSide::Long => self.quantity.as_decimal(),
            PositionSide::Short => -self.quantity.as_decimal(),
            PositionSide::Flat => Decimal::ZERO,
        }
    }

    fn pnl_per_qty(is_inverse: bool, entry: Decimal, exit: Decimal, mult: Decimal) -> Decimal {
        if entry.is_zero() {
            return Decimal::ZERO;
        }
        if is_inverse {
            // Inverse (coin-margined): PnL in base ccy = qty*mult*(1/entry - 1/exit)
            mult * (Decimal::ONE / entry - Decimal::ONE / exit)
        } else {
            // Linear: PnL in quote ccy = qty*mult*(exit - entry)
            mult * (exit - entry)
        }
    }

    /// Fold a fill into the position, realizing PnL on any reduced/closed portion. Fees are
    /// deducted from realized PnL when denominated in the settlement currency.
    pub fn apply_fill(
        &mut self,
        f: &Fill,
        inst: &dyn Instrument,
    ) -> Result<(), coinext_core::ModelError> {
        let settle = inst.settlement_currency();
        let mult = inst.multiplier().as_decimal();
        let fill_px = f.last_px.as_decimal();
        let fill_qty = f.last_qty.as_decimal();
        let fill_signed = Decimal::from(f.side.sign()) * fill_qty;

        let cur_signed = self.signed();
        let new_signed = cur_signed + fill_signed;
        let avg_open = self.avg_px_open.as_decimal();

        let mut realized = Decimal::ZERO;
        let new_avg: Decimal;

        if cur_signed.is_zero() {
            new_avg = fill_px;
        } else if (cur_signed > Decimal::ZERO) == (fill_signed > Decimal::ZERO) {
            // Same direction: increase, recompute weighted average.
            let cur_abs = cur_signed.abs();
            let denom = cur_abs + fill_qty;
            new_avg = if denom.is_zero() {
                fill_px
            } else {
                (avg_open * cur_abs + fill_px * fill_qty) / denom
            };
        } else {
            // Opposite direction: reduce/close/flip — realize on the reduced portion.
            let reduce = cur_signed.abs().min(fill_qty);
            let dir = if cur_signed > Decimal::ZERO {
                Decimal::ONE
            } else {
                -Decimal::ONE
            };
            realized +=
                dir * reduce * Self::pnl_per_qty(inst.is_inverse(), avg_open, fill_px, mult);
            new_avg = if fill_qty > cur_signed.abs() {
                fill_px
            } else {
                avg_open
            };
        }

        // Deduct fee (in settlement currency) from realized PnL.
        if f.fee.currency() == settle {
            realized -= f.fee.amount();
        }
        if !realized.is_zero() {
            let add = Money::from_decimal(realized, settle)?;
            self.realized_pnl = self.realized_pnl.checked_add(add)?;
        }

        // Update side / quantity / average.
        self.side = if new_signed.is_zero() {
            PositionSide::Flat
        } else if new_signed > Decimal::ZERO {
            PositionSide::Long
        } else {
            PositionSide::Short
        };
        self.quantity = Quantity::from_decimal(new_signed.abs(), self.size_precision)?;
        self.avg_px_open = if self.side == PositionSide::Flat {
            Price::zero(self.price_precision)
        } else {
            Price::from_decimal(new_avg, self.price_precision)?
        };
        Ok(())
    }

    /// Apply a funding charge (perps only). Positive `rate` charges longs and credits shorts.
    pub fn apply_funding(
        &mut self,
        rate: Decimal,
        mark: Price,
        inst: &dyn Instrument,
    ) -> Result<(), coinext_core::ModelError> {
        if self.side == PositionSide::Flat {
            return Ok(());
        }
        let mult = inst.multiplier().as_decimal();
        // Funding is charged on the position NOTIONAL in the settlement currency. For a linear perp
        // that notional is `qty*mark*mult` (quote ccy); for an inverse (coin-margined) perp it is
        // `qty*mult/mark` (coin ccy) — the same shape as `notional()`. Using the linear formula on
        // an inverse perp would charge funding in the wrong dimension and magnitude.
        let mark_d = mark.as_decimal();
        let notional = if inst.is_inverse() {
            if mark_d.is_zero() {
                Decimal::ZERO
            } else {
                self.signed() * mult / mark_d
            }
        } else {
            self.signed() * mark_d * mult
        };
        let payment = -(notional * rate); // long pays when rate>0
        let m = Money::from_decimal(payment, inst.settlement_currency())?;
        self.realized_pnl = self.realized_pnl.checked_add(m)?;
        Ok(())
    }

    /// Unrealized PnL at `mark`, in the settlement currency.
    pub fn unrealized_pnl(&self, mark: Price, inst: &dyn Instrument) -> Money {
        let settle = inst.settlement_currency();
        if self.side == PositionSide::Flat {
            return Money::zero(settle);
        }
        let mult = inst.multiplier().as_decimal();
        let dir = if self.side == PositionSide::Long {
            Decimal::ONE
        } else {
            -Decimal::ONE
        };
        let per = Self::pnl_per_qty(
            inst.is_inverse(),
            self.avg_px_open.as_decimal(),
            mark.as_decimal(),
            mult,
        );
        let pnl = dir * self.quantity.as_decimal() * per;
        Money::from_decimal(pnl, settle).unwrap_or_else(|_| Money::zero(settle))
    }

    /// Position notional at `mark`, in the settlement currency.
    pub fn notional(&self, mark: Price, inst: &dyn Instrument) -> Money {
        let settle = inst.settlement_currency();
        let mult = inst.multiplier().as_decimal();
        let qty = self.quantity.as_decimal();
        let value = if inst.is_inverse() {
            if mark.as_decimal().is_zero() {
                Decimal::ZERO
            } else {
                qty * mult / mark.as_decimal()
            }
        } else {
            qty * mark.as_decimal() * mult
        };
        Money::from_decimal(value, settle).unwrap_or_else(|_| Money::zero(settle))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::enums::{LiquiditySide, OrderSide};
    use crate::identifiers::{ClientOrderId, TradeId, VenueOrderId};
    use crate::instrument::CryptoPerpetual;
    use coinext_core::{Currency, UnixNanos};
    use rust_decimal_macros::dec;

    /// An inverse (coin-margined) BTCUSD perp: quoted in USD, SETTLED in BTC. Multiplier 1 USD
    /// per contract, so coin notional = qty/price BTC.
    fn inverse_perp() -> CryptoPerpetual {
        let usdt = Currency::new("USDT", 8).unwrap();
        let btc = Currency::new("BTC", 8).unwrap();
        CryptoPerpetual {
            id: InstrumentId::parse("BTCUSD.BINANCE").unwrap(),
            base: btc,
            quote: usdt,
            settlement: btc, // coin-margined -> settles in the base coin
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
        }
    }

    fn fill(side: OrderSide, px: &str, qty: &str, settle: Currency) -> Fill {
        Fill {
            trade_id: TradeId::from("T"),
            client_order_id: ClientOrderId::from("C"),
            venue_order_id: VenueOrderId::from("V"),
            instrument_id: InstrumentId::parse("BTCUSD.BINANCE").unwrap(),
            side,
            last_px: Price::from_decimal(px.parse().unwrap(), 1).unwrap(),
            last_qty: Quantity::from_decimal(qty.parse().unwrap(), 0).unwrap(),
            fee: Money::zero(settle),
            liquidity: LiquiditySide::Taker,
            ts_event: UnixNanos(0),
            ts_init: UnixNanos(0),
        }
    }

    fn flat_pos(inst: &CryptoPerpetual) -> Position {
        Position::flat(
            PositionId::from("P"),
            inst.id.clone(),
            inst.price_precision,
            inst.size_precision,
            inst.settlement_currency(),
        )
    }

    // Inverse PnL: long 100 contracts (100 USD) from 50000 -> 25000 doubles the coin value.
    // realized = qty*mult*(1/entry - 1/exit) = 100*(1/50000 - 1/25000) ... but closing realizes the
    // favorable move. Long from 50000, mark 25000 is a LOSS in coin terms (price fell). Use a clean
    // case: long from 50000, close at 100000 -> coin PnL = 100*(1/50000 - 1/100000) = +0.001 BTC.
    #[test]
    fn inverse_realized_pnl_is_in_coin() {
        let inst = inverse_perp();
        let btc = inst.settlement_currency();
        let mut pos = flat_pos(&inst);
        pos.apply_fill(&fill(OrderSide::Buy, "50000", "100", btc), &inst)
            .unwrap();
        // Close the 100 contracts at 100000.
        pos.apply_fill(&fill(OrderSide::Sell, "100000", "100", btc), &inst)
            .unwrap();
        assert_eq!(pos.side, PositionSide::Flat);
        // 100*(1/50000 - 1/100000) = 100 * (2 - 1)/100000 = 100/100000 = 0.001 BTC.
        assert_eq!(pos.realized_pnl.currency(), btc);
        assert_eq!(pos.realized_pnl.amount(), dec!(0.001));
    }

    // Inverse unrealized PnL uses the same coin formula.
    #[test]
    fn inverse_unrealized_pnl_is_in_coin() {
        let inst = inverse_perp();
        let btc = inst.settlement_currency();
        let mut pos = flat_pos(&inst);
        pos.apply_fill(&fill(OrderSide::Buy, "50000", "100", btc), &inst)
            .unwrap();
        let mark = Price::from_decimal(dec!(100000), 1).unwrap();
        let u = pos.unrealized_pnl(mark, &inst);
        assert_eq!(u.currency(), btc);
        assert_eq!(u.amount(), dec!(0.001));
    }

    // Inverse notional is the COIN notional qty*mult/price, NOT the linear qty*price.
    #[test]
    fn inverse_notional_is_coin_notional() {
        let inst = inverse_perp();
        let btc = inst.settlement_currency();
        let mut pos = flat_pos(&inst);
        pos.apply_fill(&fill(OrderSide::Buy, "50000", "100", btc), &inst)
            .unwrap();
        let mark = Price::from_decimal(dec!(50000), 1).unwrap();
        let n = pos.notional(mark, &inst);
        assert_eq!(n.currency(), btc);
        assert_eq!(n.amount(), dec!(0.002)); // 100/50000 BTC
    }

    // Inverse funding is charged on the COIN notional (qty*mult/mark), in the coin currency. A long
    // paying a positive rate is DEBITED `rate * coin_notional` BTC.
    #[test]
    fn inverse_funding_is_charged_on_coin_notional() {
        let inst = inverse_perp();
        let btc = inst.settlement_currency();
        let mut pos = flat_pos(&inst);
        pos.apply_fill(&fill(OrderSide::Buy, "100", "100", btc), &inst)
            .unwrap();
        let mark = Price::from_decimal(dec!(100), 1).unwrap();
        // coin notional = 100/100 = 1 BTC. rate 0.01 -> long pays 0.01 BTC.
        pos.apply_funding(dec!(0.01), mark, &inst).unwrap();
        assert_eq!(pos.realized_pnl.currency(), btc);
        assert_eq!(pos.realized_pnl.amount(), dec!(-0.01));
    }
}
