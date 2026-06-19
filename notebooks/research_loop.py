# %% [markdown]
# # Coinext research loop (end-to-end)
#
# One runnable demo that strings the whole research workflow together on the **same** Rust core the
# live path uses:
#
# 1. **Screen** — a fast vectorized `(fast, slow)` sweep (`coinext_screen`), cross-checked vs the
#    authoritative runner.
# 2. **Optimize** — walk-forward with out-of-sample degradation (`coinext_optimize`).
# 3. **Backtest** — the authoritative event-driven run (`coinext_backtest.run`) + a `coinext_analytics`
#    tear sheet (trade stats + bias screen).
# 4. **Indicators** — an RSI strategy off the shared Rust `coinext_indicators`.
# 5. **Portfolio** — a multi-instrument run through one kernel.
# 6. **Ticks** — quote/trade feed so `on_trade` fires.
#
# This is a `py:percent` script (each `# %%` is a cell). Run it: `uv run python
# notebooks/research_loop.py` (after `just py-build`). It uses **synthetic** bars by default, so it
# fully reproducible and needs no network; set `USE_LAKE = True` to run over the REAL Parquet lake
# (after `uv run coinext download --symbols BTCUSDT,ETHUSDT --days 30`).

# %%
from __future__ import annotations

import coinext_backtest as bt
import coinext_screen
from coinext_analytics import tear_sheet
from coinext_optimize import walk_forward_optimize
from coinext_strategy import MultiSma, RsiReversion, SmaCross

USE_LAKE = False  # flip to True to use real downloaded BTCUSDT/ETHUSDT history


def _bars(symbol: str = "BTCUSDT", n: int = 600):
    if USE_LAKE:
        from coinext_data import DataLake

        return DataLake().read_ohlcv("BINANCE", symbol, "1m")
    # Distinct synthetic regimes per symbol so the portfolio isn't N copies of one series.
    base = 50_000.0 if symbol == "BTCUSDT" else 3_000.0
    period = 40 if symbol == "BTCUSDT" else 55
    return bt.synthetic_ohlc_bars(n=n, base=base, period=period)


# %% [markdown]
# ## 1. Vectorized screen + cross-check
#
# Rank a `(fast, slow)` grid in milliseconds with the NON-authoritative numpy screen, then check the
# best params against the event-driven runner — signals should agree; absolute PnL drifts (no
# fees/slippage in the screen, by design).

# %%
bars = _bars("BTCUSDT")
rows = coinext_screen.sweep_sma_cross(bars, fasts=[5, 10, 15, 20], slows=[30, 50])
print("top vectorized (fast,slow) by Sharpe:")
for r in rows[:4]:
    print(f"  fast={r.params['fast']:>2} slow={r.params['slow']:>2}  sharpe={r.sharpe:>8.2f}")
best = rows[0].params
drift = coinext_screen.cross_check_vs_event(bars, best["fast"], best["slow"])
print("cross-check drift:", drift or "none (screen tracks the runner)")

# %% [markdown]
# ## 2. Walk-forward optimization (out-of-sample)
#
# Optimize IN-SAMPLE per fold and re-score OUT-of-sample; the headline is the OOS degradation that
# guards against overfitting (grid search over the AUTHORITATIVE backtest).

# %%
from coinext_analytics import compute_metrics  # noqa: E402


def objective(params, window):
    if params["fast"] >= params["slow"] or len(window) < 2:
        return float("-inf")
    res = bt.run(SmaCross(**params), bars=window)
    return compute_metrics(list(res.equity_curve)).sharpe


report = walk_forward_optimize(
    bars, objective, param_grid={"fast": [5, 10, 15], "slow": [30, 50]}, n_splits=3, mode="anchored"
)
print(report.render())

# %% [markdown]
# ## 3. Authoritative backtest + tear sheet
#
# Run the chosen params through the Rust kernel and print the full tear sheet — headline metrics,
# trade-level stats (win rate / profit factor / exposure / turnover), and the inline bias screen.

# %%
result = bt.run(SmaCross(**report.chosen_params), bars=bars)
print(tear_sheet(result, bars=bars))

# %% [markdown]
# ## 4. Indicators — RSI mean-reversion
#
# `RsiReversion` uses the SHARED Rust `coinext_indicators.Rsi` (identical to warm-up / live), not a
# re-rolled Python copy.

# %%
rsi_res = bt.run(RsiReversion(period=14, low=35.0, high=65.0), bars=bars)
print(f"RsiReversion: {rsi_res.orders_submitted} orders, final equity {rsi_res.final_equity:,.2f}")

# %% [markdown]
# ## 5. Multi-instrument portfolio
#
# A per-symbol SMA portfolio through ONE kernel (shared Cache / risk / portfolio). The aggregate
# result equals the union of the per-symbol standalone runs.

# %%
portfolio = bt.run_multi(
    MultiSma(10, 30), bars={"BTCUSDT": _bars("BTCUSDT"), "ETHUSDT": _bars("ETHUSDT")}
)
print(f"portfolio: {portfolio.fills} fills, total return {portfolio.total_return * 100:.2f}%")

# %% [markdown]
# ## 6. Tick feed — on_trade fires
#
# Interleave a (synthetic) trade stream with the bars so `on_trade` fires on real prints. Swap in
# `coinext_data.fetch_binance_agg_trades("BTCUSDT")` for genuine microstructure.

# %%
from coinext_strategy import Strategy  # noqa: E402


class TradeCounter(Strategy):
    def __init__(self):
        self.n = 0

    def on_trade(self, tr, ctx):
        self.n += 1


counter = TradeCounter()
bt.run(counter, bars=bars, trades=bt.synth_trades(bars))
print(f"on_trade fired {counter.n} times over {len(bars)} bars")

# %% [markdown]
# Each step ran on the SAME deterministic Rust core that runs live (only the Clock + Data/Execution
# clients are swapped). Flip `USE_LAKE = True` (top) to run the identical loop over real history.
print("\nresearch loop complete.")
