//! `coinext-model::order_book` — an L2 (price-level) order book folded from `OrderBookDelta`s.
//!
//! This is the **consumer** side of the delta stream. Adapters emit
//! `BookAction::{Clear, Add, Update, Delete}` (see [`crate::market_data::OrderBookDelta`]); the
//! `DataEngine` folds them into the per-instrument `OrderBook` held in the Cache, so strategies and
//! analytics can read live depth / top-of-book. `Clear` is a **snapshot boundary**: it wipes the book
//! so the `Add`s that follow rebuild it from a fresh REST snapshot (closing the resync loop). The book
//! is venue-agnostic and integer-exact — levels are keyed by `Price::raw()`, never `f64`.

use crate::enums::{BookAction, OrderSide};
use crate::identifiers::InstrumentId;
use crate::market_data::{OrderBookDelta, QuoteTick};
use coinext_core::{Price, Quantity, UnixNanos};
use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;

/// An L2 order book. Bids/asks are `price.raw() -> size` maps: the best bid is the highest bid key,
/// the best ask the lowest ask key. A zero-size `Add`/`Update` removes the level (Binance diff
/// semantics); `Delete` removes it; `Clear` wipes both sides.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OrderBook {
    instrument_id: InstrumentId,
    bids: BTreeMap<i64, Quantity>,
    asks: BTreeMap<i64, Quantity>,
    /// Price precision, captured from the first applied delta (all levels share it).
    precision: u8,
    /// `sequence` of the last applied delta. A non-`Clear` delta with a strictly-smaller sequence is
    /// stale and skipped; `Clear` always applies and re-bases the sequence (a snapshot boundary).
    last_sequence: u64,
    seq_set: bool,
    ts_last: UnixNanos,
}

impl OrderBook {
    pub fn new(instrument_id: InstrumentId) -> Self {
        OrderBook {
            instrument_id,
            bids: BTreeMap::new(),
            asks: BTreeMap::new(),
            precision: 0,
            last_sequence: 0,
            seq_set: false,
            ts_last: UnixNanos(0),
        }
    }

    /// Fold one delta into the book. Returns `false` (and does nothing) if the delta is stale
    /// (a non-`Clear` delta whose sequence is older than the last applied one).
    pub fn apply(&mut self, delta: &OrderBookDelta) -> bool {
        let is_clear = delta.action == BookAction::Clear;
        if !is_clear && self.seq_set && delta.sequence < self.last_sequence {
            return false; // stale / out-of-order — already superseded
        }
        if self.precision == 0 {
            self.precision = delta.price.precision();
        }
        match delta.action {
            BookAction::Clear => {
                self.bids.clear();
                self.asks.clear();
            }
            BookAction::Add | BookAction::Update => {
                let levels = self.side_mut(delta.side);
                if delta.size.raw() == 0 {
                    levels.remove(&delta.price.raw());
                } else {
                    levels.insert(delta.price.raw(), delta.size);
                }
            }
            BookAction::Delete => {
                self.side_mut(delta.side).remove(&delta.price.raw());
            }
        }
        // `Clear` re-bases the sequence to the snapshot boundary; other deltas advance monotonically.
        self.last_sequence = if is_clear {
            delta.sequence
        } else {
            self.last_sequence.max(delta.sequence)
        };
        self.seq_set = true;
        self.ts_last = delta.ts_event;
        true
    }

    pub fn instrument_id(&self) -> &InstrumentId {
        &self.instrument_id
    }

    /// Best (highest) bid level, if any.
    pub fn best_bid(&self) -> Option<(Price, Quantity)> {
        self.bids
            .iter()
            .next_back()
            .map(|(&raw, &q)| (self.price(raw), q))
    }

    /// Best (lowest) ask level, if any.
    pub fn best_ask(&self) -> Option<(Price, Quantity)> {
        self.asks
            .iter()
            .next()
            .map(|(&raw, &q)| (self.price(raw), q))
    }

    /// Mid price `(best_bid + best_ask) / 2`. `None` unless both sides have a level.
    pub fn mid(&self) -> Option<Price> {
        match (self.best_bid(), self.best_ask()) {
            (Some((bid, _)), Some((ask, _))) => {
                let sum = bid.raw().saturating_add(ask.raw());
                Some(Price::from_raw(sum / 2, self.precision).unwrap_or(bid))
            }
            _ => None,
        }
    }

    /// Spread `best_ask - best_bid`. `None` unless both sides have a level.
    pub fn spread(&self) -> Option<Price> {
        match (self.best_bid(), self.best_ask()) {
            (Some((bid, _)), Some((ask, _))) => {
                let raw = ask.raw().saturating_sub(bid.raw());
                Some(
                    Price::from_raw(raw, self.precision)
                        .unwrap_or_else(|_| Price::zero(self.precision)),
                )
            }
            _ => None,
        }
    }

    /// Bid levels, best (highest) first.
    pub fn bids(&self) -> impl Iterator<Item = (Price, Quantity)> + '_ {
        self.bids
            .iter()
            .rev()
            .map(|(&raw, &q)| (self.price(raw), q))
    }

    /// Ask levels, best (lowest) first.
    pub fn asks(&self) -> impl Iterator<Item = (Price, Quantity)> + '_ {
        self.asks.iter().map(|(&raw, &q)| (self.price(raw), q))
    }

    pub fn is_empty(&self) -> bool {
        self.bids.is_empty() && self.asks.is_empty()
    }

    /// Total number of price levels across both sides.
    pub fn level_count(&self) -> usize {
        self.bids.len() + self.asks.len()
    }

    pub fn last_sequence(&self) -> u64 {
        self.last_sequence
    }

    /// Venue timestamp of the last applied delta (`UnixNanos(0)` if none applied yet).
    pub fn ts_last(&self) -> UnixNanos {
        self.ts_last
    }

    /// Derive a top-of-book [`QuoteTick`] from the best bid/ask. `None` unless both sides have a
    /// level. Display/derivation helper — the book never auto-overwrites the cached mark.
    pub fn to_quote(&self, ts_init: UnixNanos) -> Option<QuoteTick> {
        match (self.best_bid(), self.best_ask()) {
            (Some((bid, bid_size)), Some((ask, ask_size))) => Some(QuoteTick {
                instrument_id: self.instrument_id.clone(),
                bid,
                ask,
                bid_size,
                ask_size,
                ts_event: self.ts_last,
                ts_init,
            }),
            _ => None,
        }
    }

    fn side_mut(&mut self, side: OrderSide) -> &mut BTreeMap<i64, Quantity> {
        match side {
            OrderSide::Buy => &mut self.bids,
            OrderSide::Sell => &mut self.asks,
        }
    }

    fn price(&self, raw: i64) -> Price {
        Price::from_raw(raw, self.precision).unwrap_or_else(|_| Price::zero(self.precision))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const P: u8 = 2; // price precision
    const S: u8 = 3; // size precision

    fn iid() -> InstrumentId {
        InstrumentId::parse("BTCUSDT.BINANCE").unwrap()
    }

    fn delta(action: BookAction, side: OrderSide, px: i64, sz: i64, seq: u64) -> OrderBookDelta {
        OrderBookDelta {
            instrument_id: iid(),
            action,
            side,
            price: Price::from_raw(px, P).unwrap(),
            size: Quantity::from_raw(sz, S).unwrap(),
            sequence: seq,
            ts_event: UnixNanos(seq),
            ts_init: UnixNanos(seq),
        }
    }

    #[test]
    fn add_update_delete_and_zero_size_maintain_levels() {
        let mut b = OrderBook::new(iid());
        b.apply(&delta(BookAction::Add, OrderSide::Buy, 10_000, 5, 1));
        b.apply(&delta(BookAction::Add, OrderSide::Sell, 10_010, 7, 2));
        assert_eq!(b.best_bid().unwrap().0.raw(), 10_000);
        assert_eq!(b.best_ask().unwrap().0.raw(), 10_010);
        // Update overwrites the size at a level.
        b.apply(&delta(BookAction::Update, OrderSide::Buy, 10_000, 9, 3));
        assert_eq!(b.best_bid().unwrap().1.raw(), 9);
        // A zero-size Update removes the level (Binance diff semantics).
        b.apply(&delta(BookAction::Update, OrderSide::Buy, 10_000, 0, 4));
        assert!(b.best_bid().is_none());
        // Delete removes a level explicitly.
        b.apply(&delta(BookAction::Delete, OrderSide::Sell, 10_010, 7, 5));
        assert!(b.is_empty());
    }

    #[test]
    fn best_bid_ask_mid_and_spread() {
        let mut b = OrderBook::new(iid());
        b.apply(&delta(BookAction::Add, OrderSide::Buy, 9_900, 1, 1));
        b.apply(&delta(BookAction::Add, OrderSide::Buy, 10_000, 2, 2)); // better bid
        b.apply(&delta(BookAction::Add, OrderSide::Sell, 10_020, 3, 3));
        b.apply(&delta(BookAction::Add, OrderSide::Sell, 10_010, 4, 4)); // better ask
        assert_eq!(b.best_bid().unwrap().0.raw(), 10_000);
        assert_eq!(b.best_ask().unwrap().0.raw(), 10_010);
        assert_eq!(b.mid().unwrap().raw(), 10_005);
        assert_eq!(b.spread().unwrap().raw(), 10);
        // Ordering: bids best-first (desc), asks best-first (asc).
        let bids: Vec<i64> = b.bids().map(|(p, _)| p.raw()).collect();
        let asks: Vec<i64> = b.asks().map(|(p, _)| p.raw()).collect();
        assert_eq!(bids, vec![10_000, 9_900]);
        assert_eq!(asks, vec![10_010, 10_020]);
    }

    #[test]
    fn clear_wipes_and_following_adds_rebuild() {
        let mut b = OrderBook::new(iid());
        b.apply(&delta(BookAction::Add, OrderSide::Buy, 9_000, 1, 50));
        b.apply(&delta(BookAction::Add, OrderSide::Sell, 9_100, 1, 51));
        assert!(!b.is_empty());
        // A snapshot boundary: Clear + fresh levels, all at the snapshot's lastUpdateId.
        assert!(b.apply(&delta(BookAction::Clear, OrderSide::Buy, 0, 0, 100)));
        assert!(b.is_empty());
        b.apply(&delta(BookAction::Add, OrderSide::Buy, 10_000, 2, 100));
        b.apply(&delta(BookAction::Add, OrderSide::Sell, 10_010, 2, 100));
        assert_eq!(b.best_bid().unwrap().0.raw(), 10_000);
        assert_eq!(b.best_ask().unwrap().0.raw(), 10_010);
        assert_eq!(b.last_sequence(), 100);
    }

    #[test]
    fn stale_non_clear_delta_is_skipped_but_clear_always_applies() {
        let mut b = OrderBook::new(iid());
        b.apply(&delta(BookAction::Add, OrderSide::Buy, 10_000, 2, 100));
        // An older diff (seq 99) is stale and ignored.
        assert!(!b.apply(&delta(BookAction::Update, OrderSide::Buy, 10_000, 5, 99)));
        assert_eq!(b.best_bid().unwrap().1.raw(), 2);
        // A resync Clear at a higher seq always applies, re-basing the book.
        assert!(b.apply(&delta(BookAction::Clear, OrderSide::Buy, 0, 0, 200)));
        assert!(b.is_empty());
        assert_eq!(b.last_sequence(), 200);
    }

    #[test]
    fn to_quote_requires_both_sides_and_round_trips_serde() {
        let mut b = OrderBook::new(iid());
        b.apply(&delta(BookAction::Add, OrderSide::Buy, 10_000, 2, 1));
        assert!(b.to_quote(UnixNanos(9)).is_none()); // ask side empty
        b.apply(&delta(BookAction::Add, OrderSide::Sell, 10_010, 3, 2));
        let q = b.to_quote(UnixNanos(9)).unwrap();
        assert_eq!(q.bid.raw(), 10_000);
        assert_eq!(q.ask.raw(), 10_010);
        // The aggregate is serde-round-trippable (Redis/cache snapshotting).
        let json = serde_json::to_string(&b).unwrap();
        let b2: OrderBook = serde_json::from_str(&json).unwrap();
        assert_eq!(b2.best_bid().unwrap().0.raw(), 10_000);
        assert_eq!(b2.best_ask().unwrap().0.raw(), 10_010);
    }
}
