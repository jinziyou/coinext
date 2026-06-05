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

    /// A bar handed to a Python strategy. Minimal surface (close + ts); the full domain mirror is a
    /// follow-up.
    #[pyclass(unsendable, name = "Bar")]
    pub struct PyBar {
        #[pyo3(get)]
        pub close: f64,
        #[pyo3(get)]
        pub open: f64,
        #[pyo3(get)]
        pub high: f64,
        #[pyo3(get)]
        pub low: f64,
        #[pyo3(get)]
        pub ts: u64,
    }

    /// The strategy context exposed to Python during a handler. Reads (now, position) are snapshot
    /// before the call; `submit_market` accumulates intents that the adapter replays onto the real
    /// Rust `StrategyContext` after the handler returns (safe — no Rust references cross the GIL).
    #[pyclass(unsendable, name = "Ctx")]
    pub struct PyCtx {
        #[pyo3(get)]
        pub now: u64,
        signed_position: f64,
        outbox: RefCell<Vec<(String, f64)>>,
    }

    #[pymethods]
    impl PyCtx {
        /// Signed position quantity for the (single) backtest instrument (+long / -short).
        fn position(&self) -> f64 {
            self.signed_position
        }
        /// Queue a market order. `side` is "buy" or "sell".
        fn submit_market(&self, side: &str, qty: f64) {
            self.outbox.borrow_mut().push((side.to_string(), qty));
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

    /// Bridges a Python `Strategy` object into the synchronous Rust `Strategy` trait.
    struct PyStrategyAdapter {
        obj: Py<PyAny>,
        iid: InstrumentId,
        size_precision: u8,
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
            let signed = signed_qty(ctx, &self.iid);
            let now = ctx.now_ns().as_u64();
            // Acquire the GIL only for the handler; collect intents, then replay GIL-free.
            let intents: Vec<(String, f64)> = Python::attach(|py| {
                let py_bar = Py::new(
                    py,
                    PyBar {
                        close: bar.close.as_f64(),
                        open: bar.open.as_f64(),
                        high: bar.high.as_f64(),
                        low: bar.low.as_f64(),
                        ts: bar.ts_event.as_u64(),
                    },
                )
                .expect("alloc PyBar");
                let py_ctx = Py::new(
                    py,
                    PyCtx {
                        now,
                        signed_position: signed,
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
                let out = py_ctx.bind(py).borrow().outbox.borrow().clone();
                out
            });
            for (side, qty) in intents {
                let side = if side.eq_ignore_ascii_case("sell") {
                    OrderSide::Sell
                } else {
                    OrderSide::Buy
                };
                if let Ok(q) = Quantity::from_f64(qty, self.size_precision) {
                    ctx.submit_market(self.iid.clone(), side, q);
                }
            }
        }
    }

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
        bars: Vec<(u64, f64)>,
        price_precision: u8,
        size_precision: u8,
        maker_fee: f64,
        taker_fee: f64,
    ) -> PyResult<PyResultObj> {
        let usdt = Currency::new("USDT", 8)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
        let base = Currency::new("BASE", 8)
            .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
        let iid = InstrumentId::new(Symbol::from(symbol.as_str()), Venue::from(venue.as_str()));

        let inst: Arc<dyn Instrument> = Arc::new(CurrencyPair {
            id: iid.clone(),
            base,
            quote: usdt,
            price_precision,
            size_precision,
            price_increment: Price::from_raw(1, price_precision)
                .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?,
            size_increment: Quantity::from_raw(1, size_precision)
                .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?,
            min_notional: None,
            maker_fee: Decimal::from_f64(maker_fee).unwrap_or(Decimal::ZERO),
            taker_fee: Decimal::from_f64(taker_fee).unwrap_or(Decimal::ZERO),
        });

        let bar_type = BarType {
            instrument_id: iid.clone(),
            spec: BarSpec {
                step: 1,
                aggregation: BarAggregation::Minute,
                price_type: PriceType::Last,
            },
            source: AggregationSource::External,
        };
        let mut events = Vec::with_capacity(bars.len());
        for (ts, close) in bars {
            let px = Price::from_f64(close, price_precision)
                .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
            events.push(MarketEvent::Bar(Bar {
                bar_type: bar_type.clone(),
                open: px,
                high: px,
                low: px,
                close: px,
                volume: Quantity::from_f64(1.0, size_precision).unwrap(),
                ts_event: UnixNanos(ts),
                ts_init: UnixNanos(ts),
            }));
        }

        let starting = Money::from_decimal(
            Decimal::from_f64(starting_balance).unwrap_or(Decimal::ZERO),
            usdt,
        )
        .map_err(|e| pyo3::exceptions::PyValueError::new_err(e.to_string()))?;
        let cfg = BacktestConfig::new(Venue::from(venue.as_str()), vec![inst], usdt, starting);

        let adapter = PyStrategyAdapter {
            obj: strategy,
            iid,
            size_precision,
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
        Ok(PyResultObj {
            starting_equity: res.starting_equity,
            final_equity: res.final_equity,
            total_return,
            fills: res.fills,
            orders_submitted: res.orders_submitted,
            orders_denied: res.orders_denied,
            realized_pnl: res.realized_pnl,
            equity_curve: res.equity_curve,
            fills_log: res.fills_log,
        })
    }

    /// The `qv_py` extension module.
    #[pymodule]
    fn qv_py(m: &Bound<'_, PyModule>) -> PyResult<()> {
        m.add_class::<PyBar>()?;
        m.add_class::<PyCtx>()?;
        m.add_class::<PyResultObj>()?;
        m.add_function(wrap_pyfunction!(run_backtest, m)?)?;
        m.add("__doc__", "VeloxQuant Rust core exposed to Python (PyO3).")?;
        Ok(())
    }
}
