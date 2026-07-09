# Coinext research notebooks

Research notebooks are tracked as `py:percent` scripts so they diff cleanly and run as plain Python.

## Files

- `quickstart.py` — builds synthetic bars, runs the authoritative event-driven backtest with `coinext_strategy.SmaCross` through the Rust kernel, and prints an analytics tear sheet.
- `research_loop.py` — runs the full research workflow: vectorized screen + cross-check → walk-forward optimize → authoritative backtest + tear sheet → shared Rust indicators → multi-instrument portfolio → tick feed. It uses synthetic data by default and can read the local lake when `USE_LAKE = True`.

`tests/strategy-research/test_notebook.py` covers the research loop when `coinext_py` is built.

## Running directly

```bash
just py-setup
just py-build        # builds foundation/ffi-bridge/rust/coinext-py

uv run python strategy-research/research-notebooks/notebooks/quickstart.py
uv run python strategy-research/research-notebooks/notebooks/research_loop.py
```

## Jupytext conversion

```bash
uv pip install jupytext
jupytext --to notebook strategy-research/research-notebooks/notebooks/quickstart.py
jupytext --set-formats ipynb,py:percent strategy-research/research-notebooks/notebooks/quickstart.py
jupytext --to py:percent strategy-research/research-notebooks/notebooks/quickstart.ipynb
```

Commit the `.py` form. Generated `.ipynb` files are intentionally ignored because they create noisy diffs.
