# notebooks/

Research notebooks for VeloxQuant, stored as **`py:percent` scripts** rather than `.ipynb` so they
diff cleanly in git and run as plain Python.

## Files

- `quickstart.py` — build synthetic bars, run the authoritative event-driven backtest
  (`qv_backtest.run`) with `qv_strategy.SmaCross` through the Rust kernel, print the
  `qv_analytics.tear_sheet`.

## Running directly

The scripts are runnable as-is (each `# %%` is just a cell marker / no-op comment):

```bash
# one-time: create the venv and build the qv_py extension
just py-setup        # uv sync --extra research --group dev
just py-build        # maturin develop (compiles crates/qv-py)

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
