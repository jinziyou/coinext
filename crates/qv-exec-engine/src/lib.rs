//! `qv-exec-engine` — the OMS. Routes strategy order intents through the pre-trade RiskEngine to
//! the ExecutionClient, and folds `ExecutionReport`s back into the event-sourced Order FSM and the
//! Position. It TRACKS the OrderFactory-assigned `ClientOrderId` (never mints one). In the scaffold
//! the ExecutionClient is the SimulatedExecutionClient; the live path swaps it behind the same port.

use qv_cache::Cache;
use qv_core::UnixNanos;
use qv_model::{Order, OrderEvent, Position, PositionId};
use qv_ports::{ExecutionReport, Portfolio, RiskDecision, RiskEngine};
use qv_sim::SimulatedExecutionClient;
use std::cell::RefCell;
use std::rc::Rc;

pub struct ExecutionEngine {
    cache: Rc<RefCell<Cache>>,
}

impl ExecutionEngine {
    pub fn new(cache: Rc<RefCell<Cache>>) -> Self {
        ExecutionEngine { cache }
    }

    /// Submit an order: run the pre-trade risk gate, then route to the sim (or deny). Returns the
    /// order events applied (for strategy notification + bus publish).
    pub fn submit(
        &self,
        risk: &dyn RiskEngine,
        portfolio: &dyn Portfolio,
        sim: &SimulatedExecutionClient,
        mut order: Order,
        now: UnixNanos,
    ) -> Vec<OrderEvent> {
        let inst = match self.cache.borrow().instrument(&order.instrument_id) {
            Some(i) => i,
            None => return Vec::new(),
        };
        match risk.check(&order, portfolio, &*inst) {
            RiskDecision::Approved => {
                let ev = OrderEvent::Submitted { ts: now };
                let _ = order.apply(ev.clone());
                self.cache.borrow_mut().add_order(order.clone());
                sim.on_submit(order); // sim schedules Accepted + Fill on the delayed queue
                vec![ev]
            }
            RiskDecision::Denied(reason) => {
                let ev = OrderEvent::Denied {
                    reason: reason.to_string(),
                    ts: now,
                };
                let _ = order.apply(ev.clone());
                self.cache.borrow_mut().add_order(order);
                vec![ev]
            }
        }
    }

    /// Request cancellation of a resting order.
    pub fn cancel(
        &self,
        sim: &SimulatedExecutionClient,
        client_order_id: qv_model::ClientOrderId,
        now: UnixNanos,
    ) -> Vec<OrderEvent> {
        let mut applied = Vec::new();
        if let Some(o) = self.cache.borrow_mut().order_mut(&client_order_id) {
            let ev = OrderEvent::PendingCancel { ts: now };
            if o.apply(ev.clone()).is_ok() {
                applied.push(ev);
            }
        }
        sim.on_cancel(client_order_id);
        applied
    }

    /// Fold an execution report into the cached order (FSM) and Position. Returns the order events
    /// applied so the kernel can notify the strategy and publish them.
    pub fn apply_report(&self, report: ExecutionReport, now: UnixNanos) -> Vec<OrderEvent> {
        let mut cache = self.cache.borrow_mut();
        let mut applied = Vec::new();
        match report {
            ExecutionReport::Accepted {
                client_order_id,
                venue_order_id,
            } => {
                if let Some(o) = cache.order_mut(&client_order_id) {
                    let ev = OrderEvent::Accepted {
                        venue_order_id,
                        ts: now,
                    };
                    if o.apply(ev.clone()).is_ok() {
                        applied.push(ev);
                    }
                }
            }
            ExecutionReport::Fill(fill) => {
                let iid = fill.instrument_id.clone();
                let inst = cache.instrument(&iid);
                // 1) Fold into the order FSM (Filled vs PartiallyFilled by remaining qty).
                if let Some(o) = cache.order_mut(&fill.client_order_id) {
                    let full = fill.last_qty.as_decimal() >= o.leaves_qty().as_decimal();
                    let ev = if full {
                        OrderEvent::Filled(fill.clone())
                    } else {
                        OrderEvent::PartiallyFilled(fill.clone())
                    };
                    if o.apply(ev.clone()).is_ok() {
                        applied.push(ev);
                    }
                }
                // 2) Fold into the Position.
                if let Some(inst) = inst {
                    let mut pos = cache.position(&iid).cloned().unwrap_or_else(|| {
                        Position::flat(
                            PositionId::from(format!("{iid}-POS")),
                            iid.clone(),
                            inst.price_precision(),
                            inst.size_precision(),
                            inst.settlement_currency(),
                        )
                    });
                    let _ = pos.apply_fill(&fill, &*inst);
                    cache.upsert_position(pos);
                }
            }
            ExecutionReport::Rejected {
                client_order_id,
                reason,
            } => {
                if let Some(o) = cache.order_mut(&client_order_id) {
                    let ev = OrderEvent::Rejected { reason, ts: now };
                    if o.apply(ev.clone()).is_ok() {
                        applied.push(ev);
                    }
                }
            }
            ExecutionReport::Canceled { client_order_id } => {
                if let Some(o) = cache.order_mut(&client_order_id) {
                    let ev = OrderEvent::Canceled { ts: now };
                    if o.apply(ev.clone()).is_ok() {
                        applied.push(ev);
                    }
                }
            }
            ExecutionReport::Expired { client_order_id } => {
                if let Some(o) = cache.order_mut(&client_order_id) {
                    let ev = OrderEvent::Expired { ts: now };
                    if o.apply(ev.clone()).is_ok() {
                        applied.push(ev);
                    }
                }
            }
            ExecutionReport::PendingUpdate { client_order_id } => {
                if let Some(o) = cache.order_mut(&client_order_id) {
                    let ev = OrderEvent::PendingUpdate { ts: now };
                    if o.apply(ev.clone()).is_ok() {
                        applied.push(ev);
                    }
                }
            }
            ExecutionReport::Modified {
                client_order_id,
                quantity,
                price,
            } => {
                if let Some(o) = cache.order_mut(&client_order_id) {
                    let ev = OrderEvent::Updated {
                        quantity,
                        price,
                        ts: now,
                    };
                    if o.apply(ev.clone()).is_ok() {
                        applied.push(ev);
                    }
                }
            }
            ExecutionReport::PendingCancel { client_order_id } => {
                if let Some(o) = cache.order_mut(&client_order_id) {
                    let ev = OrderEvent::PendingCancel { ts: now };
                    if o.apply(ev.clone()).is_ok() {
                        applied.push(ev);
                    }
                }
            }
        }
        applied
    }
}
