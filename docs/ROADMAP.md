# Coinext Roadmap

> Current phase: **research-first, no live trading yet.**
>
> Layout: **status snapshot** → **done (verified)** → **next** → **deferred (live/ops)** →
> **open questions**. See root [`ARCHITECTURE.md`](../ARCHITECTURE.md), build order + open questions
> in [`ARCHITECTURE.md`](ARCHITECTURE.md), the doc index in [`README.md`](README.md),
> [`CHANGELOG.md`](CHANGELOG.md), [`STATUS.md`](STATUS.md), and [`TESTNET.md`](TESTNET.md).

## Status snapshot

| Area | State | Notes |
|---|---|---|
| Domain + ports + cache + in-proc bus | ✅ verified | fixed-precision values, Order FSM, hexagonal ports |
| Deterministic Kernel + SimulatedExchange | ✅ verified | modularized (`types`/`backtest`/`live` + sim `port`) |
| PyO3 bridge + Python Strategy API | ✅ verified | same engines as Rust path; multi-instrument; full event surface |
| Data lake + sample fixture + quotes | ✅ verified | committed `data/sample` Parquet; quote record/synth helpers |
| Research loop | ✅ verified | defaults to sample lake when present; screen→optimize→backtest |
| Analytics / screen / walk-forward / derivatives | ✅ verified | tear sheet, bias screens, BS greeks, margin/liquidation |
| Binance adapter + network transport | ✅ unit-tested | public MD verified live; testnet fills need keys |
| Parity gates | ✅ verified | hard `run_gate` + advisory cross-check + recorded fixtures |
| LiveKernel / TradingNode | 🚧 partial | dry-run + **paper** LiveKernel (no keys); file/SQLite reconcile |
| API read paths | 🚧 partial | local `state_store` + lake catalog; Postgres still deferred |
| exec-svc | 🚧 partial | paper OMS + optional Binance venue (API keys) + kill-switch |
| ingestor | 🚧 partial | monthly Parquet + Redis + `:9101` (connect/reconnect counters) |
| UI / risk-monitor | 🚧 scaffold | entrypoints compile; production polish deferred |
| Quote capture | ✅ verified | `coinext capture-quotes` REST poll (+ optional WS) |

**How to verify locally:** `just verify` (Rust workspace + live-edge crates + compose topology), then
`just py-build && just py-test` for the Python control plane. CI also enforces `ruff format --check`.

## Done — verified

See the **status snapshot** above and the full narrative in [`CHANGELOG.md`](CHANGELOG.md).

## Next — remaining research / polish

1. **Long-running quote capture service** — wrap `capture-quotes` as a compose sidecar writing
   rolling partitions (today: CLI REST/WS session recorder).
2. **Optional sample lake expansion** — more symbols/intervals under `data/sample` as needed by demos
   (crypto fixtures present; equity daily sample set added 2026-07-14).
2b. **Equity research path (done 2026-07-14)** — venue catalog + Yahoo OHLCV + `@default` universes +
   sample fixtures; live broker adapters still deferred.
2c. **A股 / ETF / 美股 / 港股 (done 2026-07-14)** — market groups (`ASHARE`/`US`/`HK`/`ETF`),
   `@etf` presets, A-share code auto-routing, AMEX + expanded sample fixtures; live brokers deferred.
2d. **Equity research polish (done 2026-07-14)** — `instrument_spec` fees/lots, `sh`/`sz`/`hk`
   prefixes, CLI equity defaults (`1d×365`), research-loop multi-market demo; live brokers deferred.
2e. **Calendar + multi-ccy + paper broker (done 2026-07-14)** — CN/US/HK calendars, halt filter,
   `FxBook`/`--base-ccy`, `PaperEquityBroker` + IB scaffold.
2f. **IB fill loop + A-share rules + FX lake (done 2026-07-14)** — `ib_insync` paper path, T+1/涨跌停,
   `venue=FX` sample fixtures; Kernel equity ExecutionClient still deferred.
2g. **Paper-equity CLI + session hours (done 2026-07-14)** — `coinext paper-equity` / `download-fx`,
   bar replay engine, intraday session filter; Kernel equity ExecutionClient still deferred.
2h. **Kernel T+1 + multi paper + IB runbook (done 2026-07-14)** — OMS deny same-day A-share sells,
   portfolio paper replay, `ib-status` + `docs/IB_PAPER.md`; full IB Kernel port still deferred.
2i. **Kernel 涨跌停 + equity quick path (done 2026-07-14)** — `PriceLimit` OMS, e2e tests,
   `docs/EQUITY_RESEARCH.md` + research_loop T+1 smoke; stock live ExecutionClient still deferred.
3. **Regression golden review** — re-pin intentionally when BrokerageModel economics change.

## Deferred — live / ops (start when ready to trade)

Intentionally parked while the focus is research (see [`ARCHITECTURE.md`](../ARCHITECTURE.md) §4, §7):

- **Full live venue loop** — Binance WS/REST behind `build_kernel` (keys required); paper path
  (`build_paper_kernel` / `coinext live --paper`) already exercises LiveKernel offline.
- **Venue OMS hardening** — margin/instrument filters, richer partial-fill accounting
  (reconcile-on-start + Binance route already available with keys).
- **Depth resync automation metrics** — adapter-level resync counts beyond coarse sequence gaps.
- **Observability SLOs end-to-end** — real histograms from trader/ingest/exec exporters.
- **Promotion hardening** — SOPS/Vault secrets, IP allowlist, withdrawal disabled; recorded sandbox
  gate remains the mandatory pre-live check (`coinext testnet-gate` / `run_gate`).

## Open questions

Tracked in [`ARCHITECTURE.md` (open questions)](ARCHITECTURE.md): multi-node sharding & ordered replay; per-event
Strategy compute budget beyond the GIL baseline; concrete cross-check / sandbox-parity thresholds per
asset class; BrokerageModel fidelity ceiling; data-lake retention/downsampling; reconciliation edge
cases; SeqCursor namespacing across accounts; asset-class roadmap (inverse perps, futures, options).
