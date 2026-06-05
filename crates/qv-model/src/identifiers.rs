//! Distinct newtype identifiers so categories can never be mixed (a `ClientOrderId` can never be
//! passed where a `VenueOrderId` is wanted). `InstrumentId = Symbol + Venue`.
//! `ClientOrderId` is deterministic/idempotent — assigned once by the OrderFactory.

use serde::{Deserialize, Serialize};
use std::fmt;
use std::sync::Arc;

/// Generate a `String`/`Arc<str>`-backed identifier newtype with the usual conversions.
macro_rules! string_id {
    ($(#[$meta:meta])* $name:ident) => {
        $(#[$meta])*
        #[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
        pub struct $name(Arc<str>);

        impl $name {
            pub fn new(s: impl Into<Arc<str>>) -> Self {
                $name(s.into())
            }
            pub fn as_str(&self) -> &str {
                &self.0
            }
        }
        impl From<&str> for $name {
            fn from(s: &str) -> Self {
                $name(Arc::from(s))
            }
        }
        impl From<String> for $name {
            fn from(s: String) -> Self {
                $name(Arc::from(s.as_str()))
            }
        }
        impl fmt::Display for $name {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str(&self.0)
            }
        }
    };
}

string_id!(
    /// Venue-native symbol, e.g. `BTCUSDT`.
    Symbol
);
string_id!(
    /// Trading venue, e.g. `BINANCE`.
    Venue
);
string_id!(
    /// Deterministic client order id (`{strategy_id}-{seq:020}`); stable before submit, so
    /// retries never double-submit. Assigned by the OrderFactory; the OMS only tracks/dedupes.
    ClientOrderId
);
string_id!(
    /// Venue-assigned order id (known only after `Accepted`).
    VenueOrderId
);
string_id!(StrategyId);
string_id!(TraderId);
string_id!(AccountId);
string_id!(PositionId);
string_id!(TradeId);

/// `Symbol` + `Venue`. Displays as `SYMBOL.VENUE` (e.g. `BTCUSDT.BINANCE`).
#[derive(Debug, Clone, PartialEq, Eq, Hash, PartialOrd, Ord, Serialize, Deserialize)]
pub struct InstrumentId {
    pub symbol: Symbol,
    pub venue: Venue,
}

impl InstrumentId {
    pub fn new(symbol: Symbol, venue: Venue) -> Self {
        InstrumentId { symbol, venue }
    }

    /// Parse `SYMBOL.VENUE`.
    pub fn parse(s: &str) -> Option<Self> {
        let (sym, ven) = s.rsplit_once('.')?;
        Some(InstrumentId {
            symbol: Symbol::from(sym),
            venue: Venue::from(ven),
        })
    }
}

impl fmt::Display for InstrumentId {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}.{}", self.symbol, self.venue)
    }
}
