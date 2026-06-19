# Testnet end-to-end runbook

> 把平台接到 Binance 真实行情 + testnet 纸面执行，并用一致性门做上线前比对。
> Wire the platform to real Binance market data + Binance **spot testnet** paper execution, and run
> the backtest↔sandbox parity gate before any live capital.

## The sandbox design

Coinext's `Environment::Sandbox` is **real market data + paper (testnet) execution**:

- **Market data** comes from Binance **mainnet** public streams/REST — real, liquid prices. No API
  key is needed for public data. This is the right research/backtest data source even while
  execution is on testnet.
- **Execution** routes to Binance **spot testnet** (`testnet.binance.vision`) — paper money, real
  API surface, real order lifecycle. This needs a testnet API key.

This split lets you diff a sandbox session against the deterministic backtest (the **parity gate**)
before promoting a strategy to live.

## 0. Get a Binance spot testnet API key (no Binance account / KYC)

1. Open <https://testnet.binance.vision/> → **Log In with GitHub**.
2. **Generate HMAC_SHA256 Key** → copy the **API Key** and **Secret** (secret shown once).
3. Testnet auto-credits paper balances; keys/balances reset ~monthly.

```bash
cp .env.example .env          # .env is gitignored
# edit .env:
#   COINEXT__ENV=sandbox
#   COINEXT__BINANCE__TESTNET=true
#   COINEXT__BINANCE__API_KEY=...
#   COINEXT__BINANCE__API_SECRET=...
```

Public market data needs **no** key; only order flow does.

## 1. Live market data — NO key (verified ✅)

`coinext-ingest` connects the real Binance WS combined streams via the `BinanceDataClient`, normalizes
frames to the venue-agnostic `MarketEvent` types, and prints them:

```bash
COINEXT__INGEST__SYMBOLS="BTCUSDT.BINANCE,ETHUSDT.BINANCE" \
COINEXT__INGEST__MAX_EVENTS=20 \
COINEXT__BINANCE__TESTNET=false \
cargo run --manifest-path crates/coinext-ingest/Cargo.toml --features live
```

Sample real output (mainnet BTCUSDT order-book deltas):

```
DELTA  BTCUSDT.BINANCE Buy Update px=63231.08000000 sz=0.00009000 seq=94974528889
DELTA  BTCUSDT.BINANCE Buy Delete px=63230.80000000 sz=0.00000000 seq=94974528889
...
```

The real service additionally writes the lake (`coinext_persistence::ParquetWriter`) and republishes a
versioned MessagePack `Envelope` on Redis Streams (`coinext_bus`) for the `trader`/`api`/`risk-monitor`.

## 2. Real-data backtest — NO key (verified ✅)

Backtest on REAL Binance klines (public REST, stdlib only):

```python
from coinext_data import fetch_binance_klines      # public REST, no key
from coinext_backtest import run
from coinext_strategy import SmaCross
from coinext_analytics import tear_sheet

bars = fetch_binance_klines("BTCUSDT", "1m", 500)   # real 1m closes as (ts_ns, close)
res = run(SmaCross(10, 30, 0.05), bars=bars)        # same Rust kernel as live
print(tear_sheet(res))
```

## 3. Testnet execution smoke test — needs key

Exercises the full `ExecutionClient` path against the paper venue: connect user-data stream →
submit a resting LIMIT BUY far below market → `Accepted` → reconcile → cancel → `Canceled`. No fill
(the limit is far below market), and it's paper money regardless.

```bash
export COINEXT__BINANCE__API_KEY=...      # spot testnet key
export COINEXT__BINANCE__API_SECRET=...
cargo run --manifest-path crates/coinext-adapters/binance/Cargo.toml --example testnet_order
# optional: COINEXT__ORDER__SYMBOL / COINEXT__ORDER__PRICE / COINEXT__ORDER__QTY
```

Without keys it aborts before any network call (safe wiring check). The order id is the deterministic
`ClientOrderId` (`newClientOrderId`), so retries are idempotent — never a double-submit.

## 4. The parity promotion gate

Before going live, a strategy must pass `coinext_parity.run_gate` against a recorded sandbox session:

```python
from coinext_parity import SessionResult, run_gate, render_verdict, AcceptanceCriterion

# `sandbox` is a SessionResult recorded from a testnet run (its fills + equity).
verdict = run_gate(lambda: SmaCross(10, 30, 0.05), bars=bars, sandbox=sandbox,
                   criterion=AcceptanceCriterion())  # 0.95 / 5bps / 0.90 / 0.02
print(render_verdict(verdict))   # PASS -> promote-eligible; FAIL -> BLOCKED from live
```

The gate bounds **signal-timing agreement**, **realized-vs-simulated fill-price deviation (bps)**,
**equity correlation**, and **return diff** between the deterministic backtest and the sandbox. A
quick demo (synthetic sandbox) runs via the CLI:

```bash
python -m coinext_cli.main parity        # or: just cli-backtest / coinext parity
```

A separate **advisory** `cross_check` warns on vectorized-vs-event drift but never gates.

## Going to live (later)

Flip `COINEXT__BINANCE__TESTNET=false` and supply **mainnet** keys with **withdrawal disabled** + an **IP
allowlist**; store secrets in SOPS/Vault (see ARCHITECTURE.md §11 open questions). The out-of-band
`risk-monitor` watches PnL/positions and can trip the global kill-switch; the per-order `RiskEngine`
gate runs synchronously on every order in backtest, sandbox, and live alike.
```
