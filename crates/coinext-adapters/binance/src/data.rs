//! [`BinanceDataClient`] — `impl coinext_ports::DataClient`.
//!
//! Owns the market-data WebSocket and normalizes Binance frames into the venue-agnostic
//! `coinext_model::MarketEvent`s, delivering them to the core over the single-consumer `mpsc` channel
//! taken once at wiring via [`take_stream`](coinext_ports::DataClient::take_stream).
//!
//! Combined-stream subscriptions are `<symbol>@depth@100ms` (book diffs), `<symbol>@trade`
//! (trades), and `<symbol>@bookTicker` (best bid/ask quotes). `request_bars` calls
//! `GET /api/v3/klines` and maps to `coinext_model::Bar`. The wire-parsing is a set of PURE functions
//! ([`normalize_trade`], [`normalize_book_ticker`], [`normalize_depth`], [`kline_to_bar`]) so they
//! are fixture-tested without the network.
//!
//! ## WS depth-diff + resync (see [`crate::book`])
//! On every `WsMessage::Reconnected` the adapter must re-run the snapshot+replay resync; the pure
//! gap state machine lives in `crate::book::LocalOrderBook`.

use async_trait::async_trait;
use coinext_core::{Price, Quantity, UnixNanos};
use coinext_model::{
    Bar, BarAggregation, BarType, BookAction, InstrumentId, MarketEvent, OrderBookDelta, OrderSide,
    QuoteTick, Symbol, TradeId, TradeTick,
};
use coinext_network::{
    now_unix_ms, Credentials, RateLimiter, RestClient, RestConfig, RestRequest, WsClient, WsConfig,
    WsMessage,
};
use coinext_ports::{DataClient, PortError, PortResult, SubKind, Subscription};
use rust_decimal::Decimal;
use std::collections::BTreeSet;
use std::str::FromStr;
use std::sync::Mutex;
use tokio::sync::mpsc;

use crate::config::BinanceConfig;

/// Default price/size precision used when normalizing public WS frames before an `Instrument` is
/// known. Binance spot quotes BTCUSDT at 2 dp price / 5 dp size; callers that need exact
/// instrument precision normalize again downstream. Kept generous (8) so no significant digit is
/// lost from the wire string.
const WIRE_PRECISION: u8 = 8;

/// Binance market-data client. `connect` spawns a WS task that parses combined-stream frames and
/// pushes normalized `MarketEvent`s into `tx`; the core takes `rx` once via `take_stream`.
pub struct BinanceDataClient {
    config: BinanceConfig,
    rest: RestClient,
    /// Outbound side of the core data seam; the WS task holds a clone and pushes normalized events.
    tx: mpsc::Sender<MarketEvent>,
    /// Inbound side handed to the core exactly once at wiring (`take_stream`).
    rx: Option<mpsc::Receiver<MarketEvent>>,
    /// Tracked stream subscriptions (e.g. `btcusdt@trade`), in a stable sorted set.
    streams: Mutex<BTreeSet<String>>,
    /// The running WS task handle (so `disconnect` can stop it).
    ws: Option<WsClient>,
}

impl BinanceDataClient {
    /// Build the client and the internal market-data channel. The WS task is not spawned until
    /// `connect`.
    pub fn new(config: BinanceConfig) -> PortResult<Self> {
        // Bounded channel applies natural backpressure; if the core lags, the WS task blocks rather
        // than growing memory unboundedly.
        let (tx, rx) = mpsc::channel(4096);
        let rest = RestClient::new(
            RestConfig {
                base_url: config.rest_base().to_string(),
                ..Default::default()
            },
            RateLimiter::per_minute(1200),
            Credentials::default(),
        )
        .map_err(crate::net_to_port)?;
        Ok(BinanceDataClient {
            config,
            rest,
            tx,
            rx: Some(rx),
            streams: Mutex::new(BTreeSet::new()),
            ws: None,
        })
    }

    /// The Binance stream names for a subscription (combined-stream form, lower-cased symbol).
    fn stream_names(sub: &Subscription) -> Vec<String> {
        let sym = sub.instrument_id.symbol.as_str().to_lowercase();
        match &sub.kind {
            SubKind::Quotes => vec![format!("{sym}@bookTicker")],
            SubKind::Trades => vec![format!("{sym}@trade")],
            SubKind::BookL2 { .. } => vec![format!("{sym}@depth@100ms")],
            SubKind::Bars(spec) => {
                let interval = bar_aggregation_to_interval(spec.aggregation, spec.step);
                vec![format!("{sym}@kline_{interval}")]
            }
        }
    }
}

#[async_trait]
impl DataClient for BinanceDataClient {
    async fn connect(&mut self) -> PortResult<()> {
        let streams: Vec<String> = self
            .streams
            .lock()
            .expect("streams lock")
            .iter()
            .cloned()
            .collect();
        // The combined-stream endpoint requires at least one stream in the query.
        let url = if streams.is_empty() {
            self.config.ws_market_base().to_string()
        } else {
            format!("{}?streams={}", self.config.ws_market_base(), streams.join("/"))
        };

        let mut ws = WsClient::new(WsConfig {
            url,
            ..Default::default()
        });
        let mut rx = ws.connect();
        let tx = self.tx.clone();

        // Spawn the normalize pump: combined-stream frames are `{"stream":..,"data":{..}}`.
        tokio::spawn(async move {
            while let Some(msg) = rx.recv().await {
                match msg {
                    WsMessage::Text(text) => {
                        if let Some(events) = normalize_combined_frame(&text, now_unix_ms()) {
                            for ev in events {
                                if tx.send(ev).await.is_err() {
                                    return; // core dropped the receiver
                                }
                            }
                        }
                    }
                    // On reconnect the book streams need resync; the data engine refetches a
                    // snapshot. The pure resync state machine is `crate::book::LocalOrderBook`.
                    WsMessage::Reconnected => {}
                }
            }
        });

        self.ws = Some(ws);
        Ok(())
    }

    async fn subscribe(&mut self, sub: Subscription) -> PortResult<()> {
        let mut streams = self.streams.lock().expect("streams lock");
        for name in Self::stream_names(&sub) {
            streams.insert(name);
        }
        Ok(())
    }

    async fn unsubscribe(&mut self, sub: Subscription) -> PortResult<()> {
        let mut streams = self.streams.lock().expect("streams lock");
        for name in Self::stream_names(&sub) {
            streams.remove(&name);
        }
        Ok(())
    }

    async fn request_bars(
        &self,
        bar_type: BarType,
        start: UnixNanos,
        end: UnixNanos,
    ) -> PortResult<Vec<Bar>> {
        let symbol = bar_type.instrument_id.symbol.as_str().to_string();
        let interval =
            bar_aggregation_to_interval(bar_type.spec.aggregation, bar_type.spec.step);
        let start_ms = (start.as_u64() / 1_000_000).to_string();
        let end_ms = (end.as_u64() / 1_000_000).to_string();
        let resp = self
            .rest
            .send(
                RestRequest::get("/api/v3/klines", 2)
                    .with_param("symbol", symbol)
                    .with_param("interval", interval)
                    .with_param("startTime", start_ms)
                    .with_param("endTime", end_ms)
                    .with_param("limit", "1000"),
            )
            .await
            .map_err(crate::net_to_port)?;
        let rows: Vec<serde_json::Value> = resp.json().map_err(crate::net_to_port)?;
        let mut bars = Vec::with_capacity(rows.len());
        for row in &rows {
            let bar = kline_to_bar(row, bar_type.clone())
                .map_err(|e| PortError::Io(format!("kline -> Bar: {e}")))?;
            bars.push(bar);
        }
        Ok(bars)
    }

    fn take_stream(&mut self) -> mpsc::Receiver<MarketEvent> {
        // Single-consumer: taken exactly once at Kernel build. Panic on a second take is the correct
        // fail-fast — it signals a wiring bug.
        self.rx
            .take()
            .expect("BinanceDataClient::take_stream called more than once")
    }

    async fn disconnect(&mut self) -> PortResult<()> {
        if let Some(mut ws) = self.ws.take() {
            ws.shutdown().await;
        }
        Ok(())
    }
}

/// Map a `BarAggregation` + step into the Binance kline interval string (`1m`, `5m`, `1h`, `1d`...).
pub fn bar_aggregation_to_interval(agg: BarAggregation, step: u32) -> String {
    let unit = match agg {
        BarAggregation::Second => "s",
        BarAggregation::Minute => "m",
        BarAggregation::Hour => "h",
        BarAggregation::Day => "d",
        // Binance has no native tick interval; default to 1m so warm-up still works.
        BarAggregation::Tick => "m",
    };
    format!("{}{unit}", step.max(1))
}

/// PURE: normalize a combined-stream frame `{"stream":"...","data":{...}}` into `MarketEvent`s.
/// Returns `None` for unrecognized frames (control acks, unknown stream types).
pub fn normalize_combined_frame(text: &str, ts_init_ms: u64) -> Option<Vec<MarketEvent>> {
    let frame: serde_json::Value = serde_json::from_str(text).ok()?;
    let stream = frame.get("stream").and_then(|v| v.as_str())?;
    let data = frame.get("data")?;
    let ts_init = UnixNanos(ts_init_ms.saturating_mul(1_000_000));

    if stream.ends_with("@trade") {
        let tick = normalize_trade(data, ts_init).ok()?;
        Some(vec![MarketEvent::Trade(tick)])
    } else if stream.ends_with("@bookTicker") {
        let tick = normalize_book_ticker(data, ts_init).ok()?;
        Some(vec![MarketEvent::Quote(tick)])
    } else if stream.contains("@depth") {
        let deltas = normalize_depth(data, ts_init).ok()?;
        Some(deltas.into_iter().map(MarketEvent::Delta).collect())
    } else {
        None
    }
}

/// PURE: a Binance `@trade` event -> `TradeTick`. The taker side is Buy unless the buyer is the
/// market maker (`m == true`, meaning the aggressor sold into the bid).
pub fn normalize_trade(data: &serde_json::Value, ts_init: UnixNanos) -> Result<TradeTick, String> {
    let symbol = str_field(data, "s")?;
    let price = parse_price(str_field(data, "p")?)?;
    let size = parse_qty(str_field(data, "q")?)?;
    let trade_id = num_field(data, "t")?;
    let buyer_is_maker = data.get("m").and_then(|v| v.as_bool()).unwrap_or(false);
    let ts_event = UnixNanos(num_field(data, "T")?.saturating_mul(1_000_000));
    let aggressor = if buyer_is_maker {
        OrderSide::Sell
    } else {
        OrderSide::Buy
    };
    Ok(TradeTick {
        instrument_id: instrument_id(symbol),
        price,
        size,
        aggressor,
        trade_id: TradeId::from(trade_id.to_string()),
        ts_event,
        ts_init,
    })
}

/// PURE: a Binance `@bookTicker` event -> `QuoteTick`. bookTicker has no event time, so `ts_event`
/// is set to `ts_init` (ingest time) — the freshest available timestamp.
pub fn normalize_book_ticker(
    data: &serde_json::Value,
    ts_init: UnixNanos,
) -> Result<QuoteTick, String> {
    let symbol = str_field(data, "s")?;
    let bid = parse_price(str_field(data, "b")?)?;
    let ask = parse_price(str_field(data, "a")?)?;
    let bid_size = parse_qty(str_field(data, "B")?)?;
    let ask_size = parse_qty(str_field(data, "A")?)?;
    Ok(QuoteTick {
        instrument_id: instrument_id(symbol),
        bid,
        ask,
        bid_size,
        ask_size,
        ts_event: ts_init,
        ts_init,
    })
}

/// PURE: a Binance `@depth@100ms` diff event -> a flat `Vec<OrderBookDelta>` (bids then asks). A
/// zero size means the level was removed (`BookAction::Delete`); otherwise it is an update. The
/// diff's last update id `u` becomes each delta's `sequence`.
pub fn normalize_depth(
    data: &serde_json::Value,
    ts_init: UnixNanos,
) -> Result<Vec<OrderBookDelta>, String> {
    let symbol = str_field(data, "s")?;
    let id = instrument_id(symbol);
    let sequence = num_field(data, "u")?;
    let ts_event = UnixNanos(num_field(data, "E")?.saturating_mul(1_000_000));

    let mut out = Vec::new();
    for (side, key) in [(OrderSide::Buy, "b"), (OrderSide::Sell, "a")] {
        let levels = data
            .get(key)
            .and_then(|v| v.as_array())
            .ok_or_else(|| format!("depth: missing `{key}` levels"))?;
        for level in levels {
            let pair = level
                .as_array()
                .ok_or_else(|| "depth: level not a [price, qty] pair".to_string())?;
            let price = parse_price(
                pair.first()
                    .and_then(|v| v.as_str())
                    .ok_or_else(|| "depth: missing price".to_string())?,
            )?;
            let size = parse_qty(
                pair.get(1)
                    .and_then(|v| v.as_str())
                    .ok_or_else(|| "depth: missing size".to_string())?,
            )?;
            let action = if size.is_zero() {
                BookAction::Delete
            } else {
                BookAction::Update
            };
            out.push(OrderBookDelta {
                instrument_id: id.clone(),
                action,
                side,
                price,
                size,
                sequence,
                ts_event,
                ts_init,
            });
        }
    }
    Ok(out)
}

/// PURE: a Binance kline row -> `Bar`. The REST `klines` row is a 12-element array:
/// `[openTime, open, high, low, close, volume, closeTime, ...]`. `ts_event` is the CLOSE time so a
/// bar is only emitted once complete (no look-ahead, per the `Bar` contract).
pub fn kline_to_bar(row: &serde_json::Value, bar_type: BarType) -> Result<Bar, String> {
    let arr = row
        .as_array()
        .ok_or_else(|| "kline: row is not an array".to_string())?;
    let get = |i: usize| -> Result<&serde_json::Value, String> {
        arr.get(i).ok_or_else(|| format!("kline: missing index {i}"))
    };
    let as_str = |v: &serde_json::Value| -> Result<String, String> {
        v.as_str()
            .map(|s| s.to_string())
            .ok_or_else(|| "kline: expected string field".to_string())
    };
    let close_time_ms = get(6)?
        .as_u64()
        .ok_or_else(|| "kline: closeTime not an integer".to_string())?;
    let open = parse_price(&as_str(get(1)?)?)?;
    let high = parse_price(&as_str(get(2)?)?)?;
    let low = parse_price(&as_str(get(3)?)?)?;
    let close = parse_price(&as_str(get(4)?)?)?;
    let volume = parse_qty(&as_str(get(5)?)?)?;
    let ts = UnixNanos(close_time_ms.saturating_mul(1_000_000));
    Ok(Bar {
        bar_type,
        open,
        high,
        low,
        close,
        volume,
        ts_event: ts,
        ts_init: ts,
    })
}

// --- small parsing helpers -------------------------------------------------------------------

fn instrument_id(symbol: &str) -> InstrumentId {
    InstrumentId::new(Symbol::from(symbol), crate::venue())
}

fn str_field<'a>(data: &'a serde_json::Value, key: &str) -> Result<&'a str, String> {
    data.get(key)
        .and_then(|v| v.as_str())
        .ok_or_else(|| format!("missing string field `{key}`"))
}

fn num_field(data: &serde_json::Value, key: &str) -> Result<u64, String> {
    data.get(key)
        .and_then(|v| v.as_u64())
        .ok_or_else(|| format!("missing integer field `{key}`"))
}

fn parse_price(s: &str) -> Result<Price, String> {
    let d = Decimal::from_str(s).map_err(|e| format!("bad price {s}: {e}"))?;
    Price::from_decimal(d, WIRE_PRECISION).map_err(|e| format!("price {s}: {e}"))
}

fn parse_qty(s: &str) -> Result<Quantity, String> {
    let d = Decimal::from_str(s).map_err(|e| format!("bad qty {s}: {e}"))?;
    Quantity::from_decimal(d, WIRE_PRECISION).map_err(|e| format!("qty {s}: {e}"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use rust_decimal_macros::dec;

    fn ti() -> UnixNanos {
        UnixNanos(1_700_000_000_000 * 1_000_000)
    }

    #[test]
    fn normalize_trade_sets_aggressor_from_maker_flag() {
        // m=false -> buyer is taker -> aggressor Buy.
        let data: serde_json::Value = serde_json::from_str(
            r#"{"e":"trade","E":1700000000123,"s":"BTCUSDT","t":12345,"p":"50000.50","q":"0.10000000","T":1700000000100,"m":false}"#,
        )
        .unwrap();
        let tick = normalize_trade(&data, ti()).unwrap();
        assert_eq!(tick.instrument_id.to_string(), "BTCUSDT.BINANCE");
        assert_eq!(tick.price.as_decimal(), dec!(50000.50));
        assert_eq!(tick.size.as_decimal(), dec!(0.1));
        assert_eq!(tick.aggressor, OrderSide::Buy);
        assert_eq!(tick.trade_id.as_str(), "12345");
        // T (event time) is 1700000000100 ms -> ns.
        assert_eq!(tick.ts_event, UnixNanos(1_700_000_000_100 * 1_000_000));

        // m=true -> buyer is maker -> aggressor sold -> Sell.
        let data2: serde_json::Value = serde_json::from_str(
            r#"{"e":"trade","E":1,"s":"ETHUSDT","t":9,"p":"3000","q":"1","T":2,"m":true}"#,
        )
        .unwrap();
        let tick2 = normalize_trade(&data2, ti()).unwrap();
        assert_eq!(tick2.aggressor, OrderSide::Sell);
    }

    #[test]
    fn normalize_book_ticker_maps_bid_ask() {
        let data: serde_json::Value = serde_json::from_str(
            r#"{"u":400900217,"s":"BTCUSDT","b":"49999.90","B":"2.50000000","a":"50000.10","A":"1.20000000"}"#,
        )
        .unwrap();
        let q = normalize_book_ticker(&data, ti()).unwrap();
        assert_eq!(q.bid.as_decimal(), dec!(49999.90));
        assert_eq!(q.ask.as_decimal(), dec!(50000.10));
        assert_eq!(q.bid_size.as_decimal(), dec!(2.5));
        assert_eq!(q.ask_size.as_decimal(), dec!(1.2));
        // mid = (49999.90 + 50000.10)/2 = 50000.00
        assert_eq!(q.mid().as_decimal(), dec!(50000.00000000));
    }

    #[test]
    fn normalize_depth_emits_bid_then_ask_deltas_with_delete_on_zero() {
        let data: serde_json::Value = serde_json::from_str(
            r#"{"e":"depthUpdate","E":1700000000200,"s":"BTCUSDT","U":100,"u":110,
                "b":[["49999.00","1.00000000"],["49998.00","0.00000000"]],
                "a":[["50001.00","2.00000000"]]}"#,
        )
        .unwrap();
        let deltas = normalize_depth(&data, ti()).unwrap();
        assert_eq!(deltas.len(), 3);
        // bids first
        assert_eq!(deltas[0].side, OrderSide::Buy);
        assert_eq!(deltas[0].action, BookAction::Update);
        assert_eq!(deltas[0].price.as_decimal(), dec!(49999.00));
        // zero size -> Delete
        assert_eq!(deltas[1].side, OrderSide::Buy);
        assert_eq!(deltas[1].action, BookAction::Delete);
        // then asks
        assert_eq!(deltas[2].side, OrderSide::Sell);
        assert_eq!(deltas[2].price.as_decimal(), dec!(50001.00));
        // sequence is the diff's last update id `u`.
        assert_eq!(deltas[0].sequence, 110);
        assert_eq!(deltas[0].ts_event, UnixNanos(1_700_000_000_200 * 1_000_000));
    }

    #[test]
    fn kline_to_bar_uses_close_time_and_ohlcv() {
        let bar_type = BarType {
            instrument_id: instrument_id("BTCUSDT"),
            spec: coinext_model::BarSpec {
                step: 1,
                aggregation: BarAggregation::Minute,
                price_type: coinext_model::PriceType::Last,
            },
            source: coinext_model::AggregationSource::External,
        };
        // A 12-field Binance kline row.
        let row: serde_json::Value = serde_json::from_str(
            r#"[1700000000000,"50000.00","50100.00","49900.00","50050.00","12.34500000",
                1700000059999,"617000.00",250,"6.00000000","300000.00","0"]"#,
        )
        .unwrap();
        let bar = kline_to_bar(&row, bar_type).unwrap();
        assert_eq!(bar.open.as_decimal(), dec!(50000.00));
        assert_eq!(bar.high.as_decimal(), dec!(50100.00));
        assert_eq!(bar.low.as_decimal(), dec!(49900.00));
        assert_eq!(bar.close.as_decimal(), dec!(50050.00));
        assert_eq!(bar.volume.as_decimal(), dec!(12.345));
        // ts_event is closeTime (index 6).
        assert_eq!(bar.ts_event, UnixNanos(1_700_000_059_999 * 1_000_000));
    }

    #[test]
    fn combined_frame_dispatches_by_stream_suffix() {
        let trade = r#"{"stream":"btcusdt@trade","data":{"e":"trade","E":1,"s":"BTCUSDT","t":1,"p":"50000","q":"0.1","T":1,"m":false}}"#;
        let evs = normalize_combined_frame(trade, 1).unwrap();
        assert!(matches!(evs[0], MarketEvent::Trade(_)));

        let bt = r#"{"stream":"btcusdt@bookTicker","data":{"s":"BTCUSDT","b":"1","B":"1","a":"2","A":"1"}}"#;
        let evs = normalize_combined_frame(bt, 1).unwrap();
        assert!(matches!(evs[0], MarketEvent::Quote(_)));

        let depth = r#"{"stream":"btcusdt@depth@100ms","data":{"e":"depthUpdate","E":1,"s":"BTCUSDT","U":1,"u":2,"b":[["1","1"]],"a":[]}}"#;
        let evs = normalize_combined_frame(depth, 1).unwrap();
        assert!(matches!(evs[0], MarketEvent::Delta(_)));

        // Unknown stream -> None.
        assert!(normalize_combined_frame(r#"{"stream":"x@kline_1m","data":{}}"#, 1).is_none());
    }

    #[test]
    fn bar_interval_strings() {
        assert_eq!(bar_aggregation_to_interval(BarAggregation::Minute, 1), "1m");
        assert_eq!(bar_aggregation_to_interval(BarAggregation::Minute, 5), "5m");
        assert_eq!(bar_aggregation_to_interval(BarAggregation::Hour, 1), "1h");
        assert_eq!(bar_aggregation_to_interval(BarAggregation::Day, 1), "1d");
    }

    // Network-gated: connect to the PUBLIC mainnet trade WS for btcusdt, receive 1 frame, normalize.
    #[tokio::test]
    #[ignore = "requires network access to Binance public WS"]
    async fn live_public_trade_ws_one_message() {
        let url = "wss://stream.binance.com:9443/stream?streams=btcusdt@trade";
        let text = WsClient::connect_once_text(url, 10_000)
            .await
            .expect("connect + first frame");
        let events = normalize_combined_frame(&text, now_unix_ms())
            .expect("normalize a combined trade frame");
        assert!(matches!(events[0], MarketEvent::Trade(_)));
    }
}
