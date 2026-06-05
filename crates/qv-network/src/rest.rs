//! `RestClient` — signed REST with retry + backoff, gated by a [`RateLimiter`](crate::RateLimiter).
//!
//! Adapters use this for instrument loading, order submit/cancel/modify, and the REST fill-poll
//! fallback loop (architecture §7: "Fills/acks arrive via the WS user-stream (fast) + a REST poll
//! loop (fallback)"). Backed by `reqwest` with rustls TLS. Signed requests get a `timestamp` +
//! `recvWindow` appended and an HMAC-SHA256 `signature` over the canonical query string
//! ([`crate::sign`]); the `X-MBX-APIKEY` header carries the api key.

use crate::error::{NetError, NetResult};
use crate::ratelimit::RateLimiter;
use crate::sign::{build_query, signed_query, Signer};
use reqwest::header::{HeaderMap, HeaderValue};
use reqwest::Client;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

/// HTTP verb for a [`RestRequest`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HttpMethod {
    Get,
    Post,
    Put,
    Delete,
}

/// Static config for a venue's REST surface.
#[derive(Debug, Clone)]
pub struct RestConfig {
    /// e.g. `https://api.binance.com` (or the testnet base for sandbox).
    pub base_url: String,
    /// Max retry attempts on retryable (5xx / network) failures.
    pub max_retries: u32,
    /// Initial backoff before exponential growth.
    pub base_backoff_ms: u64,
    /// Per-request timeout.
    pub timeout_ms: u64,
    /// `recvWindow` (ms) appended to signed requests — the venue rejects stale-timestamped orders.
    pub recv_window_ms: u64,
}

impl Default for RestConfig {
    fn default() -> Self {
        RestConfig {
            base_url: String::new(),
            max_retries: 3,
            base_backoff_ms: 100,
            timeout_ms: 5_000,
            recv_window_ms: 5_000,
        }
    }
}

/// A normalized outbound REST request. `signed` requests get an HMAC signature + timestamp appended
/// by the client from the credentials it was built with.
#[derive(Debug, Clone)]
pub struct RestRequest {
    pub method: HttpMethod,
    /// Path relative to the configured `base_url`, e.g. `/api/v3/order`.
    pub path: String,
    pub query: Vec<(String, String)>,
    pub body: Option<String>,
    /// Whether this endpoint requires request signing (trading endpoints do).
    pub signed: bool,
    /// Venue weight cost, charged against the [`RateLimiter`] before sending.
    pub weight: u32,
}

impl RestRequest {
    /// An unsigned GET with a given weight (instrument load, depth snapshot, klines).
    pub fn get(path: impl Into<String>, weight: u32) -> Self {
        RestRequest {
            method: HttpMethod::Get,
            path: path.into(),
            query: Vec::new(),
            body: None,
            signed: false,
            weight,
        }
    }

    /// A signed request (order submit/cancel, openOrders, user-stream listenKey).
    pub fn signed(method: HttpMethod, path: impl Into<String>, weight: u32) -> Self {
        RestRequest {
            method,
            path: path.into(),
            query: Vec::new(),
            body: None,
            signed: true,
            weight,
        }
    }

    /// Append a query param (builder style).
    pub fn with_param(mut self, key: impl Into<String>, value: impl Into<String>) -> Self {
        self.query.push((key.into(), value.into()));
        self
    }
}

/// A normalized REST response.
#[derive(Debug, Clone)]
pub struct RestResponse {
    pub status: u16,
    pub body: String,
}

impl RestResponse {
    /// Parse the body as JSON into `T`.
    pub fn json<T: serde::de::DeserializeOwned>(&self) -> NetResult<T> {
        serde_json::from_str(&self.body).map_err(|e| NetError::Decode(e.to_string()))
    }
}

/// Optional API credentials for signed endpoints.
#[derive(Clone, Default)]
pub struct Credentials {
    pub api_key: Option<String>,
    pub signer: Option<Signer>,
}

impl Credentials {
    pub fn new(api_key: impl Into<String>, api_secret: impl Into<String>) -> Self {
        Credentials {
            api_key: Some(api_key.into()),
            signer: Some(Signer::new(api_secret)),
        }
    }
}

/// Resilient, rate-limited, optionally-signing REST client shared by all venue adapters.
pub struct RestClient {
    config: RestConfig,
    rate_limiter: RateLimiter,
    http: Client,
    creds: Credentials,
}

impl RestClient {
    /// Construct a client over a config, a shared rate limiter, and optional credentials.
    pub fn new(config: RestConfig, rate_limiter: RateLimiter, creds: Credentials) -> NetResult<Self> {
        let http = Client::builder()
            .timeout(Duration::from_millis(config.timeout_ms))
            .build()
            .map_err(|e| NetError::Transport(e.to_string()))?;
        Ok(RestClient {
            config,
            rate_limiter,
            http,
            creds,
        })
    }

    /// The configured base URL.
    pub fn base_url(&self) -> &str {
        &self.config.base_url
    }

    /// Whether signing credentials are present.
    pub fn can_sign(&self) -> bool {
        self.creds.signer.is_some()
    }

    /// Build the final query string for a request, adding `timestamp`/`recvWindow`/`signature` for
    /// signed requests. Pure given the request + a fixed `now_ms`, so the timestamp/signature path
    /// is unit-testable without a clock.
    fn build_query_string(&self, req: &RestRequest, now_ms: u64) -> NetResult<String> {
        if !req.signed {
            return Ok(build_query(&req.query));
        }
        let signer = self
            .creds
            .signer
            .as_ref()
            .ok_or_else(|| NetError::Auth("signing requested but no api secret configured".into()))?;
        let mut params = req.query.clone();
        params.push(("recvWindow".to_string(), self.config.recv_window_ms.to_string()));
        params.push(("timestamp".to_string(), now_ms.to_string()));
        Ok(signed_query(&params, signer))
    }

    /// Send a request: rate-limit, sign (if needed), issue, and retry on retryable failures.
    pub async fn send(&self, req: RestRequest) -> NetResult<RestResponse> {
        self.rate_limiter.acquire(req.weight).await?;

        let mut attempt: u32 = 0;
        #[allow(unused_assignments)]
        let mut last_err = String::new();
        loop {
            let now_ms = now_unix_ms();
            let query = self.build_query_string(&req, now_ms)?;
            let mut url = format!("{}{}", self.config.base_url, req.path);
            if !query.is_empty() {
                url.push('?');
                url.push_str(&query);
            }

            let mut headers = HeaderMap::new();
            if req.signed {
                let key = self.creds.api_key.as_deref().ok_or_else(|| {
                    NetError::Auth("signing requested but no api key configured".into())
                })?;
                headers.insert(
                    "X-MBX-APIKEY",
                    HeaderValue::from_str(key)
                        .map_err(|e| NetError::Auth(format!("bad api key header: {e}")))?,
                );
            }

            let method = match req.method {
                HttpMethod::Get => reqwest::Method::GET,
                HttpMethod::Post => reqwest::Method::POST,
                HttpMethod::Put => reqwest::Method::PUT,
                HttpMethod::Delete => reqwest::Method::DELETE,
            };

            let mut builder = self.http.request(method, &url).headers(headers);
            if let Some(body) = &req.body {
                builder = builder.body(body.clone());
            }

            match builder.send().await {
                Ok(resp) => {
                    let status = resp.status().as_u16();
                    let body = resp
                        .text()
                        .await
                        .map_err(|e| NetError::Transport(e.to_string()))?;
                    if (200..300).contains(&status) {
                        return Ok(RestResponse { status, body });
                    }
                    // 429/418 are rate-limit/ban; 5xx are transient — both retryable. 4xx (client)
                    // errors are terminal (bad order, duplicate client id, etc.).
                    let retryable = status == 429 || status == 418 || (500..600).contains(&status);
                    if !retryable || attempt >= self.config.max_retries {
                        return Err(NetError::Http { status, body });
                    }
                    last_err = format!("http {status}: {body}");
                }
                Err(e) => {
                    if attempt >= self.config.max_retries {
                        return Err(NetError::Transport(e.to_string()));
                    }
                    last_err = e.to_string();
                }
            }

            attempt += 1;
            if attempt > self.config.max_retries {
                return Err(NetError::RetriesExhausted {
                    attempts: attempt,
                    last: last_err,
                });
            }
            // Exponential backoff: base * 2^(attempt-1).
            let backoff = self
                .config
                .base_backoff_ms
                .saturating_mul(1u64 << (attempt - 1).min(16));
            tokio::time::sleep(Duration::from_millis(backoff)).await;
        }
    }
}

/// Current Unix time in milliseconds (the `timestamp` param Binance signs).
pub fn now_unix_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn client_with_secret() -> RestClient {
        RestClient::new(
            RestConfig {
                base_url: "https://api.binance.com".into(),
                recv_window_ms: 5_000,
                ..Default::default()
            },
            RateLimiter::per_minute(1200),
            Credentials::new("KEY", "SECRET"),
        )
        .unwrap()
    }

    #[test]
    fn unsigned_query_is_plain() {
        let rl = RateLimiter::per_minute(1200);
        let c = RestClient::new(
            RestConfig {
                base_url: "https://api.binance.com".into(),
                ..Default::default()
            },
            rl,
            Credentials::default(),
        )
        .unwrap();
        let req = RestRequest::get("/api/v3/klines", 1)
            .with_param("symbol", "BTCUSDT")
            .with_param("interval", "1m");
        let q = c.build_query_string(&req, 1_700_000_000_000).unwrap();
        assert_eq!(q, "symbol=BTCUSDT&interval=1m");
    }

    #[test]
    fn signed_query_appends_timestamp_recvwindow_and_signature() {
        let c = client_with_secret();
        let req = RestRequest::signed(HttpMethod::Post, "/api/v3/order", 1)
            .with_param("symbol", "BTCUSDT")
            .with_param("side", "BUY");
        let q = c.build_query_string(&req, 1_700_000_000_000).unwrap();
        // Deterministic given the fixed now_ms: the unsigned portion is stable and signature is
        // appended last over exactly that string.
        let unsigned = "symbol=BTCUSDT&side=BUY&recvWindow=5000&timestamp=1700000000000";
        assert!(q.starts_with(unsigned), "got: {q}");
        let expected_sig = Signer::new("SECRET").sign(unsigned);
        assert!(q.ends_with(&format!("signature={expected_sig}")), "got: {q}");
    }

    #[test]
    fn signing_without_secret_errors() {
        let c = RestClient::new(
            RestConfig::default(),
            RateLimiter::per_minute(1200),
            Credentials::default(),
        )
        .unwrap();
        let req = RestRequest::signed(HttpMethod::Get, "/api/v3/openOrders", 1);
        assert!(matches!(
            c.build_query_string(&req, 1),
            Err(NetError::Auth(_))
        ));
    }
}
