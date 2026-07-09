//! Event-sourced Order: state is the fold of an ordered, immutable `OrderEvent` sequence, never
//! mutated directly. The FSM has a COMPLETE transition table (including the modify path
//! PendingUpdate → Updated → Accepted/PartiallyFilled and PendingCancel → Canceled). Illegal
//! transitions fail-fast. The type is identical in backtest and live.
//!
//! Transition table (event → resulting status, guarded by current status):
//! - Submitted:        Initialized → Submitted
//! - Accepted:         Submitted → Accepted
//! - PendingUpdate:    {Accepted, PartiallyFilled} → PendingUpdate
//! - Updated:          PendingUpdate → {Accepted | PartiallyFilled}
//! - PendingCancel:    {Accepted, PartiallyFilled, PendingUpdate} → PendingCancel
//! - PartiallyFilled:  {Accepted, PartiallyFilled, PendingUpdate} → PartiallyFilled
//! - Filled:           {Accepted, PartiallyFilled, PendingUpdate} → Filled (terminal)
//! - Canceled:         {Accepted, PartiallyFilled, PendingCancel} → Canceled (terminal)
//! - Expired:          {Accepted, PartiallyFilled} → Expired (terminal)
//! - Rejected:         {Submitted, PendingUpdate} → Rejected (terminal)
//! - Denied:           Initialized → Denied (terminal; never leaves the process)

use crate::enums::{OrderSide, OrderStatus, OrderType, TimeInForce};
use crate::fill::Fill;
use crate::identifiers::{ClientOrderId, InstrumentId, StrategyId, VenueOrderId};
use coinext_core::{ModelError, Price, Quantity, UnixNanos};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};

/// Execution flags carried on the order (post_only / reduce_only / display_qty).
/// Reserved — not yet consumed by the matching/fee/risk path.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, Default)]
pub struct OrderFlags {
    pub post_only: bool,
    pub reduce_only: bool,
    pub display_qty: Option<Quantity>,
}

/// The immutable events whose fold defines an Order's state.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum OrderEvent {
    Initialized {
        client_order_id: ClientOrderId,
        instrument_id: InstrumentId,
        side: OrderSide,
        order_type: OrderType,
        quantity: Quantity,
        price: Option<Price>,
        trigger: Option<Price>,
        tif: TimeInForce,
        flags: OrderFlags,
        ts: UnixNanos,
    },
    Submitted {
        ts: UnixNanos,
    },
    Accepted {
        venue_order_id: VenueOrderId,
        ts: UnixNanos,
    },
    PendingUpdate {
        ts: UnixNanos,
    },
    Updated {
        quantity: Option<Quantity>,
        price: Option<Price>,
        ts: UnixNanos,
    },
    PendingCancel {
        ts: UnixNanos,
    },
    PartiallyFilled(Fill),
    Filled(Fill),
    Denied {
        reason: String,
        ts: UnixNanos,
    },
    Rejected {
        reason: String,
        ts: UnixNanos,
    },
    Canceled {
        ts: UnixNanos,
    },
    Expired {
        ts: UnixNanos,
    },
}

/// An order aggregate. Construct via [`Order::new`] (the OrderFactory wraps this and assigns the
/// deterministic `ClientOrderId`); evolve via [`Order::apply`].
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Order {
    pub strategy_id: StrategyId,
    pub client_order_id: ClientOrderId,
    pub venue_order_id: Option<VenueOrderId>,
    pub instrument_id: InstrumentId,
    pub side: OrderSide,
    pub order_type: OrderType,
    pub quantity: Quantity,
    pub price: Option<Price>,
    pub trigger: Option<Price>,
    pub tif: TimeInForce,
    pub flags: OrderFlags,
    pub status: OrderStatus,
    pub filled_qty: Quantity,
    pub avg_px: Option<Price>,
    pub ts_init: UnixNanos,
    pub ts_last: UnixNanos,
    pub events: Vec<OrderEvent>,
}

impl Order {
    /// Construct a fresh order in `Initialized` status, recording the `Initialized` event. The
    /// `client_order_id` is expected to already be the deterministic id from the OrderFactory.
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        strategy_id: StrategyId,
        client_order_id: ClientOrderId,
        instrument_id: InstrumentId,
        side: OrderSide,
        order_type: OrderType,
        quantity: Quantity,
        price: Option<Price>,
        trigger: Option<Price>,
        tif: TimeInForce,
        flags: OrderFlags,
        ts: UnixNanos,
    ) -> Order {
        let init = OrderEvent::Initialized {
            client_order_id: client_order_id.clone(),
            instrument_id: instrument_id.clone(),
            side,
            order_type,
            quantity,
            price,
            trigger,
            tif,
            flags,
            ts,
        };
        Order {
            strategy_id,
            client_order_id,
            venue_order_id: None,
            instrument_id,
            side,
            order_type,
            quantity,
            price,
            trigger,
            tif,
            flags,
            status: OrderStatus::Initialized,
            filled_qty: Quantity::zero(quantity.precision()),
            avg_px: None,
            ts_init: ts,
            ts_last: ts,
            events: vec![init],
        }
    }

    /// Quantity not yet filled.
    pub fn leaves_qty(&self) -> Quantity {
        self.quantity
            .checked_sub(self.filled_qty)
            .unwrap_or_else(|_| Quantity::zero(self.quantity.precision()))
    }

    pub fn is_terminal(&self) -> bool {
        self.status.is_terminal()
    }

    fn ensure(&self, allowed: &[OrderStatus], ev: &str) -> Result<(), ModelError> {
        if allowed.contains(&self.status) {
            Ok(())
        } else {
            Err(ModelError::InvalidTransition(format!(
                "{ev} not allowed from {:?}",
                self.status
            )))
        }
    }

    /// Apply an event, enforcing the FSM transition table, then append it to the log.
    pub fn apply(&mut self, ev: OrderEvent) -> Result<(), ModelError> {
        use OrderStatus::*;
        match &ev {
            OrderEvent::Initialized { .. } => {
                return Err(ModelError::InvalidTransition(
                    "Initialized can only occur at construction".into(),
                ));
            }
            OrderEvent::Submitted { ts } => {
                self.ensure(&[Initialized], "Submitted")?;
                self.status = Submitted;
                self.ts_last = *ts;
            }
            OrderEvent::Accepted { venue_order_id, ts } => {
                self.ensure(&[Submitted], "Accepted")?;
                self.venue_order_id = Some(venue_order_id.clone());
                self.status = Accepted;
                self.ts_last = *ts;
            }
            OrderEvent::PendingUpdate { ts } => {
                self.ensure(&[Accepted, PartiallyFilled], "PendingUpdate")?;
                self.status = PendingUpdate;
                self.ts_last = *ts;
            }
            OrderEvent::Updated {
                quantity,
                price,
                ts,
            } => {
                self.ensure(&[PendingUpdate], "Updated")?;
                if let Some(q) = quantity {
                    self.quantity = *q;
                }
                if let Some(p) = price {
                    self.price = Some(*p);
                }
                self.status = if self.filled_qty.is_positive() {
                    PartiallyFilled
                } else {
                    Accepted
                };
                self.ts_last = *ts;
            }
            OrderEvent::PendingCancel { ts } => {
                self.ensure(&[Accepted, PartiallyFilled, PendingUpdate], "PendingCancel")?;
                self.status = PendingCancel;
                self.ts_last = *ts;
            }
            OrderEvent::PartiallyFilled(fill) | OrderEvent::Filled(fill) => {
                self.ensure(&[Accepted, PartiallyFilled, PendingUpdate], "Fill")?;
                self.apply_fill(fill)?;
                self.status = if self.leaves_qty().is_zero() {
                    Filled
                } else {
                    PartiallyFilled
                };
                self.ts_last = fill.ts_event;
            }
            OrderEvent::Denied { ts, .. } => {
                self.ensure(&[Initialized], "Denied")?;
                self.status = Denied;
                self.ts_last = *ts;
            }
            OrderEvent::Rejected { ts, .. } => {
                self.ensure(&[Submitted, PendingUpdate], "Rejected")?;
                self.status = Rejected;
                self.ts_last = *ts;
            }
            OrderEvent::Canceled { ts } => {
                self.ensure(&[Accepted, PartiallyFilled, PendingCancel], "Canceled")?;
                self.status = Canceled;
                self.ts_last = *ts;
            }
            OrderEvent::Expired { ts } => {
                self.ensure(&[Accepted, PartiallyFilled], "Expired")?;
                self.status = Expired;
                self.ts_last = *ts;
            }
        }
        self.events.push(ev);
        Ok(())
    }

    /// Accumulate a fill: update filled quantity and the volume-weighted average fill price.
    fn apply_fill(&mut self, fill: &Fill) -> Result<(), ModelError> {
        let new_filled = self.filled_qty.checked_add(fill.last_qty)?;
        // Volume-weighted average price, computed in exact Decimal then re-quantized.
        let prev_notional = self
            .avg_px
            .map(|p| p.as_decimal() * self.filled_qty.as_decimal())
            .unwrap_or(Decimal::ZERO);
        let add_notional = fill.last_px.as_decimal() * fill.last_qty.as_decimal();
        let total_qty = new_filled.as_decimal();
        let avg = if total_qty.is_zero() {
            fill.last_px.as_decimal()
        } else {
            (prev_notional + add_notional) / total_qty
        };
        self.avg_px = Some(Price::from_decimal(avg, fill.last_px.precision())?);
        self.filled_qty = new_filled;
        Ok(())
    }
}
