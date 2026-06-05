//! Binance **SPOT TESTNET** batch order executor — the execution half of the one-command parity
//! loop (`qv testnet-gate`). Reads a list of market orders, places each on testnet (paper), and
//! writes back the REAL fills, so the Python parity gate can diff testnet execution against the
//! deterministic backtest.
//!
//! IO (JSON):
//!   in  (VQ__ORDERS_IN,  default "orders.json"): `[{"side":"buy"|"sell","qty":0.001}, ...]`
//!   out (VQ__FILLS_OUT,  default "fills.json"):   `[{"ts":<ns>,"side":1|-1,"qty":..,"px":..}|{"error":".."}, ...]`
//!
//! Env: VQ__BINANCE__API_KEY / VQ__BINANCE__API_SECRET (spot testnet), VQ__ORDER__SYMBOL
//! (default BTCUSDT.BINANCE). Without keys it writes nothing and exits 2 (safe).
//!
//! Run: `cargo run --manifest-path crates/qv-adapters/binance/Cargo.toml --example testnet_orders`

use qv_adapters_binance::{BinanceConfig, BinanceExecutionClient};
use qv_core::{Clock, Quantity, SystemClock};
use qv_model::{InstrumentId, OrderSide, StrategyId};
use qv_ports::{ExecutionClient, ExecutionReport, OrderFactory, SubmitOrder};
use std::time::Duration;

fn env(key: &str, default: &str) -> String {
    std::env::var(key).unwrap_or_else(|_| default.to_string())
}

async fn wait_fill(
    rx: &mut tokio::sync::mpsc::Receiver<ExecutionReport>,
    secs: u64,
) -> Option<(u64, f64)> {
    // Drain reports until a Fill (or timeout); returns (ts_ns, fill_px).
    loop {
        match tokio::time::timeout(Duration::from_secs(secs), rx.recv()).await {
            Ok(Some(ExecutionReport::Fill(f))) => return Some((f.ts_event.as_u64(), f.last_px.as_f64())),
            Ok(Some(_)) => continue, // Accepted/etc — keep waiting for the Fill
            _ => return None,
        }
    }
}

#[tokio::main]
async fn main() {
    let api_key = std::env::var("VQ__BINANCE__API_KEY").ok().filter(|s| !s.is_empty());
    let api_secret = std::env::var("VQ__BINANCE__API_SECRET").ok().filter(|s| !s.is_empty());
    let symbol = env("VQ__ORDER__SYMBOL", "BTCUSDT.BINANCE");
    let orders_in = env("VQ__ORDERS_IN", "orders.json");
    let fills_out = env("VQ__FILLS_OUT", "fills.json");

    if api_key.is_none() || api_secret.is_none() {
        eprintln!(
            "testnet_orders: missing VQ__BINANCE__API_KEY / VQ__BINANCE__API_SECRET \
             (spot testnet: https://testnet.binance.vision/). Aborting before any network call."
        );
        std::process::exit(2);
    }

    let raw = std::fs::read_to_string(&orders_in)
        .unwrap_or_else(|e| panic!("testnet_orders: cannot read {orders_in}: {e}"));
    let orders: Vec<serde_json::Value> =
        serde_json::from_str(&raw).expect("testnet_orders: orders.json must be a JSON array");

    let Some(iid) = InstrumentId::parse(&symbol) else {
        eprintln!("bad symbol {symbol}");
        std::process::exit(1);
    };
    let cfg = BinanceConfig { api_key, api_secret, testnet: true };
    let mut exec = BinanceExecutionClient::new(cfg).expect("build exec client");
    let mut reports = exec.take_reports();
    if let Err(e) = exec.connect().await {
        eprintln!("testnet_orders: connect failed: {e}");
        std::process::exit(1);
    }
    eprintln!("testnet_orders: connected; placing {} market order(s) on testnet…", orders.len());

    let mut factory = OrderFactory::new(StrategyId::from("testnet-gate"));
    let mut results: Vec<serde_json::Value> = Vec::with_capacity(orders.len());

    for (i, o) in orders.iter().enumerate() {
        let side_str = o.get("side").and_then(|v| v.as_str()).unwrap_or("buy");
        let qty_f = o.get("qty").and_then(|v| v.as_f64()).unwrap_or(0.001);
        let side = if side_str.eq_ignore_ascii_case("sell") { OrderSide::Sell } else { OrderSide::Buy };
        let qty = match Quantity::from_f64(qty_f, 5) {
            Ok(q) => q,
            Err(e) => {
                results.push(serde_json::json!({"error": format!("bad qty: {e}")}));
                continue;
            }
        };
        let now = SystemClock::new().now_ns();
        let order = factory.market(iid.clone(), side, qty, now);
        let coid = order.client_order_id.clone();
        if let Err(e) = exec.submit_order(SubmitOrder { order }).await {
            eprintln!("  order {i} ({side_str} {qty_f}) submit failed: {e}");
            results.push(serde_json::json!({"error": format!("submit: {e}")}));
            continue;
        }
        match wait_fill(&mut reports, 8).await {
            Some((ts, px)) => {
                eprintln!("  order {i} ({side_str} {qty_f}) filled px={px} coid={coid}");
                let sign = if side == OrderSide::Sell { -1 } else { 1 };
                results.push(serde_json::json!({"ts": ts, "side": sign, "qty": qty_f, "px": px}));
            }
            None => {
                eprintln!("  order {i} ({side_str} {qty_f}) no fill within 8s");
                results.push(serde_json::json!({"error": "no fill within 8s"}));
            }
        }
    }

    let _ = exec.disconnect().await;
    std::fs::write(&fills_out, serde_json::to_string_pretty(&results).unwrap())
        .unwrap_or_else(|e| panic!("testnet_orders: cannot write {fills_out}: {e}"));
    eprintln!("testnet_orders: wrote {} fill record(s) -> {fills_out}", results.len());
}
