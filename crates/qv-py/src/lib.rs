//! `qv-py` — the ONLY in-process Python↔Rust binding (PyO3 + maturin).
//!
//! Exposes the backtest entry point and, crucially, the **PyStrategyAdapter** dispatch shim: a
//! Python `Strategy` subclass cannot implement an async Rust trait, so the adapter holds the
//! `Py<PyAny>` strategy, implements the SYNCHRONOUS Rust `Strategy` trait, and on each event
//! acquires the GIL and calls the corresponding Python method. This is the load-bearing proof of
//! the parity claim across the FFI boundary: the SAME Rust kernel that runs a native-Rust strategy
//! runs a Python one.
//!
//! All PyO3 code is gated behind the `python` feature so the default `cargo test` builds this crate
//! without linking libpython.

#[cfg(feature = "python")]
mod imp {
    use pyo3::prelude::*;
    use qv_core::{Currency, Money, Price, Quantity, UnixNanos};
    use qv_indicators::{Atr, Bollinger, Ema, Indicator, Macd, Rsi, Sma, Vwap};
    use qv_kernel::{BacktestConfig, BacktestKernel};
    use qv_model::{
        AggregationSource, Bar, BarAggregation, BarSpec, BarType, CurrencyPair, Instrument,
        InstrumentId, MarketEvent, OrderEvent, OrderSide, PositionSide, PriceType, QuoteTick,
        StrategyId, Symbol, TradeId, TradeTick, Venue,
    };
    use qv_ports::{Strategy, StrategyContext};
    use rust_decimal::prelude::FromPrimitive;
    use rust_decimal::Decimal;
    use std::cell::RefCell;
    use std::sync::Arc;

    /// A bar handed to a Python strategy: full OHLC + ts + the instrument `symbol` it belongs to
    /// (so a multi-instrument strategy can branch per symbol). The strategy can read high/low, and
    /// the sim fills resting limits against this bar's high/low — the OHLC-aware path.
    #[pyclass(unsendable, name = "Bar")]
    pub struct PyBar {
        #[pyo3(get)]
        pub symbol: String,
        #[pyo3(get)]
        pub close: f64,
        #[pyo3(get)]
        pub open: f64,
        #[pyo3(get)]
        pub high: f64,
        #[pyo3(get)]
        pub low: f64,
        #[pyo3(get)]
        pub volume: f64,
        #[pyo3(get)]
        pub ts: u64,
    }

    /// A fill of one of the strategy's own orders, delivered to `on_order_filled`.
    #[pyclass(unsendable, name = "Fill")]
    pub struct PyFill {
        #[pyo3(get)]
        pub symbol: String,
        #[pyo3(get)]
        pub side: i8, // +1 buy / -1 sell
        #[pyo3(get)]
        pub qty: f64,
        #[pyo3(get)]
        pub price: f64,
        #[pyo3(get)]
        pub client_order_id: String,
    }

    /// An order-lifecycle event, delivered to `on_order_event`. `kind` is one of
    /// submitted/accepted/partially_filled/filled/denied/rejected/canceled/expired/…; `reason` is
    /// set for denied/rejected.
    #[pyclass(unsendable, name = "OrderEvent")]
    pub struct PyOrderEvent {
        #[pyo3(get)]
        pub kind: String,
        #[pyo3(get)]
        pub reason: Option<String>,
    }

    /// A timer firing, delivered to `on_timer` (armed via `ctx.set_timer(name, at_ns)`).
    #[pyclass(unsendable, name = "Timer")]
    pub struct PyTimer {
        #[pyo3(get)]
        pub name: String,
        #[pyo3(get)]
        pub ts: u64,
    }

    /// A top-of-book quote, delivered to `on_quote` when the feed provides quotes.
    #[pyclass(unsendable, name = "Quote")]
    pub struct PyQuote {
        #[pyo3(get)]
        pub symbol: String,
        #[pyo3(get)]
        pub bid: f64,
        #[pyo3(get)]
        pub ask: f64,
        #[pyo3(get)]
        pub bid_size: f64,
        #[pyo3(get)]
        pub ask_size: f64,
        #[pyo3(get)]
        pub ts: u64,
    }

    /// A public trade print, delivered to `on_trade` when the feed provides trades.
    #[pyclass(unsendable, name = "Trade")]
    pub struct PyTrade {
        #[pyo3(get)]
        pub symbol: String,
        #[pyo3(get)]
        pub price: f64,
        #[pyo3(get)]
        pub size: f64,
        #[pyo3(get)]
        pub ts: u64,
    }

    // --- Streaming technical indicators (the SAME Rust code as warm-up + live; see qv-indicators).
    // A Python strategy owns these as state and feeds them in its handlers, instead of re-rolling
    // them in Python. `value()` returns `None` until the indicator is warm.

    /// Simple Moving Average over a fixed `period` window.
    #[pyclass(name = "Sma")]
    pub struct PySma {
        inner: Sma,
    }
    #[pymethods]
    impl PySma {
        #[new]
        fn new(period: usize) -> PyResult<Self> {
            if period == 0 {
                return Err(vexc("SMA period must be > 0"));
            }
            Ok(PySma {
                inner: Sma::new(period),
            })
        }
        fn update(&mut self, value: f64) {
            self.inner.update(value);
        }
        fn value(&self) -> Option<f64> {
            self.inner.value()
        }
        fn is_ready(&self) -> bool {
            self.inner.is_ready()
        }
    }

    /// Exponential Moving Average (`alpha = 2/(period+1)`).
    #[pyclass(name = "Ema")]
    pub struct PyEma {
        inner: Ema,
    }
    #[pymethods]
    impl PyEma {
        #[new]
        fn new(period: usize) -> PyResult<Self> {
            if period == 0 {
                return Err(vexc("EMA period must be > 0"));
            }
            Ok(PyEma {
                inner: Ema::new(period),
            })
        }
        fn update(&mut self, value: f64) {
            self.inner.update(value);
        }
        fn value(&self) -> Option<f64> {
            self.inner.value()
        }
        fn is_ready(&self) -> bool {
            self.inner.is_ready()
        }
    }

    /// Wilder's Relative Strength Index (0–100).
    #[pyclass(name = "Rsi")]
    pub struct PyRsi {
        inner: Rsi,
    }
    #[pymethods]
    impl PyRsi {
        #[new]
        fn new(period: usize) -> PyResult<Self> {
            if period == 0 {
                return Err(vexc("RSI period must be > 0"));
            }
            Ok(PyRsi {
                inner: Rsi::new(period),
            })
        }
        fn update(&mut self, value: f64) {
            self.inner.update(value);
        }
        fn value(&self) -> Option<f64> {
            self.inner.value()
        }
        fn is_ready(&self) -> bool {
            self.inner.is_ready()
        }
    }

    /// Average True Range (Wilder smoothing). Feed `update(high, low, close)`.
    #[pyclass(name = "Atr")]
    pub struct PyAtr {
        inner: Atr,
    }
    #[pymethods]
    impl PyAtr {
        #[new]
        fn new(period: usize) -> PyResult<Self> {
            if period == 0 {
                return Err(vexc("ATR period must be > 0"));
            }
            Ok(PyAtr {
                inner: Atr::new(period),
            })
        }
        fn update(&mut self, high: f64, low: f64, close: f64) {
            self.inner.update_hlc(high, low, close);
        }
        fn value(&self) -> Option<f64> {
            self.inner.value()
        }
        fn is_ready(&self) -> bool {
            self.inner.value().is_some()
        }
    }

    /// MACD; `value()` is `(macd, signal, histogram)` once warm.
    #[pyclass(name = "Macd")]
    pub struct PyMacd {
        inner: Macd,
    }
    #[pymethods]
    impl PyMacd {
        #[new]
        #[pyo3(signature = (fast=12, slow=26, signal=9))]
        fn new(fast: usize, slow: usize, signal: usize) -> PyResult<Self> {
            if fast == 0 || slow == 0 || signal == 0 {
                return Err(vexc("MACD periods must be > 0"));
            }
            Ok(PyMacd {
                inner: Macd::new(fast, slow, signal),
            })
        }
        fn update(&mut self, value: f64) {
            self.inner.update(value);
        }
        fn value(&self) -> Option<(f64, f64, f64)> {
            self.inner.value()
        }
        fn is_ready(&self) -> bool {
            self.inner.is_ready()
        }
    }

    /// Bollinger Bands; `value()` is `(lower, mid, upper)` once warm.
    #[pyclass(name = "Bollinger")]
    pub struct PyBollinger {
        inner: Bollinger,
    }
    #[pymethods]
    impl PyBollinger {
        #[new]
        #[pyo3(signature = (period=20, k=2.0))]
        fn new(period: usize, k: f64) -> PyResult<Self> {
            if period == 0 {
                return Err(vexc("Bollinger period must be > 0"));
            }
            Ok(PyBollinger {
                inner: Bollinger::new(period, k),
            })
        }
        fn update(&mut self, value: f64) {
            self.inner.update(value);
        }
        fn value(&self) -> Option<(f64, f64, f64)> {
            self.inner.value()
        }
        fn is_ready(&self) -> bool {
            self.inner.is_ready()
        }
    }

    /// Rolling VWAP over `period` bars. Feed `update(price, volume)`.
    #[pyclass(name = "Vwap")]
    pub struct PyVwap {
        inner: Vwap,
    }
    #[pymethods]
    impl PyVwap {
        #[new]
        fn new(period: usize) -> PyResult<Self> {
            if period == 0 {
                return Err(vexc("VWAP period must be > 0"));
            }
            Ok(PyVwap {
                inner: Vwap::new(period),
            })
        }
        fn update(&mut self, price: f64, volume: f64) {
            self.inner.update(price, volume);
        }
        fn value(&self) -> Option<f64> {
            self.inner.value()
        }
        fn is_ready(&self) -> bool {
            self.inner.is_ready()
        }
    }

    /// One queued intent from a Python handler, replayed onto the real `StrategyContext` afterwards.
    #[derive(Clone)]
    enum PyIntent {
        Submit {
            side: String,
            qty: f64,
            limit_px: Option<f64>,     // Some = limit
            trigger: Option<f64>,      // Some = stop (+ limit_px = stop-limit)
            trail_offset: Option<f64>, // Some = trailing stop (overrides trigger/limit_px)
            symbol: Option<String>,
        },
        Cancel {
            client_order_id: String,
        },
        SetTimer {
            name: String,
            at: u64,
        },
    }

    /// The strategy context exposed to Python during a handler. Reads (now, positions) are snapshot
    /// before the call; `submit_market`/`submit_limit` accumulate intents that the adapter replays
    /// onto the real Rust `StrategyContext` after the handler returns (safe — no Rust references
    /// cross the GIL). Position/submit take an optional `symbol`; omitting it targets the default
    /// (single) instrument, so single-instrument strategies need not pass one.
    #[pyclass(unsendable, name = "Ctx")]
    pub struct PyCtx {
        #[pyo3(get)]
        pub now: u64,
        signed_positions: std::collections::HashMap<String, f64>,
        default_symbol: String,
        // Per-symbol (price_precision, size_precision), to VALIDATE a submit at queue time exactly as
        // replay would. A submit that would be dropped on replay (unknown symbol / bad qty / bad
        // price) must NOT mint or count an id, or every later predicted id in the handler desyncs.
        metas: std::collections::HashMap<String, (u8, u8)>,
        // For deterministically pre-computing the client_order_id a submit WILL get on replay
        // (`{strategy_id}-{seq:020}`), so a handler can cancel an order it just submitted.
        strategy_id: String,
        base_seq: u64,
        submit_count: RefCell<u64>,
        outbox: RefCell<Vec<PyIntent>>,
    }

    impl PyCtx {
        /// The client_order_id the next submit will receive once replayed onto the real factory.
        fn next_coid(&self) -> String {
            let mut n = self.submit_count.borrow_mut();
            *n += 1;
            format!("{}-{:020}", self.strategy_id, self.base_seq + *n)
        }
        /// Resolve a symbol (default if `None`) to its (price_precision, size_precision), or raise.
        fn resolve(&self, symbol: &Option<String>) -> PyResult<(String, u8, u8)> {
            let sym = symbol
                .clone()
                .unwrap_or_else(|| self.default_symbol.clone());
            match self.metas.get(&sym) {
                Some(&(pp, sp)) => Ok((sym, pp, sp)),
                None => Err(vexc(format!("unknown symbol {sym:?}"))),
            }
        }
    }

    #[pymethods]
    impl PyCtx {
        /// Signed position quantity (+long / -short) for `symbol` (default instrument if omitted).
        #[pyo3(signature = (symbol=None))]
        fn position(&self, symbol: Option<&str>) -> f64 {
            let key = symbol.unwrap_or(self.default_symbol.as_str());
            self.signed_positions.get(key).copied().unwrap_or(0.0)
        }
        /// Queue a market order; returns its client_order_id (usable with `cancel`). `side` is
        /// "buy"/"sell"; `symbol` defaults to the only instrument. Raises `ValueError` for an
        /// unknown symbol or a qty the instrument's precision can't represent (so the predicted id
        /// always matches the one the order actually gets).
        #[pyo3(signature = (side, qty, symbol=None))]
        fn submit_market(&self, side: &str, qty: f64, symbol: Option<String>) -> PyResult<String> {
            let (_sym, _pp, sp) = self.resolve(&symbol)?;
            Quantity::from_f64(qty, sp).map_err(vexc)?; // same check replay does -> never drops
            self.outbox.borrow_mut().push(PyIntent::Submit {
                side: side.to_string(),
                qty,
                limit_px: None,
                trigger: None,
                trail_offset: None,
                symbol,
            });
            Ok(self.next_coid())
        }
        /// Queue a resting limit order at `price`; returns its client_order_id. Fills when a later
        /// bar's low/high crosses it — the OHLC-aware path (matched against bar high/low, not close).
        /// Raises `ValueError` for an unknown symbol or a qty/price the precision can't represent.
        #[pyo3(signature = (side, qty, price, symbol=None))]
        fn submit_limit(
            &self,
            side: &str,
            qty: f64,
            price: f64,
            symbol: Option<String>,
        ) -> PyResult<String> {
            let (_sym, pp, sp) = self.resolve(&symbol)?;
            Quantity::from_f64(qty, sp).map_err(vexc)?;
            Price::from_f64(price, pp).map_err(vexc)?;
            self.outbox.borrow_mut().push(PyIntent::Submit {
                side: side.to_string(),
                qty,
                limit_px: Some(price),
                trigger: None,
                trail_offset: None,
                symbol,
            });
            Ok(self.next_coid())
        }
        /// Queue a stop-MARKET order with `trigger`; returns its client_order_id. Rests until the
        /// market crosses the trigger (buy: rises to it / sell: falls to it), then takes liquidity at
        /// the market — a stop-loss or breakout entry. Raises `ValueError` for bad symbol/qty/trigger.
        #[pyo3(signature = (side, qty, trigger, symbol=None))]
        fn submit_stop(
            &self,
            side: &str,
            qty: f64,
            trigger: f64,
            symbol: Option<String>,
        ) -> PyResult<String> {
            let (_sym, pp, sp) = self.resolve(&symbol)?;
            Quantity::from_f64(qty, sp).map_err(vexc)?;
            Price::from_f64(trigger, pp).map_err(vexc)?;
            self.outbox.borrow_mut().push(PyIntent::Submit {
                side: side.to_string(),
                qty,
                limit_px: None,
                trigger: Some(trigger),
                trail_offset: None,
                symbol,
            });
            Ok(self.next_coid())
        }
        /// Queue a stop-LIMIT order: rests until the market crosses `trigger`, then becomes a resting
        /// limit at `price` (fills only at `price` or better — bounded slippage vs a plain stop).
        /// Returns its client_order_id. Raises `ValueError` for bad symbol/qty/trigger/price.
        #[pyo3(signature = (side, qty, trigger, price, symbol=None))]
        fn submit_stop_limit(
            &self,
            side: &str,
            qty: f64,
            trigger: f64,
            price: f64,
            symbol: Option<String>,
        ) -> PyResult<String> {
            let (_sym, pp, sp) = self.resolve(&symbol)?;
            Quantity::from_f64(qty, sp).map_err(vexc)?;
            Price::from_f64(trigger, pp).map_err(vexc)?;
            Price::from_f64(price, pp).map_err(vexc)?;
            self.outbox.borrow_mut().push(PyIntent::Submit {
                side: side.to_string(),
                qty,
                limit_px: Some(price),
                trigger: Some(trigger),
                trail_offset: None,
                symbol,
            });
            Ok(self.next_coid())
        }
        /// Queue a TRAILING stop-market order `offset` away from the current mark; returns its
        /// client_order_id. The stop trails the favorable extreme (the sim sets the initial level to
        /// `mark ∓ offset` and ratchets it). Raises `ValueError` for bad symbol/qty/offset.
        #[pyo3(signature = (side, qty, offset, symbol=None))]
        fn submit_trailing(
            &self,
            side: &str,
            qty: f64,
            offset: f64,
            symbol: Option<String>,
        ) -> PyResult<String> {
            let (_sym, pp, sp) = self.resolve(&symbol)?;
            Quantity::from_f64(qty, sp).map_err(vexc)?;
            Price::from_f64(offset, pp).map_err(vexc)?;
            self.outbox.borrow_mut().push(PyIntent::Submit {
                side: side.to_string(),
                qty,
                limit_px: None,
                trigger: None,
                trail_offset: Some(offset),
                symbol,
            });
            Ok(self.next_coid())
        }
        /// Cancel a resting order by the client_order_id returned from `submit_market`/`submit_limit`
        /// (or seen on a `Fill`/`OrderEvent`).
        fn cancel(&self, client_order_id: String) {
            self.outbox
                .borrow_mut()
                .push(PyIntent::Cancel { client_order_id });
        }
        /// Arm a one-shot timer firing at absolute `at` (ns); delivered to `on_timer` with `name`.
        /// Re-arm in `on_timer` for periodic behavior (e.g. `ctx.set_timer("rebalance", ctx.now + N)`).
        fn set_timer(&self, name: String, at: u64) {
            self.outbox
                .borrow_mut()
                .push(PyIntent::SetTimer { name, at });
        }
    }

    /// Result of a Python-driven backtest.
    #[pyclass(name = "BacktestResult")]
    pub struct PyResultObj {
        #[pyo3(get)]
        pub starting_equity: f64,
        #[pyo3(get)]
        pub final_equity: f64,
        #[pyo3(get)]
        pub total_return: f64,
        #[pyo3(get)]
        pub fills: u64,
        #[pyo3(get)]
        pub orders_submitted: u64,
        #[pyo3(get)]
        pub orders_denied: u64,
        #[pyo3(get)]
        pub realized_pnl: f64,
        #[pyo3(get)]
        pub equity_curve: Vec<(u64, f64)>,
        /// Per-fill log: `(ts_ns, symbol, side[+1 buy/-1 sell], qty, price)`. The symbol lets
        /// analytics reconstruct trades per instrument; the parity gate ignores it (single-venue).
        #[pyo3(get)]
        pub fills_log: Vec<(u64, String, i8, f64, f64)>,
    }

    /// Per-instrument data the adapter needs to translate a Python intent into a typed Rust order.
    #[derive(Clone)]
    struct InstrumentMeta {
        iid: InstrumentId,
        price_precision: u8,
        size_precision: u8,
    }

    /// Bridges a Python `Strategy` object into the synchronous Rust `Strategy` trait. Holds every
    /// instrument keyed by symbol (the kernel/sim/cache are already multi-instrument); a bar's
    /// symbol selects which one `on_bar` runs over, and an intent's optional symbol selects the
    /// order's target.
    struct PyStrategyAdapter {
        obj: Py<PyAny>,
        instruments: std::collections::HashMap<String, InstrumentMeta>,
        default_symbol: String,
        strategy_id: String,
    }

    fn signed_qty(ctx: &StrategyContext, iid: &InstrumentId) -> f64 {
        match ctx.position(iid) {
            Some(p) => match p.side {
                PositionSide::Long => p.quantity.as_f64(),
                PositionSide::Short => -p.quantity.as_f64(),
                PositionSide::Flat => 0.0,
            },
            None => 0.0,
        }
    }

    /// Map an `OrderEvent` to a `(kind, reason)` pair for the Python `on_order_event` hook.
    fn order_event_kind(e: &OrderEvent) -> (String, Option<String>) {
        use OrderEvent::*;
        match e {
            Initialized { .. } => ("initialized".into(), None),
            Submitted { .. } => ("submitted".into(), None),
            Accepted { .. } => ("accepted".into(), None),
            PendingUpdate { .. } => ("pending_update".into(), None),
            Updated { .. } => ("updated".into(), None),
            PendingCancel { .. } => ("pending_cancel".into(), None),
            PartiallyFilled(_) => ("partially_filled".into(), None),
            Filled(_) => ("filled".into(), None),
            Denied { reason, .. } => ("denied".into(), Some(reason.clone())),
            Rejected { reason, .. } => ("rejected".into(), Some(reason.clone())),
            Canceled { .. } => ("canceled".into(), None),
            Expired { .. } => ("expired".into(), None),
        }
    }

    impl PyStrategyAdapter {
        fn snapshot(&self, ctx: &StrategyContext) -> std::collections::HashMap<String, f64> {
            self.instruments
                .iter()
                .map(|(sym, meta)| (sym.clone(), signed_qty(ctx, &meta.iid)))
                .collect()
        }

        /// The shared dispatch path: snapshot state, build a fresh `PyCtx`, run the Python handler
        /// (the closure builds the event object and calls the method), then replay the queued
        /// intents onto the real `StrategyContext`. The GIL is held only inside the closure.
        fn run_handler<F>(&self, ctx: &mut StrategyContext, call: F)
        where
            F: FnOnce(Python<'_>, &Bound<'_, PyAny>, Py<PyCtx>) -> PyResult<()>,
        {
            let now = ctx.now_ns().as_u64();
            let base_seq = ctx.order_factory().seq();
            let signed_positions = self.snapshot(ctx);
            let metas: std::collections::HashMap<String, (u8, u8)> = self
                .instruments
                .iter()
                .map(|(sym, m)| (sym.clone(), (m.price_precision, m.size_precision)))
                .collect();
            let intents: Vec<PyIntent> = Python::attach(|py| {
                let py_ctx = Py::new(
                    py,
                    PyCtx {
                        now,
                        signed_positions,
                        default_symbol: self.default_symbol.clone(),
                        metas,
                        strategy_id: self.strategy_id.clone(),
                        base_seq,
                        submit_count: RefCell::new(0),
                        outbox: RefCell::new(Vec::new()),
                    },
                )
                .expect("alloc PyCtx");
                let bound = self.obj.bind(py);
                if let Err(e) = call(py, bound, py_ctx.clone_ref(py)) {
                    e.print(py);
                }
                py_ctx.bind(py).borrow().outbox.borrow().clone()
            });
            self.replay(ctx, intents);
        }

        fn replay(&self, ctx: &mut StrategyContext, intents: Vec<PyIntent>) {
            for intent in intents {
                match intent {
                    PyIntent::Submit {
                        side,
                        qty,
                        limit_px,
                        trigger,
                        trail_offset,
                        symbol,
                    } => {
                        let sym = symbol.unwrap_or_else(|| self.default_symbol.clone());
                        let Some(meta) = self.instruments.get(&sym) else {
                            continue; // unknown symbol: drop (kernel validates upstream too)
                        };
                        let side = if side.eq_ignore_ascii_case("sell") {
                            OrderSide::Sell
                        } else {
                            OrderSide::Buy
                        };
                        let Ok(q) = Quantity::from_f64(qty, meta.size_precision) else {
                            continue;
                        };
                        // A trailing stop is its own thing (offset, not an absolute trigger).
                        if let Some(off) = trail_offset {
                            if let Ok(o) = Price::from_f64(off, meta.price_precision) {
                                ctx.submit_trailing_stop(meta.iid.clone(), side, q, o);
                            }
                            continue;
                        }
                        match (trigger, limit_px) {
                            (Some(trig), Some(px)) => {
                                // Stop-limit: trigger + limit price.
                                if let (Ok(t), Ok(p)) = (
                                    Price::from_f64(trig, meta.price_precision),
                                    Price::from_f64(px, meta.price_precision),
                                ) {
                                    ctx.submit_stop_limit(meta.iid.clone(), side, q, t, p);
                                }
                            }
                            (Some(trig), None) => {
                                // Stop-market.
                                if let Ok(t) = Price::from_f64(trig, meta.price_precision) {
                                    ctx.submit_stop_market(meta.iid.clone(), side, q, t);
                                }
                            }
                            (None, Some(px)) => {
                                if let Ok(p) = Price::from_f64(px, meta.price_precision) {
                                    ctx.submit_limit(meta.iid.clone(), side, q, p);
                                }
                            }
                            (None, None) => {
                                ctx.submit_market(meta.iid.clone(), side, q);
                            }
                        }
                    }
                    PyIntent::Cancel { client_order_id } => {
                        ctx.cancel(qv_model::ClientOrderId::from(client_order_id.as_str()));
                    }
                    PyIntent::SetTimer { name, at } => {
                        ctx.set_timer(&name, UnixNanos(at));
                    }
                }
            }
        }
    }

    impl Strategy for PyStrategyAdapter {
        fn on_start(&mut self, ctx: &mut StrategyContext) {
            self.run_handler(ctx, |_py, obj, pyctx| {
                obj.call_method1("on_start", (pyctx,)).map(|_| ())
            });
        }

        fn on_bar(&mut self, bar: &Bar, ctx: &mut StrategyContext) {
            let v = (
                bar.bar_type.instrument_id.symbol.as_str().to_string(),
                bar.open.as_f64(),
                bar.high.as_f64(),
                bar.low.as_f64(),
                bar.close.as_f64(),
                bar.volume.as_f64(),
                bar.ts_event.as_u64(),
            );
            self.run_handler(ctx, move |py, obj, pyctx| {
                let py_bar = Py::new(
                    py,
                    PyBar {
                        symbol: v.0,
                        open: v.1,
                        high: v.2,
                        low: v.3,
                        close: v.4,
                        volume: v.5,
                        ts: v.6,
                    },
                )?;
                obj.call_method1("on_bar", (py_bar, pyctx)).map(|_| ())
            });
        }

        fn on_quote(&mut self, q: &qv_model::QuoteTick, ctx: &mut StrategyContext) {
            let v = (
                q.instrument_id.symbol.as_str().to_string(),
                q.bid.as_f64(),
                q.ask.as_f64(),
                q.bid_size.as_f64(),
                q.ask_size.as_f64(),
                q.ts_event.as_u64(),
            );
            self.run_handler(ctx, move |py, obj, pyctx| {
                let pq = Py::new(
                    py,
                    PyQuote {
                        symbol: v.0,
                        bid: v.1,
                        ask: v.2,
                        bid_size: v.3,
                        ask_size: v.4,
                        ts: v.5,
                    },
                )?;
                obj.call_method1("on_quote", (pq, pyctx)).map(|_| ())
            });
        }

        fn on_trade(&mut self, t: &qv_model::TradeTick, ctx: &mut StrategyContext) {
            let v = (
                t.instrument_id.symbol.as_str().to_string(),
                t.price.as_f64(),
                t.size.as_f64(),
                t.ts_event.as_u64(),
            );
            self.run_handler(ctx, move |py, obj, pyctx| {
                let pt = Py::new(
                    py,
                    PyTrade {
                        symbol: v.0,
                        price: v.1,
                        size: v.2,
                        ts: v.3,
                    },
                )?;
                obj.call_method1("on_trade", (pt, pyctx)).map(|_| ())
            });
        }

        fn on_order_filled(&mut self, f: &qv_model::Fill, ctx: &mut StrategyContext) {
            let v = (
                f.instrument_id.symbol.as_str().to_string(),
                f.side.sign() as i8,
                f.last_qty.as_f64(),
                f.last_px.as_f64(),
                f.client_order_id.as_str().to_string(),
            );
            self.run_handler(ctx, move |py, obj, pyctx| {
                let pf = Py::new(
                    py,
                    PyFill {
                        symbol: v.0,
                        side: v.1,
                        qty: v.2,
                        price: v.3,
                        client_order_id: v.4,
                    },
                )?;
                obj.call_method1("on_order_filled", (pf, pyctx)).map(|_| ())
            });
        }

        fn on_order_event(&mut self, e: &OrderEvent, ctx: &mut StrategyContext) {
            let (kind, reason) = order_event_kind(e);
            self.run_handler(ctx, move |py, obj, pyctx| {
                let pe = Py::new(py, PyOrderEvent { kind, reason })?;
                obj.call_method1("on_order_event", (pe, pyctx)).map(|_| ())
            });
        }

        fn on_timer(&mut self, ev: &qv_core::TimerEvent, ctx: &mut StrategyContext) {
            let v = (ev.name.clone(), ev.ts_event.as_u64());
            self.run_handler(ctx, move |py, obj, pyctx| {
                let pt = Py::new(py, PyTimer { name: v.0, ts: v.1 })?;
                obj.call_method1("on_timer", (pt, pyctx)).map(|_| ())
            });
        }

        fn on_stop(&mut self, ctx: &mut StrategyContext) {
            self.run_handler(ctx, |_py, obj, pyctx| {
                obj.call_method1("on_stop", (pyctx,)).map(|_| ())
            });
        }
    }

    /// Map any displayable error to a Python `ValueError`.
    fn vexc<E: std::fmt::Display>(e: E) -> PyErr {
        pyo3::exceptions::PyValueError::new_err(e.to_string())
    }

    /// Build one `CurrencyPair` instrument + the `InstrumentMeta` the adapter keeps for it.
    fn build_pair(
        symbol: &str,
        venue: &str,
        quote: Currency,
        price_precision: u8,
        size_precision: u8,
        maker_fee: f64,
        taker_fee: f64,
    ) -> PyResult<(Arc<dyn Instrument>, InstrumentMeta)> {
        let base = Currency::new("BASE", 8).map_err(vexc)?;
        let iid = InstrumentId::new(Symbol::from(symbol), Venue::from(venue));
        let inst: Arc<dyn Instrument> = Arc::new(CurrencyPair {
            id: iid.clone(),
            base,
            quote,
            price_precision,
            size_precision,
            price_increment: Price::from_raw(1, price_precision).map_err(vexc)?,
            size_increment: Quantity::from_raw(1, size_precision).map_err(vexc)?,
            min_notional: None,
            maker_fee: Decimal::from_f64(maker_fee).unwrap_or(Decimal::ZERO),
            taker_fee: Decimal::from_f64(taker_fee).unwrap_or(Decimal::ZERO),
        });
        Ok((
            inst,
            InstrumentMeta {
                iid,
                price_precision,
                size_precision,
            },
        ))
    }

    /// Build one OHLCV `Bar` market event for the given instrument. `volume` drives the sim's
    /// volume-participation partial fills (a `0` volume means "no cap" — fill resting orders fully).
    fn build_bar_event(
        meta: &InstrumentMeta,
        ts: u64,
        open: f64,
        high: f64,
        low: f64,
        close: f64,
        volume: f64,
    ) -> PyResult<MarketEvent> {
        let mk = |v: f64| Price::from_f64(v, meta.price_precision).map_err(vexc);
        Ok(MarketEvent::Bar(Bar {
            bar_type: BarType {
                instrument_id: meta.iid.clone(),
                spec: BarSpec {
                    step: 1,
                    aggregation: BarAggregation::Minute,
                    price_type: PriceType::Last,
                },
                source: AggregationSource::External,
            },
            open: mk(open)?,
            high: mk(high)?,
            low: mk(low)?,
            close: mk(close)?,
            volume: Quantity::from_f64(volume.max(0.0), meta.size_precision).map_err(vexc)?,
            ts_event: UnixNanos(ts),
            ts_init: UnixNanos(ts),
        }))
    }

    /// Build a `QuoteTick` market event (top-of-book) — fires `on_quote` and updates the mark/mid.
    fn build_quote_event(
        meta: &InstrumentMeta,
        ts: u64,
        bid: f64,
        ask: f64,
        bid_size: f64,
        ask_size: f64,
    ) -> PyResult<MarketEvent> {
        Ok(MarketEvent::Quote(QuoteTick {
            instrument_id: meta.iid.clone(),
            bid: Price::from_f64(bid, meta.price_precision).map_err(vexc)?,
            ask: Price::from_f64(ask, meta.price_precision).map_err(vexc)?,
            bid_size: Quantity::from_f64(bid_size.max(0.0), meta.size_precision).map_err(vexc)?,
            ask_size: Quantity::from_f64(ask_size.max(0.0), meta.size_precision).map_err(vexc)?,
            ts_event: UnixNanos(ts),
            ts_init: UnixNanos(ts),
        }))
    }

    /// Build a `TradeTick` market event (a public print) — fires `on_trade` and updates the mark.
    /// `side` is the taker aggressor (`+1` buy / `-1` sell); `seq` makes the trade_id unique.
    fn build_trade_event(
        meta: &InstrumentMeta,
        ts: u64,
        price: f64,
        size: f64,
        side: i8,
        seq: u64,
    ) -> PyResult<MarketEvent> {
        Ok(MarketEvent::Trade(TradeTick {
            instrument_id: meta.iid.clone(),
            price: Price::from_f64(price, meta.price_precision).map_err(vexc)?,
            size: Quantity::from_f64(size.max(0.0), meta.size_precision).map_err(vexc)?,
            aggressor: if side < 0 {
                OrderSide::Sell
            } else {
                OrderSide::Buy
            },
            trade_id: TradeId::from(format!("T-{seq:020}")),
            ts_event: UnixNanos(ts),
            ts_init: UnixNanos(ts),
        }))
    }

    /// Assemble and run the kernel over `events` (kernel sorts by `ts_event`), returning the result.
    #[allow(clippy::too_many_arguments)]
    fn run_kernel(
        strategy: Py<PyAny>,
        venue: &str,
        settle: Currency,
        starting: Money,
        instruments: Vec<Arc<dyn Instrument>>,
        metas: std::collections::HashMap<String, InstrumentMeta>,
        default_symbol: String,
        events: Vec<MarketEvent>,
        queue_ahead_factor: f64,
    ) -> PyResultObj {
        let mut cfg = BacktestConfig::new(Venue::from(venue), instruments, settle, starting);
        // Opt-in queue-position modeling: a resting limit waits behind ~queue_ahead_factor x bar
        // volume at a price the market only TOUCHES (a price trading THROUGH still fills). 0 = off.
        if queue_ahead_factor > 0.0 {
            cfg.brokerage = Box::new(qv_sim::DefaultBrokerageModel {
                queue_ahead_factor: Decimal::from_f64(queue_ahead_factor).unwrap_or(Decimal::ZERO),
                ..qv_sim::DefaultBrokerageModel::default()
            });
        }
        let adapter = PyStrategyAdapter {
            obj: strategy,
            instruments: metas,
            default_symbol,
            // MUST match the StrategyId below: the adapter pre-computes client_order_ids as
            // `{strategy_id}-{seq}` so a handler can cancel an order it just submitted.
            strategy_id: "py-strategy".to_string(),
        };
        let mut kernel = BacktestKernel::build(
            cfg,
            StrategyId::from("py-strategy"),
            Box::new(adapter),
            events,
        );
        let res = kernel.run();
        let total_return = if res.starting_equity != 0.0 {
            res.final_equity / res.starting_equity - 1.0
        } else {
            0.0
        };
        PyResultObj {
            starting_equity: res.starting_equity,
            final_equity: res.final_equity,
            total_return,
            fills: res.fills,
            orders_submitted: res.orders_submitted,
            orders_denied: res.orders_denied,
            realized_pnl: res.realized_pnl,
            equity_curve: res.equity_curve,
            fills_log: res.fills_log,
        }
    }

    /// Single-instrument backtest (the common case).
    #[allow(clippy::too_many_arguments)]
    #[pyfunction]
    #[pyo3(signature = (
        strategy, symbol, venue, starting_balance, bars,
        price_precision=2, size_precision=3, maker_fee=0.0002, taker_fee=0.0004,
        queue_ahead_factor=0.0, quotes=vec![], trades=vec![],
    ))]
    pub fn run_backtest(
        strategy: Py<PyAny>,
        symbol: String,
        venue: String,
        starting_balance: f64,
        // OHLCV bars: `(ts_ns, open, high, low, close, volume)`. A close-only series is passed as
        // `(ts, c, c, c, c, 0)` by the Python wrapper; real OHLC drives intrabar (high/low) limit
        // fills and volume drives participation-based partial fills.
        bars: Vec<(u64, f64, f64, f64, f64, f64)>,
        price_precision: u8,
        size_precision: u8,
        maker_fee: f64,
        taker_fee: f64,
        queue_ahead_factor: f64,
        // Optional tick streams, interleaved with bars by ts: quotes `(ts, bid, ask, bid_size,
        // ask_size)` fire `on_quote`; trades `(ts, price, size, aggressor[+1/-1])` fire `on_trade`.
        quotes: Vec<(u64, f64, f64, f64, f64)>,
        trades: Vec<(u64, f64, f64, i8)>,
    ) -> PyResult<PyResultObj> {
        let settle = Currency::new("USDT", 8).map_err(vexc)?;
        let (inst, meta) = build_pair(
            &symbol,
            &venue,
            settle,
            price_precision,
            size_precision,
            maker_fee,
            taker_fee,
        )?;
        let mut events = Vec::with_capacity(bars.len() + quotes.len() + trades.len());
        for (ts, open, high, low, close, volume) in bars {
            events.push(build_bar_event(&meta, ts, open, high, low, close, volume)?);
        }
        for (ts, bid, ask, bid_size, ask_size) in quotes {
            events.push(build_quote_event(&meta, ts, bid, ask, bid_size, ask_size)?);
        }
        for (seq, (ts, price, size, side)) in trades.into_iter().enumerate() {
            events.push(build_trade_event(&meta, ts, price, size, side, seq as u64)?);
        }
        let starting = Money::from_decimal(
            Decimal::from_f64(starting_balance).unwrap_or(Decimal::ZERO),
            settle,
        )
        .map_err(vexc)?;
        let mut metas = std::collections::HashMap::new();
        metas.insert(symbol.clone(), meta);
        Ok(run_kernel(
            strategy,
            &venue,
            settle,
            starting,
            vec![inst],
            metas,
            symbol,
            events,
            queue_ahead_factor,
        ))
    }

    /// Multi-instrument backtest: many symbols through ONE kernel (shared Cache/sim/risk/portfolio),
    /// fed a single timestamp-tagged bar stream. The Rust core is already multi-instrument; this is
    /// the Python-facing entry point. The Python `Strategy` reads `bar.symbol` and targets orders
    /// via the optional `symbol` arg on `ctx.submit_market`/`submit_limit`/`position`.
    #[pyfunction]
    #[pyo3(signature = (strategy, venue, starting_balance, instruments, bars, queue_ahead_factor=0.0))]
    pub fn run_backtest_multi(
        strategy: Py<PyAny>,
        venue: String,
        starting_balance: f64,
        // Per instrument: `(symbol, price_precision, size_precision, maker_fee, taker_fee)`.
        instruments: Vec<(String, u8, u8, f64, f64)>,
        // Tagged OHLCV stream: `(ts_ns, symbol, open, high, low, close, volume)` (any order; sorted).
        bars: Vec<(u64, String, f64, f64, f64, f64, f64)>,
        queue_ahead_factor: f64,
    ) -> PyResult<PyResultObj> {
        if instruments.is_empty() {
            return Err(vexc("run_backtest_multi needs at least one instrument"));
        }
        let settle = Currency::new("USDT", 8).map_err(vexc)?;
        let default_symbol = instruments[0].0.clone();
        let mut inst_arcs: Vec<Arc<dyn Instrument>> = Vec::with_capacity(instruments.len());
        let mut metas = std::collections::HashMap::new();
        for (symbol, price_precision, size_precision, maker_fee, taker_fee) in instruments {
            let (inst, meta) = build_pair(
                &symbol,
                &venue,
                settle,
                price_precision,
                size_precision,
                maker_fee,
                taker_fee,
            )?;
            inst_arcs.push(inst);
            metas.insert(symbol, meta);
        }
        let mut events = Vec::with_capacity(bars.len());
        for (ts, symbol, open, high, low, close, volume) in bars {
            let meta = metas
                .get(&symbol)
                .ok_or_else(|| vexc(format!("bar for unknown symbol {symbol:?}")))?;
            events.push(build_bar_event(meta, ts, open, high, low, close, volume)?);
        }
        let starting = Money::from_decimal(
            Decimal::from_f64(starting_balance).unwrap_or(Decimal::ZERO),
            settle,
        )
        .map_err(vexc)?;
        Ok(run_kernel(
            strategy,
            &venue,
            settle,
            starting,
            inst_arcs,
            metas,
            default_symbol,
            events,
            queue_ahead_factor,
        ))
    }

    /// The `qv_py` extension module.
    #[pymodule]
    fn qv_py(m: &Bound<'_, PyModule>) -> PyResult<()> {
        m.add_class::<PyBar>()?;
        m.add_class::<PyCtx>()?;
        m.add_class::<PyResultObj>()?;
        m.add_class::<PyFill>()?;
        m.add_class::<PyOrderEvent>()?;
        m.add_class::<PyTimer>()?;
        m.add_class::<PyQuote>()?;
        m.add_class::<PyTrade>()?;
        m.add_class::<PySma>()?;
        m.add_class::<PyEma>()?;
        m.add_class::<PyRsi>()?;
        m.add_class::<PyAtr>()?;
        m.add_class::<PyMacd>()?;
        m.add_class::<PyBollinger>()?;
        m.add_class::<PyVwap>()?;
        m.add_function(wrap_pyfunction!(run_backtest, m)?)?;
        m.add_function(wrap_pyfunction!(run_backtest_multi, m)?)?;
        m.add("__doc__", "VeloxQuant Rust core exposed to Python (PyO3).")?;
        Ok(())
    }
}
