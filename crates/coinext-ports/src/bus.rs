//! MessageBus trait + typed payloads. The in-process bus (impl in `coinext-bus`) passes typed
//! `Arc` payloads with ZERO serialization (the deterministic hot path); the Redis-Streams bus
//! serializes a MessagePack `Envelope` for cross-service / UI fan-out.

use coinext_core::TimerEvent;
use coinext_model::{Bar, BarType, Fill, InstrumentId, OrderEvent, QuoteTick, StrategyId, TradeTick};
use std::sync::Arc;

/// Typed in-process bus payload (no serialization).
#[derive(Clone)]
pub enum BusMsg {
    Quote(Arc<QuoteTick>),
    Trade(Arc<TradeTick>),
    Bar(Arc<Bar>),
    Order(Arc<OrderEvent>),
    Fill(Arc<Fill>),
    Timer(Arc<TimerEvent>),
    Ctrl(CtrlMsg),
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum CtrlMsg {
    KillSwitch(bool),
}

/// Typed topic constants (no free-form strings → no silent-drop hazard).
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Topic {
    Quote(InstrumentId),
    Trade(InstrumentId),
    Bar(BarType),
    OrderEvent(StrategyId),
    Fill(StrategyId),
    Timer(StrategyId),
    Ctrl,
}

/// Tags the payload kind for cross-language (Redis Envelope) decode.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum MsgType {
    Quote,
    Trade,
    Bar,
    Delta,
    OrderEvent,
    Fill,
    Timer,
    Cmd,
    Ctrl,
}

/// Handle returned by `subscribe`, used to `unsubscribe`.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct HandlerId(pub u64);

/// A subscriber callback. `FnMut` because handlers carry state; the in-proc bus uses interior
/// mutability so `publish` can take `&self`.
pub type BoxedHandler = Box<dyn FnMut(&BusMsg)>;

pub trait MessageBus {
    /// Cache-then-publish: callers update the Cache BEFORE publishing so subscribers read fresh state.
    fn publish(&self, topic: Topic, msg: BusMsg);
    fn subscribe(&self, topic: Topic, handler: BoxedHandler) -> HandlerId;
    fn unsubscribe(&self, id: HandlerId);
}
