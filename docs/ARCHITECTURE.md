# Coinext Architecture

> **This document has moved to the repo root: [`../ARCHITECTURE.md`](../ARCHITECTURE.md).**

The canonical architecture description now lives at the top of the repository. This stub remains so
existing links keep resolving; it also holds the two reference sections that other docs point at
(build order and open questions), which did not move into the root narrative.

The root [`ARCHITECTURE.md`](../ARCHITECTURE.md) covers:

1. Overview + the parity invariant
2. Domain model (value types, typed IDs, Instrument, event-sourced Order FSM, Fill, Position, market data)
3. Components & boundaries (hexagonal ports, engines, Kernel, Rust hot path vs Python control plane, the PyO3 bridge)
4. Data flow (the deterministic synchronous core loop; Backtest/Sandbox/Live kernels)
5. Tech stack & key tradeoffs
6. Key invariants & constraints
7. Deployment forms + the observability stack
8. Doc map

---

## Build order

1. Bootstrap workspace + CI + empty compose with redis/postgres/observability. ✅
2. `coinext-core` (value types, clock, timers) — property tested. ✅
3. `coinext-model` (IDs, Instrument, Order FSM, Fill, Position, market data). ✅
4. `coinext-ports` (all port traits + command/report types). ✅
5. `coinext-py` PyO3 + `PyStrategyAdapter` dispatch shim. ✅
6. `coinext-bus` + `coinext-cache` (in-proc bus, indexed cache). ✅
7. Engines on the ports (data/exec/risk/portfolio) wired in `coinext-kernel`. ✅
8. `coinext-sim` (matching + BrokerageModel + DelayedEventQueue) — first proof of parity. ✅
9. Data lake foundation (`coinext_data`). ✅
10. `coinext_strategy` + `coinext_backtest` (authoritative runner + advisory screen). ✅
11. `coinext_analytics` (metrics + bias detectors). ✅
12. `coinext_optimize` (walk-forward, grid/Optuna). ✅
13. `coinext-network` + `coinext-adapters/binance`. 🚧 stub
14. `coinext-persistence` + reconciliation. 🚧 stub
15. Python bus client + standalone services. 🚧 scaffolded
16. Observability wiring. 🚧 scaffolded
17. `coinext_live` + api + ui + risk-monitor. 🚧 scaffolded
18. Ops + hardening + sandbox-vs-backtest parity acceptance gate. 🚧

See [`docs/ROADMAP.md`](ROADMAP.md) for the current, fuller status.

## Open questions

Tracked for later: multi-node sharding & ordered replay; heavy per-event Strategy compute beyond the
GIL baseline; concrete cross-check & sandbox-vs-backtest parity thresholds per asset class;
BrokerageModel queue/partial-fill fidelity ceiling; prod secrets management (SOPS/Vault); asset-class
roadmap (inverse perps, futures-with-expiry, options, equities) and settlement-PnL validation against
venue statements; data-lake retention/downsampling; reconciliation edge cases (WS vs REST
disagreement, modify-then-fill races); SeqCursor namespacing across accounts.
