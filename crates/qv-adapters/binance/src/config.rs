//! Adapter configuration. Sourced from the layered config under the `VQ__BINANCE__*` env
//! convention (see `.env.example`): `API_KEY`, `API_SECRET`, `TESTNET`.

/// Binance endpoint + credential configuration.
///
/// `testnet = true` selects the sandbox endpoints — the SAME adapter code runs against testnet
/// (sandbox) and production (live); only the URLs and keys change, preserving the parity invariant.
#[derive(Debug, Clone)]
pub struct BinanceConfig {
    pub api_key: Option<String>,
    pub api_secret: Option<String>,
    /// `true` -> testnet/sandbox endpoints; `false` -> production/live.
    pub testnet: bool,
}

impl BinanceConfig {
    /// Public market-data config (no credentials) for the given environment.
    pub fn public(testnet: bool) -> Self {
        BinanceConfig {
            api_key: None,
            api_secret: None,
            testnet,
        }
    }

    /// Spot REST base URL for the selected environment.
    pub fn rest_base(&self) -> &'static str {
        if self.testnet {
            "https://testnet.binance.vision"
        } else {
            "https://api.binance.com"
        }
    }

    /// Market-data WS base URL for the selected environment (combined-streams endpoint). Append
    /// `?streams=<a>/<b>/...` to subscribe to the combined stream.
    pub fn ws_market_base(&self) -> &'static str {
        if self.testnet {
            "wss://stream.testnet.binance.vision/stream"
        } else {
            "wss://stream.binance.com:9443/stream"
        }
    }

    /// Raw single-stream WS base (used by the user-data stream: `<base>/<listenKey>`).
    pub fn ws_stream_base(&self) -> &'static str {
        if self.testnet {
            "wss://stream.testnet.binance.vision/ws"
        } else {
            "wss://stream.binance.com:9443/ws"
        }
    }

    /// Whether trading credentials are present (required for sandbox/live order flow).
    pub fn has_credentials(&self) -> bool {
        self.api_key.is_some() && self.api_secret.is_some()
    }
}

impl Default for BinanceConfig {
    fn default() -> Self {
        // Public market-data needs no keys; default to testnet for safety.
        BinanceConfig {
            api_key: None,
            api_secret: None,
            testnet: true,
        }
    }
}
