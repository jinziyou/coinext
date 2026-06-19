# services/risk-monitor — out-of-band global risk supervisor

A standalone Python process (`main.py`) that is the **second, out-of-band** line of risk defense.
The first line is the per-order `coinext-risk-engine` gate *inside* each trading node's core; this service
watches **all** PnL / position / fill telemetry on the Redis-Streams bus and enforces **account-wide**
limits the in-core gate cannot see in isolation (ARCHITECTURE.md §7, §8):

- **max drawdown** — peak-to-trough equity decline across the account,
- **gross / net exposure** — sum of abs(notional) and signed notional across instruments,
- **loss-of-day** — PnL loss since the session boundary.

On a breach it **trips the global kill-switch** by publishing a `CtrlKillSwitch` (engaged) command on
the control stream; every `trader` process's in-core gate honours it atomically, halting new order
routing platform-wide. Being out-of-band, a crash or deadlock in a trading node cannot silence it.

## Canonical service / port

| Item        | Value                                                       |
|-------------|-------------------------------------------------------------|
| Kind        | Python                                                       |
| Build       | `deploy/docker/risk-monitor.Dockerfile`                    |
| Metrics     | `:9104` (Prometheus)                                         |
| Limits      | `COINEXT__RISK__*` (shared with the in-core gate — `.env.example`) |
| Bus         | `COINEXT__REDIS__URL`                                             |

Planned metrics on `:9104`: `risk_drawdown_pct`, `risk_gross_exposure`, `risk_net_exposure`,
`risk_loss_of_day`, `risk_killswitch_trips_total`.

## Run (docker)

```bash
docker build -f deploy/docker/risk-monitor.Dockerfile -t coinext/risk-monitor .
docker run --rm -p 9104:9104 \
  -e COINEXT__REDIS__URL=redis://redis:6379/0 \
  -e COINEXT__RISK__MAX_GROSS_EXPOSURE=1000000 \
  -e COINEXT__RISK__MAX_LOSS_OF_DAY=50000 \
  coinext/risk-monitor
```

The limit-evaluation core (`RiskSupervisor` / `RiskLimits` / `AccountState`) is pure (no I/O) and
unit-testable without the bus or a running Redis. `coinext_bus` and `prometheus_client` are imported
lazily; without them the process runs in an idle stub mode so `/metrics` stays scrapeable.

## TODOs

- Wire the real `coinext_bus` async consumer of the telemetry stream + `Envelope` decode.
- Publish the real `CtrlKillSwitch` Envelope (`MsgType.CTRL`) on breach.
- Export the Prometheus gauges/counters listed above.
