# market-data/services/ingestor — market-data ingestion daemon

This service directory is a **deployment wrapper only — it contains no application code.** The
ingestor is a **Rust binary**: its source is the [`coinext-ingest`](../../rust/coinext-ingest) crate (status:
stub — [`ARCHITECTURE.md`](../../../../ARCHITECTURE.md) §7; build order step 15 in [`docs/ARCHITECTURE.md`](../../../../docs/ARCHITECTURE.md)).

## What it does

The ingestor is the live-data front door (ARCHITECTURE.md §4, Live). It:

1. connects to the venue WebSocket market-data streams (Binance, via `coinext-network` + the
   `coinext-adapters/binance` Data client),
2. **normalizes** raw venue frames into the canonical `coinext-model` market-data types (quote / trade /
   bar / book delta),
3. **republishes** them on the Redis-Streams bus as versioned MessagePack `Envelope`s (the
   `coinext-bus` cross-service contract — ARCHITECTURE.md §3),

so that every `trader` process's DataEngine consumes one normalized, fan-out-able feed instead of
each node holding its own venue socket. The SLO histogram `ingest_to_publish_ns` (ARCHITECTURE.md §7)
is measured here.

> Note: a node's strategy **warm-up** is served from the LOCAL HistoryReader, never from this live
> feed (ARCHITECTURE.md §4, §6) — the ingestor supplies the *real-time* tail only.

## Canonical service / port

| Item        | Value                                                              |
|-------------|-------------------------------------------------------------------|
| Kind        | Rust binary (`coinext-ingest`)                                          |
| Build       | `operations-interface/deployment/docker/ingestor.Dockerfile`                               |
| Metrics     | `:9101` (Prometheus)                                               |
| Bus         | `COINEXT__REDIS__URL` (default `redis://redis:6379/0`)                  |
| Venue       | public market-data streams need **no** API keys                   |

## Build & run (docker)

```bash
# Build the image (Dockerfile builds market-data/crates/coinext-ingest via its own manifest).
docker build -f operations-interface/deployment/docker/ingestor.Dockerfile -t coinext/ingestor .

# Run it. Public market data needs no keys; point it at Redis and pick the symbols/streams.
docker run --rm \
  -p 9101:9101 \
  -e COINEXT__REDIS__URL=redis://redis:6379/0 \
  -e COINEXT__BINANCE__TESTNET=true \
  -e COINEXT__LOG__LEVEL=info \
  coinext/ingestor
```

Usually started via `docker-compose` so it shares the network with `redis`, the `trader`
process(es), and the observability stack (Prometheus scrapes `:9101`).

## Run from source (dev, no docker)

```bash
cargo run --release --manifest-path market-data/crates/coinext-ingest/Cargo.toml
# configured via the same COINEXT__* env vars (see .env.example)
```

## Known gaps

Tracked in the `coinext-ingest` crate, not this deployment wrapper:

- publish normalized events as versioned `Envelope`s on `coinext_bus`,
- append market data to the data lake (`coinext-persistence`),
- export `ingest_to_publish_ns`, `book_gaps`, and `ws_reconnects` on `:9101`,
- harden reconnect / gap-detection behavior for long-lived live sessions.
