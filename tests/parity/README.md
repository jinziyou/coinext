# tests/parity/ — the parity test plan

Coinext's single most important property is **backtest↔live parity** (see
[`ARCHITECTURE.md`](../../ARCHITECTURE.md) §1, §6): ONE Strategy API, ONE set of engines, ONE deterministic core. Only
the Kernel-injected **Clock**, **Cache** contents, and **Data/Execution clients** differ between
`Backtest` / `Sandbox` / `Live`. These tests are the guardrails that keep that promise true.

There are two independent parity checks. Both are implemented in the **`coinext_parity`** package
(`python/coinext_parity/__init__.py`, stdlib-only) and exercised by `test_parity_gate.py`. A run is
reduced to a `coinext_parity.SessionResult` (an `equity_curve` of `(ts_ns, equity)` + a `fills` log of
`(ts_ns, side, qty, px)`); `SessionResult.from_backtest(result)` builds it from a `coinext_py`
`BacktestResult` (which exposes `equity_curve` + `fills_log`).

## 1. Event-driven vs vectorized screen cross-check (advisory)

The authoritative runner is the **event-driven** `coinext_backtest.run` (through the Rust kernel: Risk +
Exec + BrokerageModel + SimulatedExecutionClient). A separate **vectorized** `populate_*` research
screen exists for fast parameter sweeps but is **explicitly non-authoritative** — it skips the
Risk/Exec/Brokerage path, so it can never validate a strategy for promotion (ARCHITECTURE.md §1, §5).

The cross-check runs the SAME strategy logic both ways over the SAME bars and measures **drift**:

- **What is compared:** signal timing (which bars trigger entries/exits) and a coarse return proxy.
- **What is NOT expected to match:** exact PnL — the vectorized path has no fees, slippage, latency,
  or partial fills, so absolute equity will differ by design.
- **Gate type:** **advisory**. A drift beyond a configured threshold emits a *warning*, not a hard
  failure — it flags that the fast screen is misleading for this strategy, never that the strategy
  is invalid. Only the event-driven result is a parity surface.

**Implementation.** `coinext_parity.cross_check(event_result, vector_result, *, max_pnl_diff_bps=50.0)
-> list[str]` returns advisory warning strings and **never raises**. It warns when signal timing
drifts (the two methods fire on different bar buckets/sides) or when the coarse return proxy drifts
beyond `max_pnl_diff_bps`. An empty list means the fast screen tracks the authoritative runner for
this strategy; a non-empty list flags only that the screen is misleading here — never that the
strategy is invalid. Covered by `test_parity_gate.py::test_cross_check_*`.

## 2. Sandbox-vs-backtest acceptance gate (hard, promotion gate)

This is the promotion gate before live (build order step 18; see `docs/ARCHITECTURE.md`). Run the SAME strategy with the
SAME `RunConfig` and the SAME local-HistoryReader warm-up in two environments:

- **Backtest** — `HistoricalClock` + `SimulatedExecutionClient` (deterministic).
- **Sandbox** — `LiveClock` + live market data + the **testnet** execution variant of the
  `ExecutionClient` port (real timing, paper fills).

Because the BrokerageModel economics are **shared** (ARCHITECTURE.md §6), the two should agree closely.

**This gate is MANDATORY and HARD: a strategy may go live ONLY if it passes.** `coinext_parity.run_gate`
is the seam — it runs the authoritative event-driven backtest for a fresh strategy instance over the
same bars, reduces it to a `SessionResult`, compares it to the recorded sandbox (testnet)
`SessionResult`, and returns a `Verdict(passed, reasons, metrics)`. Promotion to live is gated on
`verdict.passed`.

`coinext_parity.parity_metrics(backtest, sandbox, *, ts_bucket_ns=60_000_000_000)` quantifies agreement
(fills are matched at `(ts bucket, side)` granularity because the HistoricalClock and LiveClock never
align to the nanosecond):

- **`signal_timing_agreement`** — matched-fraction `2*|matched buckets| / (|a| + |b|)` over distinct
  `(bucket, side)` fill keys (a symmetric Jaccard-style ratio; `1.0` iff both sessions fired the same
  signals in the same buckets). This is the parity analogue of "order-flow equality".
- **`fill_price_deviation_bps`** — mean `|sandbox_px - backtest_px| / backtest_px * 1e4` over
  time-and-side-matched fills (the shared-BrokerageModel fill-economics check).
- **`equity_correlation`** — Pearson correlation of the two equity curves, resampled to the shorter
  length.
- **`return_diff`** — `|final_return_backtest - final_return_sandbox|`.

The **acceptance criterion** (`coinext_parity.AcceptanceCriterion`, the mandatory pre-live thresholds —
start tight, widen with evidence — see `docs/ARCHITECTURE.md` open questions) defaults to:

| condition | default | meaning |
|---|---|---|
| `min_signal_agreement` | `0.95` | ≥95% of fills agree on `(ts bucket, side)` |
| `max_fill_dev_bps`     | `5.0`  | mean matched fill-price deviation ≤ 5 bps |
| `min_equity_corr`      | `0.90` | equity curves correlate ≥ 0.90 |
| `max_return_diff`      | `0.02` | final returns differ by ≤ 2 percentage points |

`coinext_parity.evaluate(metrics, criterion) -> Verdict` checks all four; `Verdict.reasons` lists every
failing condition, and `coinext_parity.render_verdict(verdict)` renders the decision report
(`promote-eligible` vs `BLOCKED from live`). These thresholds also live under the `parity` section of
`config/*.yaml`. Covered by `test_parity_gate.py` (identical → PASS; +2 bps + tiny equity noise →
PASS within tolerance; +50 bps / dropped signals → FAIL with reasons; end-to-end `run_gate` with
SmaCross over synthetic bars → PASS).

### Running

```bash
just py-build           # coinext_py must be compiled
uv run pytest tests/parity
coinext parity               # demo gate: SmaCross backtest vs a slightly-perturbed sandbox (PASS)
```

The `coinext parity` CLI subcommand (`coinext_cli.main`, both the Typer and argparse fronts) runs a demo
promotion gate: it backtests `SmaCross` over synthetic bars, builds a near-identical sandbox session
(fills nudged +1.5 bps + a tiny equity wobble) from the same run, and prints `render_verdict`. It
exits `0` on PASS (promote-eligible) and `1` on FAIL (blocked from live).

The sandbox half requires Binance **testnet** credentials (`COINEXT__BINANCE__API_KEY` /
`COINEXT__BINANCE__API_SECRET`, `COINEXT__BINANCE__TESTNET=true`) and network access; those tests are skipped
(`importorskip` on `coinext_py` + a `requires_sandbox` marker) when credentials or connectivity are
absent, so CI runs the offline cross-check while the sandbox gate runs in a gated environment.

## Relationship to the other suites

- `tests/regression/` — pins deterministic backtest **statistics** (a stable engine over time).
- `tests/parity/` — pins **cross-environment / cross-method agreement** (this directory).
- `tests/test_python_backtest.py` — proves a *Python* Strategy runs through the *Rust* kernel at all.
