# services/trader — live trading node (one process per account)

A thin Python wrapper (`main.py`) that builds a single [`coinext_live`](../../python/coinext_live) `TradingNode`
for **one account** and runs it. All load-bearing logic lives in the Rust core + `coinext_live`; this
process just selects the account, wires the strategy, and drives the run loop.

**One process per account** (ARCHITECTURE.md §7, §11): each set of API keys / sub-account gets its
own trader process. This isolates blast radius, preserves the deterministic single-threaded core per
node, and sidesteps cross-account SeqCursor namespacing. Scale out = more trader processes,
coordinated only via the Redis bus.

The live node injects a `LiveClock` + Binance Data/Exec clients behind **byte-identical ports** — the
same engines, risk gate, and strategy code as backtest (the parity invariant, §1). Market data
arrives normalized from the standalone `ingestor`; warm-up is served from the **local HistoryReader**
(never live REST), so indicators match backtest exactly.

## Canonical service / port

| Item        | Value                                                       |
|-------------|-------------------------------------------------------------|
| Kind        | Python (`coinext_live`)                                           |
| Build       | `deploy/docker/trader.Dockerfile`                           |
| Metrics     | `:9103` (Prometheus)                                         |
| Account     | `COINEXT__TRADER__ACCOUNT_ID`                                     |
| Env         | `COINEXT__ENV` (`sandbox` \| `live`), `COINEXT__REDIS__URL`, `COINEXT__BINANCE__*` |

## Run (docker, one container per account)

```bash
docker build -f deploy/docker/trader.Dockerfile -t coinext/trader .
docker run --rm -p 9103:9103 \
  -e COINEXT__ENV=sandbox \
  -e COINEXT__TRADER__ACCOUNT_ID=acct-01 \
  -e COINEXT__REDIS__URL=redis://redis:6379/0 \
  -e COINEXT__BINANCE__API_KEY=... -e COINEXT__BINANCE__API_SECRET=... \
  -e COINEXT__BINANCE__TESTNET=true \
  coinext/trader
```

## TODOs

- Finalize the `coinext_live.TradingNode` builder surface and feed strategy params from `coinext_config`.
- Wire reconcile-on-restart and graceful shutdown (signal traps) through `coinext_live`.
- Export the node SLO histograms (`strategy_dispatch_ns`, `submit_to_ack_ns`, …) on `:9103`.
