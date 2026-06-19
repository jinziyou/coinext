//! `coinext-cache` — the central in-memory object store. A single read/write interface, identical
//! across backtest and live, with integer-keyed `O(1)` lookups. The mark source for Portfolio
//! unrealized PnL. (In live it can be Redis-backed for crash recovery; the scaffold is in-memory.)

use fnv::FnvHashMap;
use coinext_model::{
    AccountState, ClientOrderId, Instrument, InstrumentId, Order, Position, Price, QuoteTick,
};
use std::sync::Arc;

#[derive(Default)]
pub struct Cache {
    instruments: FnvHashMap<InstrumentId, Arc<dyn Instrument>>,
    quotes: FnvHashMap<InstrumentId, QuoteTick>,
    marks: FnvHashMap<InstrumentId, Price>,
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
