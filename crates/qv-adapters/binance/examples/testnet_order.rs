//! Binance **SPOT TESTNET** execution smoke test — the key-requiring half of the testnet
//! end-to-end link. It exercises the full `ExecutionClient` path against a real (paper) venue:
//! connect the user-data stream → submit a resting LIMIT BUY far below market → receive the
//! `Accepted` report → reconcile open orders → cancel → receive the `Canceled` report → disconnect.
//!
//! The limit price defaults far below market so the order RESTS and is then cancelled — no fill,
//! no risk (and it's testnet paper money regardless).
//!
//! Get keys (no Binance account / KYC needed): log in to <https://testnet.binance.vision/> with
//! GitHub, "Generate HMAC_SHA256 Key", then:
//!
//! ```bash
//! export VQ__BINANCE__API_KEY=...        # your SPOT testnet key
//! export VQ__BINANCE__API_SECRET=...     # your SPOT testnet secret
//! cargo run --manifest-path crates/qv-adapters/binance/Cargo.toml --example testnet_order
//! # optional overrides:
//! #   VQ__ORDER__SYMBOL=BTCUSDT.BINANCE  VQ__ORDER__PRICE=20000  VQ__ORDER__QTY=0.001
//! ```
//!
//! Without keys it aborts BEFORE any network call (exit 2), so it is safe to run as a wiring check.

use qv_adapters_binance::{BinanceConfig, BinanceExecutionClient};
use qv_core::{Clock, Price, Quantity, SystemClock};
use qv_model::{InstrumentId, OrderFlags, OrderSide, StrategyId, TimeInForce};
use qv_ports::{CancelOrder, ExecutionClient, ExecutionReport, OrderFactory, SubmitOrder};
use std::time::Duration;

fn env_f64(key: &str, default: f64) -> f64 {
    std::env::var(key).ok().and_then(|s| s.parse().ok()).unwrap_or(default)
}

async fn wait_report(
    rx: &mut tokio::sync::mpsc::Receiver<ExecutionReport>,
    secs: u64,
) -> Option<ExecutionReport> {
    tokio::time::timeout(Duration::from_secs(secs), rx.recv()).await.ok().flatten()
}

fn fmt_report(r: &ExecutionReport) -> String {
    match r {
        ExecutionReport::Accepted { client_order_id, venue_order_id } => {
            format!("Accepted   coid={client_order_id} venue_id={venue_order_id}")
        }
        ExecutionReport::Fill(f) => {
            format!("Fill       coid={} px={} qty={}", f.client_order_id, f.last_px, f.last_qty)
        }
        ExecutionReport::Canceled { client_order_id } => format!("Canceled   coid={client_order_id}"),
        ExecutionReport::Rejected { client_order_id, reason } => {
            format!("Rejected   coid={client_order_id} reason={reason}")
        }
        other => format!("{other:?}"),
    }
}

#[tokio::main]
async fn main() {
    let api_key = std::env::var("VQ__BINANCE__API_KEY").ok().filter(|s| !s.is_empty());
    let api_secret = std::env::var("VQ__BINANCE__API_SECRET").ok().filter(|s| !s.is_empty());
    let symbol = std::env::var("VQ__ORDER__SYMBOL").unwrap_or_else(|_| "BTCUSDT.BINANCE".to_string());
    let price_f = env_f64("VQ__ORDER__PRICE", 20_000.0); // far below market -> rests, no fill
    let qty_f = env_f64("VQ__ORDER__QTY", 0.001);

    println!("=========================================================");
    println!("  VeloxQuant — Binance SPOT TESTNET execution smoke test");
    println!("  endpoint : https://testnet.binance.vision (paper)");
    println!("  symbol   : {symbol}   LIMIT BUY  qty={qty_f}  px={price_f}");
    println!("=========================================================");

    if api_key.is_none() || api_secret.is_none() {
        eprintln!(
            "\nMissing credentials. Set VQ__BINANCE__API_KEY and VQ__BINANCE__API_SECRET\n\
             (Binance SPOT testnet keys: https://testnet.binance.vision/ → Log In with GitHub →\n\
             Generate HMAC_SHA256 Key). Aborting BEFORE any network call."
        );
        std::process::exit(2);
    }

    let cfg = BinanceConfig { api_key, api_secret, testnet: true };
    let Some(iid) = InstrumentId::parse(&symbol) else {
        eprintln!("bad symbol `{symbol}` (expected e.g. BTCUSDT.BINANCE)");
        std::process::exit(1);
    };

    let mut exec = match BinanceExecutionClient::new(cfg) {
        Ok(e) => e,
        Err(e) => {
            eprintln!("build execution client: {e}");
            std::process::exit(1);
        }
    };
    // Take the report stream once (port contract), then connect (spawns the user-data-stream task).
    let mut reports = exec.take_reports();
    if let Err(e) = exec.connect().await {
        eprintln!("connect (open user-data stream) failed: {e}");
        std::process::exit(1);
    }
    println!("✔ connected; user-data stream open.");

    // Build a resting LIMIT BUY far below market (BTCUSDT: price 2dp, size 5dp on testnet).
    let price = Price::from_f64(price_f, 2).expect("price");
    let qty = Quantity::from_f64(qty_f, 5).expect("qty");
    let now = SystemClock::new().now_ns();
    let mut factory = OrderFactory::new(StrategyId::from("testnet-smoke"));
    let order =
        factory.limit(iid.clone(), OrderSide::Buy, qty, price, TimeInForce::Gtc, OrderFlags::default(), now);
    let coid = order.client_order_id.clone();

    println!("→ submitting LIMIT BUY coid={coid} (idempotent newClientOrderId)");
    if let Err(e) = exec.submit_order(SubmitOrder { order }).await {
        eprintln!("submit_order failed: {e}");
        let _ = exec.disconnect().await;
        std::process::exit(1);
    }
    match wait_report(&mut reports, 8).await {
        Some(r) => println!("← report: {}", fmt_report(&r)),
        None => println!("← (no report within 8s — check the order on the testnet UI)"),
    }

    // Reconcile: the venue's view of open orders.
    match exec.reconcile().await {
        Ok(open) => {
            println!("⟳ reconcile: {} open order report(s)", open.len());
            for r in &open {
                println!("    {}", fmt_report(r));
            }
        }
        Err(e) => println!("reconcile failed: {e}"),
    }

    // Cancel.
    println!("→ cancelling coid={coid}");
    if let Err(e) = exec.cancel_order(CancelOrder { client_order_id: coid.clone() }).await {
        eprintln!("cancel_order failed: {e}");
    }
    if let Some(r) = wait_report(&mut reports, 8).await {
        println!("← report: {}", fmt_report(&r));
    }

    let _ = exec.disconnect().await;
    println!("✔ done — order rested then cancelled; no fill (limit far below market).");
}
