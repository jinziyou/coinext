# IB paper trading runbook (Coinext)

Research path for equities via Interactive Brokers TWS / IB Gateway.
Rust Kernel `ExecutionClient` for stocks remains deferred — this is the **Python**
`coinext_broker.IbPaperBroker` path.

## Prerequisites

1. Install IB TWS or IB Gateway (paper account).
2. Enable API:
   - **TWS:** Edit → Global Configuration → API → Settings  
     - Enable ActiveX and Socket Clients  
     - Socket port **7497** (paper) / **7496** (live)  
     - Uncheck "Read-Only API" if you will place orders  
     - Trusted IPs: `127.0.0.1`
   - **Gateway paper:** port **4002**
3. Python extra:

   ```bash
   uv pip install ib_insync
   # or
   uv sync --extra ib
   ```

## Environment

```bash
export COINEXT__IB__HOST=127.0.0.1
export COINEXT__IB__PORT=7497          # TWS paper
export COINEXT__IB__CLIENT_ID=1
export COINEXT__IB__ACCOUNT=           # optional; first managed account if empty
export COINEXT__IB__READONLY=0
export COINEXT__IB__FILL_WAIT=3        # seconds to wait for paper fills after submit
```

## Smoke test

```bash
# Connectivity only (no orders if readonly)
uv run coinext ib-status

# Python one-liner market order (paper)
uv run python - <<'PY'
from coinext_broker import IbConfig, IbPaperBroker
br = IbPaperBroker(config=IbConfig.from_env(), mode="ib")
br.connect()
print("cash", br.cash())
print("mark AAPL", br.req_mark("NASDAQ", "AAPL"))
# Uncomment to place a 1-share paper market buy:
# o = br.submit_market("NASDAQ", "AAPL", "buy", 1)
# print(o.status, o.avg_price, br.fills())
br.disconnect()
PY
```

## Contract map

| Coinext venue | IB exchange | Currency | Notes |
|---|---|---|---|
| NYSE / NASDAQ / AMEX | SMART | USD | US stocks/ETFs |
| HKEX | SEHK | HKD | 4-digit codes |
| SSE | SEHKNTL | CNH | Northbound |
| SZSE | SEHKSZSE | CNH | Northbound |

```python
from coinext_broker import ib_contract_fields
print(ib_contract_fields("HKEX", "700"))
# → {'symbol': '0700', 'exchange': 'SEHK', ...}
```

## Offline fallback

```python
br = IbPaperBroker(mode="paper_local")  # no TWS; in-process PaperEquityBroker
```

## Safety

- Prefer **paper** ports (7497 / 4002) until you have reviewed fills.
- Set `COINEXT__IB__READONLY=1` for market-data-only sessions.
- A-share T+1 / 涨跌停 on IB are enforced by the **exchange**; local paper rules apply only in `paper_local` mode.

## Troubleshooting

| Symptom | Check |
|---|---|
| `TimeoutError` / connect fail | TWS running? API enabled? Port/firewall? |
| `ImportError: ib_insync` | `uv pip install ib_insync` |
| Order rejected | Account permissions, market hours, contract qualification |
| No fill after market | Increase `COINEXT__IB__FILL_WAIT`; check TWS order ticket |
