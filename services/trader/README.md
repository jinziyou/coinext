# services/trader — live trading node (one process per account)

A thin Python wrapper (`main.py`) that builds a single [`qv_live`](../../python/qv_live) `TradingNode`
for **one account** and runs it. All load-bearing logic lives in the Rust core + `qv_live`; this
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
| Kind        | Python (`qv_live`)                                           |
| Build       | `deploy/docker/trader.Dockerfile`                           |
| Metrics     | `:9103` (Prometheus)                                         |
| Account     | `VQ__TRADER__ACCOUNT_ID`                                     |
| Env         | `VQ__ENV` (`sandbox` \| `live`), `VQ__REDIS__URL`, `VQ__BINANCE__*` |

## Run (docker, one container per account)

```bash
docker build -f deploy/docker/trader.Dockerfile -t veloxquant/trader .
docker run --rm -p 9103:9103 \
  -e VQ__ENV=sandbox \
  -e VQ__TRADER__ACCOUNT_ID=acct-01 \
  -e VQ__REDIS__URL=redis://redis:6379/0 \
  -e VQ__BINANCE__API_KEY=... -e VQ__BINANCE__API_SECRET=... \
  -e VQ__BINANCE__TESTNET=true \
  veloxquant/trader
```

## TODOs

- Finalize the `qv_live.TradingNode` builder surface and feed strategy params from `qv_config`.
- Wire reconcile-on-restart and graceful shutdown (signal traps) through `qv_live`.
- Export the node SLO histograms (`strategy_dispatch_ns`, `submit_to_ack_ns`, …) on `:9103`.
