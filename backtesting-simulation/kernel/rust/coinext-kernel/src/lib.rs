//! `coinext-kernel` — the single place backtest vs live differs, and the deterministic synchronous core
//! loop. For backtest the [`BacktestKernel`] merge-sorts four event sources by timestamp — incoming
//! market data, due delayed execution reports from the sim's DelayedEventQueue, due timers from the
//! HistoricalClock, and due dated-contract expiry/settlement from the expiry schedule — and
//! dispatches each to the engines and the Strategy SYNCHRONOUSLY.
//!
//! For sandbox/live the [`LiveKernel`] runs the SAME engine set + Strategy, but driven by the
//! `coinext_ports::DataClient`/`ExecutionClient` PORTS (market events + execution reports arrive over
//! `tokio::mpsc` instead of the inherent sim queue). [`Environment`] selects which kernel is used;
//! only the Clock and the Data/Execution clients change — the OMS/Risk/Portfolio/Strategy above the
//! `ExecutionClient` seam are byte-for-byte identical (the parity invariant). NOTE: the live path
//! structurally consumes the ports and folds reports through the shared engines, but end-to-end
//! live/sandbox trading against a real venue (reconnect, reconcile, full trader loop) is not yet
//! the default verified path.


mod backtest;
mod live;
mod paper;
mod types;

pub use backtest::BacktestKernel;
pub use live::LiveKernel;
pub use paper::{PaperFillExec, ReplayDataClient};
pub use types::{
    BacktestConfig, Environment, LiveKernelStopHandle, PortfolioSnapshot, PositionSnapshot,
    RunResult,
};

pub(crate) use types::snapshot_portfolio;

#[cfg(test)]
mod tests {
    use super::*;
    use coinext_core::{Currency, Money, Price, Quantity, UnixNanos};
    use coinext_model::{
        AggregationSource, Bar, BarAggregation, BarSpec, BarType, BookAction, CurrencyPair,
        FuturesContract, Instrument, InstrumentId, MarketEvent, OptionContract, OptionRight,
        OrderBookDelta, OrderSide, PositionSide, StrategyId, Venue, PriceType,
    };
    use coinext_ports::{Strategy, StrategyContext};
    use rust_decimal_macros::dec;
    use std::cell::RefCell;
    use std::rc::Rc;
    use std::sync::Arc;

    struct BuyOnceStrategy {
        iid: InstrumentId,
        bought: bool,
    }
    impl Strategy for BuyOnceStrategy {
        fn on_bar(&mut self, _b: &Bar, ctx: &mut StrategyContext) {
            if !self.bought {
                self.bought = true;
                ctx.submit_market(
                    self.iid.clone(),
                    OrderSide::Buy,
                    Quantity::from_decimal(dec!(1), 3).unwrap(),
                );
            }
        }
    }

    /// Buys one contract of a SPECIFIC instrument on that instrument's first bar (so its mark is set
    /// before the market order, even when other instruments share the timestamp).
    struct BuyContractOnce {
        target: InstrumentId,
        bought: bool,
    }
    impl Strategy for BuyContractOnce {
        fn on_bar(&mut self, b: &Bar, ctx: &mut StrategyContext) {
            if !self.bought && b.bar_type.instrument_id == self.target {
                self.bought = true;
                ctx.submit_market(
                    self.target.clone(),
                    OrderSide::Buy,
                    Quantity::from_decimal(dec!(1), 3).unwrap(),
                );
            }
        }
    }

    /// Buys one contract whenever flat, up to `max_buys` times — used to prove liquidation re-arms.
    struct BuyWhenFlat {
        iid: InstrumentId,
        buys: usize,
        max_buys: usize,
    }
    impl Strategy for BuyWhenFlat {
        fn on_bar(&mut self, _b: &Bar, ctx: &mut StrategyContext) {
            let flat = ctx
                .position(&self.iid)
                .map(|p| p.side == PositionSide::Flat)
                .unwrap_or(true);
            if flat && self.buys < self.max_buys {
                self.buys += 1;
                ctx.submit_market(
                    self.iid.clone(),
                    OrderSide::Buy,
                    Quantity::from_decimal(dec!(1), 3).unwrap(),
                );
            }
        }
    }

    fn opt_inst(
        strike: &str,
        right: OptionRight,
        expiry: u64,
        under: InstrumentId,
    ) -> Arc<dyn Instrument> {
        let usdt = Currency::new("USDT", 8).unwrap();
        Arc::new(OptionContract {
            id: InstrumentId::parse("BTCC.DERIBIT").unwrap(),
            underlying: under,
            quote: usdt,
            settlement: usdt,
            price_precision: 2,
            size_precision: 3,
            price_increment: Price::from_decimal(dec!(0.01), 2).unwrap(),
            size_increment: Quantity::from_decimal(dec!(0.001), 3).unwrap(),
            min_notional: None,
            multiplier: Quantity::from_raw(1, 0).unwrap(),
            maker_fee: dec!(0), // zero fees so settlement PnL is exact
            taker_fee: dec!(0),
            strike: Price::from_decimal(strike.parse().unwrap(), 2).unwrap(),
            right,
            expiry_ns: UnixNanos(expiry),
        })
    }

    fn fut_inst(expiry: u64) -> Arc<dyn Instrument> {
        let usdt = Currency::new("USDT", 8).unwrap();
        Arc::new(FuturesContract {
            id: InstrumentId::parse("BTCF.BINANCE").unwrap(),
            underlying: None,
            quote: usdt,
            settlement: usdt,
            price_precision: 2,
            size_precision: 3,
            price_increment: Price::from_decimal(dec!(0.01), 2).unwrap(),
            size_increment: Quantity::from_decimal(dec!(0.001), 3).unwrap(),
            min_notional: None,
            multiplier: Quantity::from_raw(1, 0).unwrap(),
            maker_fee: dec!(0),
            taker_fee: dec!(0),
            expiry_ns: UnixNanos(expiry),
        })
    }

    fn cfg(insts: Vec<Arc<dyn Instrument>>, venue: &str) -> BacktestConfig {
        let usdt = Currency::new("USDT", 8).unwrap();
        BacktestConfig::new(
            Venue::from(venue),
            insts,
            usdt,
            Money::from_decimal(dec!(100000), usdt).unwrap(),
        )
    }

    fn inst() -> Arc<dyn Instrument> {
        let usdt = Currency::new("USDT", 8).unwrap();
        let btc = Currency::new("BTC", 8).unwrap();
        Arc::new(CurrencyPair {
            id: InstrumentId::parse("BTCUSDT.BINANCE").unwrap(),
            base: btc,
            quote: usdt,
            price_precision: 2,
            size_precision: 3,
            price_increment: Price::from_decimal(dec!(0.01), 2).unwrap(),
            size_increment: Quantity::from_decimal(dec!(0.001), 3).unwrap(),
            min_notional: None,
            maker_fee: dec!(0.0002),
            taker_fee: dec!(0.0004),
        })
    }

    /// Top-of-book `(best_bid, best_ask)` seen on an `on_book`, shared back to the test via `Rc`.
    type SeenTops = Rc<RefCell<Vec<(Option<f64>, Option<f64>)>>>;

    /// Records the top-of-book it is handed on each `on_book`.
    struct RecordBook {
        seen: SeenTops,
    }
    impl Strategy for RecordBook {
        fn on_book(&mut self, book: &coinext_model::OrderBook, _ctx: &mut StrategyContext) {
            self.seen.borrow_mut().push((
                book.best_bid().map(|(p, _)| p.as_f64()),
                book.best_ask().map(|(p, _)| p.as_f64()),
            ));
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn delta(
        iid: &InstrumentId,
        action: BookAction,
        side: OrderSide,
        px: &str,
        sz: &str,
        seq: u64,
        ts: u64,
    ) -> MarketEvent {
        MarketEvent::Delta(OrderBookDelta {
            instrument_id: iid.clone(),
            action,
            side,
            price: Price::from_decimal(px.parse().unwrap(), 2).unwrap(),
            size: Quantity::from_decimal(sz.parse().unwrap(), 3).unwrap(),
            sequence: seq,
            ts_event: UnixNanos(ts),
            ts_init: UnixNanos(ts),
        })
    }

    fn bar(iid: &InstrumentId, close: &str, ts: u64) -> MarketEvent {
        let p = |s: &str| Price::from_decimal(s.parse().unwrap(), 2).unwrap();
        MarketEvent::Bar(Bar {
            bar_type: BarType {
                instrument_id: iid.clone(),
                spec: BarSpec {
                    step: 1,
                    aggregation: BarAggregation::Minute,
                    price_type: PriceType::Last,
                },
                source: AggregationSource::External,
            },
            open: p(close),
            high: p(close),
            low: p(close),
            close: p(close),
            volume: Quantity::from_decimal(dec!(10), 3).unwrap(),
            ts_event: UnixNanos(ts),
            ts_init: UnixNanos(ts),
        })
    }

    #[test]
    fn backtest_runs_and_fills_market_order() {
        let usdt = Currency::new("USDT", 8).unwrap();
        let i = inst();
        let iid = i.id();
        let cfg = BacktestConfig::new(
            Venue::from("BINANCE"),
            vec![i],
            usdt,
            Money::from_decimal(dec!(100000), usdt).unwrap(),
        );
        let strat = Box::new(BuyOnceStrategy {
            iid: iid.clone(),
            bought: false,
        });
        let events = vec![
            bar(&iid, "50000", 1_000_000_000),
            bar(&iid, "51000", 2_000_000_000),
            bar(&iid, "52000", 3_000_000_000),
        ];
        let mut kernel = BacktestKernel::build(cfg, StrategyId::from("sma"), strat, events);
        let res = kernel.run();
        assert_eq!(res.orders_submitted, 1);
        assert_eq!(res.fills, 1);
        // Bought ~1 BTC at ~50000 then price rose to 52000 -> equity should exceed start.
        assert!(
            res.final_equity > res.starting_equity,
            "equity {} !> {}",
            res.final_equity,
            res.starting_equity
        );
        assert!(!res.equity_curve.is_empty());
    }

    #[test]
    fn strategy_on_book_receives_maintained_l2_book() {
        let usdt = Currency::new("USDT", 8).unwrap();
        let i = inst();
        let iid = i.id();
        let cfg = BacktestConfig::new(
            Venue::from("BINANCE"),
            vec![i],
            usdt,
            Money::from_decimal(dec!(100000), usdt).unwrap(),
        );
        let seen = Rc::new(RefCell::new(Vec::new()));
        let strat = Box::new(RecordBook { seen: seen.clone() });
        // A snapshot boundary (Clear) then a rebuild, then a better bid on the next sequence.
        let events = vec![
            delta(
                &iid,
                BookAction::Clear,
                OrderSide::Buy,
                "0",
                "0",
                100,
                1_000_000_000,
            ),
            delta(
                &iid,
                BookAction::Add,
                OrderSide::Buy,
                "50000",
                "1",
                100,
                1_000_000_000,
            ),
            delta(
                &iid,
                BookAction::Add,
                OrderSide::Sell,
                "50010",
                "1",
                100,
                1_000_000_000,
            ),
            delta(
                &iid,
                BookAction::Add,
                OrderSide::Buy,
                "50005",
                "2",
                101,
                2_000_000_000,
            ),
        ];
        let mut kernel = BacktestKernel::build(cfg, StrategyId::from("book"), strat, events);
        let _ = kernel.run();

        let seen = seen.borrow();
        assert_eq!(seen.len(), 4, "on_book fires once per delta");
        assert_eq!(seen[0], (None, None), "empty book right after Clear");
        assert_eq!(
            seen[2],
            (Some(50000.0), Some(50010.0)),
            "book rebuilt from the snapshot Adds"
        );
        assert_eq!(
            seen[3],
            (Some(50005.0), Some(50010.0)),
            "the better bid becomes the top of book"
        );
    }

    #[test]
    fn option_settles_to_intrinsic_at_expiry_itm() {
        // Buy a 50000 call @ premium 1000; underlying at expiry is 54000 -> intrinsic 4000.
        let under = inst(); // BTCUSDT.BINANCE
        let under_iid = under.id();
        let opt = opt_inst("50000", OptionRight::Call, 2_500_000_000, under_iid.clone());
        let opt_iid = opt.id();
        let strat = Box::new(BuyContractOnce {
            target: opt_iid.clone(),
            bought: false,
        });
        let events = vec![
            bar(&opt_iid, "1000", 1_000_000_000), // premium; strategy decides to buy on this close
            bar(&under_iid, "52000", 1_000_000_000),
            bar(&opt_iid, "1500", 2_000_000_000), // no-look-ahead: the buy fills at THIS bar's open (1500)
            bar(&under_iid, "54000", 2_000_000_000), // last underlying mark before the 2.5e9 expiry
            bar(&under_iid, "55000", 3_000_000_000), // after expiry (option already settled)
        ];
        let mut kernel = BacktestKernel::build(
            cfg(vec![opt, under], "DERIBIT"),
            StrategyId::from("opt"),
            strat,
            events,
        );
        let res = kernel.run();
        assert_eq!(res.fills, 2, "one buy + one settlement fill");
        // No-look-ahead: the buy fills at the next opt bar's open (1500), not the decision close
        // (1000). Settled at intrinsic 4000 -> realized ~2500 (vs ~3000 under the old close-fill).
        assert!(
            (res.realized_pnl - 2500.0).abs() < 1.0,
            "realized {}",
            res.realized_pnl
        );
    }

    #[test]
    fn option_expires_worthless_when_out_of_the_money() {
        // Buy a 50000 call @ premium 1000; underlying stays at 48000 -> intrinsic 0 -> lose premium.
        let under = inst();
        let under_iid = under.id();
        let opt = opt_inst("50000", OptionRight::Call, 2_500_000_000, under_iid.clone());
        let opt_iid = opt.id();
        let strat = Box::new(BuyContractOnce {
            target: opt_iid.clone(),
            bought: false,
        });
        let events = vec![
            bar(&opt_iid, "1000", 1_000_000_000), // decide to buy on this close
            bar(&under_iid, "48000", 1_000_000_000),
            bar(&opt_iid, "1000", 2_000_000_000), // no-look-ahead: the buy fills at THIS open (1000)
            bar(&under_iid, "48000", 2_000_000_000),
        ];
        let mut kernel = BacktestKernel::build(
            cfg(vec![opt, under], "DERIBIT"),
            StrategyId::from("opt"),
            strat,
            events,
        );
        let res = kernel.run();
        assert_eq!(res.fills, 2);
        // Settled worthless (0), bought at ~1000 (next opt bar's open) -> realized ~ -1000.
        assert!(
            (res.realized_pnl + 1000.0).abs() < 1.0,
            "realized {}",
            res.realized_pnl
        );
    }

    #[test]
    fn account_is_liquidated_when_equity_breaches_maintenance() {
        // Start with 10k, buy 1 future @ ~50k (notional 50k). As the price falls the mark-to-market
        // equity drops; at 44k, equity (~4k) < maintenance (gross 44k x 0.1 = 4.4k) -> liquidate.
        let usdt = Currency::new("USDT", 8).unwrap();
        let fut = fut_inst(9_000_000_000_000_000_000); // far expiry -> no settlement during the test
        let fut_iid = fut.id();
        let mut config = BacktestConfig::new(
            Venue::from("BINANCE"),
            vec![fut],
            usdt,
            Money::from_decimal(dec!(10000), usdt).unwrap(),
        );
        config.risk.maintenance_margin_rate = Some(dec!(0.1));
        let strat = Box::new(BuyOnceStrategy {
            iid: fut_iid.clone(),
            bought: false,
        });
        // The price dips to 44k (breaching maintenance) THEN recovers to 50k — but liquidation is
        // irreversible, so the recovery does not save the account. No-look-ahead: the buy decided on
        // bar 1's close fills at bar 2's open (still 50k), so an extra 50k bar precedes the dip.
        let events = vec![
            bar(&fut_iid, "50000", 1_000_000_000), // decide to buy on this close
            bar(&fut_iid, "50000", 2_000_000_000), // entry fills at this open (50k)
            bar(&fut_iid, "44000", 3_000_000_000), // equity ~4k < maint ~4.4k -> liquidated
            bar(&fut_iid, "50000", 4_000_000_000), // recovers, but already flat
        ];
        let mut kernel = BacktestKernel::build(config, StrategyId::from("liq"), strat, events);
        let res = kernel.run();
        assert_eq!(res.fills, 2, "entry + liquidation close");
        assert!(res.realized_pnl < -5000.0, "realized {}", res.realized_pnl);
        assert!(res.final_equity < 5000.0, "final {}", res.final_equity);
    }

    #[test]
    fn liquidation_re_arms_after_a_prior_liquidation() {
        // After the first liquidation the strategy re-buys; the re-opened position must be liquidated
        // AGAIN on the next breach (the old one-shot latch left it unprotected = blow-through).
        let usdt = Currency::new("USDT", 8).unwrap();
        let fut = fut_inst(9_000_000_000_000_000_000);
        let fut_iid = fut.id();
        let mut config = BacktestConfig::new(
            Venue::from("BINANCE"),
            vec![fut],
            usdt,
            Money::from_decimal(dec!(10000), usdt).unwrap(),
        );
        config.risk.maintenance_margin_rate = Some(dec!(0.1));
        let strat = Box::new(BuyWhenFlat {
            iid: fut_iid.clone(),
            buys: 0,
            max_buys: 2,
        });
        // No-look-ahead: each buy decided on a 50k close fills at the NEXT bar's open, so every
        // entry is preceded by a second 50k bar before the dip that liquidates it.
        let events = vec![
            bar(&fut_iid, "50000", 1_000_000_000), // flat -> decide buy #1
            bar(&fut_iid, "50000", 2_000_000_000), // buy #1 fills at this open (50k)
            bar(&fut_iid, "44000", 3_000_000_000), // liquidate #1
            bar(&fut_iid, "50000", 4_000_000_000), // flat -> decide buy #2
            bar(&fut_iid, "50000", 5_000_000_000), // buy #2 fills at this open (50k)
            bar(&fut_iid, "44000", 6_000_000_000), // liquidate #2
        ];
        let mut kernel = BacktestKernel::build(config, StrategyId::from("liq2"), strat, events);
        let res = kernel.run();
        assert!(
            res.fills >= 4,
            "expected >=2 buys + 2 liquidations, got {} fills",
            res.fills
        );
    }

    #[test]
    fn no_liquidation_without_a_maintenance_rate() {
        // The IDENTICAL dip-then-recover, but no maintenance rate -> the position is NOT force-closed:
        // it rides the dip and recovers, settling ~flat at expiry (vs the locked-in loss above).
        let usdt = Currency::new("USDT", 8).unwrap();
        let fut = fut_inst(9_000_000_000_000_000_000); // far expiry
        let fut_iid = fut.id();
        let config = BacktestConfig::new(
            Venue::from("BINANCE"),
            vec![fut],
            usdt,
            Money::from_decimal(dec!(10000), usdt).unwrap(),
        );
        let strat = Box::new(BuyOnceStrategy {
            iid: fut_iid.clone(),
            bought: false,
        });
        let events = vec![
            bar(&fut_iid, "50000", 1_000_000_000),
            bar(&fut_iid, "44000", 2_000_000_000), // would breach, but no maintenance configured
            bar(&fut_iid, "50000", 3_000_000_000), // recovers
        ];
        let mut kernel = BacktestKernel::build(config, StrategyId::from("noliq"), strat, events);
        let res = kernel.run();
        // Survived the dip, recovered, settled ~flat at expiry (only entry slippage lost).
        assert!(res.realized_pnl > -100.0, "realized {}", res.realized_pnl);
        assert!(res.final_equity > 9000.0, "final {}", res.final_equity);
    }

    #[test]
    fn submitting_on_an_expired_contract_is_denied() {
        // The future already expired (500ms) before the first bar (1s); a buy on it is denied, so no
        // post-expiry position can be opened that settlement would then miss.
        let fut = fut_inst(500_000_000);
        let fut_iid = fut.id();
        let strat = Box::new(BuyOnceStrategy {
            iid: fut_iid.clone(),
            bought: false,
        });
        let events = vec![
            bar(&fut_iid, "50000", 1_000_000_000),
            bar(&fut_iid, "51000", 2_000_000_000),
        ];
        let mut kernel = BacktestKernel::build(
            cfg(vec![fut], "BINANCE"),
            StrategyId::from("fut"),
            strat,
            events,
        );
        let res = kernel.run();
        assert_eq!(res.orders_submitted, 1);
        assert_eq!(res.orders_denied, 1);
        assert_eq!(res.fills, 0, "expired-contract order must not fill");
    }

    #[test]
    fn future_cash_settles_to_mark_at_expiry() {
        // Buy a future @ 50000, price rises to 52000 by expiry -> cash-settle realizes ~2000.
        // No-look-ahead: the buy decided on bar 1's close fills at bar 2's open (still 50000), so a
        // third bar is needed to carry the price up to 52000 before the 3.5e9 expiry.
        let fut = fut_inst(3_500_000_000);
        let fut_iid = fut.id();
        let strat = Box::new(BuyOnceStrategy {
            iid: fut_iid.clone(),
            bought: false,
        });
        let events = vec![
            bar(&fut_iid, "50000", 1_000_000_000), // decide to buy on this close
            bar(&fut_iid, "50000", 2_000_000_000), // fills at this open (50000)
            bar(&fut_iid, "52000", 3_000_000_000), // last mark before the 3.5e9 expiry
        ];
        let mut kernel = BacktestKernel::build(
            cfg(vec![fut], "BINANCE"),
            StrategyId::from("fut"),
            strat,
            events,
        );
        let res = kernel.run();
        assert_eq!(res.fills, 2, "one buy + one settlement fill");
        // Settled at mark 52000; bought at ~50000 plus entry slippage -> realized just under 2000.
        assert!(
            (1990.0..2000.0).contains(&res.realized_pnl),
            "realized {}",
            res.realized_pnl
        );
    }

    // FIX 5: engaging the kill-switch on the kernel's RiskGate denies every order, so nothing fills.
    #[test]
    fn kill_switch_denies_all_orders_in_kernel() {
        let usdt = Currency::new("USDT", 8).unwrap();
        let i = inst();
        let iid = i.id();
        let cfg = BacktestConfig::new(
            Venue::from("BINANCE"),
            vec![i],
            usdt,
            Money::from_decimal(dec!(100000), usdt).unwrap(),
        );
        let strat = Box::new(BuyOnceStrategy {
            iid: iid.clone(),
            bought: false,
        });
        let events = vec![
            bar(&iid, "50000", 1_000_000_000),
            bar(&iid, "51000", 2_000_000_000),
            bar(&iid, "52000", 3_000_000_000),
        ];
        let mut kernel = BacktestKernel::build(cfg, StrategyId::from("kill"), strat, events);
        kernel.set_kill_switch(true);
        let res = kernel.run();
        assert_eq!(res.orders_submitted, 1);
        assert_eq!(res.orders_denied, 1, "kill-switch denies the order");
        assert_eq!(res.fills, 0, "no fills while killed");
    }

    // FIX 5: a configured max-order-notional limit (via BacktestConfig.risk) is enforced -> denied.
    #[test]
    fn configured_notional_limit_denies_oversized_order() {
        let usdt = Currency::new("USDT", 8).unwrap();
        let i = inst();
        let iid = i.id();
        let mut cfg = BacktestConfig::new(
            Venue::from("BINANCE"),
            vec![i],
            usdt,
            Money::from_decimal(dec!(100000), usdt).unwrap(),
        );
        // qty 1 @ ~50000 -> notional ~50000 > 40000 cap.
        cfg.risk.max_order_notional = Some(Money::from_decimal(dec!(40000), usdt).unwrap());
        let strat = Box::new(BuyOnceStrategy {
            iid: iid.clone(),
            bought: false,
        });
        let events = vec![
            bar(&iid, "50000", 1_000_000_000),
            bar(&iid, "51000", 2_000_000_000),
        ];
        let mut kernel = BacktestKernel::build(cfg, StrategyId::from("cap"), strat, events);
        let res = kernel.run();
        assert_eq!(res.orders_denied, 1, "over-notional order denied by config");
        assert_eq!(res.fills, 0);
    }

    // ---- LiveKernel (port-driven sandbox/live path) -------------------------------------------

    use coinext_model::{Fill, LiquiditySide, TradeId, VenueOrderId};
    use coinext_ports::{
        CancelOrder, DataClient, ExecutionClient, ExecutionReport, ModifyOrder, PortResult,
        SubmitOrder, Subscription,
    };
    use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering as AtomicOrdering};
    use tokio::sync::mpsc;

    /// A fake DataClient that replays a fixed list of market events, then closes the stream.
    struct ReplayDataClient {
        tx: Option<mpsc::Sender<MarketEvent>>,
        rx: Option<mpsc::Receiver<MarketEvent>>,
        events: Vec<MarketEvent>,
    }
    impl ReplayDataClient {
        fn new(events: Vec<MarketEvent>) -> Self {
            let (tx, rx) = mpsc::channel(64);
            ReplayDataClient {
                tx: Some(tx),
                rx: Some(rx),
                events,
            }
        }
    }
    #[async_trait::async_trait]
    impl DataClient for ReplayDataClient {
        async fn connect(&mut self) -> PortResult<()> {
            // Push all events now; dropping the sender afterwards closes the stream so `run` returns.
            let tx = self.tx.take().expect("connect called twice");
            for ev in self.events.drain(..) {
                tx.send(ev).await.ok();
            }
            Ok(())
        }
        async fn subscribe(&mut self, _sub: Subscription) -> PortResult<()> {
            Ok(())
        }
        async fn unsubscribe(&mut self, _sub: Subscription) -> PortResult<()> {
            Ok(())
        }
        async fn request_bars(
            &self,
            _bar_type: BarType,
            _start: UnixNanos,
            _end: UnixNanos,
        ) -> PortResult<Vec<Bar>> {
            Ok(Vec::new())
        }
        fn take_stream(&mut self) -> mpsc::Receiver<MarketEvent> {
            self.rx.take().expect("take_stream called twice")
        }
        async fn disconnect(&mut self) -> PortResult<()> {
            Ok(())
        }
    }

    /// A fake ExecutionClient that, on each submit, emits an Accepted then a Fill at the order's
    /// quantity and a fixed price — proving the LiveKernel routes intents to the PORT and folds the
    /// returned reports through the SAME OMS. Counts submits so the test can assert the port was used.
    struct InstantFillExec {
        tx: Option<mpsc::Sender<ExecutionReport>>,
        report_tx: mpsc::Sender<ExecutionReport>,
        rx: Option<mpsc::Receiver<ExecutionReport>>,
        submits: std::sync::Arc<AtomicUsize>,
        price: Price,
        settle: Currency,
        trade_seq: AtomicU64,
    }
    impl InstantFillExec {
        fn new(price: Price, settle: Currency, submits: std::sync::Arc<AtomicUsize>) -> Self {
            let (tx, rx) = mpsc::channel(64);
            InstantFillExec {
                report_tx: tx.clone(),
                tx: Some(tx),
                rx: Some(rx),
                submits,
                price,
                settle,
                trade_seq: AtomicU64::new(0),
            }
        }
    }
    #[async_trait::async_trait]
    impl ExecutionClient for InstantFillExec {
        fn venue(&self) -> Venue {
            Venue::from("BINANCE")
        }
        async fn connect(&mut self) -> PortResult<()> {
            // Keep a sender alive on `self` (report_tx); drop the extra so only the held one remains.
            self.tx.take();
            Ok(())
        }
        async fn submit_order(&self, cmd: SubmitOrder) -> PortResult<()> {
            self.submits.fetch_add(1, AtomicOrdering::SeqCst);
            let o = &cmd.order;
            self.report_tx
                .send(ExecutionReport::Accepted {
                    client_order_id: o.client_order_id.clone(),
                    venue_order_id: VenueOrderId::from("V-1"),
                })
                .await
                .ok();
            let seq = self.trade_seq.fetch_add(1, AtomicOrdering::SeqCst) + 1;
            let fill = Fill {
                trade_id: TradeId::from(format!("T-{seq}")),
                client_order_id: o.client_order_id.clone(),
                venue_order_id: VenueOrderId::from("V-1"),
                instrument_id: o.instrument_id.clone(),
                side: o.side,
                last_px: self.price,
                last_qty: o.quantity,
                fee: Money::zero(self.settle),
                liquidity: LiquiditySide::Taker,
                ts_event: UnixNanos(0),
                ts_init: UnixNanos(0),
            };
            self.report_tx.send(ExecutionReport::Fill(fill)).await.ok();
            Ok(())
        }
        async fn cancel_order(&self, _cmd: CancelOrder) -> PortResult<()> {
            Ok(())
        }
        async fn modify_order(&self, _cmd: ModifyOrder) -> PortResult<()> {
            Ok(())
        }
        async fn reconcile(&self) -> PortResult<Vec<ExecutionReport>> {
            Ok(Vec::new())
        }
        fn take_reports(&mut self) -> mpsc::Receiver<ExecutionReport> {
            self.rx.take().expect("take_reports called twice")
        }
        async fn disconnect(&mut self) -> PortResult<()> {
            Ok(())
        }
    }

    /// A fake DataClient whose stream stays open and idle until the live kernel's stop signal wins
    /// the select. Disconnect is counted so the test proves the graceful shutdown path ran.
    struct IdleDataClient {
        rx: Option<mpsc::Receiver<MarketEvent>>,
        _keepalive: mpsc::Sender<MarketEvent>,
        connects: std::sync::Arc<AtomicUsize>,
        disconnects: std::sync::Arc<AtomicUsize>,
        connect_notify: std::sync::Arc<tokio::sync::Notify>,
    }
    impl IdleDataClient {
        fn new(
            connects: std::sync::Arc<AtomicUsize>,
            disconnects: std::sync::Arc<AtomicUsize>,
            connect_notify: std::sync::Arc<tokio::sync::Notify>,
        ) -> Self {
            let (tx, rx) = mpsc::channel(1);
            IdleDataClient {
                rx: Some(rx),
                _keepalive: tx,
                connects,
                disconnects,
                connect_notify,
            }
        }
    }
    #[async_trait::async_trait]
    impl DataClient for IdleDataClient {
        async fn connect(&mut self) -> PortResult<()> {
            self.connects.fetch_add(1, AtomicOrdering::SeqCst);
            self.connect_notify.notify_waiters();
            Ok(())
        }
        async fn subscribe(&mut self, _sub: Subscription) -> PortResult<()> {
            Ok(())
        }
        async fn unsubscribe(&mut self, _sub: Subscription) -> PortResult<()> {
            Ok(())
        }
        async fn request_bars(
            &self,
            _bar_type: BarType,
            _start: UnixNanos,
            _end: UnixNanos,
        ) -> PortResult<Vec<Bar>> {
            Ok(Vec::new())
        }
        fn take_stream(&mut self) -> mpsc::Receiver<MarketEvent> {
            self.rx.take().expect("take_stream called twice")
        }
        async fn disconnect(&mut self) -> PortResult<()> {
            self.disconnects.fetch_add(1, AtomicOrdering::SeqCst);
            Ok(())
        }
    }

    /// A fake ExecutionClient whose report stream stays open and idle; no reports or reconciled
    /// orders are produced, so the only way `run` can finish is the stop handle.
    struct IdleExecClient {
        rx: Option<mpsc::Receiver<ExecutionReport>>,
        _keepalive: mpsc::Sender<ExecutionReport>,
        connects: std::sync::Arc<AtomicUsize>,
        disconnects: std::sync::Arc<AtomicUsize>,
        connect_notify: std::sync::Arc<tokio::sync::Notify>,
    }
    impl IdleExecClient {
        fn new(
            connects: std::sync::Arc<AtomicUsize>,
            disconnects: std::sync::Arc<AtomicUsize>,
            connect_notify: std::sync::Arc<tokio::sync::Notify>,
        ) -> Self {
            let (tx, rx) = mpsc::channel(1);
            IdleExecClient {
                rx: Some(rx),
                _keepalive: tx,
                connects,
                disconnects,
                connect_notify,
            }
        }
    }
    #[async_trait::async_trait]
    impl ExecutionClient for IdleExecClient {
        fn venue(&self) -> Venue {
            Venue::from("BINANCE")
        }
        async fn connect(&mut self) -> PortResult<()> {
            self.connects.fetch_add(1, AtomicOrdering::SeqCst);
            self.connect_notify.notify_waiters();
            Ok(())
        }
        async fn submit_order(&self, _cmd: SubmitOrder) -> PortResult<()> {
            Ok(())
        }
        async fn cancel_order(&self, _cmd: CancelOrder) -> PortResult<()> {
            Ok(())
        }
        async fn modify_order(&self, _cmd: ModifyOrder) -> PortResult<()> {
            Ok(())
        }
        async fn reconcile(&self) -> PortResult<Vec<ExecutionReport>> {
            Ok(Vec::new())
        }
        fn take_reports(&mut self) -> mpsc::Receiver<ExecutionReport> {
            self.rx.take().expect("take_reports called twice")
        }
        async fn disconnect(&mut self) -> PortResult<()> {
            self.disconnects.fetch_add(1, AtomicOrdering::SeqCst);
            Ok(())
        }
    }

    struct StopCountingStrategy {
        starts: std::sync::Arc<AtomicUsize>,
        stops: std::sync::Arc<AtomicUsize>,
    }
    impl Strategy for StopCountingStrategy {
        fn on_start(&mut self, _ctx: &mut StrategyContext) {
            self.starts.fetch_add(1, AtomicOrdering::SeqCst);
        }
        fn on_stop(&mut self, _ctx: &mut StrategyContext) {
            self.stops.fetch_add(1, AtomicOrdering::SeqCst);
        }
    }

    /// Records fills the Strategy is notified of, so the test can assert the live path delivered the
    /// port's reports all the way back up to the user Strategy (the shared seam end-to-end).
    struct CountingBuyStrategy {
        iid: InstrumentId,
        bought: bool,
        fills: std::sync::Arc<AtomicUsize>,
    }
    impl Strategy for CountingBuyStrategy {
        fn on_bar(&mut self, _b: &Bar, ctx: &mut StrategyContext) {
            if !self.bought {
                self.bought = true;
                ctx.submit_market(
                    self.iid.clone(),
                    OrderSide::Buy,
                    Quantity::from_decimal(dec!(1), 3).unwrap(),
                );
            }
        }
        fn on_order_filled(&mut self, _f: &Fill, _ctx: &mut StrategyContext) {
            self.fills.fetch_add(1, AtomicOrdering::SeqCst);
        }
    }

    #[test]
    fn live_kernel_stop_handle_wakes_idle_open_streams_and_disconnects_ports() {
        let usdt = Currency::new("USDT", 8).unwrap();
        let i = inst();
        let connected = std::sync::Arc::new(AtomicUsize::new(0));
        let connect_notify = std::sync::Arc::new(tokio::sync::Notify::new());
        let data_disconnects = std::sync::Arc::new(AtomicUsize::new(0));
        let exec_disconnects = std::sync::Arc::new(AtomicUsize::new(0));
        let starts = std::sync::Arc::new(AtomicUsize::new(0));
        let stops = std::sync::Arc::new(AtomicUsize::new(0));

        let data = Box::new(IdleDataClient::new(
            connected.clone(),
            data_disconnects.clone(),
            connect_notify.clone(),
        ));
        let exec = Box::new(IdleExecClient::new(
            connected.clone(),
            exec_disconnects.clone(),
            connect_notify.clone(),
        ));
        let strat = Box::new(StopCountingStrategy {
            starts: starts.clone(),
            stops: stops.clone(),
        });

        let mut kernel = LiveKernel::build(
            Environment::Sandbox,
            BacktestConfig::new(
                Venue::from("BINANCE"),
                vec![i],
                usdt,
                Money::from_decimal(dec!(100000), usdt).unwrap(),
            ),
            StrategyId::from("live-stop"),
            strat,
            exec,
            data,
        );
        assert!(!kernel.is_stop_requested());
        let stop = kernel.stop_handle();

        let rt = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap();
        let local = tokio::task::LocalSet::new();
        let connected_in_task = connected.clone();
        let connect_notify_in_task = connect_notify.clone();
        let stop_in_task = stop.clone();
        let starts_in_task = starts.clone();
        local.block_on(&rt, async move {
            let run = tokio::task::spawn_local(async move { kernel.run().await });
            while connected_in_task.load(AtomicOrdering::SeqCst) < 2 {
                connect_notify_in_task.notified().await;
            }
            assert_eq!(
                starts_in_task.load(AtomicOrdering::SeqCst),
                1,
                "strategy should start before the live loop waits on idle streams"
            );

            stop_in_task.request_stop();
            assert!(stop_in_task.is_stop_requested());

            run.await
                .expect("live kernel task should not panic")
                .expect("live kernel should shut down cleanly");
        });

        assert!(stop.is_stop_requested());
        assert_eq!(
            data_disconnects.load(AtomicOrdering::SeqCst),
            1,
            "data client disconnected after stop"
        );
        assert_eq!(
            exec_disconnects.load(AtomicOrdering::SeqCst),
            1,
            "execution client disconnected after stop"
        );
        assert_eq!(
            stops.load(AtomicOrdering::SeqCst),
            1,
            "strategy on_stop ran during graceful shutdown"
        );
    }

    #[test]
    fn live_kernel_consumes_ports_and_folds_fills_through_the_same_engines() {
        let usdt = Currency::new("USDT", 8).unwrap();
        let i = inst();
        let iid = i.id();
        let submits = std::sync::Arc::new(AtomicUsize::new(0));
        let strat_fills = std::sync::Arc::new(AtomicUsize::new(0));

        let exec = Box::new(InstantFillExec::new(
            Price::from_decimal(dec!(50000), 2).unwrap(),
            usdt,
            submits.clone(),
        ));
        let data = Box::new(ReplayDataClient::new(vec![
            bar(&iid, "50000", 1_000_000_000),
            bar(&iid, "51000", 2_000_000_000),
        ]));
        let strat = Box::new(CountingBuyStrategy {
            iid: iid.clone(),
            bought: false,
            fills: strat_fills.clone(),
        });

        let mut kernel = LiveKernel::build(
            Environment::Sandbox,
            BacktestConfig::new(
                Venue::from("BINANCE"),
                vec![i],
                usdt,
                Money::from_decimal(dec!(100000), usdt).unwrap(),
            ),
            StrategyId::from("live"),
            strat,
            exec,
            data,
        );
        assert_eq!(kernel.environment(), Environment::Sandbox);

        let rt = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap();
        let local = tokio::task::LocalSet::new();
        local.block_on(&rt, async move {
            kernel.run().await.unwrap();
            // The strategy submitted exactly one order over the PORT, and its Fill flowed back through
            // the SAME OMS `apply_report` into a long position the strategy was notified of.
            assert_eq!(
                submits.load(AtomicOrdering::SeqCst),
                1,
                "one order routed to the port"
            );
            assert_eq!(
                strat_fills.load(AtomicOrdering::SeqCst),
                1,
                "fill delivered to the Strategy"
            );
            let pos = kernel.cache.borrow().position(&iid).cloned();
            let pos = pos.expect("position opened from the port's fill");
            assert_eq!(pos.side, PositionSide::Long);
            assert_eq!(pos.quantity, Quantity::from_decimal(dec!(1), 3).unwrap());
        });
    }

    #[test]
    fn live_kernel_portfolio_callback_observes_fill_and_subsequent_mark() {
        let usdt = Currency::new("USDT", 8).unwrap();
        let i = inst();
        let iid = i.id();
        let submits = std::sync::Arc::new(AtomicUsize::new(0));
        let strat_fills = std::sync::Arc::new(AtomicUsize::new(0));

        let exec = Box::new(InstantFillExec::new(
            Price::from_decimal(dec!(50000), 2).unwrap(),
            usdt,
            submits,
        ));
        let data = Box::new(ReplayDataClient::new(vec![
            bar(&iid, "50000", 1_000_000_000),
            bar(&iid, "51000", 2_000_000_000),
        ]));
        let strat = Box::new(CountingBuyStrategy {
            iid: iid.clone(),
            bought: false,
            fills: strat_fills,
        });

        let mut kernel = LiveKernel::build(
            Environment::Sandbox,
            BacktestConfig::new(
                Venue::from("BINANCE"),
                vec![i],
                usdt,
                Money::from_decimal(dec!(100000), usdt).unwrap(),
            ),
            StrategyId::from("live-callback"),
            strat,
            exec,
            data,
        );

        let rt = tokio::runtime::Builder::new_current_thread()
            .build()
            .unwrap();
        let local = tokio::task::LocalSet::new();
        local.block_on(&rt, async move {
            let mut snapshots = Vec::new();
            kernel
                .run_with_portfolio_callback(|snapshot| snapshots.push(snapshot))
                .await
                .unwrap();

            assert!(!snapshots.is_empty(), "callback should receive snapshots");

            let has_long_btc_at = |snapshot: &PortfolioSnapshot, mark: f64| {
                snapshot.positions.iter().any(|pos| {
                    pos.symbol == "BTCUSDT"
                        && pos.venue == "BINANCE"
                        && (pos.net_qty - 1.0).abs() < f64::EPSILON
                        && (pos.mark_price - mark).abs() < f64::EPSILON
                })
            };
            let fill_snapshot_idx = snapshots
                .iter()
                .position(|snapshot| has_long_btc_at(snapshot, 50000.0))
                .expect("callback after fill should expose the opened long at the fill mark");
            let marked_snapshot_idx = snapshots
                .iter()
                .position(|snapshot| {
                    has_long_btc_at(snapshot, 51000.0)
                        && snapshot.gross_exposure > 0.0
                        && snapshot.net_exposure > 0.0
                        && snapshot.unrealized_pnl > 0.0
                        && snapshot.equity > 0.0
                })
                .expect("callback after subsequent mark should expose updated portfolio values");
            assert!(
                fill_snapshot_idx < marked_snapshot_idx,
                "fill callback should precede the later mark callback"
            );

            let marked_snapshot = &snapshots[marked_snapshot_idx];
            let position = marked_snapshot
                .positions
                .iter()
                .find(|pos| {
                    pos.symbol == "BTCUSDT"
                        && pos.venue == "BINANCE"
                        && (pos.net_qty - 1.0).abs() < f64::EPSILON
                })
                .expect("marked snapshot should include the long BTCUSDT row");
            assert_eq!(position.symbol, "BTCUSDT");
            assert_eq!(position.venue, "BINANCE");
            assert!((position.net_qty - 1.0).abs() < f64::EPSILON);
            assert!((position.notional - 51000.0).abs() < f64::EPSILON);
            assert!((position.unrealized_pnl - 1000.0).abs() < f64::EPSILON);
            assert!((marked_snapshot.gross_exposure - 51000.0).abs() < f64::EPSILON);
            assert!((marked_snapshot.net_exposure - 51000.0).abs() < f64::EPSILON);
            assert!((marked_snapshot.unrealized_pnl - 1000.0).abs() < f64::EPSILON);
            assert!((marked_snapshot.equity - 101000.0).abs() < f64::EPSILON);
        });
    }

    #[test]
    #[should_panic(expected = "LiveKernel requires")]
    fn live_kernel_rejects_backtest_environment() {
        let usdt = Currency::new("USDT", 8).unwrap();
        let i = inst();
        let submits = std::sync::Arc::new(AtomicUsize::new(0));
        let exec = Box::new(InstantFillExec::new(
            Price::from_decimal(dec!(50000), 2).unwrap(),
            usdt,
            submits,
        ));
        let data = Box::new(ReplayDataClient::new(vec![]));
        let _ = LiveKernel::build(
            Environment::Backtest,
            BacktestConfig::new(
                Venue::from("BINANCE"),
                vec![i],
                usdt,
                Money::from_decimal(dec!(100000), usdt).unwrap(),
            ),
            StrategyId::from("x"),
            Box::new(BuyOnceStrategy {
                iid: InstrumentId::parse("BTCUSDT.BINANCE").unwrap(),
                bought: false,
            }),
            exec,
            data,
        );
    }
}
