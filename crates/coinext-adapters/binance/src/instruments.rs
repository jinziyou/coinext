//! [`BinanceInstrumentProvider`] — `impl coinext_ports::InstrumentProvider`.
//!
//! Maps Binance `exchangeInfo` symbology into the shared `coinext_model::Instrument` model, enforcing
//! the venue's tick/lot/min-notional filters as the domain's price/size increments. This is the
//! per-venue symbology seam (architecture §3): the rest of the system only ever sees the normalized
//! `Instrument`, never Binance's wire format.
//!
//! The wire-parsing is a PURE function ([`parse_exchange_info`]) so it is fixture-tested without the
//! network; `load_all`/`load` are the thin async REST wrappers around it.

use async_trait::async_trait;
use coinext_core::{Currency, Money, Price, Quantity};
use coinext_model::{CurrencyPair, Instrument, InstrumentId, Symbol, Venue};
use coinext_network::{
    Credentials, NetError, RateLimiter, RestClient, RestConfig, RestRequest,
};
use coinext_ports::{InstrumentProvider, PortError, PortResult};
use rust_decimal::Decimal;
use std::collections::HashMap;
use std::str::FromStr;
use std::sync::{Arc, RwLock};

use crate::config::BinanceConfig;

/// Default maker/taker fees applied when account-specific commission rates are unavailable. Binance
/// spot publishes 0.10% / 0.10% as the standard tier; analytics/backtest can override per-account.
const DEFAULT_MAKER_FEE: &str = "0.001";
const DEFAULT_TAKER_FEE: &str = "0.001";

/// Loads + caches Binance instruments. After `load_all`, `find` is a cheap synchronous lookup the
/// engines use on the hot path.
pub struct BinanceInstrumentProvider {
    config: BinanceConfig,
    rest: RestClient,
    cache: RwLock<HashMap<InstrumentId, Arc<dyn Instrument>>>,
}

impl BinanceInstrumentProvider {
    pub fn new(config: BinanceConfig) -> PortResult<Self> {
        let rest = RestClient::new(
            RestConfig {
                base_url: config.rest_base().to_string(),
                ..Default::default()
            },
            RateLimiter::per_minute(1200),
            Credentials::default(),
        )
        .map_err(net_to_port)?;
        Ok(BinanceInstrumentProvider {
            config,
            rest,
            cache: RwLock::new(HashMap::new()),
        })
    }
}

#[async_trait]
impl InstrumentProvider for BinanceInstrumentProvider {
    async fn load_all(&self) -> PortResult<Vec<Arc<dyn Instrument>>> {
        let resp = self
            .rest
            .send(RestRequest::get("/api/v3/exchangeInfo", 20))
            .await
            .map_err(net_to_port)?;
        let instruments = parse_exchange_info(&resp.body)
            .map_err(|e| PortError::Io(format!("exchangeInfo parse: {e}")))?;
        let mut cache = self.cache.write().expect("cache lock");
        for inst in &instruments {
            cache.insert(inst.id(), inst.clone());
        }
        Ok(instruments)
    }

    async fn load(&self, id: &InstrumentId) -> PortResult<Arc<dyn Instrument>> {
        if let Some(found) = self.find(id) {
            return Ok(found);
        }
        // Fetch just this symbol; reuse the same pure parser.
        let resp = self
            .rest
            .send(RestRequest::get("/api/v3/exchangeInfo", 20).with_param("symbol", id.symbol.as_str()))
            .await
            .map_err(net_to_port)?;
        let instruments = parse_exchange_info(&resp.body)
            .map_err(|e| PortError::Io(format!("exchangeInfo parse: {e}")))?;
        let mut cache = self.cache.write().expect("cache lock");
        for inst in &instruments {
            cache.insert(inst.id(), inst.clone());
        }
        cache
            .get(id)
            .cloned()
            .ok_or_else(|| PortError::Io(format!("symbol {id} not found in exchangeInfo")))
    }

    fn find(&self, id: &InstrumentId) -> Option<Arc<dyn Instrument>> {
        self.cache.read().expect("cache lock").get(id).cloned()
    }
}

/// Map a `coinext-network` transport error into a `coinext-ports` error at the adapter boundary.
fn net_to_port(e: NetError) -> PortError {
    match e {
        NetError::Http { status, body } => PortError::Io(format!("http {status}: {body}")),
        other => PortError::Io(other.to_string()),
    }
}

/// PURE: parse a Binance `GET /api/v3/exchangeInfo` JSON body into normalized spot instruments.
///
/// For each `TRADING` symbol it derives:
///   - `price_precision` / `price_increment` from the `PRICE_FILTER` `tickSize`,
///   - `size_precision` / `size_increment`  from the `LOT_SIZE` `stepSize`,
///   - `min_notional` from `NOTIONAL.minNotional` (or legacy `MIN_NOTIONAL.minNotional`),
///   - base/quote currencies from `baseAsset`/`quoteAsset` (precision taken from the filter scales).
///
/// Non-`TRADING` symbols are skipped. Returns an error only on structurally invalid JSON.
pub fn parse_exchange_info(json: &str) -> Result<Vec<Arc<dyn Instrument>>, String> {
    let root: serde_json::Value =
        serde_json::from_str(json).map_err(|e| format!("invalid json: {e}"))?;
    let symbols = root
        .get("symbols")
        .and_then(|s| s.as_array())
        .ok_or_else(|| "missing `symbols` array".to_string())?;

    let venue = Venue::from("BINANCE");
    let maker_fee = Decimal::from_str(DEFAULT_MAKER_FEE).unwrap();
    let taker_fee = Decimal::from_str(DEFAULT_TAKER_FEE).unwrap();

    let mut out: Vec<Arc<dyn Instrument>> = Vec::with_capacity(symbols.len());
    for sym in symbols {
        let status = sym.get("status").and_then(|v| v.as_str()).unwrap_or("");
        if status != "TRADING" {
            continue;
        }
        let symbol = match sym.get("symbol").and_then(|v| v.as_str()) {
            Some(s) => s,
            None => continue,
        };
        let base_asset = sym.get("baseAsset").and_then(|v| v.as_str()).unwrap_or("");
        let quote_asset = sym.get("quoteAsset").and_then(|v| v.as_str()).unwrap_or("");
        if base_asset.is_empty() || quote_asset.is_empty() {
            continue;
        }

        let filters = sym
            .get("filters")
            .and_then(|f| f.as_array())
            .ok_or_else(|| format!("symbol {symbol}: missing filters"))?;

        let tick_size = filter_field(filters, "PRICE_FILTER", "tickSize")
            .ok_or_else(|| format!("symbol {symbol}: missing PRICE_FILTER.tickSize"))?;
        let step_size = filter_field(filters, "LOT_SIZE", "stepSize")
            .ok_or_else(|| format!("symbol {symbol}: missing LOT_SIZE.stepSize"))?;
        // NOTIONAL is the current filter; MIN_NOTIONAL is the legacy name.
        let min_notional_str = filter_field(filters, "NOTIONAL", "minNotional")
            .or_else(|| filter_field(filters, "MIN_NOTIONAL", "minNotional"));

        let price_precision = decimal_scale(tick_size)?;
        let size_precision = decimal_scale(step_size)?;

        let tick_dec = Decimal::from_str(tick_size)
            .map_err(|e| format!("symbol {symbol}: bad tickSize {tick_size}: {e}"))?;
        let step_dec = Decimal::from_str(step_size)
            .map_err(|e| format!("symbol {symbol}: bad stepSize {step_size}: {e}"))?;

        let price_increment = Price::from_decimal(tick_dec, price_precision)
            .map_err(|e| format!("symbol {symbol}: tickSize -> Price: {e}"))?;
        let size_increment = Quantity::from_decimal(step_dec, size_precision)
            .map_err(|e| format!("symbol {symbol}: stepSize -> Quantity: {e}"))?;

        let quote_ccy = Currency::new(quote_asset, price_precision)
            .map_err(|e| format!("symbol {symbol}: quote currency: {e}"))?;
        let base_ccy = Currency::new(base_asset, size_precision)
            .map_err(|e| format!("symbol {symbol}: base currency: {e}"))?;

        let min_notional = match min_notional_str {
            Some(mn) => {
                let mn_dec = Decimal::from_str(mn)
                    .map_err(|e| format!("symbol {symbol}: bad minNotional {mn}: {e}"))?;
                if mn_dec.is_zero() {
                    None
                } else {
                    Some(
                        Money::from_decimal(mn_dec, quote_ccy)
                            .map_err(|e| format!("symbol {symbol}: minNotional -> Money: {e}"))?,
                    )
                }
            }
            None => None,
        };

        let id = InstrumentId::new(Symbol::from(symbol), venue.clone());
        let pair = CurrencyPair {
            id,
            base: base_ccy,
            quote: quote_ccy,
            price_precision,
            size_precision,
            price_increment,
            size_increment,
            min_notional,
            maker_fee,
            taker_fee,
        };
        out.push(Arc::new(pair) as Arc<dyn Instrument>);
    }
    Ok(out)
}

/// Find a named field in a specific filter type within a symbol's `filters` array.
fn filter_field<'a>(
    filters: &'a [serde_json::Value],
    filter_type: &str,
    field: &str,
) -> Option<&'a str> {
    filters
        .iter()
        .find(|f| f.get("filterType").and_then(|v| v.as_str()) == Some(filter_type))
        .and_then(|f| f.get(field))
        .and_then(|v| v.as_str())
}

/// The number of fractional digits in a Binance decimal string like `"0.00010000"` -> 4, after
/// trimming trailing zeros (Binance pads increments to 8 dp, but the meaningful precision is the
/// position of the lowest non-zero digit, e.g. tickSize `0.01000000` is precision 2).
fn decimal_scale(s: &str) -> Result<u8, String> {
    let dec = Decimal::from_str(s).map_err(|e| format!("bad decimal {s}: {e}"))?;
    let normalized = dec.normalize();
    Ok(normalized.scale().min(u8::MAX as u32) as u8)
}

#[cfg(test)]
mod tests {
    use super::*;
    use rust_decimal_macros::dec;

    // A minimal but realistic exchangeInfo fixture: two TRADING symbols + one HALTED (skipped).
    const FIXTURE: &str = r#"
    {
      "timezone": "UTC",
      "serverTime": 1700000000000,
      "symbols": [
        {
          "symbol": "BTCUSDT",
          "status": "TRADING",
          "baseAsset": "BTC",
          "baseAssetPrecision": 8,
          "quoteAsset": "USDT",
          "quotePrecision": 8,
          "filters": [
            {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "1000000.00", "tickSize": "0.01000000"},
            {"filterType": "LOT_SIZE", "minQty": "0.00001000", "maxQty": "9000.00000000", "stepSize": "0.00001000"},
            {"filterType": "NOTIONAL", "minNotional": "5.00000000", "maxNotional": "9000000.00000000"}
          ]
        },
        {
          "symbol": "ETHUSDT",
          "status": "TRADING",
          "baseAsset": "ETH",
          "quoteAsset": "USDT",
          "filters": [
            {"filterType": "PRICE_FILTER", "tickSize": "0.10000000"},
            {"filterType": "LOT_SIZE", "stepSize": "0.00010000"},
            {"filterType": "MIN_NOTIONAL", "minNotional": "10.00000000"}
          ]
        },
        {
          "symbol": "DEADUSDT",
          "status": "HALT",
          "baseAsset": "DEAD",
          "quoteAsset": "USDT",
          "filters": []
        }
      ]
    }
    "#;

    #[test]
    fn parses_trading_symbols_and_skips_non_trading() {
        let instruments = parse_exchange_info(FIXTURE).unwrap();
        // The HALT symbol is skipped.
        assert_eq!(instruments.len(), 2);
        let ids: Vec<String> = instruments.iter().map(|i| i.id().to_string()).collect();
        assert!(ids.contains(&"BTCUSDT.BINANCE".to_string()));
        assert!(ids.contains(&"ETHUSDT.BINANCE".to_string()));
        assert!(!ids.iter().any(|s| s.starts_with("DEAD")));
    }

    #[test]
    fn derives_precision_and_increments_from_filters() {
        let instruments = parse_exchange_info(FIXTURE).unwrap();
        let btc = instruments
            .iter()
            .find(|i| i.id().symbol.as_str() == "BTCUSDT")
            .unwrap();
        // tickSize 0.01 -> price precision 2; stepSize 0.00001 -> size precision 5.
        assert_eq!(btc.price_precision(), 2);
        assert_eq!(btc.size_precision(), 5);
        assert_eq!(btc.price_increment().as_decimal(), dec!(0.01));
        assert_eq!(btc.size_increment().as_decimal(), dec!(0.00001));
        // NOTIONAL.minNotional 5 USDT.
        let mn = btc.min_notional().unwrap();
        assert_eq!(mn.amount(), dec!(5.00));
        assert_eq!(mn.currency().code(), "USDT");
        // Spot economics.
        assert_eq!(btc.base_currency().code(), "BTC");
        assert_eq!(btc.quote_currency().code(), "USDT");
        assert!(!btc.is_inverse());
    }

    #[test]
    fn legacy_min_notional_filter_is_honored() {
        let instruments = parse_exchange_info(FIXTURE).unwrap();
        let eth = instruments
            .iter()
            .find(|i| i.id().symbol.as_str() == "ETHUSDT")
            .unwrap();
        assert_eq!(eth.price_precision(), 1); // tickSize 0.1
        assert_eq!(eth.size_precision(), 4); // stepSize 0.0001
        assert_eq!(eth.min_notional().unwrap().amount(), dec!(10.0));
    }

    #[test]
    fn invalid_json_errors() {
        assert!(parse_exchange_info("not json").is_err());
        assert!(parse_exchange_info("{}").is_err());
    }
}
