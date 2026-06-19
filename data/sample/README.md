# data/sample/ — sample data-lake placeholder

A tiny, committed slice of the Coinext **data lake** so examples, notebooks, and tests have
something to read without downloading gigabytes. The full lake (under `COINEXT__DATA__LAKE_ROOT`, default
`/data`) is gitignored; only this `sample/` subtree and the `/data/.gitkeep` marker are tracked
(see the root `.gitignore`).

## Role in the architecture

The `HistoryReader` (`coinext_data`, [stub]) serves **both** the backtest data feed **and** live warm-up
from the lake — identical in both environments, so streaming indicators (`coinext-indicators`) warm up
the same way in backtest and live (`docs/ARCHITECTURE.md` §7, §10). This sample directory is where a
minimal fixture lives so that contract holds end-to-end before any real ingestion runs.

## Layout (catalog convention)

The lake is Parquet, partitioned by venue / symbol / interval / date — the partitioning
`coinext_data`'s catalog expects:

```
data/sample/
└── binance/
    └── BTCUSDT/
        └── 1m/
            └── date=2024-01-01/
                └── part-0.parquet      # (placeholder — no bulk data committed yet)
```

## Notes

- No real Parquet is committed yet; runnable examples/tests generate **synthetic** bars in-memory
  via `coinext_backtest.synthetic_bars` (deterministic, no RNG), which is sufficient for the parity and
  regression gates.
- To populate a real local lake, ingest via the `ingestor` service (Rust `coinext-ingest`) or a
  `coinext_data` backfill, pointing `COINEXT__DATA__LAKE_ROOT` at your mount.
- TODO: drop a small genuine `.parquet` fixture here once `coinext_data`'s catalog reader lands, and
  point a `tests/` fixture at it to exercise the real HistoryReader path.
