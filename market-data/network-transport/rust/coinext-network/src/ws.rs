//! `WsClient` — a resilient WebSocket with auto-reconnect, the substrate for every venue's
//! market-data and user (order/fill) streams.
//!
//! The architecture (§7) requires that on a gap or reconnect the adapter performs a depth-diff
//! resync (snapshot + replay buffered diffs). This client owns the connect / auto-reconnect
//! mechanics and pushes raw text frames onto a `tokio::mpsc` channel; on every transparent
//! reconnect it first pushes a [`WsMessage::Reconnected`] control signal so the adapter knows to
//! resync stateful streams. The *resync policy* (track `lastUpdateId`, fetch a REST snapshot, drop
//! stale diffs) lives in the adapter on top.
//!
//! Backed by `tokio-tungstenite` with rustls (webpki roots).

use crate::error::{NetError, NetResult};
use futures_util::{SinkExt, StreamExt};
use std::time::Duration;
use tokio::sync::mpsc;
use tokio_tungstenite::connect_async;
use tokio_tungstenite::tungstenite::Message;

/// Config for a WebSocket connection.
#[derive(Debug, Clone)]
pub struct WsConfig {
    /// Full connect URL, e.g. `wss://stream.binance.com:9443/stream?streams=btcusdt@trade`.
    pub url: String,
    /// Reconnect backoff ceiling.
    pub max_backoff_ms: u64,
    /// Initial reconnect backoff before exponential growth.
    pub base_backoff_ms: u64,
    /// Capacity of the outbound frame channel (backpressure bound).
    pub channel_capacity: usize,
}

impl Default for WsConfig {
    fn default() -> Self {
        WsConfig {
            url: String::new(),
            max_backoff_ms: 30_000,
            base_backoff_ms: 500,
            channel_capacity: 4096,
        }
    }
}

/// A received WebSocket message. Text is kept raw so the adapter owns parsing; `Reconnected` is a
/// control signal telling the adapter to trigger its depth-diff resync.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum WsMessage {
    Text(String),
    /// The client transparently reconnected — the adapter MUST resync any stateful streams
    /// (order-book snapshot + diff replay) because intervening updates were missed.
    Reconnected,
}

/// Handle to a running WS connection task. The task auto-reconnects forever and streams frames over
/// the receiver returned by [`connect`](WsClient::connect); dropping the handle (or calling
/// [`shutdown`](WsClient::shutdown)) stops the task.
pub struct WsClient {
    config: WsConfig,
    /// Set once the task is spawned; signals it to stop on shutdown/drop.
    shutdown_tx: Option<mpsc::Sender<()>>,
    handle: Option<tokio::task::JoinHandle<()>>,
}

impl WsClient {
    pub fn new(config: WsConfig) -> Self {
        WsClient {
            config,
            shutdown_tx: None,
            handle: None,
        }
    }

    /// The configured URL.
    pub fn url(&self) -> &str {
        &self.config.url
    }

    /// Spawn the connect/auto-reconnect task and return the receiver of [`WsMessage`]s. On every
    /// (re)connect after the first, a [`WsMessage::Reconnected`] is delivered before any frames so
    /// the adapter can resync. The task runs until the receiver is dropped or [`shutdown`] is
    /// called.
    pub fn connect(&mut self) -> mpsc::Receiver<WsMessage> {
        let (tx, rx) = mpsc::channel(self.config.channel_capacity);
        let (shutdown_tx, shutdown_rx) = mpsc::channel(1);
        self.shutdown_tx = Some(shutdown_tx);
        let cfg = self.config.clone();
        let handle = tokio::spawn(run_loop(cfg, tx, shutdown_rx));
        self.handle = Some(handle);
        rx
    }

    /// Signal the task to stop and abort it.
    pub async fn shutdown(&mut self) {
        if let Some(tx) = self.shutdown_tx.take() {
            let _ = tx.send(()).await;
        }
        if let Some(h) = self.handle.take() {
            h.abort();
        }
    }

    /// One-shot connect that yields the underlying split stream — used by the `#[ignore]` live test
    /// to grab a single frame without the reconnect machinery. Returns the first text frame.
    pub async fn connect_once_text(url: &str, timeout_ms: u64) -> NetResult<String> {
        let connect = connect_async(url);
        let (mut stream, _resp) = tokio::time::timeout(Duration::from_millis(timeout_ms), connect)
            .await
            .map_err(|_| NetError::Timeout(timeout_ms))?
            .map_err(|e| NetError::Transport(e.to_string()))?;
        loop {
            let next = tokio::time::timeout(Duration::from_millis(timeout_ms), stream.next())
                .await
                .map_err(|_| NetError::Timeout(timeout_ms))?;
            match next {
                Some(Ok(Message::Text(t))) => return Ok(t.to_string()),
                Some(Ok(Message::Ping(p))) => {
                    let _ = stream.send(Message::Pong(p)).await;
                }
                Some(Ok(_)) => continue,
                Some(Err(e)) => return Err(NetError::Transport(e.to_string())),
                None => return Err(NetError::NotConnected),
            }
        }
    }
}

impl Drop for WsClient {
    fn drop(&mut self) {
        if let Some(h) = self.handle.take() {
            h.abort();
        }
    }
}

/// The reconnect supervisor: connect, stream frames, and on any disconnect back off and reconnect
/// (emitting a `Reconnected` signal). Exits when the receiver is gone or shutdown fires.
async fn run_loop(
    cfg: WsConfig,
    tx: mpsc::Sender<WsMessage>,
    mut shutdown_rx: mpsc::Receiver<()>,
) {
    let mut backoff = cfg.base_backoff_ms;
    let mut first = true;
    loop {
        if !first {
            // Notify the consumer it must resync, then back off before reconnecting.
            if tx.send(WsMessage::Reconnected).await.is_err() {
                return; // consumer dropped
            }
            tokio::select! {
                _ = shutdown_rx.recv() => return,
                _ = tokio::time::sleep(Duration::from_millis(backoff)) => {}
            }
            backoff = (backoff.saturating_mul(2)).min(cfg.max_backoff_ms);
        }
        first = false;

        let connect = connect_async(&cfg.url);
        let stream = tokio::select! {
            _ = shutdown_rx.recv() => return,
            res = connect => res,
        };
        let mut stream = match stream {
            Ok((s, _resp)) => {
                backoff = cfg.base_backoff_ms; // reset on a successful connect
                s
            }
            Err(_) => continue, // back off + retry
        };

        // Pump frames until the socket drops or shutdown fires.
        loop {
            tokio::select! {
                _ = shutdown_rx.recv() => return,
                msg = stream.next() => match msg {
                    Some(Ok(Message::Text(t))) => {
                        if tx.send(WsMessage::Text(t.to_string())).await.is_err() {
                            return; // consumer dropped
                        }
                    }
                    Some(Ok(Message::Ping(p))) => {
                        // Keep the socket alive; tungstenite handles pong automatically on read,
                        // but we answer explicitly to be safe across versions.
                        let _ = stream.send(Message::Pong(p)).await;
                    }
                    Some(Ok(Message::Close(_))) | None => break, // reconnect
                    Some(Ok(_)) => {} // binary/pong/frame — ignore
                    Some(Err(_)) => break, // transport error — reconnect
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ws_message_variants() {
        assert_eq!(
            WsMessage::Text("hi".into()),
            WsMessage::Text("hi".to_string())
        );
        assert_ne!(WsMessage::Reconnected, WsMessage::Text("x".into()));
    }

    #[test]
    fn default_config_has_sane_bounds() {
        let cfg = WsConfig::default();
        assert!(cfg.channel_capacity > 0);
        assert!(cfg.max_backoff_ms >= cfg.base_backoff_ms);
    }
}
