//! The OrderFactory (single owner of the deterministic ClientOrderId) and the StrategyContext —
//! the only surface through which a Strategy reaches the platform. Handlers receive `&mut
//! StrategyContext`; orders/cancels/modifies are collected into an outbox the kernel drains after
//! each handler and routes through Risk → Execution.

use crate::commands::StrategyCommand;
use qv_cache::Cache;
use qv_core::{Clock, Price, Quantity, TimerId, UnixNanos};
use qv_model::{
    ClientOrderId, Instrument, InstrumentId, Order, OrderFlags, OrderSide, OrderType, Position,
    QuoteTick, StrategyId, TimeInForce,
};
use std::cell::RefCell;
use std::rc::Rc;
use std::sync::Arc;

/// The single owner of the deterministic, idempotent `ClientOrderId`
/// (`{strategy_id}-{seq:020}`). The seq is persisted (SeqCursor) in live so it survives crashes;
/// the OMS only tracks/dedupes by the id, never mints one.
pub struct OrderFactory {
    strategy_id: StrategyId,
    seq: u64,
}

impl OrderFactory {
    pub fn new(strategy_id: StrategyId) -> Self {
        OrderFactory {
            strategy_id,
            seq: 0,
        }
    }

    pub fn seq(&self) -> u64 {
        self.seq
    }

    /// Restore the cursor on crash-recovery so regenerated ids never collide / double-submit.
    pub fn restore_seq(&mut self, seq: u64) {
        self.seq = seq;
    }

    fn next_id(&mut self) -> ClientOrderId {
        self.seq += 1;
        ClientOrderId::from(format!("{}-{:020}", self.strategy_id, self.seq))
    }

    pub fn market(
        &mut self,
        instrument_id: InstrumentId,
        side: OrderSide,
        qty: Quantity,
        ts: UnixNanos,
    ) -> Order {
        let id = self.next_id();
        Order::new(
            self.strategy_id.clone(),
            id,
            instrument_id,
            side,
            OrderType::Market,
            qty,
            None,
            None,
            TimeInForce::Ioc,
            OrderFlags::default(),
            ts,
        )
    }

    #[allow(clippy::too_many_arguments)]
    pub fn limit(
        &mut self,
        instrument_id: InstrumentId,
        side: OrderSide,
        qty: Quantity,
        price: Price,
        tif: TimeInForce,
        flags: OrderFlags,
        ts: UnixNanos,
    ) -> Order {
        let id = self.next_id();
        Order::new(
            self.strategy_id.clone(),
            id,
            instrument_id,
            side,
            OrderType::Limit,
            qty,
            Some(price),
            None,
            tif,
            flags,
            ts,
        )
    }
}

/// Everything a Strategy needs from the platform, injected by the kernel. Reads go straight to
/// the shared Cache; order intents accumulate in `outbox`.
pub struct StrategyContext {
    pub strategy_id: StrategyId,
    clock: Rc<dyn Clock>,
    cache: Rc<RefCell<Cache>>,
    factory: OrderFactory,
    outbox: Vec<StrategyCommand>,
}

impl StrategyContext {
    pub fn new(strategy_id: StrategyId, clock: Rc<dyn Clock>, cache: Rc<RefCell<Cache>>) -> Self {
        StrategyContext {
            factory: OrderFactory::new(strategy_id.clone()),
            strategy_id,
            clock,
            cache,
            outbox: Vec::new(),
        }
    }

    // --- time / timers ---
    pub fn now_ns(&self) -> UnixNanos {
        self.clock.now_ns()
    }
    pub fn set_timer(&self, name: &str, at: UnixNanos) -> TimerId {
        self.clock.set_timer(name, at)
    }
    pub fn cancel_timer(&self, id: TimerId) {
        self.clock.cancel_timer(id)
    }

    // --- cache reads (short-lived borrows; never held across a handler) ---
    pub fn position(&self, id: &InstrumentId) -> Option<Position> {
        self.cache.borrow().position(id).cloned()
    }
    pub fn quote(&self, id: &InstrumentId) -> Option<QuoteTick> {
        self.cache.borrow().quote(id).cloned()
    }
    pub fn mark(&self, id: &InstrumentId) -> Option<Price> {
        self.cache.borrow().mark(id)
    }
    pub fn instrument(&self, id: &InstrumentId) -> Option<Arc<dyn Instrument>> {
        self.cache.borrow().instrument(id)
    }

    // --- order intents ---
    pub fn order_factory(&mut self) -> &mut OrderFactory {
        &mut self.factory
    }
    pub fn submit(&mut self, order: Order) {
        self.outbox.push(StrategyCommand::Submit(order));
    }
    pub fn submit_market(
        &mut self,
        instrument_id: InstrumentId,
        side: OrderSide,
        qty: Quantity,
    ) -> ClientOrderId {
        let ts = self.clock.now_ns();
        let order = self.factory.market(instrument_id, side, qty, ts);
        let id = order.client_order_id.clone();
        self.submit(order);
        id
    }
    pub fn submit_limit(
        &mut self,
        instrument_id: InstrumentId,
        side: OrderSide,
        qty: Quantity,
        price: Price,
    ) -> ClientOrderId {
        let ts = self.clock.now_ns();
        let order = self.factory.limit(
            instrument_id,
            side,
            qty,
            price,
            TimeInForce::Gtc,
            OrderFlags::default(),
            ts,
        );
        let id = order.client_order_id.clone();
        self.submit(order);
        id
    }
    pub fn cancel(&mut self, client_order_id: ClientOrderId) {
        self.outbox.push(StrategyCommand::Cancel(client_order_id));
    }
    pub fn modify(
        &mut self,
        client_order_id: ClientOrderId,
        quantity: Option<Quantity>,
        price: Option<Price>,
    ) {
        self.outbox.push(StrategyCommand::Modify {
            client_order_id,
            quantity,
            price,
        });
    }

    /// Kernel-only: take the accumulated intents after a handler returns.
    pub fn drain_outbox(&mut self) -> Vec<StrategyCommand> {
        std::mem::take(&mut self.outbox)
    }
}
