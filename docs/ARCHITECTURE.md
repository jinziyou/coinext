# Coinext Architecture (stub)

> **Canonical document:** [`../ARCHITECTURE.md`](../ARCHITECTURE.md).
>
> Full doc index: [`README.md`](README.md).

The root architecture document is the single source of truth for lifecycle modules, the parity
invariant, domain model, ports, Kernel, data flow, deployment forms, and the doc map. **This file
is intentionally a stub** for stable deep-links (CI, historical issues, nested README paths). It
only keeps the historical build order and open questions.

## Build order

1. Bootstrap workspace + CI + root compose with redis/postgres/observability. ✅
2. `coinext-core` in `foundation/primitives/rust/coinext-core` — value types, clock, timers. ✅
3. `coinext-model` in `foundation/domain-model/rust/coinext-model` — IDs, Instrument, Order FSM, Fill, Position, market data. ✅
4. `coinext-ports` in `foundation/ports/rust/coinext-ports` — port traits + command/report types. ✅
5. `coinext-py` in `foundation/ffi-bridge/rust/coinext-py` — PyO3 bridge + `PyStrategyAdapter`. ✅
6. `coinext-bus` and `coinext-cache` — in-proc bus, Redis Envelope contract, indexed cache. ✅
7. Data/execution/risk/portfolio engines wired through `coinext-kernel`. ✅
8. `coinext-sim` in `backtesting-simulation/simulated-exchange/rust/coinext-sim` — matching + BrokerageModel + DelayedEventQueue. ✅
9. Data lake foundation (`coinext_data`) under `market-data/data-lake`. ✅
10. Strategy API + authoritative runner + advisory screen. ✅
11. Analytics metrics + bias detectors. ✅
12. Walk-forward optimizer. ✅
13. Network transport + Binance adapter. ✅
14. Persistence + reconciliation primitives. ✅
15. Python bus client + standalone service wrappers. 🚧 scaffolded
16. Deployment and observability wiring under `operations-interface/deployment`. 🚧 scaffolded
17. `coinext_live` + API + UI + risk-monitor. 🚧 scaffolded
18. Ops hardening + sandbox-vs-backtest parity acceptance gate. 🚧

See [`ROADMAP.md`](ROADMAP.md) for the current status narrative.

## Open questions

Tracked for later: multi-node sharding and ordered replay; heavy per-event Strategy compute beyond the GIL baseline; concrete cross-check and sandbox-vs-backtest parity thresholds per asset class; BrokerageModel queue/partial-fill fidelity ceiling; production secrets management (SOPS/Vault); asset-class roadmap (inverse perps, futures-with-expiry, options, equities) and settlement-PnL validation against venue statements; data-lake retention/downsampling; reconciliation edge cases (WS vs REST disagreement, modify-then-fill races); SeqCursor namespacing across accounts.
