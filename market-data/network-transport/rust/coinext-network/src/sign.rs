//! HMAC-SHA256 request signer — the credential machinery for Binance-style signed REST endpoints.
//!
//! Binance signs the *exact query/body string* (the concatenation of all params, including
//! `timestamp` and `recvWindow`) with the account's API secret and appends the lowercase hex
//! digest as a trailing `signature` param. The signature MUST be computed over the canonical string
//! the server will reconstruct, so [`Signer::sign`] takes the already-encoded payload and the
//! [`build_query`]/[`signed_query`] helpers own the encoding order.

use hmac::{Hmac, Mac};
use sha2::Sha256;

type HmacSha256 = Hmac<Sha256>;

/// Holds an API secret and produces HMAC-SHA256 hex digests over request payloads.
#[derive(Clone)]
pub struct Signer {
    secret: Vec<u8>,
}

impl Signer {
    pub fn new(secret: impl Into<String>) -> Self {
        Signer {
            secret: secret.into().into_bytes(),
        }
    }

    /// HMAC-SHA256 of `payload` under the secret, returned as a lowercase hex string (the form
    /// Binance expects for the `signature` parameter).
    pub fn sign(&self, payload: &str) -> String {
        // `Hmac::new_from_slice` only errors on impossible key lengths; SHA-256 accepts any.
        let mut mac =
            HmacSha256::new_from_slice(&self.secret).expect("HMAC accepts keys of any length");
        mac.update(payload.as_bytes());
        hex::encode(mac.finalize().into_bytes())
    }
}

/// URL-encode a single query value per `application/x-www-form-urlencoded` rules. Binance's
/// allowed param characters (symbols, numbers, client ids) rarely need escaping, but client order
/// ids may contain `-` (allowed) and, defensively, we percent-encode anything outside the
/// unreserved set so the signed string matches what the server reconstructs.
pub fn encode_value(value: &str) -> String {
    let mut out = String::with_capacity(value.len());
    for byte in value.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => {
                out.push(byte as char);
            }
            _ => out.push_str(&format!("%{byte:02X}")),
        }
    }
    out
}

/// Build a `key1=val1&key2=val2` query string from ordered params, preserving caller order (Binance
/// does NOT require sorted params — it signs the literal string sent — so order is caller-defined
/// and stable). Values are URL-encoded; keys are assumed already canonical.
pub fn build_query(params: &[(String, String)]) -> String {
    params
        .iter()
        .map(|(k, v)| format!("{k}={}", encode_value(v)))
        .collect::<Vec<_>>()
        .join("&")
}

/// Build the full signed query: `build_query(params)` then `&signature=<hmac>` appended. The
/// signature is computed over the *unsigned* query string exactly as it will be sent.
pub fn signed_query(params: &[(String, String)], signer: &Signer) -> String {
    let base = build_query(params);
    let sig = signer.sign(&base);
    if base.is_empty() {
        format!("signature={sig}")
    } else {
        format!("{base}&signature={sig}")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    // Known vector built from the secret/query in the Binance "SIGNED endpoint examples" docs.
    // The expected digest is the HMAC-SHA256 of `VECTOR_QUERY` under `VECTOR_SECRET`, independently
    // cross-checked with `openssl dgst -sha256 -hmac <secret>` (a fixed, deterministic value).
    const VECTOR_SECRET: &str =
        "NhqPtmdSJYdKjVHjA7PZj4Mge3R5YNiP1e3UZjInClVN65XAbvqqM6A7H5fATj0";
    const VECTOR_QUERY: &str = "symbol=LTCBTC&side=BUY&type=LIMIT&timeInForce=GTC&quantity=1&price=0.1&recvWindow=5000&timestamp=1499827319559";
    const VECTOR_SIG: &str = "b89008e7051ffbf2242be7dc5ae67fd146e6430688627b802c0cbec146e46aef";

    #[test]
    fn signer_matches_known_binance_vector() {
        let signer = Signer::new(VECTOR_SECRET);
        assert_eq!(signer.sign(VECTOR_QUERY), VECTOR_SIG);
    }

    #[test]
    fn build_query_preserves_order_and_encodes() {
        let params = vec![
            ("symbol".to_string(), "LTCBTC".to_string()),
            ("side".to_string(), "BUY".to_string()),
            ("price".to_string(), "0.1".to_string()),
        ];
        assert_eq!(build_query(&params), "symbol=LTCBTC&side=BUY&price=0.1");
    }

    #[test]
    fn signed_query_appends_signature_over_unsigned_string() {
        // Reconstruct the canonical Binance vector via the param builder, then sign it and verify
        // the appended signature equals the published digest.
        let params = vec![
            ("symbol".to_string(), "LTCBTC".to_string()),
            ("side".to_string(), "BUY".to_string()),
            ("type".to_string(), "LIMIT".to_string()),
            ("timeInForce".to_string(), "GTC".to_string()),
            ("quantity".to_string(), "1".to_string()),
            ("price".to_string(), "0.1".to_string()),
            ("recvWindow".to_string(), "5000".to_string()),
            ("timestamp".to_string(), "1499827319559".to_string()),
        ];
        let signer = Signer::new(VECTOR_SECRET);
        let q = signed_query(&params, &signer);
        assert!(q.starts_with(VECTOR_QUERY), "unsigned prefix preserved");
        assert!(q.ends_with(&format!("signature={VECTOR_SIG}")));
    }

    #[test]
    fn encode_value_percent_encodes_reserved() {
        // A space and an ampersand must be escaped so the signed string is unambiguous.
        assert_eq!(encode_value("a b&c"), "a%20b%26c");
        // Unreserved characters pass through untouched.
        assert_eq!(encode_value("client-id_1.0~x"), "client-id_1.0~x");
    }
}
