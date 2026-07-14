# trader service

| | |
|---|---|
| **Status** | scaffold |
| **Source** | `execution-live/services/trader` |
| **Image** | `operations-interface/deployment/docker/trader.Dockerfile` |
| **Metrics** | `:9103` |
| **Config** | `COINEXT__TRADER__*`, `COINEXT__ENV`, `COINEXT__REDIS__URL` |

Thin **one process per account** wrapper around `coinext_live.TradingNode` (parity: same engines as backtest).

```bash
COINEXT__ENV=sandbox uv run python -m main
# cwd or PYTHONPATH: execution-live/services/trader
```
