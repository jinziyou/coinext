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
- **Walk-forward optimization** (`qv_optimize`): genuine walk-forward (rolling + anchored/expanding
  splits) that optimizes params IN-SAMPLE per fold and re-scores them OUT-of-sample, reporting
  **OOS degradation** (the overfitting guard). Pure-Python grid search by default (no extra deps) or
  Optuna TPE (`--optuna`); each evaluation runs the AUTHORITATIVE event-driven backtest. `qv optimize
  [--mode rolling|anchored] [--optuna] [--from-lake]`. Unit + integration tested.
- **Analytics depth** (`qv_analytics`): FIFO round-trip **trade reconstruction** + trade-level stats
  (win rate, profit factor, avg/largest win-loss, expectancy, exposure, turnover); heuristic **bias
  screens** (look-ahead: pre-data / past-span fills, non-monotonic equity; overfitting: near-perfect
  win rate, zero-drawdown-with-gains) folded into the tear sheet; optional matplotlib **tear-sheet
  plots** (equity / drawdown / per-bar returns). Unit + integration tested.
- **OHLC-aware fills** (`qv-sim` + `qv-py`): the PyO3 bridge now passes real **OHLC** (not a
  close-flattened bar), so the sim matches resting **limit** orders against each bar's high/low — a
  limit fills on an intrabar wick its close never reached. Python `Strategy` gains `ctx.submit_limit`
  and a `LimitMaker` example; `qv backtest --strategy limit-maker` and `DataLake.read_ohlc` exercise
  it end-to-end. Rust (sim) + Python (bridge) tested, incl. the close-only-vs-OHLC discriminator.
- **Multi-instrument backtests** (`qv-py` + `qv_backtest`): the bridge now runs MANY symbols through
  ONE kernel (shared Cache/sim/risk/portfolio) via `run_backtest_multi` + `qv_backtest.run_multi`.
  `bar.symbol` tags each event and `ctx.{submit_market,submit_limit,position}` take an optional
  `symbol` (single-instrument calls still default it). A `MultiSma` example + `qv backtest-multi`
  drive it; tested incl. position isolation and **portfolio == sum of standalone single runs**.
- **Richer BrokerageModel** (`qv-sim`): (a) **volume-participation partial fills** — a resting limit
  fills at most `participation_rate` of each bar's volume, so a large order fills across several bars
  as multiple `PartiallyFilled` events (the FSM/Position machinery already supported this; the sim
  now emits it, with a stable per-order venue id and re-rested residuals); (b) **OHLC-aware market
  slippage** — market fills add an intrabar-range component on top of the base bps, capped at the
  bar's high/low. Real **volume** is threaded through the bridge (`bar.volume`, OHLCV bars,
  `DataLake.read_ohlcv`); `volume=0` means "no cap" (close-only series unchanged). Rust + Python
  tested (a qty-5 limit splitting into 5 fills over 5 bars; slippage direction/cap; backward compat).

- **Limit-order queue position** (`qv-sim`): a resting limit waits behind an estimated
  `queue_ahead_factor` × bar-volume queue at its price; a price that trades **through** the level
  sweeps it (fills), one that merely **touches** it pays the queue down first (you wait your turn).
  Composes with the participation cap (the per-bar share is the queue budget); `0` = off (default,
  backward-compatible — most fills are through-crosses). Opt in via `qv_backtest.run(...,
  queue_ahead_factor=0.5)`. Rust + Python tested (touch waits, through fills immediately).
- **Broadened Strategy event surface** (`qv-py`): the full trait is bridged — `on_start`/`on_stop`,
  `on_order_filled`/`on_order_event`, `on_timer` (via `ctx.set_timer`), plus `on_quote`/`on_trade`
  (feed-dependent). `ctx.cancel` and a cancelable client_order_id returned from `submit_*`.
- **Vectorized research screen + cross-check** (`qv_screen`): a FAST, NON-authoritative numpy screen
  (signals → positions → mark-to-market PnL in one pass) for coarse `(fast, slow)` sweeps, plus
  `cross_check_vs_event` wiring the advisory `qv_parity.cross_check` drift warning against the
  AUTHORITATIVE event-driven runner. `qv screen [--from-lake]`. Fixed a real bucket-alignment bug
  (real bars close at `:59.999`, so event-fill latency crosses the minute boundary) by snapping both
  fill streams to the bar grid. Unit + integration tested.
- **Stop-market orders** (`qv-sim`): a stop-market rests until the market crosses its `trigger` (buy:
  price rises to it / sell: falls to it), then takes liquidity at the market — a stop-loss or
  breakout entry. Fills at the trigger, worsened to the bar if the price gapped past it, then slipped
  by the brokerage model; volume-capped on bars, fills on ticks too. `ctx.submit_stop(side, qty,
  trigger)` (cancelable). `OrderFactory.stop_market` + `StrategyContext.submit_stop_market`. Rust
  (trigger cross, gap fill) + Python (breakout fires, cancel before trigger) tested.
- **Streaming indicators in Python** (`qv_indicators`): the tested Rust `qv-indicators` (SMA / EMA /
  RSI / ATR / **MACD / Bollinger / VWAP**) are bridged through `qv_py`, so a Python strategy uses the
  IDENTICAL incremental implementation as warm-up / live rather than a re-rolled copy. Plus a pure-
  Python **`Resampler`** for multi-timeframe (1m → 5m / 1h) so a strategy can drive indicators off
  coarser bars. `from qv_indicators import Rsi`; an `RsiReversion` example. Values verified equal to
  the Rust crate + the existing hand-rolled `_Sma`.
- **Market-order volume participation** (`qv-sim`): a large market order takes at most
  `participation × bar volume` at submit; the remainder rests as an AGGRESSIVE order (taker, filled
  at each later bar's market price, also volume-capped) instead of dumping in one bar. Reuses the
  resting/two-phase machinery + the cached bar volume; close-only series (volume 0) fill fully, so
  existing behavior is unchanged. Rust + Python tested.
- **Quote/trade tick feed** (`qv-py` + `qv_backtest` + `qv_data`): `run(..., quotes=…, trades=…)`
  interleaves optional tick streams with the bars, so `on_quote`/`on_trade` fire AND ticks drive the
  mark + resting-limit fills + the bid/ask reference for market orders. Synthesize from bars
  (`synth_quotes`/`synth_trades`) or feed REAL Binance `aggTrades` (`fetch_binance_agg_trades`, no
  key). The kernel now samples the equity curve at BAR cadence so sub-bar ticks don't distort the
  annualized metrics. Tested incl. a real-aggTrades run and a tick-driven limit fill.

## Next — research side (recommended order)

1. **Research notebook** — an end-to-end demo (download → screen → optimize → backtest → tear-sheet)
   over real data, stringing the features together.
2. **Strategy ergonomics** — historical bookTicker (websocket capture) for real quotes; remaining
   order types (stop-limit, trailing-stop) in the sim.

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
