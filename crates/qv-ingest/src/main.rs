//! `qv-ingest` — the standalone market-data **ingestion daemon** (service name `ingestor`).
//!
//! Per the architecture (`docs/ARCHITECTURE.md` §7, "Live"): "A standalone Rust `ingestor`
//! normalizes Binance WS frames and republishes on the Redis bus; the `trader` process's DataEngine
//! consumes them." It also persists normalized data to the data lake so warm-up/backtest read the
//! SAME bytes (the parity invariant for indicators).
//!
//! This wires a real [`BinanceDataClient`] against the PUBLIC combined market-data streams (no API
//! keys), takes its `MarketEvent` receiver via `take_stream`, and drains it in a tokio loop printing
//! each normalized event. The actual WS connect is gated behind the `live` cargo feature so the
//! binary always compiles (and the default run exits cleanly) without touching the network; run the
//! live path with `cargo run -p qv-ingest --features live`.
//!
//! Config (env, `VQ__` convention):
//!   VQ__BINANCE__TESTNET   "true"/"false" — which public streams to read (default false = mainnet,
//!                          which is the sandbox design: REAL market data, testnet PAPER execution).
//!   VQ__INGEST__SYMBOLS    comma-separated InstrumentIds (default "BTCUSDT.BINANCE,ETHUSDT.BINANCE").
//!   VQ__INGEST__MAX_EVENTS bounded demo: exit after N events (default 0 = run forever).

use qv_adapters_binance::{BinanceConfig, BinanceDataClient};
use qv_model::{InstrumentId, MarketEvent};
use qv_ports::{DataClient, SubKind, Subscription};

fn env_or(key: &str, default: &str) -> String {
    std::env::var(key).unwrap_or_else(|_| default.to_string())
}

#[tokio::main]
async fn main() {
    let testnet = env_or("VQ__BINANCE__TESTNET", "false").eq_ignore_ascii_case("true");
    let symbols: Vec<String> = env_or("VQ__INGEST__SYMBOLS", "BTCUSDT.BINANCE,ETHUSDT.BINANCE")
        .split(',')
        .map(|s| s.trim().to_string())
        .filter(|s| !s.is_empty())
        .collect();
    let max_events: u64 = env_or("VQ__INGEST__MAX_EVENTS", "0").parse().unwrap_or(0);

    println!("=========================================================");
    println!("  VeloxQuant ingestor (qv-ingest)");
    println!("  role    : market-data ingestion daemon");
    println!("  source  : Binance public WS ({})", if testnet { "TESTNET" } else { "MAINNET" });
    println!("  symbols : {}", symbols.join(", "));
    println!(
        "  max ev  : {}",
        if max_events == 0 { "infinite".to_string() } else { max_events.to_string() }
    );
    println!("  metrics : http://0.0.0.0:9101/metrics  (TODO)");
    println!("=========================================================");

    // Public market-data needs no credentials.
    let config = BinanceConfig::public(testnet);
    let mut client = match BinanceDataClient::new(config) {
        Ok(c) => c,
        Err(e) => {
            eprintln!("qv-ingest: failed to build BinanceDataClient: {e}");
            std::process::exit(1);
        }
    };

    // Register subscriptions: trades, bookTicker (quotes), and depth (book) for each symbol.
    for sym in &symbols {
        let Some(id) = InstrumentId::parse(sym) else {
            eprintln!("qv-ingest: bad instrument id `{sym}` — skipping");
            continue;
        };
        for kind in [SubKind::Trades, SubKind::Quotes, SubKind::BookL2 { depth: 20 }] {
            let sub = Subscription { instrument_id: id.clone(), kind };
            if let Err(e) = client.subscribe(sub).await {
                eprintln!("qv-ingest: subscribe failed for {sym}: {e}");
            }
        }
    }

    // Take the normalized event stream exactly once (the port contract).
    let rx = client.take_stream();
    run(client, rx, symbols.len(), max_events).await;
}

/// Drain the normalized `MarketEvent` stream and print each event. The connect step (which opens
/// the live WS) only runs under the `live` feature so the default build is fully offline.
async fn run(
    mut client: BinanceDataClient,
    mut rx: tokio::sync::mpsc::Receiver<MarketEvent>,
    n_symbols: usize,
    max_events: u64,
) {
    #[cfg(feature = "live")]
    {
        if let Err(e) = client.connect().await {
            eprintln!("qv-ingest: connect failed: {e}");
            return;
        }
        println!("qv-ingest: connected; streaming normalized events (Ctrl-C to stop)...");
        let mut count: u64 = 0;
        while let Some(ev) = rx.recv().await {
            print_event(&ev);
            count += 1;
            if max_events != 0 && count >= max_events {
                println!("qv-ingest: reached max_events={max_events}, stopping.");
                break;
            }
            // TODO(ingest): append to the data lake (qv_persistence::ParquetWriter) and XADD a
            // versioned MessagePack Envelope to the Redis stream (qv_bus); record
            // `ingest_to_publish_ns`; increment `book_gaps`/`ws_reconnects` on resync events.
        }
        let _ = client.disconnect().await;
    }

    #[cfg(not(feature = "live"))]
    {
        let _ = (&mut client, max_events);
        while let Ok(ev) = rx.try_recv() {
            print_event(&ev);
        }
        println!(
            "qv-ingest: built client + {} subscriptions and took the event stream; \
             live WS connect is gated behind `--features live` (offline build), exiting.",
            n_symbols * 3
        );
    }
    let _ = n_symbols;
}

/// Print a single normalized event in a compact, human-readable form.
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
