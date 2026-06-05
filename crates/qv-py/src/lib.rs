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
    use qv_kernel::{BacktestConfig, BacktestKernel};
    use qv_model::{
        AggregationSource, Bar, BarAggregation, BarSpec, BarType, CurrencyPair, Instrument,
        InstrumentId, MarketEvent, OrderSide, PositionSide, PriceType, StrategyId, Symbol, Venue,
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

    /// One queued order intent: side, quantity, an optional limit price (`None` = market), and an
    /// optional target `symbol` (`None` = the default/only instrument).
    struct Intent {
        side: String,
        qty: f64,
        limit_px: Option<f64>,
        symbol: Option<String>,
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
        outbox: RefCell<Vec<Intent>>,
    }

    #[pymethods]
    impl PyCtx {
        /// Signed position quantity (+long / -short) for `symbol` (default instrument if omitted).
        #[pyo3(signature = (symbol=None))]
        fn position(&self, symbol: Option<&str>) -> f64 {
            let key = symbol.unwrap_or(self.default_symbol.as_str());
            self.signed_positions.get(key).copied().unwrap_or(0.0)
        }
        /// Queue a market order. `side` is "buy" or "sell"; `symbol` defaults to the only instrument.
        #[pyo3(signature = (side, qty, symbol=None))]
        fn submit_market(&self, side: &str, qty: f64, symbol: Option<String>) {
            self.outbox.borrow_mut().push(Intent {
                side: side.to_string(),
                qty,
                limit_px: None,
                symbol,
            });
        }
        /// Queue a resting limit order at `price`. Fills when a later bar's low/high crosses it —
        /// the OHLC-aware path (the sim matches resting limits against bar high/low, not just close).
        #[pyo3(signature = (side, qty, price, symbol=None))]
        fn submit_limit(&self, side: &str, qty: f64, price: f64, symbol: Option<String>) {
            self.outbox.borrow_mut().push(Intent {
                side: side.to_string(),
                qty,
                limit_px: Some(price),
                symbol,
            });
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
        /// Per-fill log: `(ts_ns, side[+1 buy/-1 sell], qty, price)`. Used by the parity gate.
        #[pyo3(get)]
        pub fills_log: Vec<(u64, i8, f64, f64)>,
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

    impl Strategy for PyStrategyAdapter {
        fn on_bar(&mut self, bar: &Bar, ctx: &mut StrategyContext) {
            let now = ctx.now_ns().as_u64();
            let bar_symbol = bar.bar_type.instrument_id.symbol.as_str().to_string();
            // Snapshot the signed position of every instrument before the call (no Rust refs cross
            // the GIL); the Python ctx reads them by symbol.
            let signed_positions: std::collections::HashMap<String, f64> = self
                .instruments
                .iter()
                .map(|(sym, meta)| (sym.clone(), signed_qty(ctx, &meta.iid)))
                .collect();

            // Acquire the GIL only for the handler; collect intents, then replay GIL-free.
            let intents: Vec<(String, f64, Option<f64>, Option<String>)> = Python::attach(|py| {
                let py_bar = Py::new(
                    py,
                    PyBar {
                        symbol: bar_symbol,
                        close: bar.close.as_f64(),
                        open: bar.open.as_f64(),
                        high: bar.high.as_f64(),
                        low: bar.low.as_f64(),
                        volume: bar.volume.as_f64(),
                        ts: bar.ts_event.as_u64(),
                    },
                )
                .expect("alloc PyBar");
                let py_ctx = Py::new(
                    py,
                    PyCtx {
                        now,
                        signed_positions,
                        default_symbol: self.default_symbol.clone(),
                        outbox: RefCell::new(Vec::new()),
                    },
                )
                .expect("alloc PyCtx");
                if let Err(e) = self
                    .obj
                    .bind(py)
                    .call_method1("on_bar", (py_bar, py_ctx.clone_ref(py)))
                {
                    e.print(py);
                }
                let out: Vec<(String, f64, Option<f64>, Option<String>)> = py_ctx
                    .bind(py)
                    .borrow()
                    .outbox
                    .borrow()
                    .iter()
                    .map(|i| (i.side.clone(), i.qty, i.limit_px, i.symbol.clone()))
                    .collect();
                out
            });
            for (side, qty, limit_px, symbol) in intents {
                let sym = symbol.unwrap_or_else(|| self.default_symbol.clone());
                let Some(meta) = self.instruments.get(&sym) else {
                    continue; // unknown symbol: drop the intent (kernel validates upstream too)
                };
                let side = if side.eq_ignore_ascii_case("sell") {
                    OrderSide::Sell
                } else {
                    OrderSide::Buy
                };
                let Ok(q) = Quantity::from_f64(qty, meta.size_precision) else {
                    continue;
                };
                match limit_px {
                    None => {
                        ctx.submit_market(meta.iid.clone(), side, q);
                    }
                    Some(px) => {
                        if let Ok(p) = Price::from_f64(px, meta.price_precision) {
                            ctx.submit_limit(meta.iid.clone(), side, q, p);
                        }
                    }
                }
            }
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
    ) -> PyResultObj {
        let cfg = BacktestConfig::new(Venue::from(venue), instruments, settle, starting);
        let adapter = PyStrategyAdapter {
            obj: strategy,
            instruments: metas,
            default_symbol,
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
        let mut events = Vec::with_capacity(bars.len());
        for (ts, open, high, low, close, volume) in bars {
            events.push(build_bar_event(&meta, ts, open, high, low, close, volume)?);
        }
        let starting =
            Money::from_decimal(Decimal::from_f64(starting_balance).unwrap_or(Decimal::ZERO), settle)
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
        ))
    }

    /// Multi-instrument backtest: many symbols through ONE kernel (shared Cache/sim/risk/portfolio),
    /// fed a single timestamp-tagged bar stream. The Rust core is already multi-instrument; this is
    /// the Python-facing entry point. The Python `Strategy` reads `bar.symbol` and targets orders
    /// via the optional `symbol` arg on `ctx.submit_market`/`submit_limit`/`position`.
    #[pyfunction]
    #[pyo3(signature = (strategy, venue, starting_balance, instruments, bars))]
    pub fn run_backtest_multi(
        strategy: Py<PyAny>,
        venue: String,
        starting_balance: f64,
        // Per instrument: `(symbol, price_precision, size_precision, maker_fee, taker_fee)`.
        instruments: Vec<(String, u8, u8, f64, f64)>,
        // Tagged OHLCV stream: `(ts_ns, symbol, open, high, low, close, volume)` (any order; sorted).
        bars: Vec<(u64, String, f64, f64, f64, f64, f64)>,
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
        let starting =
            Money::from_decimal(Decimal::from_f64(starting_balance).unwrap_or(Decimal::ZERO), settle)
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
        ))
    }

    /// The `qv_py` extension module.
    #[pymodule]
    fn qv_py(m: &Bound<'_, PyModule>) -> PyResult<()> {
        m.add_class::<PyBar>()?;
        m.add_class::<PyCtx>()?;
        m.add_class::<PyResultObj>()?;
        m.add_function(wrap_pyfunction!(run_backtest, m)?)?;
        m.add_function(wrap_pyfunction!(run_backtest_multi, m)?)?;
        m.add("__doc__", "VeloxQuant Rust core exposed to Python (PyO3).")?;
        Ok(())
    }
}
