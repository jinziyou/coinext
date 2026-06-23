//! Domain enumerations shared across orders, fills, positions, and market data.

use serde::{Deserialize, Serialize};

/// The asset family of an instrument — the plug-in seam for new markets.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum AssetClass {
    Spot,
    Perp,
    Future,
    Equity,
    Option,
}

/// Call vs put — the exercise direction of an option contract.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum OptionRight {
    Call,
    Put,
}

impl OptionRight {
    /// Intrinsic value per unit of underlying at spot `s` and strike `k`: `max(s-k,0)` for a call,
    /// `max(k-s,0)` for a put (never negative).
    pub fn intrinsic(
        self,
        spot: rust_decimal::Decimal,
        strike: rust_decimal::Decimal,
    ) -> rust_decimal::Decimal {
        let v = match self {
            OptionRight::Call => spot - strike,
            OptionRight::Put => strike - spot,
        };
        v.max(rust_decimal::Decimal::ZERO)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum OrderSide {
    Buy,
    Sell,
}

impl OrderSide {
    /// +1 for Buy, -1 for Sell — the signed direction used in PnL/position math.
    pub fn sign(self) -> i64 {
        match self {
            OrderSide::Buy => 1,
            OrderSide::Sell => -1,
        }
    }
    pub fn opposite(self) -> OrderSide {
        match self {
            OrderSide::Buy => OrderSide::Sell,
            OrderSide::Sell => OrderSide::Buy,
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum OrderType {
    Market,
    Limit,
    StopMarket,
    StopLimit,
    MarketIfTouched,
    TrailingStopMarket,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum TimeInForce {
    /// Good-til-cancel.
    Gtc,
    /// Immediate-or-cancel.
    Ioc,
    /// Fill-or-kill.
    Fok,
    /// Good-til-date.
    Gtd,
    /// Day order.
    Day,
}

/// Order lifecycle status (see the FSM transition table in `order.rs`).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum OrderStatus {
    Initialized,
    Denied,
    Submitted,
    Accepted,
    PendingUpdate,
    PendingCancel,
    PartiallyFilled,
    Filled,
    Canceled,
    Expired,
    Rejected,
}

impl OrderStatus {
    /// Terminal states never transition further.
    pub fn is_terminal(self) -> bool {
        matches!(
            self,
            OrderStatus::Denied
                | OrderStatus::Filled
                | OrderStatus::Canceled
                | OrderStatus::Expired
                | OrderStatus::Rejected
        )
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum LiquiditySide {
    Maker,
    Taker,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum PositionSide {
    Long,
    Short,
    Flat,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum PriceType {
    Bid,
    Ask,
    Mid,
    Last,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum BarAggregation {
    Tick,
    Second,
    Minute,
    Hour,
    Day,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum AggregationSource {
    /// Aggregated by us from ticks.
    Internal,
    /// Provided pre-aggregated by the venue.
    External,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum BookAction {
    Add,
    Update,
    Delete,
    /// Full-book reset / snapshot boundary: a consumer MUST discard every level it currently holds
    /// for the instrument; the deltas that follow are the fresh snapshot. Emitted by adapters when a
    /// REST depth snapshot is installed (initial sync or after a gap-triggered resync).
    Clear,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn book_action_clear_round_trips_serde() {
        // The snapshot-boundary variant must survive a JSON round-trip like the others.
        let json = serde_json::to_string(&BookAction::Clear).unwrap();
        assert_eq!(json, "\"Clear\"");
        let back: BookAction = serde_json::from_str(&json).unwrap();
        assert_eq!(back, BookAction::Clear);

        for action in [
            BookAction::Add,
            BookAction::Update,
            BookAction::Delete,
            BookAction::Clear,
        ] {
            let s = serde_json::to_string(&action).unwrap();
            let round: BookAction = serde_json::from_str(&s).unwrap();
            assert_eq!(round, action);
        }
    }
}
