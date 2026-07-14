//! Fixed-precision, integer-backed value types. **No `f64` lives in the domain** — every
//! Price/Quantity/Money is an integer `raw` plus a `precision`, so PnL and matching never drift.
//! `as_f64()` exists for display/analytics ONLY. The Python mirror (coinext-py) keeps the same integer
//! representation and exposes decimals only via `as_decimal()`, never as a field.

use crate::error::ModelError;
use rust_decimal::prelude::ToPrimitive;
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

/// Max supported decimal precision (10^18 fits in u64 / i64 comfortably for crypto sizes).
pub const MAX_PRECISION: u8 = 18;

#[inline]
fn check_precision(precision: u8) -> Result<(), ModelError> {
    if precision > MAX_PRECISION {
        return Err(ModelError::OutOfRange(format!(
            "precision {precision} exceeds max {MAX_PRECISION}"
        )));
    }
    Ok(())
}

/// Quantize a `Decimal` to exactly `precision` decimal places and return the integer mantissa.
fn quantize(d: Decimal, precision: u8) -> Result<i128, ModelError> {
    check_precision(precision)?;
    let mut q = d.round_dp(precision as u32);
    q.rescale(precision as u32); // force scale == precision so mantissa() is at `precision`
                                 // `rescale` SILENTLY caps the scale when the 96-bit mantissa would overflow, yielding a
                                 // wrong mantissa instead of erroring. If the resulting scale is not exactly `precision`,
                                 // the value cannot be represented at the requested precision — fail fast.
    if q.scale() != precision as u32 {
        return Err(ModelError::OutOfRange(format!(
            "value {d} cannot be represented at precision {precision}"
        )));
    }
    Ok(q.mantissa())
}

/// A price: `value = raw / 10^precision`. Totally ordered (same-precision comparisons are exact).
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct Price {
    raw: i64,
    precision: u8,
}

impl Price {
    pub fn from_raw(raw: i64, precision: u8) -> Result<Price, ModelError> {
        check_precision(precision)?;
        if raw < 0 {
            return Err(ModelError::Negative(format!("price raw {raw}")));
        }
        Ok(Price { raw, precision })
    }

    pub fn from_decimal(d: Decimal, precision: u8) -> Result<Price, ModelError> {
        let raw = i64::try_from(quantize(d, precision)?)
            .map_err(|_| ModelError::OutOfRange(format!("price {d} overflows i64@{precision}")))?;
        Price::from_raw(raw, precision)
    }

    /// f64 entry point — the ONLY place NaN/Inf is rejected (rust_decimal cannot hold them).
    pub fn from_f64(v: f64, precision: u8) -> Result<Price, ModelError> {
        if !v.is_finite() {
            return Err(ModelError::NotFinite(format!("price {v}")));
        }
        let d = Decimal::from_f64_retain(v)
            .ok_or_else(|| ModelError::OutOfRange(format!("price {v}")))?;
        Price::from_decimal(d, precision)
    }

    pub fn zero(precision: u8) -> Price {
        Price { raw: 0, precision }
    }

    #[inline]
    pub fn raw(self) -> i64 {
        self.raw
    }
    #[inline]
    pub fn precision(self) -> u8 {
        self.precision
    }
    #[inline]
    pub fn is_zero(self) -> bool {
        self.raw == 0
    }

    pub fn as_decimal(self) -> Decimal {
        Decimal::from_i128_with_scale(self.raw as i128, self.precision as u32)
    }
    /// DISPLAY ONLY — do not use in domain math.
    pub fn as_f64(self) -> f64 {
        self.as_decimal().to_f64().unwrap_or(f64::NAN)
    }

    fn same_precision(self, o: Price) -> Result<(), ModelError> {
        if self.precision != o.precision {
            return Err(ModelError::PrecisionMismatch(self.precision, o.precision));
        }
        Ok(())
    }

    pub fn checked_add(self, o: Price) -> Result<Price, ModelError> {
        self.same_precision(o)?;
        let raw = self.raw.checked_add(o.raw).ok_or(ModelError::Overflow)?;
        Ok(Price {
            raw,
            precision: self.precision,
        })
    }

    pub fn checked_sub(self, o: Price) -> Result<Price, ModelError> {
        self.same_precision(o)?;
        let raw = self.raw.checked_sub(o.raw).ok_or(ModelError::Overflow)?;
        Ok(Price {
            raw,
            precision: self.precision,
        })
    }
}

impl std::fmt::Display for Price {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.as_decimal())
    }
}

/// A non-negative quantity/size: `value = raw / 10^precision`, `raw >= 0`.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub struct Quantity {
    raw: i64,
    precision: u8,
}

impl Quantity {
    pub fn from_raw(raw: i64, precision: u8) -> Result<Quantity, ModelError> {
        check_precision(precision)?;
        if raw < 0 {
            return Err(ModelError::Negative(format!("quantity raw {raw}")));
        }
        Ok(Quantity { raw, precision })
    }

    pub fn from_decimal(d: Decimal, precision: u8) -> Result<Quantity, ModelError> {
        if d.is_sign_negative() && !d.is_zero() {
            return Err(ModelError::Negative(format!("quantity {d}")));
        }
        let raw = i64::try_from(quantize(d, precision)?).map_err(|_| {
            ModelError::OutOfRange(format!("quantity {d} overflows i64@{precision}"))
        })?;
        Quantity::from_raw(raw, precision)
    }

    pub fn from_f64(v: f64, precision: u8) -> Result<Quantity, ModelError> {
        if !v.is_finite() {
            return Err(ModelError::NotFinite(format!("quantity {v}")));
        }
        let d = Decimal::from_f64_retain(v)
            .ok_or_else(|| ModelError::OutOfRange(format!("quantity {v}")))?;
        Quantity::from_decimal(d, precision)
    }

    pub fn zero(precision: u8) -> Quantity {
        Quantity { raw: 0, precision }
    }

    #[inline]
    pub fn raw(self) -> i64 {
        self.raw
    }
    #[inline]
    pub fn precision(self) -> u8 {
        self.precision
    }
    #[inline]
    pub fn is_zero(self) -> bool {
        self.raw == 0
    }
    #[inline]
    pub fn is_positive(self) -> bool {
        self.raw > 0
    }

    pub fn as_decimal(self) -> Decimal {
        Decimal::from_i128_with_scale(self.raw as i128, self.precision as u32)
    }
    /// DISPLAY ONLY.
    pub fn as_f64(self) -> f64 {
        self.as_decimal().to_f64().unwrap_or(f64::NAN)
    }

    fn same_precision(self, o: Quantity) -> Result<(), ModelError> {
        if self.precision != o.precision {
            return Err(ModelError::PrecisionMismatch(self.precision, o.precision));
        }
        Ok(())
    }

    pub fn checked_add(self, o: Quantity) -> Result<Quantity, ModelError> {
        self.same_precision(o)?;
        let raw = self.raw.checked_add(o.raw).ok_or(ModelError::Overflow)?;
        Quantity::from_raw(raw, self.precision)
    }

    /// Saturating subtraction at zero (a filled qty can never exceed ordered qty in practice,
    /// but `leaves_qty` must never go negative).
    pub fn checked_sub(self, o: Quantity) -> Result<Quantity, ModelError> {
        self.same_precision(o)?;
        let raw = self.raw.checked_sub(o.raw).ok_or(ModelError::Overflow)?;
        Quantity::from_raw(raw.max(0), self.precision)
    }
}

impl std::fmt::Display for Quantity {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.as_decimal())
    }
}

/// An ISO-ish currency code (up to 8 bytes) with its native precision (e.g. USDT@8, BTC@8).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct Currency {
    code: [u8; 8],
    precision: u8,
}

impl Currency {
    pub fn new(code: &str, precision: u8) -> Result<Currency, ModelError> {
        check_precision(precision)?;
        let bytes = code.as_bytes();
        if bytes.is_empty() || bytes.len() > 8 {
            return Err(ModelError::Invalid(format!(
                "currency code '{code}' (1..=8 bytes)"
            )));
        }
        let mut buf = [0u8; 8];
        buf[..bytes.len()].copy_from_slice(bytes);
        Ok(Currency {
            code: buf,
            precision,
        })
    }

    pub fn code(&self) -> &str {
        let end = self.code.iter().position(|&b| b == 0).unwrap_or(8);
        std::str::from_utf8(&self.code[..end]).unwrap_or("???")
    }

    #[inline]
    pub fn precision(&self) -> u8 {
        self.precision
    }
}

impl std::fmt::Display for Currency {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}", self.code())
    }
}

/// A signed monetary amount in a given currency: `value = amount / 10^currency.precision`.
/// `amount` is `i128` (room for large notionals); decimals are exposed only via methods.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub struct Money {
    amount: i128,
    currency: Currency,
}

impl Money {
    pub fn from_raw(amount: i128, currency: Currency) -> Money {
        Money { amount, currency }
    }

    pub fn from_decimal(d: Decimal, currency: Currency) -> Result<Money, ModelError> {
        let amount = quantize(d, currency.precision())?;
        Ok(Money { amount, currency })
    }

    pub fn zero(currency: Currency) -> Money {
        Money {
            amount: 0,
            currency,
        }
    }

    #[inline]
    pub fn raw(self) -> i128 {
        self.amount
    }
    #[inline]
    pub fn currency(self) -> Currency {
        self.currency
    }
    #[inline]
    pub fn is_zero(self) -> bool {
        self.amount == 0
    }

    /// Exact decimal value — a METHOD, never a field, to preserve the integer invariant at the
    /// FFI boundary.
    pub fn amount(self) -> Decimal {
        Decimal::from_i128_with_scale(self.amount, self.currency.precision() as u32)
    }
    /// DISPLAY ONLY.
    pub fn as_f64(self) -> f64 {
        self.amount().to_f64().unwrap_or(f64::NAN)
    }

    fn same_currency(self, o: Money) -> Result<(), ModelError> {
        if self.currency != o.currency {
            return Err(ModelError::CurrencyMismatch(
                self.currency.code().to_string(),
                o.currency.code().to_string(),
            ));
        }
        Ok(())
    }

    pub fn checked_add(self, o: Money) -> Result<Money, ModelError> {
        self.same_currency(o)?;
        let amount = self
            .amount
            .checked_add(o.amount)
            .ok_or(ModelError::Overflow)?;
        Ok(Money {
            amount,
            currency: self.currency,
        })
    }

    pub fn checked_sub(self, o: Money) -> Result<Money, ModelError> {
        self.same_currency(o)?;
        let amount = self
            .amount
            .checked_sub(o.amount)
            .ok_or(ModelError::Overflow)?;
        Ok(Money {
            amount,
            currency: self.currency,
        })
    }
}

impl std::fmt::Display for Money {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{} {}", self.amount(), self.currency.code())
    }
}
