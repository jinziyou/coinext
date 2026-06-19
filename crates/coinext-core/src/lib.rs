//! `coinext-core` — Coinext foundation primitives.
//!
//! Fixed-precision integer-backed value types ([`Price`], [`Quantity`], [`Money`], [`Currency`]),
//! time ([`UnixNanos`]), and the [`Clock`] abstraction with timers. Every other crate depends on
//! this one. No `f64` in the domain; decimals via `as_decimal()`/`amount()` only.

pub mod clock;
pub mod error;
pub mod time;
pub mod value;

pub use clock::{Clock, HistoricalClock, SystemClock, TimerEvent, TimerId};
pub use error::{ModelError, Result};
pub use time::UnixNanos;
pub use value::{Currency, Money, Price, Quantity, MAX_PRECISION};

#[cfg(test)]
mod tests {
    use super::*;
    use rust_decimal_macros::dec;

    #[test]
    fn price_roundtrips_exact_decimal() {
        let p = Price::from_decimal(dec!(42123.45), 2).unwrap();
        assert_eq!(p.raw(), 4212345);
        assert_eq!(p.as_decimal(), dec!(42123.45));
    }

    #[test]
    fn price_quantizes_to_precision() {
        // 0.123456 at precision 2 rounds to 0.12
        let p = Price::from_decimal(dec!(0.123456), 2).unwrap();
        assert_eq!(p.as_decimal(), dec!(0.12));
    }

    #[test]
    fn price_add_requires_matching_precision() {
        let a = Price::from_decimal(dec!(1.0), 2).unwrap();
        let b = Price::from_decimal(dec!(2.0), 4).unwrap();
        assert!(matches!(
            a.checked_add(b),
            Err(ModelError::PrecisionMismatch(2, 4))
        ));
    }

    #[test]
    fn quantity_rejects_negative() {
        assert!(matches!(
            Quantity::from_decimal(dec!(-1), 8),
            Err(ModelError::Negative(_))
        ));
    }

    #[test]
    fn quantity_sub_saturates_at_zero() {
        let a = Quantity::from_decimal(dec!(1), 8).unwrap();
        let b = Quantity::from_decimal(dec!(3), 8).unwrap();
        assert!(a.checked_sub(b).unwrap().is_zero());
    }

    #[test]
    fn price_rejects_nan_inf() {
        assert!(matches!(
            Price::from_f64(f64::NAN, 2),
            Err(ModelError::NotFinite(_))
        ));
        assert!(matches!(
            Price::from_f64(f64::INFINITY, 2),
            Err(ModelError::NotFinite(_))
        ));
    }

    #[test]
    fn money_currency_mismatch_errors() {
        let usdt = Currency::new("USDT", 8).unwrap();
        let btc = Currency::new("BTC", 8).unwrap();
        let a = Money::from_decimal(dec!(100), usdt).unwrap();
        let b = Money::from_decimal(dec!(1), btc).unwrap();
        assert!(matches!(
            a.checked_add(b),
            Err(ModelError::CurrencyMismatch(_, _))
        ));
    }

    #[test]
    fn money_amount_is_exact() {
        let usdt = Currency::new("USDT", 8).unwrap();
        let m = Money::from_decimal(dec!(12345.6789), usdt).unwrap();
        assert_eq!(m.amount(), dec!(12345.67890000));
        assert_eq!(m.currency().code(), "USDT");
    }

    #[test]
    fn historical_clock_fires_timers_in_order() {
        let clk = HistoricalClock::new(UnixNanos(0));
        let _t1 = clk.set_timer("a", UnixNanos(100));
        let t2 = clk.set_timer("b", UnixNanos(50));
        clk.cancel_timer(t2); // cancel the earlier one
        assert_eq!(clk.peek_next_timer(), Some(UnixNanos(100)));
        clk.advance_to(UnixNanos(100));
        let fired = clk.pop_due(UnixNanos(100));
        assert_eq!(fired.len(), 1);
        assert_eq!(fired[0].name, "a");
        assert_eq!(clk.now_ns(), UnixNanos(100));
    }
}
