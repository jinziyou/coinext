//! Runnable example: an SMA-crossover strategy through the Coinext backtest kernel.
//!
//! Demonstrates the whole pure-Rust pipeline: synthetic bars → Strategy (native Rust, no GIL) →
//! pre-trade risk gate → SimulatedExecutionClient with delayed fills → Position/PnL → tear sheet.
//! The SAME `Strategy` trait is what a Python strategy implements (bridged by coinext-py) and what runs
//! live — only the Clock and Data/Execution clients differ.

use coinext_core::{Currency, Money, Quantity};
use coinext_indicators::{Indicator, Sma};
use coinext_kernel::{BacktestConfig, BacktestKernel};
use coinext_model::{Bar, InstrumentId, OrderSide, StrategyId, Venue};
use coinext_ports::{Strategy, StrategyContext};
use rust_decimal::Decimal;

/// Classic SMA crossover: go long when the fast SMA crosses above the slow SMA, flatten when it
/// crosses back below.
struct SmaCross {
    iid: InstrumentId,
    fast: Sma,
    slow: Sma,
    prev_fast: Option<f64>,
    prev_slow: Option<f64>,
    in_position: bool,
    qty: Quantity,
}

impl SmaCross {
    fn new(iid: InstrumentId, fast: usize, slow: usize, qty: Quantity) -> Self {
        SmaCross {
            iid,
            fast: Sma::new(fast),
            slow: Sma::new(slow),
            prev_fast: None,
            prev_slow: None,
            in_position: false,
            qty,
        }
    }
}

impl Strategy for SmaCross {
    fn on_bar(&mut self, bar: &Bar, ctx: &mut StrategyContext) {
        let close = bar.close.as_f64();
        self.fast.update(close);
        self.slow.update(close);

        if let (Some(f), Some(s)) = (self.fast.value(), self.slow.value()) {
            if let (Some(pf), Some(ps)) = (self.prev_fast, self.prev_slow) {
                let cross_up = pf <= ps && f > s;
                let cross_down = pf >= ps && f < s;
                if cross_up && !self.in_position {
                    ctx.submit_market(self.iid.clone(), OrderSide::Buy, self.qty);
                    self.in_position = true;
                } else if cross_down && self.in_position {
                    ctx.submit_market(self.iid.clone(), OrderSide::Sell, self.qty);
                    self.in_position = false;
                }
            }
            self.prev_fast = Some(f);
            self.prev_slow = Some(s);
        }
    }
}

/// Compute Sharpe (per-step, annualization left to the analytics layer) and max drawdown from an
/// equity curve.
fn quick_metrics(curve: &[(u64, f64)]) -> (f64, f64) {
    if curve.len() < 2 {
        return (0.0, 0.0);
    }
    let rets: Vec<f64> = curve
        .windows(2)
        .map(|w| {
            if w[0].1 != 0.0 {
                (w[1].1 - w[0].1) / w[0].1
            } else {
                0.0
            }
        })
        .collect();
    let mean = rets.iter().sum::<f64>() / rets.len() as f64;
    let var = rets.iter().map(|r| (r - mean).powi(2)).sum::<f64>() / rets.len() as f64;
    let std = var.sqrt();
    let sharpe = if std > 0.0 { mean / std } else { 0.0 };

    let mut peak = curve[0].1;
    let mut max_dd = 0.0;
    for &(_, eq) in curve {
        if eq > peak {
            peak = eq;
        }
        if peak > 0.0 {
            let dd = (peak - eq) / peak;
            if dd > max_dd {
                max_dd = dd;
            }
        }
    }
    (sharpe, max_dd)
}

fn main() {
    let usdt = Currency::new("USDT", 8).unwrap();
    let inst = coinext_testkit::sample_spot("BINANCE");
    let iid = inst.id();

    // A trending + oscillating synthetic series so the crossover actually trades.
    let mut closes = coinext_testkit::sine_closes(400, 50_000.0, 1_500.0, 40);
    for (i, c) in closes.iter_mut().enumerate() {
        *c += i as f64 * 12.0; // gentle uptrend
    }
    let events =
        coinext_testkit::bars_from_closes(&iid, 1_700_000_000_000_000_000, 60_000_000_000, &closes);

    let cfg = BacktestConfig::new(
        Venue::from("BINANCE"),
        vec![inst],
        usdt,
        Money::from_decimal(Decimal::new(100_000, 0), usdt).unwrap(),
    );

    let strategy = Box::new(SmaCross::new(
        iid.clone(),
        10,
        30,
        Quantity::from_f64(0.5, 3).unwrap(),
    ));

    let mut kernel = BacktestKernel::build(cfg, StrategyId::from("sma-cross"), strategy, events);
    let res = kernel.run();
    let (sharpe, max_dd) = quick_metrics(&res.equity_curve);

    println!("================ Coinext backtest: SMA crossover ================");
    println!("instrument        : {iid}");
    println!("bars processed    : {}", res.equity_curve.len());
    println!("orders submitted  : {}", res.orders_submitted);
    println!("orders denied     : {}", res.orders_denied);
    println!("fills             : {}", res.fills);
    println!("starting equity   : {:>14.2} USDT", res.starting_equity);
    println!("final equity      : {:>14.2} USDT", res.final_equity);
    println!(
        "total return      : {:>13.2}%",
        (res.final_equity / res.starting_equity - 1.0) * 100.0
    );
    println!("realized PnL      : {:>14.2} USDT", res.realized_pnl);
    println!("sharpe (per-bar)  : {sharpe:>14.3}");
    println!("max drawdown      : {:>13.2}%", max_dd * 100.0);
    println!("===================================================================");
}
