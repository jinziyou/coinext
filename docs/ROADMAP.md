# VeloxQuant Roadmap

> 当前阶段：**研究优先，暂不实盘**。下面按「现状 → 研究侧后续 → 实盘/运维（待启动）→ 开放问题」组织。
> 详见 [`ARCHITECTURE.md`](ARCHITECTURE.md)（§9 build order、§11 open questions）与 [`TESTNET.md`](TESTNET.md)。

## Done — verified

- **Rust core** (`cargo test` green): fixed-precision value types, event-sourced Order FSM +
  Position PnL, hexagonal ports, cache, in-proc bus, streaming indicators, pre-trade risk gate,
  portfolio, data/exec engines, **SimulatedExchange** (BrokerageModel + delayed-fill queue on the
  time-frontier), **deterministic synchronous kernel**. `examples/backtest-sma` runs end-to-end.
- **PyO3 bridge** (`qv-py`, maturin): a Python `Strategy` runs through the SAME Rust kernel via
  `PyStrategyAdapter` (GIL per event) — the cross-FFI parity proof (`pytest` green).
- **Binance adapter**: real WS market data (verified live) + REST execution (idempotent submit,
  reconcile) + InstrumentProvider; `qv-network` (rustls REST/WS, governor, HMAC). Unit-tested.
- **Persistence**: rusqlite event store + crash-recovery SeqCursor + Parquet writer.
- **Parity gate**: `qv_parity` (signal agreement / fill-price deviation bps / equity corr / return
  diff) + `qv testnet-gate` one-command loop (real data → backtest → testnet fills → gate).
- **Local Parquet data lake**: paginated downloader (breaks the 1000/req limit), partitioned store
  (dedup/idempotent), HistoryReader; `qv download` / `qv backtest --from-lake` / `qv catalog`.

## Next — research side (recommended order)

1. **Walk-forward optimization (Optuna)** — make `qv_optimize` real: TPE parameter search over the
   AUTHORITATIVE event-driven backtest, walk-forward / CV splits, in-sample vs **out-of-sample (OOS)**
   degradation reporting. Now that the data lake gives months of reproducible history, this is the
   highest-value next step (and OOS validation is the main guard against overfitting).
2. **Analytics depth** — trade-level stats (win rate, profit factor, avg trade, exposure, turnover),
   tear-sheet **plots** (equity/drawdown/returns), and actually implement the lookahead + recursion
   **bias detectors** (currently stubs) so every backtest is auto-screened.
3. **Backtest fidelity** — OHLC-aware fills in `qv-sim` (use the bar high/low already stored in the
   lake, not just close), multi-instrument backtests, richer `BrokerageModel` (queue/partial-fill).
4. **Vectorized research screen + cross-check** — the fast, NON-authoritative `populate_*` screen for
   coarse sweeps, with the advisory `qv_parity.cross_check` drift warning vs the event-driven runner.
5. **Strategy ergonomics** — broaden the Python `Strategy` surface beyond `on_bar` (quotes/trades/
   timers/order events), wire indicators, notebooks for the research loop.

## Deferred — live / ops (start when ready to trade)

Intentionally parked while the focus is research (see ARCHITECTURE.md §7):

- **Live `TradingNode` (Rust)** — assemble the continuously-running live/sandbox loop: async WS/REST
  I/O feeding the synchronous deterministic core over tokio MPSC, LiveClock + Binance clients +
  engines + risk, on testnet first. This is the prerequisite for everything below.
- **Persistence wiring + crash recovery** — event log + SeqCursor into the live exec path; `reconcile()`
  on restart (replay + diff venue truth).
- **Data lake in live** — `qv-ingest` writes Parquet + republishes; warm-up served from the SAME
  HistoryReader (the ONE history path) so indicators are identical in backtest and live.
- **Observability + cross-process bus** — Redis Streams carrying a versioned MessagePack `Envelope`
  across processes; `/metrics` (SLO histograms: `strategy_dispatch_ns`, `submit_to_ack_ns`,
  `ingest_to_publish_ns`, `risk_denials`, `ws_reconnects`, `book_gaps`); out-of-band `risk-monitor`
  with a global kill-switch; React dashboard on real data.
- **Promotion to live** — sandbox(testnet)-vs-backtest parity gate as the mandatory pre-live gate;
  secrets management (SOPS/Vault), IP allowlist, withdrawal disabled.

## Open questions

Tracked in [`ARCHITECTURE.md` §11](ARCHITECTURE.md): multi-node sharding & ordered replay; per-event
Strategy compute budget beyond the GIL baseline; concrete cross-check / sandbox-parity thresholds per
asset class; BrokerageModel fidelity ceiling; data-lake retention/downsampling; reconciliation edge
cases; SeqCursor namespacing across accounts; asset-class roadmap (inverse perps, futures, options).
