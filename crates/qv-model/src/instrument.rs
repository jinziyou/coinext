//! Venue-agnostic instrument abstraction — the asset-class plug-in seam. Carries enforced
//! precision/increments and venue economics (maker/taker fees, multiplier, inverse flag) so the
//! SimulatedExchange and the live venue share the SAME economics.

use crate::enums::AssetClass;
use crate::identifiers::InstrumentId;
use qv_core::{Currency, ModelError, Money, Price, Quantity};
use rust_decimal::Decimal;

/// Object-safe instrument trait (used as `Arc<dyn Instrument>`).
pub trait Instrument: Send + Sync {
    fn id(&self) -> InstrumentId;
    fn asset_class(&self) -> AssetClass;
    fn base_currency(&self) -> Currency;
    fn quote_currency(&self) -> Currency;
    fn settlement_currency(&self) -> Currency;
    fn price_precision(&self) -> u8;
    fn size_precision(&self) -> u8;
    fn price_increment(&self) -> Price;
    fn size_increment(&self) -> Quantity;
    fn min_notional(&self) -> Option<Money>;
    /// Contract multiplier (1 for spot; contract size for derivatives).
    fn multiplier(&self) -> Quantity;
    fn maker_fee(&self) -> Decimal;
    fn taker_fee(&self) -> Decimal;
    /// Inverse (coin-margined) perpetual? Determines the PnL formula family.
    fn is_inverse(&self) -> bool;

    /// Quantize a decimal into a valid `Price` at this instrument's precision.
    fn make_price(&self, d: Decimal) -> Result<Price, ModelError> {
        Price::from_decimal(d, self.price_precision())
    }
    /// Quantize a decimal into a valid `Quantity` at this instrument's size precision.
    fn make_qty(&self, d: Decimal) -> Result<Quantity, ModelError> {
        Quantity::from_decimal(d, self.size_precision())
    }
}

/// Spot currency pair (e.g. BTCUSDT spot). Linear, multiplier 1, settles in quote.
#[derive(Debug, Clone)]
pub struct CurrencyPair {
    pub id: InstrumentId,
    pub base: Currency,
    pub quote: Currency,
    pub price_precision: u8,
    pub size_precision: u8,
    pub price_increment: Price,
    pub size_increment: Quantity,
    pub min_notional: Option<Money>,
    pub maker_fee: Decimal,
    pub taker_fee: Decimal,
}

impl Instrument for CurrencyPair {
    fn id(&self) -> InstrumentId {
        self.id.clone()
    }
    fn asset_class(&self) -> AssetClass {
        AssetClass::Spot
    }
    fn base_currency(&self) -> Currency {
        self.base
    }
    fn quote_currency(&self) -> Currency {
        self.quote
    }
    fn settlement_currency(&self) -> Currency {
        self.quote
    }
    fn price_precision(&self) -> u8 {
        self.price_precision
    }
    fn size_precision(&self) -> u8 {
        self.size_precision
    }
    fn price_increment(&self) -> Price {
        self.price_increment
    }
    fn size_increment(&self) -> Quantity {
        self.size_increment
    }
    fn min_notional(&self) -> Option<Money> {
        self.min_notional
    }
    fn multiplier(&self) -> Quantity {
        Quantity::from_raw(1, 0).expect("unit multiplier")
    }
    fn maker_fee(&self) -> Decimal {
        self.maker_fee
    }
    fn taker_fee(&self) -> Decimal {
        self.taker_fee
    }
    fn is_inverse(&self) -> bool {
        false
    }
}

/// USDⓈ-M (linear) or coin-margined (inverse) perpetual.
#[derive(Debug, Clone)]
pub struct CryptoPerpetual {
    pub id: InstrumentId,
    pub base: Currency,
    pub quote: Currency,
    pub settlement: Currency,
    pub price_precision: u8,
    pub size_precision: u8,
    pub price_increment: Price,
    pub size_increment: Quantity,
    pub min_notional: Option<Money>,
    pub multiplier: Quantity,
    pub maker_fee: Decimal,
    pub taker_fee: Decimal,
    pub is_inverse: bool,
    /// Funding charge interval in nanoseconds (e.g. 8h).
    pub funding_interval_ns: u64,
}

impl Instrument for CryptoPerpetual {
    fn id(&self) -> InstrumentId {
        self.id.clone()
    }
    fn asset_class(&self) -> AssetClass {
        AssetClass::Perp
    }
    fn base_currency(&self) -> Currency {
        self.base
    }
    fn quote_currency(&self) -> Currency {
        self.quote
    }
    fn settlement_currency(&self) -> Currency {
        self.settlement
    }
    fn price_precision(&self) -> u8 {
        self.price_precision
    }
    fn size_precision(&self) -> u8 {
        self.size_precision
    }
    fn price_increment(&self) -> Price {
        self.price_increment
    }
    fn size_increment(&self) -> Quantity {
        self.size_increment
    }
    fn min_notional(&self) -> Option<Money> {
        self.min_notional
    }
    fn multiplier(&self) -> Quantity {
        self.multiplier
    }
    fn maker_fee(&self) -> Decimal {
        self.maker_fee
    }
    fn taker_fee(&self) -> Decimal {
        self.taker_fee
    }
    fn is_inverse(&self) -> bool {
        self.is_inverse
    }
}
