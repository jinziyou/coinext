//! Paper OMS: Redis CMD envelopes → SeqCursor + EventStore → exec report stream.
//!
//! Command payload (MessagePack map inside `MsgType::Cmd` Envelope)::
//!
//! ```json
//! {
//!   "kind": "SubmitMarket" | "Cancel",
//!   "strategy_id": "SmaCross",
//!   "symbol": "BTCUSDT",
//!   "venue": "BINANCE",
//!   "side": "buy" | "sell",
//!   "qty": 0.01,
//!   "client_order_id": "optional-override"
//! }
//! ```
//!
//! When the kill-switch is engaged, submits are denied and a denial event is published.
//! When a [`crate::venue::VenueBridge`] is present, submits/cancels are forwarded to the venue
//! and paper fills are suppressed (venue reports arrive on a separate pump).

use crate::venue::VenueBridge;
use coinext_core::UnixNanos;
use coinext_model::{ClientOrderId, OrderEvent, StrategyId, VenueOrderId};
use coinext_persistence::{EventStore, SeqCursor};
use coinext_ports::MsgType;
use serde::Deserialize;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

pub const STREAM_EXEC_CMD: &str = "coinext.exec.cmd";
pub const STREAM_EXEC: &str = "coinext.exec";

#[derive(Debug, Deserialize)]
pub struct ExecCommand {
    pub kind: String,
    #[serde(default = "default_strategy")]
    pub strategy_id: String,
    #[serde(default = "default_symbol")]
    pub symbol: String,
    #[serde(default = "default_venue")]
    pub venue: String,
    #[serde(default)]
    pub side: Option<String>,
    #[serde(default)]
    pub qty: Option<f64>,
    #[serde(default)]
    pub client_order_id: Option<String>,
}

fn default_strategy() -> String {
    "default".into()
}
fn default_symbol() -> String {
    "BTCUSDT".into()
}
fn default_venue() -> String {
    "BINANCE".into()
}

pub struct PaperOms {
    store: Arc<dyn EventStore>,
    cursor: Arc<dyn SeqCursor>,
    kill: Arc<AtomicBool>,
    pub submits: AtomicU64,
    pub cancels: AtomicU64,
    pub denials: AtomicU64,
}

impl PaperOms {
    pub fn new(
        store: Arc<dyn EventStore>,
        cursor: Arc<dyn SeqCursor>,
        kill: Arc<AtomicBool>,
    ) -> Self {
        PaperOms {
            store,
            cursor,
            kill,
            submits: AtomicU64::new(0),
            cancels: AtomicU64::new(0),
            denials: AtomicU64::new(0),
        }
    }

    /// Handle one command map; returns events published to `coinext.exec` (JSON maps).
    ///
    /// For unit tests (no running Tokio runtime) this spins a current-thread runtime.
    pub fn handle(&self, cmd: ExecCommand) -> Vec<serde_json::Value> {
        match tokio::runtime::Handle::try_current() {
            Ok(h) => h.block_on(self.handle_async(cmd, None)),
            Err(_) => tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .expect("test runtime")
                .block_on(self.handle_async(cmd, None)),
        }
    }

    /// Async path: optional venue bridge for real submit/cancel.
    pub async fn handle_async(
        &self,
        cmd: ExecCommand,
        venue: Option<&VenueBridge>,
    ) -> Vec<serde_json::Value> {
        let now = now_ns();
        let kind = cmd.kind.to_ascii_lowercase();
        match kind.as_str() {
            "submitmarket" | "submit_market" | "submit" => {
                self.handle_submit(cmd, now, venue).await
            }
            "cancel" => self.handle_cancel(cmd, now, venue).await,
            _ => {
                vec![serde_json::json!({
                    "kind": "error",
                    "reason": format!("unknown command kind {}", cmd.kind),
                    "ts_ns": now,
                })]
            }
        }
    }

    async fn handle_submit(
        &self,
        cmd: ExecCommand,
        now: u64,
        venue: Option<&VenueBridge>,
    ) -> Vec<serde_json::Value> {
        let strategy = StrategyId::from(cmd.strategy_id.as_str());
        let coid = match &cmd.client_order_id {
            Some(id) if !id.is_empty() => ClientOrderId::from(id.as_str()),
            _ => {
                let seq = self.cursor.next(strategy.as_str()).unwrap_or(1);
                ClientOrderId::from(format!("{}-{:020}", strategy.as_str(), seq))
            }
        };
        let instrument = format!("{}.{}", cmd.symbol, cmd.venue);
        let side = cmd.side.clone().unwrap_or_else(|| "buy".into());
        let qty = cmd.qty.unwrap_or(0.0);

        if self.kill.load(Ordering::SeqCst) {
            self.denials.fetch_add(1, Ordering::SeqCst);
            let ev = OrderEvent::Denied {
                reason: "kill-switch engaged".into(),
                ts: UnixNanos(now),
            };
            let _ = self.store.append(&strategy, &coid, &ev, UnixNanos(now));
            return vec![serde_json::json!({
                "kind": "denied",
                "client_order_id": coid.as_str(),
                "strategy_id": strategy.as_str(),
                "instrument": instrument,
                "side": side,
                "qty": qty,
                "reason": "kill-switch engaged",
                "ts_ns": now,
            })];
        }

        self.submits.fetch_add(1, Ordering::SeqCst);
        let submitted = OrderEvent::Submitted {
            ts: UnixNanos(now),
        };
        let _ = self
            .store
            .append(&strategy, &coid, &submitted, UnixNanos(now));

        if let Some(v) = venue {
            match v
                .submit_market(
                    strategy.as_str(),
                    coid.as_str(),
                    &cmd.symbol,
                    &cmd.venue,
                    &side,
                    qty,
                    now,
                )
                .await
            {
                Ok(()) => {
                    return vec![serde_json::json!({
                        "kind": "submitted",
                        "client_order_id": coid.as_str(),
                        "strategy_id": strategy.as_str(),
                        "instrument": instrument,
                        "side": side,
                        "qty": qty,
                        "mode": "venue",
                        "note": "routed to Binance ExecutionClient; fills via user-stream pump",
                        "ts_ns": now,
                    })];
                }
                Err(e) => {
                    self.denials.fetch_add(1, Ordering::SeqCst);
                    let ev = OrderEvent::Rejected {
                        reason: e.clone(),
                        ts: UnixNanos(now),
                    };
                    let _ = self.store.append(&strategy, &coid, &ev, UnixNanos(now));
                    return vec![serde_json::json!({
                        "kind": "rejected",
                        "client_order_id": coid.as_str(),
                        "reason": e,
                        "mode": "venue",
                        "ts_ns": now,
                    })];
                }
            }
        }

        // Paper path: accept + synthetic fill.
        let accepted = OrderEvent::Accepted {
            venue_order_id: VenueOrderId::from(format!("PAPER-{}", coid.as_str())),
            ts: UnixNanos(now),
        };
        let _ = self
            .store
            .append(&strategy, &coid, &accepted, UnixNanos(now));

        vec![
            serde_json::json!({
                "kind": "submitted",
                "client_order_id": coid.as_str(),
                "strategy_id": strategy.as_str(),
                "instrument": instrument,
                "side": side,
                "qty": qty,
                "mode": "paper",
                "ts_ns": now,
            }),
            serde_json::json!({
                "kind": "accepted",
                "client_order_id": coid.as_str(),
                "venue_order_id": format!("PAPER-{}", coid.as_str()),
                "mode": "paper",
                "ts_ns": now,
            }),
            serde_json::json!({
                "kind": "filled",
                "client_order_id": coid.as_str(),
                "instrument": instrument,
                "side": side,
                "qty": qty,
                "px": 0.0,
                "mode": "paper",
                "note": "paper fill",
                "ts_ns": now,
            }),
        ]
    }

    async fn handle_cancel(
        &self,
        cmd: ExecCommand,
        now: u64,
        venue: Option<&VenueBridge>,
    ) -> Vec<serde_json::Value> {
        let strategy = StrategyId::from(cmd.strategy_id.as_str());
        let Some(id) = cmd.client_order_id.filter(|s| !s.is_empty()) else {
            return vec![serde_json::json!({
                "kind": "error",
                "reason": "cancel requires client_order_id",
                "ts_ns": now,
            })];
        };
        let coid = ClientOrderId::from(id.as_str());
        self.cancels.fetch_add(1, Ordering::SeqCst);

        if let Some(v) = venue {
            if let Err(e) = v.cancel(coid.as_str(), &cmd.symbol, &cmd.venue).await {
                return vec![serde_json::json!({
                    "kind": "error",
                    "client_order_id": coid.as_str(),
                    "reason": e,
                    "mode": "venue",
                    "ts_ns": now,
                })];
            }
            // Venue cancel ack arrives via report pump; record local intent.
            let ev = OrderEvent::PendingCancel {
                ts: UnixNanos(now),
            };
            let _ = self.store.append(&strategy, &coid, &ev, UnixNanos(now));
            return vec![serde_json::json!({
                "kind": "pending_cancel",
                "client_order_id": coid.as_str(),
                "strategy_id": strategy.as_str(),
                "mode": "venue",
                "ts_ns": now,
            })];
        }

        let ev = OrderEvent::Canceled {
            ts: UnixNanos(now),
        };
        let _ = self.store.append(&strategy, &coid, &ev, UnixNanos(now));
        vec![serde_json::json!({
            "kind": "canceled",
            "client_order_id": coid.as_str(),
            "strategy_id": strategy.as_str(),
            "mode": "paper",
            "ts_ns": now,
        })]
    }
}

pub fn decode_cmd_envelope(frame: &[u8]) -> Option<ExecCommand> {
    let decoded: (u16, u8, serde_bytes::ByteBuf, u64, serde_bytes::ByteBuf) =
        rmp_serde::from_slice(frame).ok()?;
    let (_schema, msg_type, _trace, _ts, payload) = decoded;
    // MsgType::Cmd = 7, also accept Ctrl=8 only for kill (handled elsewhere)
    if msg_type != 7 && msg_type != 0 {
        // allow raw payload maps published without strict typing in tests (msg_type 0 rare)
        if msg_type != 7 {
            // still try if payload looks like a command
        }
    }
    if msg_type != 7 {
        return None;
    }
    rmp_serde::from_slice(payload.as_slice()).ok()
}

pub fn encode_event_envelope(payload: &serde_json::Value) -> Result<Vec<u8>, String> {
    let body = rmp_serde::to_vec_named(payload).map_err(|e| e.to_string())?;
    let frame = (
        1u16,
        MsgType::OrderEvent as u8,
        [0u8; 16].as_slice(),
        now_ns(),
        body.as_slice(),
    );
    rmp_serde::to_vec(&frame).map_err(|e| e.to_string())
}

fn now_ns() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos() as u64)
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use coinext_persistence::{InMemorySeqCursor, NullEventStore};

    #[test]
    fn paper_submit_and_kill_denial() {
        let store: Arc<dyn EventStore> = Arc::new(NullEventStore);
        let cursor: Arc<dyn SeqCursor> = Arc::new(InMemorySeqCursor::new());
        let kill = Arc::new(AtomicBool::new(false));
        let oms = PaperOms::new(store.clone(), cursor.clone(), kill.clone());

        let evs = oms.handle(ExecCommand {
            kind: "SubmitMarket".into(),
            strategy_id: "s1".into(),
            symbol: "BTCUSDT".into(),
            venue: "BINANCE".into(),
            side: Some("buy".into()),
            qty: Some(0.1),
            client_order_id: None,
        });
        assert!(evs.iter().any(|e| e["kind"] == "submitted"));
        assert!(evs.iter().any(|e| e["kind"] == "filled"));
        assert_eq!(oms.submits.load(Ordering::SeqCst), 1);

        kill.store(true, Ordering::SeqCst);
        let denied = oms.handle(ExecCommand {
            kind: "SubmitMarket".into(),
            strategy_id: "s1".into(),
            symbol: "BTCUSDT".into(),
            venue: "BINANCE".into(),
            side: Some("buy".into()),
            qty: Some(0.1),
            client_order_id: None,
        });
        assert!(denied.iter().any(|e| e["kind"] == "denied"));
        assert_eq!(oms.denials.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn encode_decode_cmd_roundtrip() {
        let payload = serde_json::json!({
            "kind": "SubmitMarket",
            "strategy_id": "s",
            "symbol": "ETHUSDT",
            "venue": "BINANCE",
            "side": "sell",
            "qty": 1.5,
        });
        let body = rmp_serde::to_vec_named(&payload).unwrap();
        let frame = rmp_serde::to_vec(&(1u16, 7u8, [0u8; 16].as_slice(), 1u64, body.as_slice()))
            .unwrap();
        let cmd = decode_cmd_envelope(&frame).expect("decode");
        assert_eq!(cmd.kind, "SubmitMarket");
        assert_eq!(cmd.symbol, "ETHUSDT");
        assert_eq!(cmd.qty, Some(1.5));
    }
}
