//! A single execution against an order, deduped by `trade_id`. Folds into both Order and Position.
//! Carries fee as first-class `Money` so PnL and slippage analytics stay exact. Identical shape
//! whether it comes from the SimulatedExchange (backtest) or a real venue (live).

use crate::enums::{LiquiditySide, OrderSide};
use crate::identifiers::{ClientOrderId, InstrumentId, TradeId, VenueOrderId};
use qv_core::{Money, Price, Quantity, UnixNanos};
use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Fill {
    pub trade_id: TradeId,
    pub client_order_id: ClientOrderId,
    pub venue_order_id: VenueOrderId,
    pub instrument_id: InstrumentId,
    pub side: OrderSide,
    pub last_px: Price,
    pub last_qty: Quantity,
    pub fee: Money,
    pub liquidity: LiquiditySide,
    /// Venue time of the execution.
    pub ts_event: UnixNanos,
    /// Ingest time (for latency analytics).
    pub ts_init: UnixNanos,
}
