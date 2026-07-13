# data/sample/ — sample data-lake fixture area

A committed slice of the Coinext **data lake** so examples, notebooks, and tests can point at a
stable path without downloading gigabytes. The full lake (under `COINEXT__DATA__LAKE_ROOT`, default
`data/` locally or `/data` in compose) is gitignored; only this `sample/` subtree and the data-root
marker are tracked (see the root `.gitignore`).

## Role in the architecture

The `HistoryReader` / `DataLake` (`coinext_data`) serve **both** the backtest data feed **and** live
warm-up from the lake — identical in both environments — so streaming indicators
(`coinext-indicators`) warm up the same way in backtest and live
([`ARCHITECTURE.md`](../../ARCHITECTURE.md) §4, §6). This directory is the documented fixture
location for that contract.

## Layout (catalog convention)

The lake is Parquet, partitioned Hive-style by venue / symbol / interval (as implemented in
`coinext_data.lake`):

```
data/sample/
└── bars/
    └── venue=BINANCE/
        └── symbol=BTCUSDT/
            └── interval=1m/
                └── 202401.parquet   # optional committed fixture; none yet
```

`coinext download` and the CLI write into `COINEXT__DATA__LAKE_ROOT` (default `data/`), not necessarily
this `sample/` subtree. Point the env var at `data/sample` only when you intentionally want fixtures
isolated from a working lake.

## Current state

- No real Parquet fixture is committed yet. Runnable examples and most tests generate **synthetic**
  bars in-memory via `coinext_backtest.synthetic_bars` / `synthetic_ohlc_bars` (deterministic, no
  RNG), which is sufficient for parity and regression gates.
- To populate a real local lake:

  ```bash
  just py-setup
  uv run coinext download --symbols BTCUSDT,ETHUSDT --interval 1m --days 30
  uv run coinext catalog
  ```

- When adding a committed fixture: drop a small genuine `.parquet` under the partition layout above
  and point a `tests/market-data/` case at `DataLake(lake_root="data/sample")` so the real
  HistoryReader path is exercised without network access.


## Quote recordings

Optional offline quote fixtures for `on_quote` replay live under:

```
data/sample/quotes/BINANCE/BTCUSDT/sample_quotes.json
```

Load with `coinext_data.load_quote_recording(...)`. The committed sample is synthesized from the
BTCUSDT bar fixture (not a live bookTicker capture).
