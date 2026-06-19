//! [`BinanceExecutionClient`] — `impl coinext_ports::ExecutionClient`, the live side of THE parity seam.
//!
//! Submits/cancels orders over signed REST and ingests acks/fills from the user-data WebSocket
//! stream, normalizing them into `coinext_ports::ExecutionReport`s delivered over the `mpsc` channel
//! taken once via [`take_reports`](coinext_ports::ExecutionClient::take_reports). The OMS/Risk above is
//! byte-for-byte identical to backtest (`coinext-sim`'s `SimulatedExecutionClient`).
//!
//! ## Idempotent submit (architecture §5)
//! The deterministic `ClientOrderId` (single-owner: the OrderFactory) is passed straight through as
//! Binance `newClientOrderId`, so a retried submit is a venue no-op (duplicate id rejected) and
//! `reconcile()` can diff venue truth against the local event log by `ClientOrderId`.
//!
//! ## Pure builders / parsers
//! [`build_order_params`] turns an `Order` into the signed REST params; [`map_execution_report`]
//! turns a user-stream `executionReport` event into an `ExecutionReport`. Both are PURE and
//! fixture-tested without the network.

use async_trait::async_trait;
use coinext_core::{Currency, Money, Price, Quantity, UnixNanos};
use coinext_model::{
    ClientOrderId, Fill, InstrumentId, LiquiditySide, Order, OrderSide, OrderType, Symbol,
    TimeInForce, TradeId, Venue, VenueOrderId,
};
use coinext_network::{
    Credentials, HttpMethod, NetError, RateLimiter, RestClient, RestConfig, RestRequest, WsClient,
    WsConfig, WsMessage,
};
use coinext_ports::{
    CancelOrder, ExecutionClient, ExecutionReport, ModifyOrder, PortError, PortResult, SubmitOrder,
};
use rust_decimal::Decimal;
use std::str::FromStr;
use tokio::sync::mpsc;

use crate::config::BinanceConfig;

/// Precision used when normalizing user-stream fills before an `Instrument` is known. The wire
/// strings carry full precision (Binance pads to the symbol's precision); 8 dp loses nothing.
const WIRE_PRECISION: u8 = 8;

/// Binance execution client. Order commands go out over signed REST; reports come back over the
/// user-data WS (fast) and are pushed onto `tx`; the core takes `rx` once.
pub struct BinanceExecutionClient {
    config: BinanceConfig,
    rest: RestClient,
    /// Outbound side of the report seam; the user-stream task holds a clone.
    tx: mpsc::Sender<ExecutionReport>,
    /// Inbound side handed to the core exactly once at wiring (`take_reports`).
    rx: Option<mpsc::Receiver<ExecutionReport>>,
    /// The user-data stream listenKey (set on connect; refreshed by a keepalive).
    listen_key: Option<String>,
    /// The running user-data WS task.
    ws: Option<WsClient>,
}

impl BinanceExecutionClient {
    pub fn new(config: BinanceConfig) -> PortResult<Self> {
        let (tx, rx) = mpsc::channel(2048);
        let creds = match (&config.api_key, &config.api_secret) {
            (Some(k), Some(s)) => Credentials::new(k.clone(), s.clone()),
            _ => Credentials::default(),
        };
        let rest = RestClient::new(
            RestConfig {
                base_url: config.rest_base().to_string(),
                ..Default::default()
            },
            // The ORDER pool (50 / 10s) gates order flow; weight pool gates queries. We use the
            // weight pool here and charge order endpoints a representative weight.
            RateLimiter::per_minute(1200),
            creds,
        )
        .map_err(net_to_port)?;
        Ok(BinanceExecutionClient {
            config,
            rest,
            tx,
            rx: Some(rx),
            listen_key: None,
            ws: None,
        })
    }
}

#[async_trait]
impl ExecutionClient for BinanceExecutionClient {
    fn venue(&self) -> Venue {
        crate::venue()
    }

    async fn connect(&mut self) -> PortResult<()> {
        if !self.config.has_credentials() {
            return Err(PortError::Unsupported(
                "BinanceExecutionClient::connect requires api credentials".into(),
            ));
        }
        // Create a listenKey (POST /api/v3/userDataStream — api-key-only, not signed). The api key
        // header is attached because `signed: false` keeps the key path but skips the HMAC; for
        // userDataStream the venue only requires the key header, so we add it via a no-signature
        // request whose key header is supplied by the RestClient credentials.
        let key_resp = self
            .rest
            .send(RestRequest {
                method: HttpMethod::Post,
                path: "/api/v3/userDataStream".into(),
                query: Vec::new(),
                body: None,
                signed: false,
                weight: 2,
            })
            .await
            .map_err(net_to_port)?;
        let listen_key = parse_listen_key(&key_resp.body)
            .map_err(|e| PortError::Io(format!("listenKey: {e}")))?;

        // Open the user-data WS (`<ws_stream_base>/<listenKey>`) and spawn the normalize pump.
        let url = format!("{}/{listen_key}", self.config.ws_stream_base());
        let mut ws = WsClient::new(WsConfig {
            url,
            ..Default::default()
        });
        let mut rx = ws.connect();
        let tx = self.tx.clone();
        tokio::spawn(async move {
            while let Some(msg) = rx.recv().await {
                match msg {
                    WsMessage::Text(text) => {
                        if let Ok(report) = map_execution_report(&text) {
                            if tx.send(report).await.is_err() {
                                return;
                            }
                        }
                    }
                    WsMessage::Reconnected => {}
                }
            }
        });

        self.listen_key = Some(listen_key);
        self.ws = Some(ws);
        Ok(())
    }

    async fn submit_order(&self, cmd: SubmitOrder) -> PortResult<()> {
        let params = build_order_params(&cmd.order)
            .map_err(|e| PortError::Rejected(format!("build order params: {e}")))?;
        let mut req = RestRequest::signed(HttpMethod::Post, "/api/v3/order", 1);
        req.query = params;
        self.rest.send(req).await.map_err(net_to_port)?;
        Ok(())
    }

    async fn cancel_order(&self, cmd: CancelOrder) -> PortResult<()> {
        // Cancel by the deterministic client id (`origClientOrderId`). NOTE: Binance spot's
        // `DELETE /api/v3/order` also requires a `symbol`; the `CancelOrder` port command only
        // carries the `ClientOrderId` today, so a future port revision must thread the symbol
        // through (tracked as a TODO). We send what the port provides.
        let mut req = RestRequest::signed(HttpMethod::Delete, "/api/v3/order", 1);
        req.query = vec![(
            "origClientOrderId".to_string(),
            cmd.client_order_id.as_str().to_string(),
        )];
        self.rest.send(req).await.map_err(net_to_port)?;
        Ok(())
    }

    async fn modify_order(&self, _cmd: ModifyOrder) -> PortResult<()> {
        // Binance spot has NO native amend. The modify path is cancel-replace, which the local FSM
        // models as PendingUpdate -> Updated. Implementing cancel-replace safely (atomic, no double
        // exposure) is a TODO; until then this is explicitly unsupported so callers fail fast rather
        // than silently no-op.
        Err(PortError::Unsupported(
            "Binance spot has no order modify; use cancel-replace (TODO)".into(),
        ))
    }

    async fn reconcile(&self) -> PortResult<Vec<ExecutionReport>> {
        // GET /api/v3/openOrders -> a list of resting orders, mapped to `Accepted` reports the OMS
        // folds against the local event log by ClientOrderId.
        let req = RestRequest::signed(HttpMethod::Get, "/api/v3/openOrders", 40);
        let resp = self.rest.send(req).await.map_err(net_to_port)?;
        let rows: Vec<serde_json::Value> = resp.json().map_err(net_to_port)?;
        let mut reports = Vec::with_capacity(rows.len());
        for row in &rows {
            if let Some(r) = map_open_order(row) {
                reports.push(r);
            }
        }
        Ok(reports)
    }

    fn take_reports(&mut self) -> mpsc::Receiver<ExecutionReport> {
        self.rx
            .take()
            .expect("BinanceExecutionClient::take_reports called more than once")
    }

    async fn disconnect(&mut self) -> PortResult<()> {
        if let Some(mut ws) = self.ws.take() {
            ws.shutdown().await;
        }
        // Best-effort listenKey close (DELETE /api/v3/userDataStream); ignore errors on shutdown.
        if let Some(key) = self.listen_key.take() {
            let req = RestRequest {
                method: HttpMethod::Delete,
                path: "/api/v3/userDataStream".into(),
                query: vec![("listenKey".to_string(), key)],
                body: None,
                signed: false,
                weight: 2,
            };
            let _ = self.rest.send(req).await;
        }
        Ok(())
    }
}

/// Map a `coinext-network` transport error into a `coinext-ports` error at the adapter boundary. A 4xx with a
/// duplicate-client-id body is the idempotent-retry case — surfaced as `Rejected` so the OMS can
/// recognize it without treating it as a fatal transport error.
fn net_to_port(e: NetError) -> PortError {
    match e {
        NetError::Http { status, body } => {
            if (400..500).contains(&status) {
                PortError::Rejected(format!("http {status}: {body}"))
            } else {
                PortError::Io(format!("http {status}: {body}"))
            }
        }
        other => PortError::Io(other.to_string()),
    }
}

/// PURE: build the signed REST params for `POST /api/v3/order` from an `Order`. The deterministic
/// `client_order_id` becomes `newClientOrderId` (idempotency, §5). Market orders carry no price /
/// timeInForce; limit orders carry both.
pub fn build_order_params(order: &Order) -> Result<Vec<(String, String)>, String> {
    let symbol = order.instrument_id.symbol.as_str().to_string();
    let side = match order.side {
        OrderSide::Buy => "BUY",
        OrderSide::Sell => "SELL",
    };
    let order_type = match order.order_type {
        OrderType::Market => "MARKET",
        OrderType::Limit => "LIMIT",
        OrderType::StopLimit => "STOP_LOSS_LIMIT",
        OrderType::StopMarket => "STOP_LOSS",
        OrderType::MarketIfTouched => "TAKE_PROFIT",
        OrderType::TrailingStopMarket => {
            return Err("trailing stop is unsupported on Binance spot".into())
        }
    };

    let mut params: Vec<(String, String)> = vec![
        ("symbol".into(), symbol),
        ("side".into(), side.into()),
        ("type".into(), order_type.into()),
        ("quantity".into(), trim_decimal(order.quantity.as_decimal())),
        (
            "newClientOrderId".into(),
            order.client_order_id.as_str().to_string(),
        ),
    ];

    // LIMIT-family orders require a price + timeInForce.
    let is_limit = matches!(order.order_type, OrderType::Limit | OrderType::StopLimit);
    if is_limit {
        let price = order
            .price
            .ok_or_else(|| "limit order missing price".to_string())?;
        params.push(("price".into(), trim_decimal(price.as_decimal())));
        params.push(("timeInForce".into(), tif_to_str(order.tif).into()));
    }
    // Stop orders carry a stopPrice trigger.
    if matches!(
        order.order_type,
        OrderType::StopLimit | OrderType::StopMarket | OrderType::MarketIfTouched
    ) {
        if let Some(trigger) = order.trigger {
            params.push(("stopPrice".into(), trim_decimal(trigger.as_decimal())));
        }
    }
    Ok(params)
}

/// Map a `TimeInForce` into the Binance enum string. (Day/Gtd are not supported on spot; map to GTC
/// as the safe default.)
fn tif_to_str(tif: TimeInForce) -> &'static str {
    match tif {
        TimeInForce::Gtc | TimeInForce::Gtd | TimeInForce::Day => "GTC",
        TimeInForce::Ioc => "IOC",
        TimeInForce::Fok => "FOK",
    }
}

/// Render a decimal as a trimmed string (no trailing zeros, no scientific notation) for the wire.
fn trim_decimal(d: Decimal) -> String {
    d.normalize().to_string()
}

/// PURE: parse the listenKey from `POST /api/v3/userDataStream`'s body `{"listenKey":"..."}`.
pub fn parse_listen_key(body: &str) -> Result<String, String> {
    let v: serde_json::Value =
        serde_json::from_str(body).map_err(|e| format!("invalid json: {e}"))?;
    v.get("listenKey")
        .and_then(|k| k.as_str())
        .map(|s| s.to_string())
        .ok_or_else(|| "missing listenKey".to_string())
}

/// PURE: map a user-data-stream `executionReport` event into an `ExecutionReport`.
///
/// Binance's `executionReport` carries `x` (current execution type: NEW / TRADE / CANCELED /
/// REJECTED / EXPIRED) and `X` (current order status). We map `x` to the report variant; for a
/// TRADE we build a `Fill` from the `l`/`L`/`n`/`t` last-trade fields.
pub fn map_execution_report(text: &str) -> Result<ExecutionReport, String> {
    let v: serde_json::Value =
        serde_json::from_str(text).map_err(|e| format!("invalid json: {e}"))?;
    let event_type = v.get("e").and_then(|x| x.as_str()).unwrap_or("");
    if event_type != "executionReport" {
        return Err(format!("not an executionReport event: `{event_type}`"));
    }
    let exec_type = v
        .get("x")
        .and_then(|x| x.as_str())
        .ok_or_else(|| "missing execution type `x`".to_string())?;
    let client_id = ClientOrderId::from(
        v.get("c")
            .and_then(|x| x.as_str())
            .ok_or_else(|| "missing client order id `c`".to_string())?,
    );

    match exec_type {
        "NEW" => {
            let venue_id = v
                .get("i")
                .and_then(|x| x.as_u64())
                .map(|i| VenueOrderId::from(i.to_string()))
                .ok_or_else(|| "NEW missing order id `i`".to_string())?;
            Ok(ExecutionReport::Accepted {
                client_order_id: client_id,
                venue_order_id: venue_id,
            })
        }
        "TRADE" => {
            let fill = build_fill(&v, client_id)?;
            Ok(ExecutionReport::Fill(fill))
        }
        "CANCELED" => Ok(ExecutionReport::Canceled {
            client_order_id: client_id,
        }),
        "REJECTED" => Ok(ExecutionReport::Rejected {
            client_order_id: client_id,
            reason: v
                .get("r")
                .and_then(|x| x.as_str())
                .unwrap_or("NONE")
                .to_string(),
        }),
        "EXPIRED" => Ok(ExecutionReport::Expired {
            client_order_id: client_id,
        }),
        other => Err(format!("unhandled execution type `{other}`")),
    }
}

/// Build a `Fill` from an `executionReport` TRADE event's last-trade fields.
fn build_fill(v: &serde_json::Value, client_id: ClientOrderId) -> Result<Fill, String> {
    let symbol = v
        .get("s")
        .and_then(|x| x.as_str())
        .ok_or_else(|| "missing symbol `s`".to_string())?;
    let venue_order_id = v
        .get("i")
        .and_then(|x| x.as_u64())
        .map(|i| VenueOrderId::from(i.to_string()))
        .ok_or_else(|| "missing order id `i`".to_string())?;
    let trade_id = v
        .get("t")
        .and_then(|x| x.as_u64())
        .map(|t| TradeId::from(t.to_string()))
        .ok_or_else(|| "missing trade id `t`".to_string())?;
    let side = match v.get("S").and_then(|x| x.as_str()) {
        Some("BUY") => OrderSide::Buy,
        Some("SELL") => OrderSide::Sell,
        other => return Err(format!("bad side `{other:?}`")),
    };
    let last_px = parse_dec(field_str(v, "L")?)
        .and_then(|d| Price::from_decimal(d, WIRE_PRECISION).map_err(|e| e.to_string()))?;
    let last_qty = parse_dec(field_str(v, "l")?)
        .and_then(|d| Quantity::from_decimal(d, WIRE_PRECISION).map_err(|e| e.to_string()))?;
    // Commission `n` in asset `N`; default to a zero USDT fee if absent.
    let fee_amount = parse_dec(v.get("n").and_then(|x| x.as_str()).unwrap_or("0"))?;
    let fee_ccy_code = v.get("N").and_then(|x| x.as_str()).unwrap_or("USDT");
    let fee_ccy = Currency::new(fee_ccy_code, WIRE_PRECISION)
        .map_err(|e| format!("fee currency {fee_ccy_code}: {e}"))?;
    let fee = Money::from_decimal(fee_amount, fee_ccy).map_err(|e| e.to_string())?;
    // Liquidity flag `m` (maker?).
    let is_maker = v.get("m").and_then(|x| x.as_bool()).unwrap_or(false);
    let liquidity = if is_maker {
        LiquiditySide::Maker
    } else {
        LiquiditySide::Taker
    };
    let ts_event = UnixNanos(
        v.get("T")
            .and_then(|x| x.as_u64())
            .unwrap_or(0)
            .saturating_mul(1_000_000),
    );
    Ok(Fill {
        trade_id,
        client_order_id: client_id,
        venue_order_id,
        instrument_id: InstrumentId::new(Symbol::from(symbol), Venue::from("BINANCE")),
        side,
        last_px,
        last_qty,
        fee,
        liquidity,
        ts_event,
        ts_init: ts_event,
    })
}

/// Map a `GET /api/v3/openOrders` row to an `Accepted` report (a resting order's venue truth).
fn map_open_order(row: &serde_json::Value) -> Option<ExecutionReport> {
    let client_id = ClientOrderId::from(row.get("clientOrderId").and_then(|v| v.as_str())?);
    let venue_id = VenueOrderId::from(row.get("orderId").and_then(|v| v.as_u64())?.to_string());
    Some(ExecutionReport::Accepted {
        client_order_id: client_id,
        venue_order_id: venue_id,
    })
}

fn field_str<'a>(v: &'a serde_json::Value, key: &str) -> Result<&'a str, String> {
    v.get(key)
        .and_then(|x| x.as_str())
        .ok_or_else(|| format!("missing field `{key}`"))
}

fn parse_dec(s: &str) -> Result<Decimal, String> {
    Decimal::from_str(s).map_err(|e| format!("bad decimal {s}: {e}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use coinext_model::{OrderFlags, StrategyId};
    use rust_decimal_macros::dec;

    fn iid() -> InstrumentId {
        InstrumentId::parse("BTCUSDT.BINANCE").unwrap()
    }

    fn limit_order() -> Order {
        Order::new(
            StrategyId::from("s1"),
            ClientOrderId::from("s1-00000000000000000042"),
            iid(),
            OrderSide::Buy,
            OrderType::Limit,
            Quantity::from_decimal(dec!(0.5), 5).unwrap(),
            Some(Price::from_decimal(dec!(50000.10), 2).unwrap()),
            None,
            TimeInForce::Gtc,
            OrderFlags::default(),
            UnixNanos(0),
        )
    }

    #[test]
    fn build_params_for_limit_carries_price_tif_and_idempotent_client_id() {
        let params = build_order_params(&limit_order()).unwrap();
        let get = |k: &str| params.iter().find(|(kk, _)| kk == k).map(|(_, v)| v.clone());
        assert_eq!(get("symbol").as_deref(), Some("BTCUSDT"));
        assert_eq!(get("side").as_deref(), Some("BUY"));
        assert_eq!(get("type").as_deref(), Some("LIMIT"));
        assert_eq!(get("quantity").as_deref(), Some("0.5"));
        assert_eq!(get("price").as_deref(), Some("50000.1"));
        assert_eq!(get("timeInForce").as_deref(), Some("GTC"));
        // The deterministic client id flows straight through as newClientOrderId (idempotency §5).
        assert_eq!(
            get("newClientOrderId").as_deref(),
            Some("s1-00000000000000000042")
        );
    }

    #[test]
    fn build_params_for_market_omits_price_and_tif() {
        let mut o = limit_order();
        o.order_type = OrderType::Market;
        o.price = None;
        o.tif = TimeInForce::Ioc;
        let params = build_order_params(&o).unwrap();
        assert!(params.iter().all(|(k, _)| k != "price"));
        assert!(params.iter().all(|(k, _)| k != "timeInForce"));
        assert_eq!(
            params.iter().find(|(k, _)| k == "type").map(|(_, v)| v.as_str()),
            Some("MARKET")
        );
    }

    #[test]
    fn map_new_execution_report_to_accepted() {
        let ev = r#"{"e":"executionReport","s":"BTCUSDT","c":"s1-00000000000000000042",
            "S":"BUY","o":"LIMIT","x":"NEW","X":"NEW","i":28457}"#;
        let report = map_execution_report(ev).unwrap();
        match report {
            ExecutionReport::Accepted {
                client_order_id,
                venue_order_id,
            } => {
                assert_eq!(client_order_id.as_str(), "s1-00000000000000000042");
                assert_eq!(venue_order_id.as_str(), "28457");
            }
            other => panic!("expected Accepted, got {other:?}"),
        }
    }

    #[test]
    fn map_trade_execution_report_to_fill() {
        // A TRADE executionReport with last-trade fields.
        let ev = r#"{"e":"executionReport","s":"BTCUSDT","c":"s1-00000000000000000042",
            "S":"BUY","x":"TRADE","X":"PARTIALLY_FILLED","i":28457,"t":98765,
            "l":"0.20000000","L":"50000.50000000","n":"0.00010000","N":"BNB","m":false,
            "T":1700000000123}"#;
        let report = map_execution_report(ev).unwrap();
        match report {
            ExecutionReport::Fill(fill) => {
                assert_eq!(fill.client_order_id.as_str(), "s1-00000000000000000042");
                assert_eq!(fill.venue_order_id.as_str(), "28457");
                assert_eq!(fill.trade_id.as_str(), "98765");
                assert_eq!(fill.side, OrderSide::Buy);
                assert_eq!(fill.last_px.as_decimal(), dec!(50000.50));
                assert_eq!(fill.last_qty.as_decimal(), dec!(0.2));
                assert_eq!(fill.fee.amount(), dec!(0.0001));
                assert_eq!(fill.fee.currency().code(), "BNB");
                assert_eq!(fill.liquidity, LiquiditySide::Taker);
                assert_eq!(fill.ts_event, UnixNanos(1_700_000_000_123 * 1_000_000));
            }
            other => panic!("expected Fill, got {other:?}"),
        }
    }

    #[test]
    fn map_canceled_rejected_expired() {
        let canceled = r#"{"e":"executionReport","s":"BTCUSDT","c":"cid","x":"CANCELED","X":"CANCELED","i":1}"#;
        assert!(matches!(
            map_execution_report(canceled).unwrap(),
            ExecutionReport::Canceled { .. }
        ));
        let rejected = r#"{"e":"executionReport","s":"BTCUSDT","c":"cid","x":"REJECTED","X":"REJECTED","r":"INSUFFICIENT_BALANCE","i":1}"#;
        match map_execution_report(rejected).unwrap() {
            ExecutionReport::Rejected { reason, .. } => assert_eq!(reason, "INSUFFICIENT_BALANCE"),
            other => panic!("expected Rejected, got {other:?}"),
        }
        let expired = r#"{"e":"executionReport","s":"BTCUSDT","c":"cid","x":"EXPIRED","X":"EXPIRED","i":1}"#;
        assert!(matches!(
            map_execution_report(expired).unwrap(),
            ExecutionReport::Expired { .. }
        ));
    }

    #[test]
    fn non_execution_report_event_errors() {
        let other = r#"{"e":"outboundAccountPosition","u":123}"#;
        assert!(map_execution_report(other).is_err());
    }

    #[test]
    fn parse_listen_key_extracts_field() {
        assert_eq!(
            parse_listen_key(r#"{"listenKey":"pqia91ma19a5s61cv6a81va65sdf"}"#).unwrap(),
            "pqia91ma19a5s61cv6a81va65sdf"
        );
        assert!(parse_listen_key("{}").is_err());
    }
}
