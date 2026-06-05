//! Transport-layer error type. Adapters map these into `qv_ports::PortError` at their boundary so
//! the engines above never see venue/transport details.

use thiserror::Error;

/// Errors raised by the shared WS/REST framework. Kept transport-agnostic so the concrete backend
/// (reqwest/tungstenite) can change without rippling into adapters.
#[derive(Debug, Clone, Error)]
pub enum NetError {
    /// The socket/connection is not established (call `connect` first).
    #[error("not connected")]
    NotConnected,

    /// A transport-level failure (DNS, TLS, socket reset, frame decode).
    #[error("transport error: {0}")]
    Transport(String),

    /// Non-2xx HTTP status with the venue's error body.
    #[error("http {status}: {body}")]
    Http { status: u16, body: String },

    /// Local rate limiter refused the request (would exceed the venue weight budget).
    #[error("rate limited: retry after {retry_after_ms} ms")]
    RateLimited { retry_after_ms: u64 },

    /// Request signing / credential failure (e.g. signing requested without a secret).
    #[error("auth error: {0}")]
    Auth(String),

    /// Exhausted the retry budget without a successful response.
    #[error("retries exhausted after {attempts} attempts: {last}")]
    RetriesExhausted { attempts: u32, last: String },

    /// Request timed out.
    #[error("timed out after {0} ms")]
    Timeout(u64),

    /// A response/frame body could not be parsed as expected JSON.
    #[error("decode error: {0}")]
    Decode(String),
}

pub type NetResult<T> = Result<T, NetError>;
