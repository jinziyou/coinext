//! Normalized market-data value types — the venue-agnostic output of every adapter. `ts_event`
//! (venue time) drives the time-frontier ordering that prevents look-ahead; `ts_init` enables
//! latency measurement.

use crate::enums::{AggregationSource, BarAggregation, BookAction, OrderSide, PriceType};
use crate::identifiers::{InstrumentId, TradeId};
use qv_core::{Price, Quantity, UnixNanos};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct QuoteTick {
    pub instrument_id: InstrumentId,
    pub bid: Price,
    pub ask: Price,
    pub bid_size: Quantity,
    pub ask_size: Quantity,
    pub ts_event: UnixNanos,
    pub ts_init: UnixNanos,
}

impl QuoteTick {
    /// Mid price `(bid + ask) / 2` at bid precision (used as the spot mark).
    pub fn mid(&self) -> Price {
        let sum = self.bid.raw().saturating_add(self.ask.raw());
        Price::from_raw(sum / 2, self.bid.precision()).unwrap_or(self.bid)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct TradeTick {
    pub instrument_id: InstrumentId,
    pub price: Price,
    pub size: Quantity,
    /// Side of the aggressor (taker).
    pub aggressor: OrderSide,
    pub trade_id: TradeId,
    pub ts_event: UnixNanos,
    pub ts_init: UnixNanos,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct OrderBookDelta {
    pub instrument_id: InstrumentId,
    pub action: BookAction,
    pub side: OrderSide,
    pub price: Price,
    pub size: Quantity,
    pub sequence: u64,
    pub ts_event: UnixNanos,
    pub ts_init: UnixNanos,
}

/// Identity of a bar series.
#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct BarSpec {
    pub step: u32,
    pub aggregation: BarAggregation,
    pub price_type: PriceType,
}

#[derive(Debug, Clone, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub struct BarType {
    pub instrument_id: InstrumentId,
    pub spec: BarSpec,
    pub source: AggregationSource,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Bar {
    pub bar_type: BarType,
    pub open: Price,
    pub high: Price,
    pub low: Price,
    pub close: Price,
    pub volume: Quantity,
    /// `ts_event` is the bar's CLOSE time (so a bar is emitted only once complete — no look-ahead).
    pub ts_event: UnixNanos,
    pub ts_init: UnixNanos,
}

/// The unified market-data event delivered into the core.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum MarketEvent {
    Quote(QuoteTick),
    Trade(TradeTick),
    Delta(OrderBookDelta),
    Bar(Bar),
}

impl MarketEvent {
    /// The venue timestamp — the merge-sort key for the time-frontier.
    pub fn ts_event(&self) -> UnixNanos {
        match self {
            MarketEvent::Quote(q) => q.ts_event,
            MarketEvent::Trade(t) => t.ts_event,
            MarketEvent::Delta(d) => d.ts_event,
            MarketEvent::Bar(b) => b.ts_event,
        }
    }

    pub fn instrument_id(&self) -> &InstrumentId {
        match self {
            MarketEvent::Quote(q) => &q.instrument_id,
            MarketEvent::Trade(t) => &t.instrument_id,
            MarketEvent::Delta(d) => &d.instrument_id,
            MarketEvent::Bar(b) => &b.bar_type.instrument_id,
        }
    }
}
