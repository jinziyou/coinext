//! `coinext-network` — the shared WS/REST framework that every venue adapter builds on.
//!
//! Per the architecture (`docs/ARCHITECTURE.md` §3, the `coinext-network` crate), this is the common
//! plumbing that the `coinext-adapters/*` venue adapters reuse so each adapter only has to encode the
//! venue's *symbology and wire format*, not its own retry/auth/rate-limit machinery:
//!
//! - [`RestClient`] — signed REST request/response with retry + backoff (instrument load, order
//!   submit/cancel, the fill-poll fallback loop). Signs the query string with HMAC-SHA256 +
//!   `timestamp`/`recvWindow` ([`Signer`]).
//! - [`WsClient`] — a resilient WebSocket with auto-reconnect: streams raw text frames over a
//!   `tokio::mpsc` and emits a [`WsMessage::Reconnected`] control signal on every reconnect so the
//!   adapter can resync stateful (order-book) streams.
//! - [`RateLimiter`] — a `governor`-backed token-bucket gate honoring Binance weight pools before
//!   any request leaves.
//!
//! TLS is rustls everywhere (NOT native-tls/openssl) to avoid a system-openssl dependency.

#![allow(dead_code)]

mod error;
mod ratelimit;
mod rest;
mod sign;
mod ws;

pub use error::{NetError, NetResult};
pub use ratelimit::RateLimiter;
pub use rest::{
    now_unix_ms, Credentials, HttpMethod, RestClient, RestConfig, RestRequest, RestResponse,
};
pub use sign::{build_query, encode_value, signed_query, Signer};
pub use ws::{WsClient, WsConfig, WsMessage};
