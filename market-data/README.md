# market-data

Ingestion, normalization, historical data lake, and venue adapters.

| Path | Role | Status |
|---|---|---|
| `crates/coinext-data-engine` | In-process data engine | verified |
| `crates/coinext-network` | REST/WS transport (workspace-excluded) | unit-tested |
| `crates/coinext-adapters-binance` | Binance adapter (workspace-excluded) | unit-tested |
| `crates/coinext-ingest` | Ingest daemon binary (workspace-excluded) | partial |
| `python/coinext_data` | Parquet data lake + HistoryReader | verified |
| `python/coinext_broker` | Equity paper / IB brokers | partial |
| `services/ingestor` | Deploy notes for the ingest daemon | partial |

Venue pattern: normalize to domain events, implement port traits, publish via bus or engine.
