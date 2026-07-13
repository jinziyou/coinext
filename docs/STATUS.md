# Component status labels

Use these labels in module docstrings, service READMEs, and the roadmap snapshot so “scaffold”
language is reserved for true stubs.

| Label | Meaning |
|---|---|
| **verified** | Covered by automated tests; research/backtest default path |
| **partial** | Core wiring exists; production venue loop / persistence incomplete |
| **scaffold** | Entry point or daemon shell only; not production-capable |
| **deferred** | Intentionally parked (see ROADMAP deferred section) |

## Current map (summary)

| Component | Status |
|---|---|
| `coinext-core` / `coinext-model` / `coinext-ports` | verified |
| `coinext-kernel` BacktestKernel | verified |
| `coinext-kernel` LiveKernel | partial |
| `coinext-sim` | verified |
| `coinext_py` + `coinext_backtest` | verified |
| `coinext_data` lake / HistoryReader | verified |
| `coinext_parity` gates | verified |
| `coinext_live` TradingNode | partial (dry-run + paper) |
| `coinext-exec-svc` | partial (paper/venue OMS; reconcile-on-start; kill-switch) |
| `coinext-ingest` | partial (lake + Redis + reconnect + book_gaps on :9101) |
| `coinext_data.quote_capture` | verified (REST; optional WS) |
| API read paths (`state_store`) | partial |
| UI / risk-monitor | partial / scaffold |

When implementing live features, prefer `// LIVE-OPS:` or `Status: partial` over calling mature
backtest code a “scaffold”.
