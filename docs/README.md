# Coinext documentation

Index of design and operator docs. Prefer the **root** architecture document as the single source of
truth for module boundaries and parity invariants.

| Document | Role |
|---|---|
| [`../ARCHITECTURE.md`](../ARCHITECTURE.md) | **Canonical** design: lifecycle modules, domain model, ports, Kernel, data flow, deployment |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Stable-link stub: historical build order + open questions (points at root) |
| [`ROADMAP.md`](ROADMAP.md) | Status snapshot, next steps, deferred live/ops, open questions |
| [`CHANGELOG.md`](CHANGELOG.md) | Historical verified-work narrative |
| [`STATUS.md`](STATUS.md) | verified / partial / scaffold label convention |
| [`TESTNET.md`](TESTNET.md) | Binance public market data + spot testnet paper-execution runbook |
| [`IB_PAPER.md`](IB_PAPER.md) | Interactive Brokers TWS/Gateway paper path (`coinext_broker`) |
| [`EQUITY_RESEARCH.md`](EQUITY_RESEARCH.md) | A股/港股/美股/ETF research quick path |
| [`../README.md`](../README.md) | Platform positioning, status table, quick starts, toolchain |
| [`../tests/backtesting-simulation/parity/README.md`](../tests/backtesting-simulation/parity/README.md) | Advisory cross-check + sandbox parity gate |
| [`../operations-interface/deployment/README.md`](../operations-interface/deployment/README.md) | Compose, Docker, observability overlay |
| [`../operations-interface/deployment/services.md`](../operations-interface/deployment/services.md) | Deployable service index |
| [`../strategy-research/research-notebooks/notebooks/README.md`](../strategy-research/research-notebooks/notebooks/README.md) | Research scripts |
| [`../data/sample/README.md`](../data/sample/README.md) | Sample lake fixture area |
| [`../market-data/venue-adapters/README.md`](../market-data/venue-adapters/README.md) | Venue adapter pattern |

## Conventions

- **Status honesty:** research/backtest paths are tested; live daemons (`ingestor`, `exec-svc`),
  `LiveKernel` end-to-end, API/UI data endpoints, and real venue parity remain scaffold or deferred.
- **Paths:** source lives under the eight root lifecycle modules — there is no `modules/` prefix.
- **Imports:** Python packages keep `coinext_*` names; Rust crates keep `coinext-*` names.
- When linking to architecture from nested READMEs, count directory depth carefully (see recent
  service README fixes under `*/service/*/README.md`).


## Language

| Audience | Language | Primary docs |
|---|---|---|
| Operators / quick start | **Chinese** | root [`README.md`](../README.md) |
| Design / architecture / roadmap | **English** | root [`ARCHITECTURE.md`](../ARCHITECTURE.md), this `docs/` tree |

Nested service READMEs may be English; keep status labels consistent with [`STATUS.md`](STATUS.md).
