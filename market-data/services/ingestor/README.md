# ingestor service

| | |
|---|---|
| **Status** | partial |
| **Binary** | `market-data/crates/coinext-ingest` (workspace-excluded) |
| **Image** | `operations-interface/deployment/docker/ingestor.Dockerfile` |
| **Metrics** | `:9101` |

Market-data daemon: normalize venue frames → lake + Redis Envelope. Build/run:

```bash
CARGO_TARGET_DIR=target/live-edge cargo run --manifest-path market-data/crates/coinext-ingest/Cargo.toml
# live WS: --features live
```
