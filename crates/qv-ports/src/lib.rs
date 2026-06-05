//! `qv-ports` — ALL hexagonal port traits and their command/report value types in one place.
//!
//! Keeping ports together (and owning the `async-trait` dependency) resolves the port/value-type
//! crate-boundary muddle: `qv-model` stays sync-only, the engines depend on these traits, and the
//! adapters/sim implement them. See [`traits`] for the seam between the async live-I/O ports and
//! the synchronous core-thread ports.

pub mod bus;
pub mod commands;
pub mod context;
pub mod error;
pub mod traits;

pub use bus::{BoxedHandler, BusMsg, CtrlMsg, HandlerId, MessageBus, MsgType, Topic};
pub use commands::{
    CancelOrder, DenyReason, ExecutionReport, ModifyOrder, RiskDecision, RiskLimits,
    StrategyCommand, SubKind, SubmitOrder, Subscription,
};
pub use context::{OrderFactory, StrategyContext};
pub use error::{PortError, PortResult};
pub use traits::{
    DataClient, ExecutionClient, InstrumentProvider, Portfolio, RiskEngine, Strategy,
};
