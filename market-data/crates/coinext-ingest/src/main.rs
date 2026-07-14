//! `coinext-ingest` — market-data **ingestion daemon** (service name `ingestor`).
//!
//! Status: **partial**.
//!
//! * Connects (feature `live`) to Binance public WS via [`BinanceDataClient`], normalizes frames
//!   to [`MarketEvent`].
//! * **Always** runs sinks: NDJSON event log, bar Parquet under `COINEXT__DATA__LAKE_ROOT`, optional
//!   Redis Streams Envelope XADD on `coinext.market`.
//! * Offline (default build): processes synthetic smoke events through the same sinks so lake/redis
//!   wiring is testable without network.
//!
//! Env (`COINEXT__` convention):
//! - `COINEXT__BINANCE__TESTNET` — public streams endpoint (default false = mainnet MD)
//! - `COINEXT__INGEST__SYMBOLS` — comma InstrumentIds
//! - `COINEXT__INGEST__MAX_EVENTS` — exit after N events (0 = forever)
//! - `COINEXT__DATA__LAKE_ROOT` — Parquet lake root (Hive bars/…)
//! - `COINEXT__INGEST__EVENT_LOG` — NDJSON path (default `{lake}/ingest/events.ndjson`)
//! - `COINEXT__REDIS__URL` — if set, XADD envelopes to `coinext.market`
//! - `COINEXT__INGEST__SMOKE_N` — offline synthetic event count (default 8)

mod sinks;

use coinext_adapters_binance::{BinanceConfig, BinanceDataClient};
use coinext_model::{InstrumentId, MarketEvent};
use coinext_ports::{DataClient, SubKind, Subscription};
use sinks::{IngestMetrics, IngestSinks};
use std::net::SocketAddr;
use std::sync::atomic::Ordering;
use std::sync::Arc;
use tokio::io::AsyncWriteExt;
use tokio::net::TcpListener;

fn env_or(key: &str, default: &str) -> String {
    std::env::var(key).unwrap_or_else(|_| default.to_string())
}

async fn serve_metrics(addr: SocketAddr, metrics: Arc<IngestMetrics>) {
    let listener = match TcpListener::bind(addr).await {
        Ok(l) => l,
        Err(e) => {
            eprintln!("coinext-ingest: metrics bind {addr} failed: {e}");
            return;
        }
    };
    loop {
        let Ok((mut sock, _)) = listener.accept().await else {
            continue;
        };
        let body = format!(
            "# HELP coinext_ingest_up 1 if process is serving\n\
             # TYPE coinext_ingest_up gauge\n\
             coinext_ingest_up 1\n\
             # HELP coinext_ingest_events_logged_total NDJSON events written\n\
             # TYPE coinext_ingest_events_logged_total counter\n\
             coinext_ingest_events_logged_total {}\n\
             # HELP coinext_ingest_bars_flushed_total bars merged into lake Parquet\n\
             # TYPE coinext_ingest_bars_flushed_total counter\n\
             coinext_ingest_bars_flushed_total {}\n\
             # HELP coinext_ingest_redis_published_total market envelopes published\n\
             # TYPE coinext_ingest_redis_published_total counter\n\
             coinext_ingest_redis_published_total {}\n\
             # HELP coinext_ingest_redis_errors_total redis publish failures\n\
             # TYPE coinext_ingest_redis_errors_total counter\n\
             coinext_ingest_redis_errors_total {}\n\
             # HELP coinext_ingest_ws_connects_total successful WS connects\n\
             # TYPE coinext_ingest_ws_connects_total counter\n\
             coinext_ingest_ws_connects_total {}\n\
             # HELP coinext_ingest_ws_reconnects_total reconnect attempts after failure\n\
             # TYPE coinext_ingest_ws_reconnects_total counter\n\
             coinext_ingest_ws_reconnects_total {}\n\
             # HELP coinext_ingest_ws_errors_total connect/stream errors\n\
             # TYPE coinext_ingest_ws_errors_total counter\n\
             coinext_ingest_ws_errors_total {}\n\
             # HELP coinext_ingest_book_gaps_total depth sequence gaps observed\n\
             # TYPE coinext_ingest_book_gaps_total counter\n\
             coinext_ingest_book_gaps_total {}\n",
            metrics.events_logged.load(Ordering::Relaxed),
            metrics.bars_flushed.load(Ordering::Relaxed),
            metrics.redis_published.load(Ordering::Relaxed),
            metrics.redis_errors.load(Ordering::Relaxed),
            metrics.ws_connects.load(Ordering::Relaxed),
            metrics.ws_reconnects.load(Ordering::Relaxed),
            metrics.ws_errors.load(Ordering::Relaxed),
            metrics.book_gaps.load(Ordering::Relaxed),
        );
        let resp = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: text/plain; version=0.0.4\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        );
        let _ = sock.write_all(resp.as_bytes()).await;
    }
}

#[tokio::main]
async fn main() {
    let testnet = env_or("COINEXT__BINANCE__TESTNET", "false").eq_ignore_ascii_case("true");
    let symbols: Vec<String> = env_or(
        "COINEXT__INGEST__SYMBOLS",
        "BTCUSDT.BINANCE,ETHUSDT.BINANCE",
    )
    .split(',')
    .map(|s| s.trim().to_string())
    .filter(|s| !s.is_empty())
    .collect();
    let max_events: u64 = env_or("COINEXT__INGEST__MAX_EVENTS", "0")
        .parse()
        .unwrap_or(0);
    let metrics_addr: SocketAddr = env_or("COINEXT__INGEST__METRICS_ADDR", "0.0.0.0:9101")
        .parse()
        .unwrap_or_else(|_| "0.0.0.0:9101".parse().unwrap());

    let mut sinks = IngestSinks::from_env();
    let metrics = sinks.metrics_handle();
    let metrics_task = tokio::spawn(serve_metrics(metrics_addr, metrics));

    println!("=========================================================");
    println!("  Coinext ingestor (coinext-ingest)  [PARTIAL]");
    println!("  role    : market-data ingestion daemon");
    println!(
        "  source  : Binance public WS ({})",
        if testnet { "TESTNET" } else { "MAINNET" }
    );
    println!("  symbols : {}", symbols.join(", "));
    println!(
        "  max ev  : {}",
        if max_events == 0 {
            "infinite".to_string()
        } else {
            max_events.to_string()
        }
    );
    println!(
        "  lake    : {}",
        std::env::var("COINEXT__DATA__LAKE_ROOT").unwrap_or_else(|_| "(unset)".into())
    );
    println!(
        "  redis   : {}",
        std::env::var("COINEXT__REDIS__URL").unwrap_or_else(|_| "(unset)".into())
    );
    println!("  metrics : http://{metrics_addr}/metrics");
    println!("=========================================================");

    #[cfg(feature = "live")]
    {
        run_live(&symbols, testnet, max_events, &mut sinks).await;
    }

    #[cfg(not(feature = "live"))]
    {
        let n: usize = env_or("COINEXT__INGEST__SMOKE_N", "8").parse().unwrap_or(8);
        println!(
            "coinext-ingest: offline build — running {n} synthetic smoke events through sinks \
             (enable `--features live` for real WS)."
        );
        // Still build the client + subscriptions so the offline path exercises adapter wiring.
        let config = BinanceConfig::public(testnet);
        match BinanceDataClient::new(config) {
            Ok(mut client) => {
                for sym in &symbols {
                    if let Some(id) = InstrumentId::parse(sym) {
                        for kind in [
                            SubKind::Trades,
                            SubKind::Quotes,
                            SubKind::BookL2 { depth: 20 },
                        ] {
                            let _ = client
                                .subscribe(Subscription {
                                    instrument_id: id.clone(),
                                    kind,
                                })
                                .await;
                        }
                    }
                }
                let _ = client.take_stream();
                println!(
                    "coinext-ingest: adapter ready ({} symbols × 3 subs); processing smoke…",
                    symbols.len()
                );
            }
            Err(e) => eprintln!("coinext-ingest: adapter build failed (continuing smoke): {e}"),
        }
        sinks::process_offline_smoke(&mut sinks, n);
        let (ev, bars, pubn) = sinks.stats();
        println!(
            "coinext-ingest: smoke done — events_logged={ev} bars_flushed={bars} redis_published={pubn}"
        );
        // Brief pause so a concurrent scrape can hit /metrics during smoke tests.
        tokio::time::sleep(std::time::Duration::from_millis(50)).await;
    }
    metrics_task.abort();
}

#[cfg(feature = "live")]
async fn run_live(
    symbols: &[String],
    testnet: bool,
    max_events: u64,
    sinks: &mut IngestSinks,
) {
    let mut count: u64 = 0;
    let mut attempt: u64 = 0;
    loop {
        attempt += 1;
        if attempt > 1 {
            sinks
                .metrics
                .ws_reconnects
                .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            let backoff = std::cmp::min(30, 1u64 << attempt.min(5));
            eprintln!("coinext-ingest: reconnect attempt {attempt} in {backoff}s…");
            tokio::time::sleep(std::time::Duration::from_secs(backoff)).await;
        }

        let config = BinanceConfig::public(testnet);
        let mut client = match BinanceDataClient::new(config) {
            Ok(c) => c,
            Err(e) => {
                sinks
                    .metrics
                    .ws_errors
                    .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                eprintln!("coinext-ingest: failed to build BinanceDataClient: {e}");
                continue;
            }
        };

        for sym in symbols {
            let Some(id) = InstrumentId::parse(sym) else {
                eprintln!("coinext-ingest: bad instrument id `{sym}` — skipping");
                continue;
            };
            for kind in [
                SubKind::Trades,
                SubKind::Quotes,
                SubKind::BookL2 { depth: 20 },
            ] {
                let sub = Subscription {
                    instrument_id: id.clone(),
                    kind,
                };
                if let Err(e) = client.subscribe(sub).await {
                    eprintln!("coinext-ingest: subscribe failed for {sym}: {e}");
                }
            }
        }

        let mut rx = client.take_stream();
        if let Err(e) = client.connect().await {
            sinks
                .metrics
                .ws_errors
                .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            eprintln!("coinext-ingest: connect failed: {e}");
            continue;
        }
        sinks
            .metrics
            .ws_connects
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        println!("coinext-ingest: connected; streaming (Ctrl-C to stop)…");

        let mut stream_ok = true;
        while let Some(ev) = rx.recv().await {
            print_event(&ev);
            sinks.handle(&ev);
            count += 1;
            if max_events != 0 && count >= max_events {
                println!("coinext-ingest: reached max_events={max_events}, stopping.");
                sinks.flush();
                let _ = client.disconnect().await;
                let (ev, bars, pubn) = sinks.stats();
                println!(
                    "coinext-ingest: stopped — events_logged={ev} bars_flushed={bars} redis_published={pubn}"
                );
                return;
            }
            let _ = stream_ok;
        }
        // Stream closed unexpectedly — reconnect.
        sinks
            .metrics
            .ws_errors
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        eprintln!("coinext-ingest: market stream closed; will reconnect");
        let _ = client.disconnect().await;
        stream_ok = false;
        let _ = stream_ok;
    }
}

#[allow(dead_code)]
fn print_event(ev: &MarketEvent) {
    match ev {
        MarketEvent::Trade(t) => println!(
            "TRADE  {} px={} qty={} {:?} ts={}",
            t.instrument_id, t.price, t.size, t.aggressor, t.ts_event
        ),
        MarketEvent::Quote(q) => println!(
            "QUOTE  {} bid={}@{} ask={}@{} ts={}",
            q.instrument_id, q.bid, q.bid_size, q.ask, q.ask_size, q.ts_event
        ),
        MarketEvent::Delta(d) => println!(
            "DELTA  {} {:?} {:?} px={} sz={} seq={}",
            d.instrument_id, d.side, d.action, d.price, d.size, d.sequence
        ),
        MarketEvent::Bar(b) => println!(
            "BAR    {} o={} h={} l={} c={} v={} ts={}",
            b.bar_type.instrument_id, b.open, b.high, b.low, b.close, b.volume, b.ts_event
        ),
    }
}
