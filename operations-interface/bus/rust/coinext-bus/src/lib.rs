//! `coinext-bus` — message bus implementations behind the `coinext_ports::MessageBus` trait.
//!
//! - [`InProcBus`]: single-node hot path. Passes typed `Arc` payloads with ZERO serialization;
//!   `publish` is deterministic and synchronous. This is what the kernel uses.
//! - [`Envelope`]: the versioned wire format for the Redis-Streams bus (cross-service + UI/o11y).
//!   The actual Redis transport lives in the service layer; here we define the contract so Rust
//!   and Python (`coinext_bus`) agree on it.

use coinext_ports::{BoxedHandler, BusMsg, HandlerId, MessageBus, MsgType, Topic};
use serde::{Deserialize, Serialize};
use std::cell::RefCell;

struct Sub {
    id: HandlerId,
    topic: Topic,
    handler: BoxedHandler,
}

#[derive(Default)]
struct Inner {
    next_id: u64,
    subs: Vec<Sub>,
}

/// In-process bus. Not `Sync` — the deterministic core is single-threaded and uses interior
/// mutability so `publish`/`subscribe` take `&self`.
#[derive(Default)]
pub struct InProcBus {
    inner: RefCell<Inner>,
}

impl InProcBus {
    pub fn new() -> Self {
        InProcBus::default()
    }
}

impl MessageBus for InProcBus {
    fn publish(&self, topic: Topic, msg: BusMsg) {
        // NB: handlers must not re-publish (would re-borrow). The kernel only publishes outside
        // of handler execution, so this holds.
        let mut inner = self.inner.borrow_mut();
        for sub in inner.subs.iter_mut() {
            if sub.topic == topic {
                (sub.handler)(&msg);
            }
        }
    }

    fn subscribe(&self, topic: Topic, handler: BoxedHandler) -> HandlerId {
        let mut inner = self.inner.borrow_mut();
        inner.next_id += 1;
        let id = HandlerId(inner.next_id);
        inner.subs.push(Sub { id, topic, handler });
        id
    }

    fn unsubscribe(&self, id: HandlerId) {
        self.inner.borrow_mut().subs.retain(|s| s.id != id);
    }
}

/// Versioned cross-service wire format (serialized to MessagePack on Redis Streams in prod; the
/// scaffold (and Python `coinext_contracts`) agree on this shape). `trace_id` propagates distributed
/// traces; `payload` is the encoded domain object identified by `msg_type`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Envelope {
    pub schema_version: u16,
    pub msg_type: u8,
    pub trace_id: [u8; 16],
    pub ts_init: u64,
    pub payload: Vec<u8>,
}

impl Envelope {
    pub const SCHEMA_VERSION: u16 = 1;

    pub fn new(msg_type: MsgType, trace_id: [u8; 16], ts_init: u64, payload: Vec<u8>) -> Self {
        Envelope {
            schema_version: Self::SCHEMA_VERSION,
            msg_type: msg_type as u8,
            trace_id,
            ts_init,
            payload,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use coinext_model::InstrumentId;
    use std::rc::Rc;

    #[test]
    fn inproc_bus_delivers_to_matching_topic() {
        let bus = InProcBus::new();
        let iid = InstrumentId::parse("BTCUSDT.BINANCE").unwrap();
        let hits = Rc::new(RefCell::new(0u32));
        let h = hits.clone();
        bus.subscribe(
            Topic::Quote(iid.clone()),
            Box::new(move |_msg| *h.borrow_mut() += 1),
        );
        // A non-matching topic does nothing.
        bus.publish(
            Topic::Ctrl,
            BusMsg::Ctrl(coinext_ports::CtrlMsg::KillSwitch(true)),
        );
        assert_eq!(*hits.borrow(), 0);
        // A matching topic fires.
        let q = coinext_model::QuoteTick {
            instrument_id: iid.clone(),
            bid: coinext_core::Price::from_raw(1, 0).unwrap(),
            ask: coinext_core::Price::from_raw(2, 0).unwrap(),
            bid_size: coinext_core::Quantity::from_raw(1, 0).unwrap(),
            ask_size: coinext_core::Quantity::from_raw(1, 0).unwrap(),
            ts_event: coinext_core::UnixNanos(1),
            ts_init: coinext_core::UnixNanos(1),
        };
        bus.publish(Topic::Quote(iid), BusMsg::Quote(std::sync::Arc::new(q)));
        assert_eq!(*hits.borrow(), 1);
    }
}
