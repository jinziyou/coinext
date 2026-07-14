# foundation

Shared domain contracts for the whole platform.

| Path | Role | Status |
|---|---|---|
| `crates/coinext-core` | Fixed-precision values, clock, timers | verified |
| `crates/coinext-model` | IDs, Instrument, Order FSM, Fill, Position | verified |
| `crates/coinext-ports` | Hexagonal port traits | verified |
| `crates/coinext-cache` | Indexed state cache | verified |
| `crates/coinext-py` | PyO3 bridge (`coinext_py`) | verified |
| `crates/coinext-testkit` | Test helpers | verified |
| `python/coinext_contracts` | Envelope / message contracts | verified |
| `python/coinext_config` | Layered YAML + env config | verified |
| `config/` | Default YAML (`base`, `backtest`, `sandbox`, `live`) | verified |

See root [ARCHITECTURE.md](../ARCHITECTURE.md).
