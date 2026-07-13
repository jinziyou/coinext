# Coinext changelog

Historical record of **verified** research/backtest work. For current status and next steps see
[`ROADMAP.md`](ROADMAP.md). For architecture see root [`ARCHITECTURE.md`](../ARCHITECTURE.md).

Language: English (design/changelog). Operator quick-start remains Chinese in the root `README.md`
— see [`README.md`](README.md) language note.

## 2026-07-13 — hygiene + research/live partial stack

- Sample lake Parquet fixtures under `data/sample` + quote recording helpers (`coinext_data.quotes`).
- Research loop defaults to sample lake; synthetic override via `COINEXT_RESEARCH_USE_LAKE=0`.
- Modularized `coinext-kernel` (`types`/`backtest`/`live`/`paper`) and `coinext-sim` (`port`).
- Split `coinext_parity` into `session`/`metrics`/`gate`.
- File-backed live reconcile + TradingNode dry-run path; API `state_store` for runs/fills/catalog.
- Expanded regression goldens; CI enforces `ruff format --check`; pydantic/pyyaml are core deps.
- Docs: `CHANGELOG.md`, `STATUS.md`, doc index, language boundary.

## 2026-07-13b — paper live + quote capture + exec-svc partial

- `coinext_py.build_paper_kernel` / `coinext live --paper`: LiveKernel offline via ReplayDataClient + PaperFillExec.
- `coinext capture-quotes`: public bookTicker REST poll (+ optional websockets WS).
- Python `coinext_live.persist`: SQLite SeqCursor + order_events aligned with Rust schemas.
- `coinext-exec-svc`: long-running process with SQLite persistence, `:8081` kill-switch control, `:9102` metrics stub.

## 2026-07-13c — ingest sinks + exec-svc Redis control

- `coinext-ingest`: NDJSON event log + OHLCV Parquet under Hive lake layout + optional Redis
  `coinext.market` Envelope XADD; offline synthetic smoke path (default build).
- `ParquetWriter::write_ohlcv` for `coinext_data.DataLake` schema compatibility.
- `coinext-exec-svc`: consumes Redis `coinext.control` for `CtrlKillSwitch` alongside HTTP control.

## 2026-07-13d — paper OMS + monthly lake merge + ingest metrics

- `coinext-exec-svc` paper OMS: `coinext.exec.cmd` → SeqCursor + EventStore → `coinext.exec` reports;
  kill-switch denials; Prometheus OMS counters.
- Ingest monthly merge into `{yyyymm}.parquet` (dedup by ts); `:9101/metrics` counters.
- Python `Publisher.publish_exec_command` + `STREAM_EXEC_CMD`.

## 2026-07-13e — venue bridge + reconnect metrics + redis ITs

- exec-svc: optional `BinanceExecutionClient` when `COINEXT__BINANCE__API_KEY/SECRET` set (testnet
  default); user-stream report pump → event store + `coinext.exec`.
- ingest live path: reconnect loop with `ws_connects` / `ws_reconnects` / `ws_errors` metrics.
- `tests/operations-interface/test_redis_bus_integration.py` (skip if Redis down).

## 2026-07-13f — reconcile-on-start + book gaps + CI Redis

- Venue **reconcile-on-start**: `openOrders` vs event-log open set; seed missing Accepted; orphans
  counted; Prometheus `coinext_reconcile_*` gauges.
- Ingest **book gap tracker** on depth deltas → `coinext_ingest_book_gaps_total`.
- CI python job: Redis 7 service + paper `exec-svc` for bus integration tests.

## Verified capabilities (changelog)

- **Rust core** (`cargo test` green): fixed-precision value types, event-sourced Order FSM +
  Position PnL, hexagonal ports, cache, in-proc bus, streaming indicators, pre-trade risk gate,
  portfolio, data/exec engines, **SimulatedExchange** (BrokerageModel + delayed-fill queue on the
  time-frontier), **deterministic synchronous kernel**. `backtesting-simulation/examples/rust/backtest-sma` runs end-to-end.
- **PyO3 bridge** (`coinext-py`, maturin): a Python `Strategy` runs through the SAME Rust kernel via
  `PyStrategyAdapter` (GIL per event) — the cross-FFI parity proof (`pytest` green).
- **Binance adapter**: real WS market data (verified live) + REST execution (idempotent submit,
  reconcile) + InstrumentProvider; `coinext-network` (rustls REST/WS, governor, HMAC). Unit-tested.
- **Persistence**: rusqlite event store + crash-recovery SeqCursor + Parquet writer.
- **Parity gates**: `coinext_parity` (signal agreement / fill-price deviation bps / equity corr /
  return diff) + `coinext parity` demo using a perturbed backtest session. `coinext testnet-gate`
  now supports replayable `--recorded-session` JSON fixtures and `--record-out` capture for the
  real-klines → backtest → Binance testnet loop; `--no-testnet` remains a dry-run path only.
- **Local Parquet data lake**: paginated downloader (breaks the 1000/req limit), partitioned store
  (dedup/idempotent), HistoryReader; `coinext download` / `coinext backtest --from-lake` / `coinext catalog`.
- **Walk-forward optimization** (`coinext_optimize`): genuine walk-forward (rolling + anchored/expanding
  splits) that optimizes params IN-SAMPLE per fold and re-scores them OUT-of-sample, reporting
  **OOS degradation** (the overfitting guard). Pure-Python grid search by default (no extra deps) or
  Optuna TPE (`--optuna`); each evaluation runs the AUTHORITATIVE event-driven backtest. `coinext optimize
  [--mode rolling|anchored] [--optuna] [--from-lake]`. Unit + integration tested.
- **Analytics depth** (`coinext_analytics`): FIFO round-trip **trade reconstruction** + trade-level stats
  (win rate, profit factor, avg/largest win-loss, expectancy, exposure, turnover); heuristic **bias
  screens** (look-ahead: pre-data / past-span fills, non-monotonic equity; overfitting: near-perfect
  win rate, zero-drawdown-with-gains) folded into the tear sheet; optional matplotlib **tear-sheet
  plots** (equity / drawdown / per-bar returns). Unit + integration tested.
- **OHLC-aware fills** (`coinext-sim` + `coinext-py`): the PyO3 bridge now passes real **OHLC** (not a
  close-flattened bar), so the sim matches resting **limit** orders against each bar's high/low — a
  limit fills on an intrabar wick its close never reached. Python `Strategy` gains `ctx.submit_limit`
  and a `LimitMaker` example; `coinext backtest --strategy limit-maker` and `DataLake.read_ohlcv` exercise
  it end-to-end. Rust (sim) + Python (bridge) tested, incl. the close-only-vs-OHLC discriminator.
- **Multi-instrument backtests** (`coinext-py` + `coinext_backtest`): the bridge now runs MANY symbols through
  ONE kernel (shared Cache/sim/risk/portfolio) via `run_backtest_multi` + `coinext_backtest.run_multi`.
  `bar.symbol` tags each event and `ctx.{submit_market,submit_limit,position}` take an optional
  `symbol` (single-instrument calls still default it). A `MultiSma` example + `coinext backtest-multi`
  drive it; tested incl. position isolation and **portfolio == sum of standalone single runs**.
- **Richer BrokerageModel** (`coinext-sim`): (a) **volume-participation partial fills** — a resting limit
  fills at most `participation_rate` of each bar's volume, so a large order fills across several bars
  as multiple `PartiallyFilled` events (the FSM/Position machinery already supported this; the sim
  now emits it, with a stable per-order venue id and re-rested residuals); (b) **OHLC-aware market
  slippage** — market fills add an intrabar-range component on top of the base bps, capped at the
  bar's high/low. Real **volume** is threaded through the bridge (`bar.volume`, OHLCV bars,
  `DataLake.read_ohlcv`); `volume=0` means "no cap" (close-only series unchanged). Rust + Python
  tested (a qty-5 limit splitting into 5 fills over 5 bars; slippage direction/cap; backward compat).

- **Limit-order queue position** (`coinext-sim`): a resting limit waits behind an estimated
  `queue_ahead_factor` × bar-volume queue at its price; a price that trades **through** the level
  sweeps it (fills), one that merely **touches** it pays the queue down first (you wait your turn).
  Composes with the participation cap (the per-bar share is the queue budget); `0` = off (default,
  backward-compatible — most fills are through-crosses). Opt in via `coinext_backtest.run(...,
  queue_ahead_factor=0.5)`. Rust + Python tested (touch waits, through fills immediately).
- **Broadened Strategy event surface** (`coinext-py`): the full trait is bridged — `on_start`/`on_stop`,
  `on_order_filled`/`on_order_event`, `on_timer` (via `ctx.set_timer`), plus `on_quote`/`on_trade`
  (feed-dependent). `ctx.cancel` and a cancelable client_order_id returned from `submit_*`.
- **Vectorized research screen + cross-check** (`coinext_screen`): a FAST, NON-authoritative numpy screen
  (signals → positions → mark-to-market PnL in one pass) for coarse `(fast, slow)` sweeps, plus
  `cross_check_vs_event` wiring the advisory `coinext_parity.cross_check` drift warning against the
  AUTHORITATIVE event-driven runner. `coinext screen [--from-lake]`. Fixed a real bucket-alignment bug
  (real bars close at `:59.999`, so event-fill latency crosses the minute boundary) by snapping both
  fill streams to the bar grid. Unit + integration tested.
- **Stop orders** (`coinext-sim`): **stop-market** rests until the market crosses its `trigger` (buy:
  rises to it / sell: falls to it), then takes liquidity at the market (stop-loss / breakout) — fills
  at the trigger, worsened to the bar on a gap, slipped by the brokerage model; **stop-limit**
  converts on trigger to a resting limit at its price (bounded slippage — fills only at the limit or
  better); **trailing-stop** trails the favorable extreme by an `offset` (the trigger ratchets
  monotonically toward the market and fires on a pull-back past the offset). `ctx.submit_stop` /
  `submit_stop_limit` / `submit_trailing` (all cancelable). `on_market` dispatches by order type.
  Rust (trigger cross, gap fill, limit conversion, no-fill-below-limit, trail ratchet + no-fire) +
  Python (breakout, cancel, stop-limit fill, trailing locks in a gain) tested.
- **Streaming indicators in Python** (`coinext_indicators`): the tested Rust `coinext-indicators` (SMA / EMA /
  RSI / ATR / **MACD / Bollinger / VWAP**) are bridged through `coinext_py`, so a Python strategy uses the
  IDENTICAL incremental implementation as warm-up / live rather than a re-rolled copy. Plus a pure-
  Python **`Resampler`** for multi-timeframe (1m → 5m / 1h) so a strategy can drive indicators off
  coarser bars. `from coinext_indicators import Rsi`; an `RsiReversion` example. Values verified equal to
  the Rust crate + the existing hand-rolled `_Sma`.
- **Market-order volume participation** (`coinext-sim`): a large market order takes at most
  `participation × bar volume` at submit; the remainder rests as an AGGRESSIVE order (taker, filled
  at each later bar's market price, also volume-capped) instead of dumping in one bar. Reuses the
  resting/two-phase machinery + the cached bar volume; close-only series (volume 0) fill fully, so
  existing behavior is unchanged. Rust + Python tested.
- **Quote/trade tick feed** (`coinext-py` + `coinext_backtest` + `coinext_data`): `run(..., quotes=…, trades=…)`
  interleaves optional tick streams with the bars, so `on_quote`/`on_trade` fire AND ticks drive the
  mark + resting-limit fills + the bid/ask reference for market orders. Synthesize from bars
  (`synth_quotes`/`synth_trades`) or feed REAL Binance `aggTrades` (`fetch_binance_agg_trades`, no
  key). The kernel now samples the equity curve at BAR cadence so sub-bar ticks don't distort the
  annualized metrics. Tested incl. a real-aggTrades run and a tick-driven limit fill.

- **Derivatives instrument foundation** (`coinext-model` + `coinext-py` + `coinext_backtest`): three new concrete
  `Instrument` types — `Equity` (linear, mult 1), `FuturesContract` (linear + `expiry_ns` +
  underlying), `OptionContract` (`strike`/`right`/`expiry_ns`/`underlying`/contract `multiplier`) —
  plus `AssetClass::Option` + `OptionRight` and default-`None` trait accessors (`expiry_ns`/`strike`/
  `option_right`/`underlying`) so spot/perp are unchanged. PnL already scales by `multiplier`/
  `is_inverse`, so all three trade through the SAME kernel as priced instruments. Python:
  `coinext_backtest.Instrument.{equity,future,option}(...)` + `run(..., instrument=...)`. Tested (mult-10
  future = 10× the spot PnL, mult-100 option = 100×, equity == spot, option accessors, intrinsic
  value). **Phase 1 of 4** — expiry settlement/exercise, BS pricing+greeks, and margin follow.

- **Derivatives expiry settlement + exercise** (`coinext-kernel`, Phase 2/4): the kernel collects dated
  contracts' `expiry_ns` and adds the expiry as a frontier; after the market event at that frontier,
  each open position is closed by a synthetic settlement fill — a **future** cash-settles to its
  final mark, an **option** settles to its intrinsic value vs the underlying's spot (`max(S−K,0)` /
  `max(K−S,0)` × multiplier), expiring worthless if OTM (falls back to its own mark if the underlying
  isn't fed). Resting orders on the dead contract are canceled; the settlement fires `on_order_filled`
  + counts as a fill. Deterministic (sorted expiries, fires once each). Bar-only backtests are
  unaffected. Rust-tested (option ITM/OTM intrinsic, future cash-settle).

- **Option pricing + greeks** (`coinext-derivatives` + `coinext-py` + `coinext_derivatives`, Phase 3/4): a new
  pure-`f64`, zero-dep crate — Black-Scholes `price`, all five `greeks` (delta/gamma/vega/theta/rho),
  and an `implied_vol` solver (Newton + bisection fallback), with a no-dep normal CDF (A&S erf).
  Bridged to Python as `coinext_py.bs_price`/`bs_greeks`/`implied_vol` and a `coinext_derivatives` module
  (`bs_price`/`greeks`/`implied_vol`, `right="call"/"put"`). A strategy can price options, compute
  greeks, and back out IV from market premiums with the SAME math the core uses. Tested against
  textbook reference values, put-call parity, the greek identities, and IV round-trips (Rust +
  Python).

- **Margin / leverage / liquidation** (`coinext-ports` + `coinext-risk-engine` + `coinext-portfolio` + `coinext-kernel`,
  Phase 4/4): `RiskLimits` gains `leverage` + `maintenance_margin_rate`; the risk gate denies an
  order that would need more **initial margin** (`added_notional / leverage`) than free equity
  (`Portfolio::equity` − margin in use); the kernel runs a mark-to-market **maintenance** check after
  each bar and **liquidates** (force-flattens every position at its mark, once) when equity falls
  below `gross × rate`. `run(..., leverage=, maintenance_margin_rate=)`; both 0 = fully funded (the
  default, byte-identical to before). Rust (liquidation fires / doesn't without a rate) + Python
  (over-leveraged order denied, within-limit allowed, liquidation vs recovery) tested. **The
  derivatives engine (instruments → expiry settlement → BS pricing/greeks → margin) is complete.**


