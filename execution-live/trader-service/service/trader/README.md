# execution-live/trader-service/service/trader — live trading node (one process per account)

A thin Python wrapper (`main.py`) that builds a single `coinext_live` `TradingNode`
for **one account** and runs it. All load-bearing logic lives in the Rust core + `coinext_live`; this
process just selects the account, wires the strategy, and drives the run loop.

**One process per account** ([`ARCHITECTURE.md`](../../ARCHITECTURE.md) §4; see also [`docs/ARCHITECTURE.md`](../../docs/ARCHITECTURE.md) open questions): each set of API keys / sub-account gets its
own trader process. This isolates blast radius, preserves the deterministic single-threaded core per
node, and sidesteps cross-account SeqCursor namespacing. Scale out = more trader processes,
coordinated only via the Redis bus.

The live node injects a `LiveClock` + Binance Data/Exec clients behind **byte-identical ports** — the
same engines, risk gate, and strategy code as backtest (the parity invariant, ARCHITECTURE.md §1). Market data
arrives normalized from the standalone `ingestor`; warm-up is served from the **local HistoryReader**
(never live REST), so indicators match backtest exactly.

`TradingNode.run()` builds the native sandbox/live kernel through `coinext_py.build_kernel(...)` and
wires `LiveKernel::run_with_portfolio_callback(...)` to
`TradingNode.publish_native_snapshot(...)`. `publish_kernel_portfolio(...)` remains the pull-based
adapter for an already-attached native handle; `publish_telemetry(...)` remains the already-flattened
escape hatch. The API `/ws/live` fan-out and the out-of-band `risk-monitor` consume that same stream.

## Canonical service / port

| Item        | Value                                                       |
|-------------|-------------------------------------------------------------|
| Kind        | Python (`coinext_live`)                                           |
| Build       | `operations-interface/deployment/docker/trader.Dockerfile`                           |
| Metrics     | `:9103` (Prometheus)                                         |
| Account     | `COINEXT__TRADER__ACCOUNT_ID`                                     |
| Env         | `COINEXT__ENV` (`sandbox` \| `live`), `COINEXT__REDIS__URL`, `COINEXT__BINANCE__*` |

## Run (docker, one container per account)

```bash
docker build -f operations-interface/deployment/docker/trader.Dockerfile -t coinext/trader .
docker run --rm -p 9103:9103 \
  -e COINEXT__ENV=sandbox \
  -e COINEXT__TRADER__ACCOUNT_ID=acct-01 \
  -e COINEXT__REDIS__URL=redis://redis:6379/0 \
  -e COINEXT__BINANCE__API_KEY=... -e COINEXT__BINANCE__API_SECRET=... \
  -e COINEXT__BINANCE__TESTNET=true \
  coinext/trader
```

## Known gaps

- Exercise the native builder/callback path against Binance testnet credentials in an operator smoke
  test; unit coverage stays fake-bus/no-network.
- Wire reconcile-on-restart and process signal handlers through `coinext_live`/the trader wrapper.
- Export the node SLO histograms (`strategy_dispatch_ns`, `submit_to_ack_ns`, …) on `:9103`.
