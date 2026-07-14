//! Downstream sinks for normalized [`MarketEvent`]s: local lake + Redis Envelope fan-out.
//!
//! * **Lake** — bars go to Hive-style Parquet under `COINEXT__DATA__LAKE_ROOT` matching
//!   `coinext_data.DataLake` layout; every event is also appended as NDJSON for audit/debug.
//! * **Redis** — optional MessagePack `Envelope` XADD onto `coinext.market` (same wire layout as
//!   Python `coinext_bus.encode_envelope`).

use coinext_bus::Envelope;
use coinext_core::UnixNanos;
use coinext_model::{Bar, MarketEvent};
use coinext_persistence::ParquetWriter;
use coinext_ports::MsgType;
use std::collections::HashMap;
use std::fs::{create_dir_all, OpenOptions};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

/// Stream key for market-data fan-out (mirrors Python `coinext_bus.STREAM_MARKET`).
pub const STREAM_MARKET: &str = "coinext.market";

/// Prometheus-facing counters shared with the metrics HTTP task.
#[derive(Default)]
pub struct IngestMetrics {
    pub events_logged: AtomicU64,
    pub bars_flushed: AtomicU64,
    pub redis_published: AtomicU64,
    pub redis_errors: AtomicU64,
    pub ws_connects: AtomicU64,
    pub ws_reconnects: AtomicU64,
    pub ws_errors: AtomicU64,
    /// Coarse book-gap detections (depth sequence jumps) observed on MarketEvent::Delta.
    pub book_gaps: AtomicU64,
}

/// Per-instrument depth sequence tracker for gap metrics.
#[derive(Default)]
pub struct BookGapTracker {
    last_seq: HashMap<String, u64>,
}

impl BookGapTracker {
    /// Observe one delta sequence. Returns true if a gap was detected.
    pub fn observe(&mut self, instrument: &str, sequence: u64) -> bool {
        let gap = match self.last_seq.get(instrument) {
            Some(&prev) if sequence > prev + 1 => true,
            _ => false,
        };
        // Only advance on non-stale sequences.
        let entry = self.last_seq.entry(instrument.to_string()).or_insert(0);
        if sequence >= *entry {
            *entry = sequence;
        }
        gap
    }
}

pub struct IngestSinks {
    lake_root: Option<PathBuf>,
    event_log: Option<PathBuf>,
    #[allow(dead_code)]
    redis_url: Option<String>,
    redis: Option<redis::Connection>,
    parquet: ParquetWriter,
    /// Buffered bars keyed by series path fragment `venue/symbol/interval`.
    bar_buf: HashMap<String, Vec<Bar>>,
    bar_flush_every: usize,
    published: u64,
    written_events: u64,
    flushed_bars: u64,
    /// Shared counters for the metrics HTTP server.
    pub metrics: Arc<IngestMetrics>,
    gap_tracker: BookGapTracker,
}

impl IngestSinks {
    pub fn from_env() -> Self {
        let lake = std::env::var("COINEXT__DATA__LAKE_ROOT")
            .ok()
            .filter(|s| !s.trim().is_empty())
            .map(PathBuf::from);
        let event_log = std::env::var("COINEXT__INGEST__EVENT_LOG")
            .ok()
            .filter(|s| !s.trim().is_empty())
            .map(PathBuf::from)
            .or_else(|| {
                lake.as_ref()
                    .map(|r| r.join("ingest").join("events.ndjson"))
            });
        let redis_url = std::env::var("COINEXT__REDIS__URL")
            .ok()
            .filter(|s| !s.trim().is_empty());
        let redis = redis_url.as_ref().and_then(|url| {
            redis::Client::open(url.as_str())
                .ok()
                .and_then(|c| c.get_connection().ok())
        });
        if redis_url.is_some() && redis.is_none() {
            eprintln!("coinext-ingest: COINEXT__REDIS__URL set but connection failed; publish disabled");
        }
        let bar_flush_every: usize = std::env::var("COINEXT__INGEST__BAR_FLUSH")
            .ok()
            .and_then(|s| s.parse().ok())
            .unwrap_or(32);
        IngestSinks {
            lake_root: lake,
            event_log,
            redis_url,
            redis,
            parquet: ParquetWriter::new(),
            bar_buf: HashMap::new(),
            bar_flush_every: bar_flush_every.max(1),
            published: 0,
            written_events: 0,
            flushed_bars: 0,
            metrics: Arc::new(IngestMetrics::default()),
            gap_tracker: BookGapTracker::default(),
        }
    }

    pub fn stats(&self) -> (u64, u64, u64) {
        (self.written_events, self.flushed_bars, self.published)
    }

    pub fn metrics_handle(&self) -> Arc<IngestMetrics> {
        self.metrics.clone()
    }

    pub fn handle(&mut self, ev: &MarketEvent) {
        self.append_event_log(ev);
        if let MarketEvent::Bar(b) = ev {
            self.buffer_bar(b.clone());
        }
        if let MarketEvent::Delta(d) = ev {
            let iid = d.instrument_id.to_string();
            if self.gap_tracker.observe(&iid, d.sequence) {
                self.metrics.book_gaps.fetch_add(1, Ordering::Relaxed);
                eprintln!(
                    "coinext-ingest: book gap instrument={iid} seq={} (resync recommended)",
                    d.sequence
                );
            }
        }
        if let Err(e) = self.publish_redis(ev) {
            eprintln!("coinext-ingest: redis publish failed: {e}");
        }
    }

    pub fn flush(&mut self) {
        let keys: Vec<String> = self.bar_buf.keys().cloned().collect();
        for k in keys {
            if let Some(bars) = self.bar_buf.remove(&k) {
                if let Err(e) = self.flush_bar_batch(&k, &bars) {
                    eprintln!("coinext-ingest: bar flush failed for {k}: {e}");
                    // put back so we don't lose data on transient IO errors
                    self.bar_buf.insert(k, bars);
                }
            }
        }
    }

    fn append_event_log(&mut self, ev: &MarketEvent) {
        let Some(path) = &self.event_log else {
            return;
        };
        if let Some(parent) = path.parent() {
            let _ = create_dir_all(parent);
        }
        let line = match event_to_json_line(ev) {
            Ok(s) => s,
            Err(e) => {
                eprintln!("coinext-ingest: event serialize failed: {e}");
                return;
            }
        };
        match OpenOptions::new().create(true).append(true).open(path) {
            Ok(mut f) => {
                if writeln!(f, "{line}").is_ok() {
                    self.written_events += 1;
                    self.metrics.events_logged.fetch_add(1, Ordering::Relaxed);
                }
            }
            Err(e) => eprintln!("coinext-ingest: event log open failed: {e}"),
        }
    }

    fn buffer_bar(&mut self, bar: Bar) {
        let key = bar_series_key(&bar);
        let buf = self.bar_buf.entry(key.clone()).or_default();
        buf.push(bar);
        if buf.len() >= self.bar_flush_every {
            let bars = self.bar_buf.remove(&key).unwrap_or_default();
            if let Err(e) = self.flush_bar_batch(&key, &bars) {
                eprintln!("coinext-ingest: bar flush failed for {key}: {e}");
                self.bar_buf.insert(key, bars);
            }
        }
    }

    fn flush_bar_batch(&mut self, series_key: &str, bars: &[Bar]) -> Result<(), String> {
        if bars.is_empty() {
            return Ok(());
        }
        let Some(root) = &self.lake_root else {
            // No lake root — still count as handled via event log only.
            return Ok(());
        };
        // series_key = "BINANCE/BTCUSDT/1m"
        let parts: Vec<&str> = series_key.split('/').collect();
        if parts.len() != 3 {
            return Err(format!("bad series key {series_key}"));
        }
        let (venue, symbol, interval) = (parts[0], parts[1], parts[2]);
        let yyyymm = yyyymm_of(bars[0].ts_event);
        let dir = root
            .join("bars")
            .join(format!("venue={venue}"))
            .join(format!("symbol={symbol}"))
            .join(format!("interval={interval}"));
        create_dir_all(&dir).map_err(|e| e.to_string())?;
        // Monthly file (DataLake-compatible name). Merge with any existing rows + stray ingest parts.
        let monthly = dir.join(format!("{yyyymm}.parquet"));
        let monthly_str = monthly.to_str().unwrap_or("bars.parquet");
        let mut rows = self
            .parquet
            .read_ohlcv(monthly_str)
            .unwrap_or_default();
        // Also fold any leftover `YYYYMM-ingest-*.parquet` parts into the monthly file.
        if let Ok(entries) = std::fs::read_dir(&dir) {
            for ent in entries.flatten() {
                let name = ent.file_name().to_string_lossy().into_owned();
                if name.starts_with(&format!("{yyyymm}-")) && name.ends_with(".parquet") {
                    if let Ok(part) = self.parquet.read_ohlcv(ent.path().to_str().unwrap_or("")) {
                        rows.extend(part);
                    }
                    let _ = std::fs::remove_file(ent.path());
                }
            }
        }
        for b in bars {
            rows.push((
                b.ts_event.as_u64() as i64,
                b.open.as_f64(),
                b.high.as_f64(),
                b.low.as_f64(),
                b.close.as_f64(),
                b.volume.as_f64(),
            ));
        }
        // Dedup by ts_event (last wins), sort.
        rows.sort_by_key(|r| r.0);
        let mut dedup: std::collections::BTreeMap<i64, (i64, f64, f64, f64, f64, f64)> =
            std::collections::BTreeMap::new();
        for r in rows {
            dedup.insert(r.0, r);
        }
        let merged: Vec<_> = dedup.into_values().collect();
        self.parquet
            .write_ohlcv_rows(monthly_str, &merged)
            .map_err(|e| e.to_string())?;
        self.flushed_bars += bars.len() as u64;
        self.metrics
            .bars_flushed
            .fetch_add(bars.len() as u64, Ordering::Relaxed);
        Ok(())
    }

    fn publish_redis(&mut self, ev: &MarketEvent) -> Result<(), String> {
        if self.redis.is_none() {
            return Ok(());
        }
        let (msg_type, payload) = event_payload(ev).map_err(|e| e.to_string())?;
        let env = Envelope::new(msg_type, [0u8; 16], now_ns(), payload);
        let frame = encode_envelope_msgpack(&env)?;
        let con = self.redis.as_mut().unwrap();
        // Field name `e` matches Python `coinext_bus.RedisBusClient.publish`.
        match redis::cmd("XADD")
            .arg(STREAM_MARKET)
            .arg("*")
            .arg("e")
            .arg(frame)
            .query::<String>(con)
        {
            Ok(_) => {
                self.published += 1;
                self.metrics.redis_published.fetch_add(1, Ordering::Relaxed);
                Ok(())
            }
            Err(e) => {
                self.metrics.redis_errors.fetch_add(1, Ordering::Relaxed);
                Err(e.to_string())
            }
        }
    }
}

fn bar_series_key(bar: &Bar) -> String {
    let id = bar.bar_type.instrument_id.to_string();
    // InstrumentId displays as "BTCUSDT.BINANCE" — split symbol/venue.
    let (symbol, venue) = match id.rsplit_once('.') {
        Some((s, v)) => (s.to_string(), v.to_string()),
        None => (id, "BINANCE".to_string()),
    };
    let interval = match bar.bar_type.spec.aggregation {
        coinext_model::BarAggregation::Minute => format!("{}m", bar.bar_type.spec.step.max(1)),
        coinext_model::BarAggregation::Hour => format!("{}h", bar.bar_type.spec.step.max(1)),
        coinext_model::BarAggregation::Day => format!("{}d", bar.bar_type.spec.step.max(1)),
        _ => "1m".to_string(),
    };
    format!("{venue}/{symbol}/{interval}")
}

fn yyyymm_of(ts: UnixNanos) -> u32 {
    let secs = (ts.as_u64() / 1_000_000_000) as i64;
    // UTC yyyymm without chrono dep: use time crate free approx via gmtime-less formula.
    // Prefer chrono if available through coinext-persistence path — use simple UTC via
    // `time` from std is limited; use chrono from persistence's transitive... avoid.
    // Fixed: use `humantime` free calculation.
    let days = secs.div_euclid(86_400);
    // Civil from days since Unix epoch (Howard Hinnant algorithm).
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let m = mp + if mp < 10 { 3 } else { -9 };
    let y = y + if m <= 2 { 1 } else { 0 };
    (y as u32) * 100 + (m as u32)
}

fn now_ns() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos() as u64)
        .unwrap_or(0)
}

fn event_to_json_line(ev: &MarketEvent) -> Result<String, String> {
    let v = match ev {
        MarketEvent::Quote(q) => serde_json::json!({
            "kind": "quote",
            "instrument": q.instrument_id.to_string(),
            "bid": q.bid.as_f64(),
            "ask": q.ask.as_f64(),
            "bid_sz": q.bid_size.as_f64(),
            "ask_sz": q.ask_size.as_f64(),
            "ts_event": q.ts_event.as_u64(),
        }),
        MarketEvent::Trade(t) => serde_json::json!({
            "kind": "trade",
            "instrument": t.instrument_id.to_string(),
            "px": t.price.as_f64(),
            "qty": t.size.as_f64(),
            "ts_event": t.ts_event.as_u64(),
        }),
        MarketEvent::Bar(b) => serde_json::json!({
            "kind": "bar",
            "instrument": b.bar_type.instrument_id.to_string(),
            "open": b.open.as_f64(),
            "high": b.high.as_f64(),
            "low": b.low.as_f64(),
            "close": b.close.as_f64(),
            "volume": b.volume.as_f64(),
            "ts_event": b.ts_event.as_u64(),
        }),
        MarketEvent::Delta(d) => serde_json::json!({
            "kind": "delta",
            "instrument": d.instrument_id.to_string(),
            "px": d.price.as_f64(),
            "sz": d.size.as_f64(),
            "seq": d.sequence,
            "ts_event": d.ts_event.as_u64(),
        }),
    };
    serde_json::to_string(&v).map_err(|e| e.to_string())
}

fn event_payload(ev: &MarketEvent) -> Result<(MsgType, Vec<u8>), String> {
    let (msg_type, v) = match ev {
        MarketEvent::Quote(q) => (
            MsgType::Quote,
            serde_json::json!({
                "instrument": q.instrument_id.to_string(),
                "bid": q.bid.as_f64(),
                "ask": q.ask.as_f64(),
                "bid_sz": q.bid_size.as_f64(),
                "ask_sz": q.ask_size.as_f64(),
                "ts_event": q.ts_event.as_u64(),
            }),
        ),
        MarketEvent::Trade(t) => (
            MsgType::Trade,
            serde_json::json!({
                "instrument": t.instrument_id.to_string(),
                "px": t.price.as_f64(),
                "qty": t.size.as_f64(),
                "ts_event": t.ts_event.as_u64(),
            }),
        ),
        MarketEvent::Bar(b) => (
            MsgType::Bar,
            serde_json::json!({
                "instrument": b.bar_type.instrument_id.to_string(),
                "open": b.open.as_f64(),
                "high": b.high.as_f64(),
                "low": b.low.as_f64(),
                "close": b.close.as_f64(),
                "volume": b.volume.as_f64(),
                "ts_event": b.ts_event.as_u64(),
            }),
        ),
        MarketEvent::Delta(d) => (
            MsgType::Delta,
            serde_json::json!({
                "instrument": d.instrument_id.to_string(),
                "px": d.price.as_f64(),
                "sz": d.size.as_f64(),
                "seq": d.sequence,
                "ts_event": d.ts_event.as_u64(),
            }),
        ),
    };
    // Payload is MessagePack map (Python decode_payload expects msgpack map).
    let payload = rmp_serde::to_vec_named(&v).map_err(|e| e.to_string())?;
    Ok((msg_type, payload))
}

/// MessagePack frame: 5-element array matching Python `encode_envelope`.
fn encode_envelope_msgpack(env: &Envelope) -> Result<Vec<u8>, String> {
    let arr = (
        env.schema_version,
        env.msg_type,
        env.trace_id.as_slice(),
        env.ts_init,
        env.payload.as_slice(),
    );
    rmp_serde::to_vec(&arr).map_err(|e| e.to_string())
}

#[cfg(test)]
mod gap_tests {
    use super::*;

    #[test]
    fn book_gap_tracker_detects_jump() {
        let mut t = BookGapTracker::default();
        assert!(!t.observe("BTC", 100));
        assert!(!t.observe("BTC", 100)); // same event multi-level
        assert!(!t.observe("BTC", 101));
        assert!(t.observe("BTC", 105)); // gap 102-104
        assert!(!t.observe("ETH", 1));
    }
}

/// Offline smoke: process synthetic events through sinks (no network).
pub fn process_offline_smoke(sinks: &mut IngestSinks, n: usize) {
    use coinext_core::{Price, Quantity};
    use coinext_model::{
        AggregationSource, BarAggregation, BarSpec, BarType, InstrumentId, PriceType, QuoteTick,
        TradeTick,
    };
    use rust_decimal_macros::dec;

    let iid = InstrumentId::parse("BTCUSDT.BINANCE").expect("iid");
    for i in 0..n {
        let ts = UnixNanos(1_700_000_000_000_000_000 + i as u64 * 60_000_000_000);
        let px = 50_000.0 + i as f64;
        let quote = QuoteTick {
            instrument_id: iid.clone(),
            bid: Price::from_f64(px - 0.5, 2).unwrap(),
            ask: Price::from_f64(px + 0.5, 2).unwrap(),
            bid_size: Quantity::from_f64(1.0, 3).unwrap(),
            ask_size: Quantity::from_f64(1.0, 3).unwrap(),
            ts_event: ts,
            ts_init: ts,
        };
        sinks.handle(&MarketEvent::Quote(quote));
        let trade = TradeTick {
            instrument_id: iid.clone(),
            price: Price::from_f64(px, 2).unwrap(),
            size: Quantity::from_f64(0.01, 3).unwrap(),
            aggressor: coinext_model::OrderSide::Buy,
            trade_id: coinext_model::TradeId::from(format!("T-{i}")),
            ts_event: ts,
            ts_init: ts,
        };
        sinks.handle(&MarketEvent::Trade(trade));
        let bar = Bar {
            bar_type: BarType {
                instrument_id: iid.clone(),
                spec: BarSpec {
                    step: 1,
                    aggregation: BarAggregation::Minute,
                    price_type: PriceType::Last,
                },
                source: AggregationSource::External,
            },
            open: Price::from_decimal(dec!(50000), 2).unwrap(),
            high: Price::from_decimal(dec!(50100), 2).unwrap(),
            low: Price::from_decimal(dec!(49900), 2).unwrap(),
            close: Price::from_f64(px, 2).unwrap(),
            volume: Quantity::from_f64(10.0, 3).unwrap(),
            ts_event: ts,
            ts_init: ts,
        };
        sinks.handle(&MarketEvent::Bar(bar));
    }
    sinks.flush();
}

#[allow(dead_code)]
fn _path_exists(p: &Path) -> bool {
    p.exists()
}
