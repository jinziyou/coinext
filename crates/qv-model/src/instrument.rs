//! Venue-agnostic instrument abstraction — the asset-class plug-in seam. Carries enforced
//! precision/increments and venue economics (maker/taker fees, multiplier, inverse flag) so the
//! SimulatedExchange and the live venue share the SAME economics.

use crate::enums::{AssetClass, OptionRight};
use crate::identifiers::InstrumentId;
use qv_core::{Currency, ModelError, Money, Price, Quantity, UnixNanos};
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

    // --- Derivative metadata (None for spot / equity / perpetual; Some for dated + optioned
    // contracts). Default-None so existing instruments need no changes and the trait stays
    // object-safe; settlement / exercise / pricing phases read these. ---

    /// Expiry timestamp for a dated contract (future / option); `None` for perpetual/spot/equity.
    fn expiry_ns(&self) -> Option<UnixNanos> {
        None
    }
    /// Option strike price; `None` unless this is an option.
    fn strike(&self) -> Option<Price> {
        None
    }
    /// Call vs put; `None` unless this is an option.
    fn option_right(&self) -> Option<OptionRight> {
        None
    }
    /// The instrument this contract derives from (option/future underlying); `None` for spot/equity.
    fn underlying(&self) -> Option<InstrumentId> {
        None
    }

    /// Quantize a decimal into a valid `Price` at this instrument's precision.
    fn make_price(&self, d: Decimal) -> Result<Price, ModelError> {
        Price::from_decimal(d, self.price_precision())
    }
    /// Quantize a decimal into a valid `Quantity` at this instrument's size precision.
    fn make_qty(&self, d: Decimal) -> Result<Quantity, ModelError> {
        Quantity::from_decimal(d, self.size_precision())
    }
}

/// The unit multiplier shared by spot/equity (contract size 1).
fn unit_multiplier() -> Quantity {
    Quantity::from_raw(1, 0).expect("unit multiplier")
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

/// A cash equity / share. Linear, multiplier 1, base == quote == settlement (the cash currency).
#[derive(Debug, Clone)]
pub struct Equity {
    pub id: InstrumentId,
    /// Trading + settlement cash currency (e.g. USD).
    pub currency: Currency,
    pub price_precision: u8,
    pub size_precision: u8,
    pub price_increment: Price,
    pub size_increment: Quantity,
    pub min_notional: Option<Money>,
    pub maker_fee: Decimal,
    pub taker_fee: Decimal,
}

impl Instrument for Equity {
    fn id(&self) -> InstrumentId {
        self.id.clone()
    }
    fn asset_class(&self) -> AssetClass {
        AssetClass::Equity
    }
    fn base_currency(&self) -> Currency {
        self.currency
    }
    fn quote_currency(&self) -> Currency {
        self.currency
    }
    fn settlement_currency(&self) -> Currency {
        self.currency
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
        unit_multiplier()
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

/// A dated (expiring) linear futures contract. PnL is linear in the contract price scaled by the
/// `multiplier` (contract size); settles in `settlement` at `expiry_ns`.
#[derive(Debug, Clone)]
pub struct FuturesContract {
    pub id: InstrumentId,
    /// The underlying the contract tracks (index / spot), used at settlement; optional.
    pub underlying: Option<InstrumentId>,
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
    pub expiry_ns: UnixNanos,
}

impl Instrument for FuturesContract {
    fn id(&self) -> InstrumentId {
        self.id.clone()
    }
    fn asset_class(&self) -> AssetClass {
        AssetClass::Future
    }
    fn base_currency(&self) -> Currency {
        self.quote
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
        false
    }
    fn expiry_ns(&self) -> Option<UnixNanos> {
        Some(self.expiry_ns)
    }
    fn underlying(&self) -> Option<InstrumentId> {
        self.underlying.clone()
    }
}

/// A European-style option contract. The TRADED price is the premium; PnL while held is linear in
/// the premium scaled by `multiplier` (contract size, e.g. 100). `strike`/`right`/`expiry_ns` drive
/// the expiry payoff (a later phase); `underlying` links it to the spot whose price it settles on.
#[derive(Debug, Clone)]
pub struct OptionContract {
    pub id: InstrumentId,
    pub underlying: InstrumentId,
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
    pub strike: Price,
    pub right: OptionRight,
    pub expiry_ns: UnixNanos,
}

impl Instrument for OptionContract {
    fn id(&self) -> InstrumentId {
        self.id.clone()
    }
    fn asset_class(&self) -> AssetClass {
        AssetClass::Option
    }
    fn base_currency(&self) -> Currency {
        self.quote
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
        false
    }
    fn expiry_ns(&self) -> Option<UnixNanos> {
        Some(self.expiry_ns)
    }
    fn strike(&self) -> Option<Price> {
        Some(self.strike)
    }
    fn option_right(&self) -> Option<OptionRight> {
        Some(self.right)
    }
    fn underlying(&self) -> Option<InstrumentId> {
        Some(self.underlying.clone())
    }
}
