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
# 6. **Ticks** — quote/trade feed so `on_trade` / `on_quote` fire.
#
# This is a `py:percent` script (each `# %%` is a cell). Run it:
# `uv run python strategy-research/research-notebooks/notebooks/research_loop.py` (after `just py-build`).
#
# **Data default:** when `data/sample/` has Parquet fixtures (committed), the loop uses the lake by
# default. Override with `COINEXT_RESEARCH_USE_LAKE=0` for pure synthetic, or `=1` +
# `COINEXT__DATA__LAKE_ROOT` for a custom lake.

# %%
from __future__ import annotations
# ruff: noqa: E402, I001

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
for _rel in (
    "foundation/python-contracts/python",
    "foundation/runtime-config/python",
    "market-data/data-lake/python",
    "strategy-research/strategy-api/python",
    "strategy-research/indicators/python",
    "backtesting-simulation/kernel/python",
    "backtesting-simulation/runner/python",
    "backtesting-simulation/parity-gates/python",
    "analytics-optimization/analytics/python",
    "analytics-optimization/screening/python",
    "analytics-optimization/optimizer/python",
    "analytics-optimization/derivatives/python",
    "risk-portfolio/risk-facade/python",
    "risk-portfolio/portfolio-facade/python",
    "execution-live/live-runtime/python",
    "operations-interface/bus/python",
    "operations-interface/cli/python",
):
    _path = str(_ROOT / _rel)
    if _path not in sys.path:
        sys.path.insert(0, _path)

import coinext_backtest as bt
import coinext_screen
from coinext_analytics import tear_sheet
from coinext_optimize import walk_forward_optimize
from coinext_strategy import MultiSma, RsiReversion, SmaCross

_SAMPLE_LAKE = _ROOT / "data" / "sample"


def _env_flag(name: str) -> str | None:
    raw = os.environ.get(name)
    return None if raw is None else raw.strip().lower()


def _sample_lake_ready() -> bool:
    try:
        from coinext_data import DataLake

        rows = DataLake(str(_SAMPLE_LAKE)).read_ohlcv("BINANCE", "BTCUSDT", "1m")
        return bool(rows)
    except Exception:
        return False


def _resolve_use_lake() -> tuple[bool, str | None]:
    """Return ``(use_lake, lake_root)``.

    Precedence:
    1. ``COINEXT_RESEARCH_USE_LAKE`` forces on/off when set.
    2. Else prefer committed ``data/sample`` Parquet when present.
    3. Else synthetic bars.
    """
    flag = _env_flag("COINEXT_RESEARCH_USE_LAKE")
    env_root = os.environ.get("COINEXT__DATA__LAKE_ROOT")
    if flag in {"0", "false", "no"}:
        return False, None
    if flag in {"1", "true", "yes"}:
        root = env_root or (str(_SAMPLE_LAKE) if _sample_lake_ready() else "data")
        return True, root
    if env_root:
        return True, env_root
    if _sample_lake_ready():
        return True, str(_SAMPLE_LAKE)
    return False, None


USE_LAKE, LAKE_ROOT = _resolve_use_lake()


def _bars(symbol: str = "BTCUSDT", n: int = 600):
    if USE_LAKE:
        from coinext_data import DataLake

        rows = DataLake(LAKE_ROOT).read_ohlcv("BINANCE", symbol, "1m")
        if not rows:
            raise RuntimeError(
                f"no {symbol} 1m bars in lake root {LAKE_ROOT!r}; seed with `coinext download` "
                "or use the committed data/sample fixture"
            )
        return rows
    base = 50_000.0 if symbol == "BTCUSDT" else 3_000.0
    period = 40 if symbol == "BTCUSDT" else 55
    return bt.synthetic_ohlc_bars(n=n, base=base, period=period)


# %% [markdown]
# ## 1. Vectorized screen + cross-check

# %%
print(f"[data] USE_LAKE={USE_LAKE} lake_root={LAKE_ROOT}")
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

# %%
result = bt.run(SmaCross(**report.chosen_params), bars=bars)
print(tear_sheet(result, bars=bars))

# %% [markdown]
# ## 4. Indicators — RSI mean-reversion

# %%
rsi_res = bt.run(RsiReversion(period=14, low=35.0, high=65.0), bars=bars)
print(f"RsiReversion: {rsi_res.orders_submitted} orders, final equity {rsi_res.final_equity:,.2f}")

# %% [markdown]
# ## 5. Multi-instrument portfolio

# %%
portfolio = bt.run_multi(
    MultiSma(10, 30), bars={"BTCUSDT": _bars("BTCUSDT"), "ETHUSDT": _bars("ETHUSDT")}
)
print(f"portfolio: {portfolio.fills} fills, total return {portfolio.total_return * 100:.2f}%")

# %% [markdown]
# ## 6. Tick + quote feed — on_trade / on_quote fire
#
# Quotes come from ``coinext_data.quotes`` (synthetic from bars or a JSON recording). Swap in a
# WS-captured recording via ``load_quote_recording`` when you have real bookTicker history.

# %%
from coinext_data.quotes import synth_quotes_from_bars  # noqa: E402
from coinext_strategy import Strategy  # noqa: E402


class TradeCounter(Strategy):
    def __init__(self):
        self.n_trades = 0
        self.n_quotes = 0

    def on_trade(self, tr, ctx):
        self.n_trades += 1

    def on_quote(self, q, ctx):
        self.n_quotes += 1


counter = TradeCounter()
quotes = synth_quotes_from_bars(bars)
bt.run(counter, bars=bars, trades=bt.synth_trades(bars), quotes=quotes)
print(
    f"on_trade fired {counter.n_trades} times, on_quote fired {counter.n_quotes} times "
    f"over {len(bars)} bars"
)

# %% [markdown]
# Each step ran on the SAME deterministic Rust core that runs live (only the Clock + Data/Execution
# clients are swapped).
print("\nresearch loop complete.")
