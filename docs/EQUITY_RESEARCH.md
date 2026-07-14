# Equity research quick path (A股 / 港股 / 美股 / ETF)

Shortest path from a clean checkout to A-share download, paper replay, and Kernel backtest.
Live stock brokers remain deferred (Binance only for Kernel execution); IB paper is optional
(see [`IB_PAPER.md`](IB_PAPER.md)).

## Setup

```bash
just py-setup
just py-build    # coinext_py with T+1 / 涨跌停 OMS
```

## 1. Venues & presets

```bash
uv run coinext venues
# Market groups: ASHARE/A股, US/美股, HK/港股, ETF
# Presets: --symbols @default | @etf
```

## 2. Download history (Yahoo, no key)

```bash
# A股 (auto-route 6xxxx→SSE, 0/3xxxx→SZSE)
uv run coinext download --venue ASHARE --symbols 600519,000001,510300 --interval 1d --days 365

# 美股 / 港股 / ETF
uv run coinext download --venue US --symbols AAPL,SPY --interval 1d --days 365
uv run coinext download --venue 港股 --symbols 0700,2800 --interval 1d --days 365
uv run coinext download --venue SSE --symbols @etf --interval 1d --days 365

# FX for multi-currency portfolios
uv run coinext download-fx --pairs USDCNY,USDHKD --days 365
```

Equity downloads filter holidays / flat-halt bars by default (`--no-calendar-filter` to keep).

## 3. Offline fixtures

```bash
COINEXT__DATA__LAKE_ROOT=data/sample uv run coinext catalog --venue ALL
COINEXT__DATA__LAKE_ROOT=data/sample uv run coinext backtest \
  --venue SSE --symbol 510300 --from-lake --interval 1d
```

## 4. Paper equity (T+1 / 涨跌停, not Kernel)

```bash
COINEXT__DATA__LAKE_ROOT=data/sample \
  uv run coinext paper-equity --venue SSE --symbol 600519 --strategy sma

COINEXT__DATA__LAKE_ROOT=data/sample \
  uv run coinext paper-equity --venue ASHARE --symbol 600519,000001 --multi --strategy buyhold
```

## 5. Kernel backtest (authoritative)

```bash
# Equity instrument + A-share venue → OMS enforces T+1 and 涨跌停
uv run coinext backtest --venue SSE --symbol 600519 --from-lake --interval 1d

# Multi-name, revalue into USD via lake FX + fallbacks
uv run coinext backtest-multi --venue ASHARE --symbols @default --from-lake \
  --interval 1d --base-ccy USD
```

### Kernel rules (SSE/SZSE + `Instrument.equity`)

| Rule | Behavior |
|------|----------|
| **T+1** | Same **Asia/Shanghai** session-day sell of newly bought shares → `TPlusOne` |
| **涨跌停** | Limit/market price outside ±10% / ±20% (300·688) / ±5% (ST) of **prior session-day** last mark (0.01 tick) → `PriceLimit` |
| **US/HK** | No T+1 / no A-share price-limit band |

```bash
# Optional: 前复权 OHLC (Yahoo adjclose scaling) → write as interval 1d by default;
# sample lake ships interval=1d_adj for 600519/AAPL/0700.
uv run coinext download --venue SSE --symbols 600519 --interval 1d --days 365 --adjust
COINEXT__DATA__LAKE_ROOT=data/sample uv run coinext backtest \
  --venue SSE --symbol 600519 --from-lake --interval 1d_adj
```

Shared A-share rules: ``coinext_data.ashare_rules`` (paper re-exports; Kernel OMS mirrors %).

E2E tests: `tests/backtesting-simulation/test_ashare_t_plus_one.py`.

## 6. Research loop

```bash
uv run python strategy-research/notebooks/research_loop.py
```

Includes multi-market sample equity demo + A-share T+1 smoke when `coinext_py` is built.

## Related modules

| Module | Role |
|--------|------|
| `coinext_data.venues` | Catalog, groups, `@etf`, symbology |
| `coinext_data.calendar` | Holidays, session hours, halt filter |
| `coinext_data.fx` | `FxBook`, `download_fx_to_lake` |
| `coinext_broker` | Paper + IB paper scaffold |
| `coinext-exec-engine` | Kernel OMS T+1 / 涨跌停 |
