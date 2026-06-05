//! Domain enumerations shared across orders, fills, positions, and market data.

use serde::{Deserialize, Serialize};

/// The asset family of an instrument — the plug-in seam for new markets.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum AssetClass {
    Spot,
    Perp,
    Future,
    Equity,
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
}
