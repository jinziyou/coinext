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
use std::collections::{BTreeSet, HashMap};
use std::str::FromStr;
use std::sync::Mutex;
use tokio::sync::mpsc;

use crate::book::{ApplyOutcome, DepthUpdate, LocalOrderBook};
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
    /// Shared so the WS pump's depth-resync task can fetch REST order-book snapshots.
    rest: std::sync::Arc<RestClient>,
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
            rest: std::sync::Arc::new(rest),
            tx,
            rx: Some(rx),
            streams: Mutex::new(BTreeSet::new()),
            ws: None,
        })
    }

    /// Fetch a depth snapshot's `lastUpdateId` for `symbol` (`GET /api/v3/depth?symbol&limit=1000`).
    async fn fetch_depth_snapshot_last_id(
        rest: &RestClient,
        symbol: &str,
    ) -> PortResult<u64> {
        let resp = rest
            .send(
                RestRequest::get("/api/v3/depth", 50)
                    .with_param("symbol", symbol.to_string())
                    .with_param("limit", "1000"),
            )
            .await
            .map_err(crate::net_to_port)?;
        parse_depth_snapshot_last_id(&resp.body).map_err(PortError::Io)
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
        let rest = self.rest.clone();

        // Spawn the normalize pump: combined-stream frames are `{"stream":..,"data":{..}}`. The
        // pump owns a `DepthResyncer` so the live book is repaired (REST snapshot + diff replay) on
        // any detected gap or reconnect — without this the book silently drifts after a dropped
        // frame.
        tokio::spawn(async move {
            let mut resyncer = DepthResyncer::new();
            while let Some(msg) = rx.recv().await {
                match msg {
                    WsMessage::Text(text) => {
                        if process_frame(&text, &tx, &rest, &mut resyncer)
                            .await
                            .is_err()
                        {
                            return; // core dropped the receiver
                        }
                    }
                    // On reconnect every book stream is suspect: mark all for resync so the next
                    // diff per symbol re-bridges from a fresh snapshot.
                    WsMessage::Reconnected => resyncer.mark_all_for_resync(),
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
        let (_update, deltas) = normalize_depth(data, ts_init).ok()?;
        Some(deltas.into_iter().map(MarketEvent::Delta).collect())
    } else {
        None
    }
}

/// Process one WS frame: parse, dispatch trade/quote events directly, and run depth diffs through
/// the gap state machine — fetching a REST snapshot and replaying buffered diffs on any gap so the
/// emitted book deltas are correct. Returns `Err(())` only when the core dropped the receiver (the
/// pump should stop). Frames that fail to parse are skipped, matching the prior best-effort policy.
async fn process_frame(
    text: &str,
    tx: &mpsc::Sender<MarketEvent>,
    rest: &RestClient,
    resyncer: &mut DepthResyncer,
) -> Result<(), ()> {
    let ts_init_ms = now_unix_ms();
    let frame: serde_json::Value = match serde_json::from_str(text) {
        Ok(f) => f,
        Err(_) => return Ok(()),
    };
    let Some(stream) = frame.get("stream").and_then(|v| v.as_str()) else {
        return Ok(());
    };
    let Some(data) = frame.get("data") else {
        return Ok(());
    };
    let ts_init = UnixNanos(ts_init_ms.saturating_mul(1_000_000));

    if stream.contains("@depth") {
        // Depth diff: thread the update ids through the resyncer before emitting deltas.
        let (update, deltas) = match normalize_depth(data, ts_init) {
            Ok(v) => v,
            Err(_) => return Ok(()),
        };
        let symbol = match str_field(data, "s") {
            Ok(s) => s.to_string(),
            Err(_) => return Ok(()),
        };
        let mut action = resyncer.on_diff(&symbol, update);
        // Loop in case the first snapshot is already stale relative to the buffered diffs.
        while action == DepthAction::NeedSnapshot {
            match BinanceDataClient::fetch_depth_snapshot_last_id(rest, &symbol).await {
                Ok(last_id) => action = resyncer.on_snapshot(&symbol, last_id),
                Err(_) => return Ok(()), // snapshot fetch failed; try again on the next diff
            }
        }
        // Only emit deltas once the book is bridged and applying in order.
        if resyncer.is_synced(&symbol) {
            for ev in deltas.into_iter().map(MarketEvent::Delta) {
                if tx.send(ev).await.is_err() {
                    return Err(());
                }
            }
        }
        return Ok(());
    }

    // Trade / quote: stateless, emit directly.
    if let Some(events) = normalize_combined_frame(text, ts_init_ms) {
        for ev in events {
            if tx.send(ev).await.is_err() {
                return Err(());
            }
        }
    }
    Ok(())
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

/// PURE: a Binance `@depth@100ms` diff event -> the diff's update ids plus a flat
/// `Vec<OrderBookDelta>` (bids then asks). A zero size means the level was removed
/// (`BookAction::Delete`); otherwise it is an update. The diff's last update id `u` becomes each
/// delta's `sequence`.
///
/// The returned [`DepthUpdate`] PRESERVES the diff's `U` (first update id) and `pu` (previous `u`,
/// when present) so the caller's gap-detection state machine ([`crate::book::LocalOrderBook`]) can
/// verify continuity — these ids are NOT carried on the per-level `OrderBookDelta`, so dropping them
/// here would make gap detection impossible.
pub fn normalize_depth(
    data: &serde_json::Value,
    ts_init: UnixNanos,
) -> Result<(DepthUpdate, Vec<OrderBookDelta>), String> {
    let symbol = str_field(data, "s")?;
    let id = instrument_id(symbol);
    let first_update_id = num_field(data, "U")?;
    let sequence = num_field(data, "u")?;
    // `pu` is only on the diff-depth stream; the legacy partial-depth stream omits it.
    let prev_update_id = data.get("pu").and_then(|v| v.as_u64());
    let update = DepthUpdate {
        first_update_id,
        last_update_id: sequence,
        prev_update_id,
    };
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
    Ok((update, out))
}

/// What the depth handler must do after feeding one diff into the gap state machine.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum DepthAction {
    /// The diff was in-order (or harmlessly stale): nothing to do.
    Continue,
    /// A gap (or a not-yet-synced book) was detected: the caller MUST fetch a REST snapshot for
    /// `symbol` and feed it back via [`DepthResyncer::on_snapshot`] before continuing.
    NeedSnapshot,
}

/// Wires the pure [`LocalOrderBook`] gap state machine into the live depth-diff data path. Tracks a
/// book per instrument; on a detected gap (or on the very first diff / a reconnect) it tells the
/// caller to fetch a REST snapshot, then replays the buffered diffs over it.
///
/// The state transitions are SYNCHRONOUS and network-free (so they are unit-testable); the actual
/// REST snapshot fetch is performed by the async caller in the WS pump.
#[derive(Default)]
pub struct DepthResyncer {
    /// Per-symbol gap-detection book + buffered diffs awaiting a snapshot.
    books: HashMap<String, BookState>,
}

#[derive(Default)]
struct BookState {
    book: LocalOrderBook,
    /// Diffs buffered while a snapshot is in flight (replayed once it lands).
    buffer: Vec<DepthUpdate>,
    /// Whether we are currently awaiting a REST snapshot for this symbol.
    awaiting_snapshot: bool,
}

impl DepthResyncer {
    pub fn new() -> Self {
        Self::default()
    }

    /// Force every tracked book to resync (called on a WS `Reconnected`): the next diff per symbol
    /// will trigger a fresh snapshot fetch.
    pub fn mark_all_for_resync(&mut self) {
        for state in self.books.values_mut() {
            state.book = LocalOrderBook::new();
            state.buffer.clear();
            state.awaiting_snapshot = false;
        }
    }

    /// Feed one parsed diff. Returns whether the caller must fetch a snapshot for `symbol`. While a
    /// snapshot is awaited the diff is buffered for replay.
    pub fn on_diff(&mut self, symbol: &str, update: DepthUpdate) -> DepthAction {
        let state = self.books.entry(symbol.to_string()).or_default();
        if state.awaiting_snapshot {
            // Snapshot in flight: buffer and keep waiting.
            state.buffer.push(update);
            return DepthAction::NeedSnapshot;
        }
        if !state.book.is_synced() && state.book.last_update_id() == 0 {
            // Never synced (fresh book / post-reconnect): need a snapshot to bridge from.
            state.awaiting_snapshot = true;
            state.buffer.push(update);
            return DepthAction::NeedSnapshot;
        }
        match state.book.apply_diff(&update) {
            ApplyOutcome::Applied | ApplyOutcome::Skipped => DepthAction::Continue,
            ApplyOutcome::Resync => {
                // A gap mid-stream: start buffering and ask for a fresh snapshot.
                state.awaiting_snapshot = true;
                state.buffer.clear();
                state.buffer.push(update);
                DepthAction::NeedSnapshot
            }
        }
    }

    /// Install a freshly fetched snapshot `lastUpdateId` and replay the buffered diffs over it,
    /// dropping the stale ones. Returns `NeedSnapshot` if the snapshot was already stale relative to
    /// the buffered diffs (the caller must fetch again).
    pub fn on_snapshot(&mut self, symbol: &str, snapshot_last_id: u64) -> DepthAction {
        let state = self.books.entry(symbol.to_string()).or_default();
        state.book.install_snapshot(snapshot_last_id);
        state.awaiting_snapshot = false;
        let buffered = std::mem::take(&mut state.buffer);
        for update in buffered {
            // Drop diffs entirely at/before the snapshot; bridge with the first that spans it.
            if update.last_update_id <= snapshot_last_id {
                continue;
            }
            if state.book.apply_diff(&update) == ApplyOutcome::Resync {
                // Snapshot is stale relative to the buffer: must refetch.
                state.awaiting_snapshot = true;
                state.buffer.clear();
                return DepthAction::NeedSnapshot;
            }
        }
        DepthAction::Continue
    }

    /// Whether the book for `symbol` has bridged a snapshot and is applying diffs in order.
    pub fn is_synced(&self, symbol: &str) -> bool {
        self.books
            .get(symbol)
            .map(|s| s.book.is_synced())
            .unwrap_or(false)
    }
}

/// PURE: extract `lastUpdateId` from a `GET /api/v3/depth` snapshot body.
pub fn parse_depth_snapshot_last_id(body: &str) -> Result<u64, String> {
    let v: serde_json::Value =
        serde_json::from_str(body).map_err(|e| format!("invalid depth snapshot json: {e}"))?;
    v.get("lastUpdateId")
        .and_then(|x| x.as_u64())
        .ok_or_else(|| "depth snapshot missing lastUpdateId".to_string())
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

    fn upd(u_first: u64, u_last: u64, pu: Option<u64>) -> DepthUpdate {
        DepthUpdate {
            first_update_id: u_first,
            last_update_id: u_last,
            prev_update_id: pu,
        }
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
        let (update, deltas) = normalize_depth(&data, ti()).unwrap();
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
        // The diff's U (first) and u (last) update ids are PRESERVED for gap detection.
        assert_eq!(update.first_update_id, 100);
        assert_eq!(update.last_update_id, 110);
        // This (combined) depth frame omits `pu`, so it is None.
        assert_eq!(update.prev_update_id, None);
    }

    #[test]
    fn normalize_depth_preserves_pu_when_present() {
        // The diff-depth stream carries `pu` (previous event's `u`); it must be preserved so the
        // resync state machine can check contiguity (`pu == previous u`).
        let data: serde_json::Value = serde_json::from_str(
            r#"{"e":"depthUpdate","E":1,"s":"BTCUSDT","U":111,"u":120,"pu":110,
                "b":[["49999.00","1.00000000"]],"a":[]}"#,
        )
        .unwrap();
        let (update, _deltas) = normalize_depth(&data, ti()).unwrap();
        assert_eq!(update.first_update_id, 111);
        assert_eq!(update.last_update_id, 120);
        assert_eq!(update.prev_update_id, Some(110));
    }

    #[test]
    fn resyncer_needs_snapshot_then_bridges_and_applies() {
        let mut r = DepthResyncer::new();
        // First diff ever -> must fetch a snapshot.
        assert_eq!(
            r.on_diff("BTCUSDT", upd(99, 105, None)),
            DepthAction::NeedSnapshot
        );
        assert!(!r.is_synced("BTCUSDT"));
        // Snapshot lastUpdateId=100 bridges the buffered diff (U=99 <= 101 <= u=105).
        assert_eq!(r.on_snapshot("BTCUSDT", 100), DepthAction::Continue);
        assert!(r.is_synced("BTCUSDT"));
        // Contiguous follow-up applies without a new snapshot.
        assert_eq!(
            r.on_diff("BTCUSDT", upd(106, 110, Some(105))),
            DepthAction::Continue
        );
        assert!(r.is_synced("BTCUSDT"));
    }

    #[test]
    fn resyncer_triggers_snapshot_on_simulated_gap() {
        let mut r = DepthResyncer::new();
        assert_eq!(
            r.on_diff("BTCUSDT", upd(99, 105, None)),
            DepthAction::NeedSnapshot
        );
        assert_eq!(r.on_snapshot("BTCUSDT", 100), DepthAction::Continue);
        assert!(r.is_synced("BTCUSDT"));
        // A GAP: pu=108 but the last applied u was 105 -> a dropped frame -> resync required.
        assert_eq!(
            r.on_diff("BTCUSDT", upd(109, 115, Some(108))),
            DepthAction::NeedSnapshot
        );
        assert!(!r.is_synced("BTCUSDT"));
        // A fresh snapshot past the gap re-bridges from the buffered diff (U=109 <= 113 <= u=115).
        assert_eq!(r.on_snapshot("BTCUSDT", 112), DepthAction::Continue);
        assert!(r.is_synced("BTCUSDT"));
    }

    #[test]
    fn resyncer_reconnect_forces_fresh_snapshot() {
        let mut r = DepthResyncer::new();
        assert_eq!(
            r.on_diff("BTCUSDT", upd(99, 105, None)),
            DepthAction::NeedSnapshot
        );
        assert_eq!(r.on_snapshot("BTCUSDT", 100), DepthAction::Continue);
        assert!(r.is_synced("BTCUSDT"));
        // A reconnect invalidates every book: the next diff must re-fetch a snapshot.
        r.mark_all_for_resync();
        assert!(!r.is_synced("BTCUSDT"));
        assert_eq!(
            r.on_diff("BTCUSDT", upd(120, 130, Some(110))),
            DepthAction::NeedSnapshot
        );
    }

    #[test]
    fn parse_depth_snapshot_last_id_extracts_field() {
        assert_eq!(
            parse_depth_snapshot_last_id(r#"{"lastUpdateId":160,"bids":[],"asks":[]}"#).unwrap(),
            160
        );
        assert!(parse_depth_snapshot_last_id("{}").is_err());
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
