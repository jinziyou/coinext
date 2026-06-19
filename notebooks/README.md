# notebooks/

Research notebooks for Coinext, stored as **`py:percent` scripts** rather than `.ipynb` so they
diff cleanly in git and run as plain Python.

## Files

- `quickstart.py` — build synthetic bars, run the authoritative event-driven backtest
  (`coinext_backtest.run`) with `coinext_strategy.SmaCross` through the Rust kernel, print the
  `coinext_analytics.tear_sheet`.
- `research_loop.py` — the full end-to-end loop: vectorized **screen** + cross-check → walk-forward
  **optimize** (OOS) → authoritative **backtest** + tear sheet → **indicators** (RSI) →
  multi-instrument **portfolio** → **tick** feed. Synthetic by default (reproducible, no network);
  set `USE_LAKE = True` to run over real downloaded history. Covered by `tests/test_notebook.py`.

## Running directly

The scripts are runnable as-is (each `# %%` is just a cell marker / no-op comment):

```bash
# one-time: create the venv and build the coinext_py extension
just py-setup        # uv sync --extra research --group dev
just py-build        # maturin develop (compiles crates/coinext-py)

# then run a notebook script
uv run python notebooks/quickstart.py
```

## Jupytext conversion

These `py:percent` scripts pair with Jupyter via [jupytext](https://jupytext.readthedocs.io):

```bash
# install jupytext (one-time)
uv pip install jupytext

# convert a script to a notebook (cells preserved from the # %% markers)
jupytext --to notebook notebooks/quickstart.py     # -> notebooks/quickstart.ipynb

# or open the .py directly in JupyterLab with the jupytext extension, and/or pair them so the
# .ipynb and .py stay in sync on save:
jupytext --set-formats ipynb,py:percent notebooks/quickstart.py

# convert a notebook back to a tracked script
jupytext --to py:percent notebooks/quickstart.ipynb
```

Commit the `.py` form; the generated `.ipynb` is ignored (large, noisy diffs).
