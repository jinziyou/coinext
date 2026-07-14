# Equity venue adapters (research + future live)

Status labels: **verified** paper broker offline · **scaffold** IB Gateway wiring.

## Layout

```
equity/
├── README.md
└── python/coinext_broker/
    ├── __init__.py
    ├── base.py          # EquityBroker protocol + PaperEquityBroker
    └── ib_paper.py      # IB config + contract map + paper_local mode
```

## Markets

| Focus | Venues | Paper broker | IB map |
|---|---|---|---|
| A股 | SSE, SZSE | yes | SEHKNTL / SEHKSZSE (Northbound scaffold) |
| 美股 | NYSE, NASDAQ, AMEX | yes | SMART |
| 港股 | HKEX | yes | SEHK |
| ETF | same venues | yes | same as stocks |

## Paper broker (usable now)

```python
from coinext_broker import PaperEquityBroker
import datetime as dt

br = PaperEquityBroker(starting_cash={"USD": 50_000, "CNY": 200_000, "HKD": 100_000})
br.connect()
br.set_mark("NASDAQ", "AAPL", 190.0)
br.submit_market("NASDAQ", "AAPL", "buy", 10)

# A股: T+1 + 涨跌停
br.set_session_day(dt.date(2024, 6, 3))
br.set_prev_close("SSE", "600519", 1700.0)
br.set_mark("SSE", "600519", 1700.0)
br.submit_market("SSE", "600519", "buy", 100)
br.submit_market("SSE", "600519", "sell", 100)  # rejected: T+1
br.set_session_day(dt.date(2024, 6, 4))
br.submit_market("SSE", "600519", "sell", 100)  # ok next day
print(br.positions(), br.cash())
```

Fees / currency come from `coinext_data.instrument_spec`.

### A-share rules

| Rule | Behavior |
|---|---|
| **T+1** | Shares bought on day D cannot be sold until D+1 (`sellable_qty`) |
| **涨跌停** | ±10% main / ±20% 300·688 / ±5% ST; market orders clamp to limit by default |

## IB paper / live (`ib_insync`)

```bash
uv pip install ib_insync   # or: uv sync --extra ib
# Start TWS or IB Gateway (paper port 7497 / 4002), enable API
export COINEXT__IB__HOST=127.0.0.1 COINEXT__IB__PORT=7497 COINEXT__IB__CLIENT_ID=1
```

```python
from coinext_broker import IbConfig, IbPaperBroker, ib_contract_fields

print(ib_contract_fields("HKEX", "0700"))
# → symbol=0700, exchange=SEHK, currency=HKD, …

# Offline research (default)
br = IbPaperBroker(mode="paper_local")
br.connect()

# Real TWS paper fill loop
br = IbPaperBroker(config=IbConfig.from_env(), mode="ib")
br.connect()
o = br.submit_market("NASDAQ", "AAPL", "buy", 1)
print(o.status, o.avg_price, br.fills())
br.disconnect()
```

Env: `COINEXT__IB__HOST`, `PORT`, `CLIENT_ID`, `ACCOUNT`, `READONLY`, `FILL_WAIT`.

## Bar replay (CLI)

```bash
# Seed lake then replay with SMA (respects T+1 on SSE/SZSE)
uv run coinext download --venue SSE --symbols 600519 --interval 1d --days 365
uv run coinext paper-equity --venue SSE --symbol 600519 --strategy sma --fast 5 --slow 20

# Sample fixtures
COINEXT__DATA__LAKE_ROOT=data/sample uv run coinext paper-equity --venue SSE --symbol 510300
COINEXT__DATA__LAKE_ROOT=data/sample uv run coinext paper-equity --venue NASDAQ --symbol AAPL --strategy buyhold --qty 10

# FX for multi-ccy backtests
uv run coinext download-fx --pairs USDCNY,USDHKD --days 365
```

Python API: `from coinext_broker import replay_from_lake, replay_bars`.

## Relation to the Kernel parity seam

The Rust `ExecutionClient` port (`coinext-ports`) remains the live Kernel seam (Binance only
today). `coinext_broker` is the **Python research / stock** side:

1. Paper-validate multi-market order flow offline (T+1 / limits).
2. Optional IB Gateway path for paper/live equities via `ib_insync`.
3. Later: thin Rust `ExecutionClient` wrapping the same IB session for Kernel parity.

## Not in scope yet

- A-share short-sell / financing constraints beyond T+1
- Smart order routing across dark pools
- Full Kernel `ExecutionClient` for equities (Binance-only today)
