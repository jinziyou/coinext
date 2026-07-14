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
    ├── venue=BINANCE/
    │   ├── symbol=BTCUSDT/interval=1m/…
    │   └── symbol=ETHUSDT/interval=1m/…
    ├── venue=NASDAQ/symbol=AAPL/interval=1d/…          # 美股
    ├── venue=NYSE/symbol=JPM|SPY/interval=1d/…         # 美股 + 美股 ETF
    ├── venue=HKEX/symbol=0700|2800/interval=1d/…       # 港股 + 港股 ETF
    ├── venue=SSE/symbol=600519|510300/interval=1d/…    # A股 + A股 ETF
    ├── venue=SZSE/symbol=000001/interval=1d/…          # A股（深交所）
    ├── venue=TSE/symbol=7203/interval=1d/…
    ├── venue=LSE/symbol=SHEL/interval=1d/…
    └── venue=INDEX/
        ├── symbol=^GSPC/interval=1d/…
        └── symbol=^HSI/interval=1d/…
```

`coinext download` and the CLI write into `COINEXT__DATA__LAKE_ROOT` (default `data/`), not necessarily
this `sample/` subtree. Point the env var at `data/sample` only when you intentionally want fixtures
isolated from a working lake.

## Current state

- **Crypto:** committed short 1m OHLCV for `BINANCE/BTCUSDT` and `BINANCE/ETHUSDT`.
- **Equities / ETFs / indices:** committed ~90–120 calendar days of **1d** bars for focus markets
  (`SAMPLE_EQUITY_SERIES` in `coinext_data.venues`):
  - **美股:** NASDAQ/AAPL, NYSE/JPM, NYSE/SPY (ETF)
  - **港股:** HKEX/0700, HKEX/2800 (Tracker Fund ETF)
  - **A股:** SSE/600519, SSE/510300 (CSI 300 ETF), SZSE/000001
  - **FX:** FX/USDCNY, FX/USDHKD (for multi-currency revaluation)
  - plus TSE/7203, LSE/SHEL, INDEX/^GSPC, INDEX/^HSI
  Sourced from Yahoo Finance for offline demos.
- Runnable examples also generate **synthetic** bars via `coinext_backtest.synthetic_bars` when
  the lake is not used.
- To refresh or expand a working lake (not the committed sample tree):

  ```bash
  just py-setup
  # Crypto (Binance public klines)
  uv run coinext download --symbols BTCUSDT,ETHUSDT --interval 1m --days 30
  # A股 / ETF / 美股 / 港股 (Yahoo Finance; see `coinext venues`)
  uv run coinext download --venue ASHARE --symbols @default --interval 1d --days 365
  uv run coinext download --venue SSE --symbols @etf --interval 1d --days 365
  uv run coinext download --venue US --symbols @default --interval 1d --days 365
  uv run coinext download --venue NYSE --symbols @etf --interval 1d --days 365
  uv run coinext download --venue 港股 --symbols @default --interval 1d --days 365
  uv run coinext download --venue INDEX --symbols @default --interval 1d --days 365
  # FX for multi-ccy backtests (writes venue=FX)
  uv run python -c "from coinext_data import DataLake, download_fx_to_lake; print(download_fx_to_lake(DataLake(), days=365))"
  uv run coinext catalog --venue ALL
  # Offline demo against the committed sample tree:
  COINEXT__DATA__LAKE_ROOT=data/sample uv run coinext backtest \
    --venue NASDAQ --symbol AAPL --from-lake --interval 1d
  COINEXT__DATA__LAKE_ROOT=data/sample uv run coinext backtest \
    --venue SSE --symbol 510300 --from-lake --interval 1d
  ```


## Quote recordings

Optional offline quote fixtures for `on_quote` replay live under:

```
data/sample/quotes/BINANCE/BTCUSDT/sample_quotes.json
```

Load with `coinext_data.load_quote_recording(...)`. The committed sample is synthesized from the
BTCUSDT bar fixture (not a live bookTicker capture).
