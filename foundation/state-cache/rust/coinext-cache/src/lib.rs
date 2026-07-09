//! `coinext-cache` — the central in-memory object store. A single read/write interface, identical
//! across backtest and live, with id-keyed `O(1)` hash lookups. The mark source for Portfolio
//! unrealized PnL. (In live it can be Redis-backed for crash recovery; the scaffold is in-memory.)

use coinext_model::{
    AccountState, ClientOrderId, Instrument, InstrumentId, Order, OrderBook, OrderBookDelta,
    Position, Price, QuoteTick,
};
use fnv::FnvHashMap;
use std::sync::Arc;

#[derive(Default)]
pub struct Cache {
    instruments: FnvHashMap<InstrumentId, Arc<dyn Instrument>>,
    quotes: FnvHashMap<InstrumentId, QuoteTick>,
    marks: FnvHashMap<InstrumentId, Price>,
    order_books: FnvHashMap<InstrumentId, OrderBook>,
    orders: FnvHashMap<ClientOrderId, Order>,
    positions: FnvHashMap<InstrumentId, Position>,
    account: Option<AccountState>,
}

impl Cache {
    pub fn new() -> Self {
        Cache::default()
    }

    // --- instruments ---
    pub fn add_instrument(&mut self, inst: Arc<dyn Instrument>) {
        self.instruments.insert(inst.id(), inst);
    }
    pub fn instrument(&self, id: &InstrumentId) -> Option<Arc<dyn Instrument>> {
        self.instruments.get(id).cloned()
    }

    // --- quotes & marks ---
    /// Store the latest quote and refresh the mark to the quote mid (perps override via `set_mark`).
    pub fn add_quote(&mut self, q: QuoteTick) {
        let id = q.instrument_id.clone();
        let mid = q.mid();
        self.quotes.insert(id.clone(), q);
        self.marks.insert(id, mid);
    }
    pub fn quote(&self, id: &InstrumentId) -> Option<&QuoteTick> {
        self.quotes.get(id)
    }
    pub fn set_mark(&mut self, id: InstrumentId, mark: Price) {
        self.marks.insert(id, mark);
    }
    pub fn mark(&self, id: &InstrumentId) -> Option<Price> {
        self.marks.get(id).copied()
    }

    // --- L2 order books ---
    /// Fold an order-book delta into the per-instrument [`OrderBook`] (creating it on first sight).
    /// Returns `false` if the delta was stale and skipped. The book is NOT auto-promoted to the mark
    /// — marks stay sourced from quotes/trades/bars so valuation semantics are unchanged.
    pub fn apply_book_delta(&mut self, delta: &OrderBookDelta) -> bool {
        self.order_books
            .entry(delta.instrument_id.clone())
            .or_insert_with(|| OrderBook::new(delta.instrument_id.clone()))
            .apply(delta)
    }
    pub fn order_book(&self, id: &InstrumentId) -> Option<&OrderBook> {
        self.order_books.get(id)
    }
    pub fn order_book_mut(&mut self, id: &InstrumentId) -> Option<&mut OrderBook> {
        self.order_books.get_mut(id)
    }

    // --- orders ---
    pub fn add_order(&mut self, order: Order) {
        self.orders.insert(order.client_order_id.clone(), order);
    }
    pub fn order(&self, id: &ClientOrderId) -> Option<&Order> {
        self.orders.get(id)
    }
    pub fn order_mut(&mut self, id: &ClientOrderId) -> Option<&mut Order> {
        self.orders.get_mut(id)
    }
    pub fn orders(&self) -> impl Iterator<Item = &Order> {
        self.orders.values()
    }
    pub fn open_orders(&self) -> impl Iterator<Item = &Order> {
        self.orders.values().filter(|o| !o.is_terminal())
    }

    // --- positions ---
    pub fn upsert_position(&mut self, pos: Position) {
        self.positions.insert(pos.instrument_id.clone(), pos);
    }
    pub fn position(&self, id: &InstrumentId) -> Option<&Position> {
        self.positions.get(id)
    }
    pub fn position_mut(&mut self, id: &InstrumentId) -> Option<&mut Position> {
        self.positions.get_mut(id)
    }
    pub fn positions(&self) -> impl Iterator<Item = &Position> {
        self.positions.values()
    }

    // --- account ---
    pub fn set_account(&mut self, account: AccountState) {
        self.account = Some(account);
    }
    pub fn account(&self) -> Option<&AccountState> {
        self.account.as_ref()
    }
    pub fn account_mut(&mut self) -> Option<&mut AccountState> {
        self.account.as_mut()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use coinext_model::{BookAction, OrderSide, Quantity, UnixNanos};

    fn delta(action: BookAction, side: OrderSide, px: i64, sz: i64, seq: u64) -> OrderBookDelta {
        OrderBookDelta {
            instrument_id: InstrumentId::parse("BTCUSDT.BINANCE").unwrap(),
            action,
            side,
            price: Price::from_raw(px, 2).unwrap(),
            size: Quantity::from_raw(sz, 3).unwrap(),
            sequence: seq,
            ts_event: UnixNanos(seq),
            ts_init: UnixNanos(seq),
        }
    }

    #[test]
    fn apply_book_delta_creates_and_maintains_the_book() {
        let mut cache = Cache::new();
        let iid = InstrumentId::parse("BTCUSDT.BINANCE").unwrap();
        assert!(cache.order_book(&iid).is_none());
        assert!(cache.apply_book_delta(&delta(BookAction::Add, OrderSide::Buy, 10_000, 5, 1)));
        assert!(cache.apply_book_delta(&delta(BookAction::Add, OrderSide::Sell, 10_010, 5, 2)));
        let book = cache.order_book(&iid).unwrap();
        assert_eq!(book.best_bid().unwrap().0.raw(), 10_000);
        assert_eq!(book.best_ask().unwrap().0.raw(), 10_010);
        // A stale (older-sequence) delta is rejected.
        assert!(!cache.apply_book_delta(&delta(BookAction::Update, OrderSide::Buy, 10_000, 9, 0)));
    }
}
