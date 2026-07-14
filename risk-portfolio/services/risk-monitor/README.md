# risk-monitor service

| | |
|---|---|
| **Status** | scaffold |
| **Source** | `risk-portfolio/services/risk-monitor` |
| **Image** | `operations-interface/deployment/docker/risk-monitor.Dockerfile` |
| **Metrics** | `:9104` |

Out-of-band supervisor: watches bus/PnL and can trip the global kill-switch (independent of trader).

```bash
uv run python -m main  # with PYTHONPATH including this directory
```
