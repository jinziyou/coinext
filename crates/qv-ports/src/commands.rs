//! Command and report value types that flow across the ports: data subscriptions, order
//! commands, execution reports, strategy outbox commands, and risk decisions.

use qv_model::{
    BarSpec, ClientOrderId, Fill, InstrumentId, Money, Order, Price, Quantity, VenueOrderId,
};

/// What kind of data to subscribe to for an instrument.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SubKind {
    Quotes,
    Trades,
    BookL2 { depth: u32 },
    Bars(BarSpec),
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Subscription {
    pub instrument_id: InstrumentId,
    pub kind: SubKind,
}

#[derive(Debug, Clone)]
pub struct SubmitOrder {
    pub order: Order,
}

#[derive(Debug, Clone)]
pub struct CancelOrder {
    pub client_order_id: ClientOrderId,
}

#[derive(Debug, Clone)]
pub struct ModifyOrder {
    pub client_order_id: ClientOrderId,
    pub quantity: Option<Quantity>,
    pub price: Option<Price>,
}

/// Normalized execution outcome emitted by any `ExecutionClient` (sim or live), identical shape.
#[derive(Debug, Clone)]
pub enum ExecutionReport {
    Accepted {
        client_order_id: ClientOrderId,
        venue_order_id: VenueOrderId,
    },
    PendingUpdate {
        client_order_id: ClientOrderId,
    },
    Modified {
        client_order_id: ClientOrderId,
        quantity: Option<Quantity>,
        price: Option<Price>,
    },
    PendingCancel {
        client_order_id: ClientOrderId,
    },
    Fill(Fill),
    Rejected {
        client_order_id: ClientOrderId,
        reason: String,
    },
    Canceled {
        client_order_id: ClientOrderId,
    },
    Expired {
        client_order_id: ClientOrderId,
    },
}

impl ExecutionReport {
    pub fn client_order_id(&self) -> &ClientOrderId {
        match self {
            ExecutionReport::Accepted {
                client_order_id, ..
            }
            | ExecutionReport::PendingUpdate { client_order_id }
            | ExecutionReport::Modified {
                client_order_id, ..
            }
            | ExecutionReport::PendingCancel { client_order_id }
            | ExecutionReport::Rejected {
                client_order_id, ..
            }
            | ExecutionReport::Canceled { client_order_id }
            | ExecutionReport::Expired { client_order_id } => client_order_id,
            ExecutionReport::Fill(f) => &f.client_order_id,
        }
    }
}

/// Commands a Strategy emits via the `StrategyContext` outbox; drained by the kernel after each
/// handler and routed through Risk → Execution.
#[derive(Debug, Clone)]
pub enum StrategyCommand {
    Submit(Order),
    Cancel(ClientOrderId),
    Modify {
        client_order_id: ClientOrderId,
        quantity: Option<Quantity>,
        price: Option<Price>,
    },
}

/// Outcome of the pre-trade risk gate.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum RiskDecision {
    Approved,
    Denied(DenyReason),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DenyReason {
    MaxPositionExceeded,
    MaxNotionalExceeded,
    MaxOrderNotional,
    OrderRateThrottled,
    MinNotional,
    KillSwitchEngaged,
    InstrumentHalted,
}

impl std::fmt::Display for DenyReason {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{self:?}")
    }
}

/// Pre-trade risk limits. Same struct configures backtest and live so risk-shaped behavior is
/// reproducible.
#[derive(Debug, Clone)]
pub struct RiskLimits {
    pub max_position_qty: Option<Quantity>,
    pub max_position_notional: Option<Money>,
    pub max_order_notional: Option<Money>,
    pub max_orders_per_sec: Option<u32>,
    pub max_gross_exposure: Option<Money>,
}

impl RiskLimits {
    /// No limits (kill-switch still applies). Useful for tests/examples.
    pub fn unlimited() -> Self {
        RiskLimits {
            max_position_qty: None,
            max_position_notional: None,
            max_order_notional: None,
            max_orders_per_sec: None,
            max_gross_exposure: None,
        }
    }
}
