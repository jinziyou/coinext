# services/ingestor — market-data ingestion daemon

This service directory is a **deployment wrapper only — it contains no application code.** The
ingestor is a **Rust binary**: its source is the [`qv-ingest`](../../crates/qv-ingest) crate (status:
stub — ARCHITECTURE.md §3, build order step 15).

## What it does

The ingestor is the live-data front door (ARCHITECTURE.md §7, Live). It:

1. connects to the venue WebSocket market-data streams (Binance, via `qv-network` + the
   `qv-adapters/binance` Data client),
2. **normalizes** raw venue frames into the canonical `qv-model` market-data types (quote / trade /
   bar / book delta),
3. **republishes** them on the Redis-Streams bus as versioned MessagePack `Envelope`s (the
   `qv-bus` cross-service contract — ARCHITECTURE.md §6),

so that every `trader` process's DataEngine consumes one normalized, fan-out-able feed instead of
each node holding its own venue socket. The SLO histogram `ingest_to_publish_ns` (ARCHITECTURE.md §8)
is measured here.

> Note: a node's strategy **warm-up** is served from the LOCAL HistoryReader, never from this live
> feed (ARCHITECTURE.md §7, §10) — the ingestor supplies the *real-time* tail only.

## Canonical service / port

| Item        | Value                                                              |
|-------------|-------------------------------------------------------------------|
| Kind        | Rust binary (`qv-ingest`)                                          |
| Build       | `deploy/docker/ingestor.Dockerfile`                               |
| Metrics     | `:9101` (Prometheus)                                               |
| Bus         | `VQ__REDIS__URL` (default `redis://redis:6379/0`)                  |
| Venue       | public market-data streams need **no** API keys                   |

## Build & run (docker)

```bash
# Build the image (Dockerfile is multi-stage: cargo build --release -p qv-ingest).
docker build -f deploy/docker/ingestor.Dockerfile -t veloxquant/ingestor .

# Run it. Public market data needs no keys; point it at Redis and pick the symbols/streams.
docker run --rm \
  -p 9101:9101 \
  -e VQ__REDIS__URL=redis://redis:6379/0 \
  -e VQ__BINANCE__TESTNET=true \
  -e VQ__LOG__LEVEL=info \
  veloxquant/ingestor
```

Usually started via `docker-compose` so it shares the network with `redis`, the `trader`
process(es), and the observability stack (Prometheus scrapes `:9101`).

## Run from source (dev, no docker)

```bash
cargo run --release -p qv-ingest
# configured via the same VQ__* env vars (see .env.example)
```

## TODOs

Tracked in the `qv-ingest` crate, not here:

- implement the Binance WS subscribe / reconnect / gap-detection loop (`qv-network`),
- normalize frames to `qv-model` types and publish `Envelope`s via `qv-bus`,
- export `ingest_to_publish_ns`, `book_gaps`, `ws_reconnects` on `:9101`.
