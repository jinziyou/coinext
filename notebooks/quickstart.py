# %% [markdown]
# # Coinext quickstart
#
# Build synthetic bars, run the **authoritative** event-driven backtest (`coinext_backtest.run`) with a
# Python `coinext_strategy.SmaCross` strategy driven through the **Rust kernel** (`coinext_py`), and print the
# `coinext_analytics.tear_sheet`.
#
# This is a `py:percent` script: each `# %%` marks a cell. Open it directly in Jupyter via jupytext,
# or just run it: `uv run python notebooks/quickstart.py` (after building `coinext_py` — see
# `notebooks/README.md`). It demonstrates the backtest↔live parity invariant from
# `docs/ARCHITECTURE.md` §1: the same Strategy + engines + SimulatedExecutionClient that the live
# path uses.

# %%
from __future__ import annotations

# coinext_py is the compiled Rust extension. coinext_backtest.run raises a clear error if it is not built.
from coinext_analytics import compute_metrics, detect_lookahead_bias, tear_sheet
from coinext_backtest import run, synthetic_bars
from coinext_strategy import SmaCross

# %% [markdown]
# ## 1. Synthetic bars
#
# A deterministic sine-wave-plus-trend close series (no RNG, so the run is fully reproducible). Each
# bar is `(ts_ns, close)`.

# %%
bars = synthetic_bars(n=400)
print(f"built {len(bars)} bars")
print("first:", bars[0])
print("last: ", bars[-1])

# %% [markdown]
# ## 2. Run the backtest through the Rust kernel
#
# `SmaCross(fast, slow, qty)` is a classic SMA crossover. `run` drives it through the Rust core: the
# DataEngine does cache-then-publish, the synchronous Strategy handler fires, orders pass the
# RiskEngine gate, and the SimulatedExecutionClient applies the BrokerageModel.

# %%
strategy = SmaCross(fast=10, slow=30, qty=0.5)
result = run(strategy, bars=bars, starting_balance=100_000.0)

print(f"orders submitted : {result.orders_submitted}")
print(f"orders denied    : {result.orders_denied}")
print(f"fills            : {result.fills}")
print(f"final equity     : {result.final_equity:,.2f}")

# %% [markdown]
# ## 3. Tear sheet + bias checks
#
# `tear_sheet` renders headline metrics (returns / Sharpe / Sortino / drawdown). The lookahead
# detector asserts the equity-curve timestamps are monotonic (a structural no-look-ahead check).

# %%
print(tear_sheet(result))

metrics = compute_metrics(list(result.equity_curve))
print(f"\nsharpe (ann)  : {metrics.sharpe:.3f}")
print(f"max drawdown  : {metrics.max_drawdown * 100:.2f}%")

warnings = detect_lookahead_bias(list(result.equity_curve))
print("lookahead warnings:", warnings or "none")

# %% [markdown]
# Next steps: sweep `fast`/`slow` with `coinext_optimize` (walk-forward), or promote to the
# sandbox-vs-backtest parity gate (`tests/parity/`). See `docs/ARCHITECTURE.md`.
