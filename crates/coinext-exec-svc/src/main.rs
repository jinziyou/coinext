//! `coinext-exec-svc` — the standalone **OMS/execution service** (service name `exec-svc`).
//!
//! Per the architecture, this hosts the `coinext-exec-engine` (OMS: risk-gated routing, FSM driving,
//! report folding — §3) and `coinext-risk-engine` (pre-trade gate + atomic kill-switch) in front of a
//! live `ExecutionClient` (the `coinext-adapters-binance` `BinanceExecutionClient`). It is the live
//! counterpart of what the Kernel wires in-process for backtest, behind the SAME `coinext-ports` seam, so
//! order-flow behavior is identical across environments (§5).
//!
//! Service contract (canonical service + port table):
//!   - service `exec-svc`, image built from `deploy/docker/exec-svc.Dockerfile`
//!   - Prometheus metrics on `:9102` (SLOs: `submit_to_ack_ns`, `risk_denials`)
//!   - control endpoint on `:8081` (e.g. trip/clear the kill-switch out-of-band)
//!   - config via the `COINEXT__SECTION__KEY` env convention.
//!
//! This is a SCAFFOLD: `main` prints a startup banner and documents the OMS/risk loop as TODOs, then
//! exits. The real loop is wired once the engine/adapter/persistence/bus deps are enabled.

#[tokio::main]
async fn main() {
    // Startup banner (the real service emits this via structured tracing -> Loki/OTel).
    println!("=========================================================");
    println!("  Coinext exec-svc (coinext-exec-svc)  [SCAFFOLD]");
    println!("  role    : OMS / execution + pre-trade risk service");
    println!("  metrics : http://0.0.0.0:9102/metrics  (TODO)");
    println!("  control : http://0.0.0.0:8081          (TODO: kill-switch)");
    println!("  venue   : Binance (live ExecutionClient behind coinext-ports)");
    println!("=========================================================");

    // TODO(exec-svc): the OMS/risk service loop —
    //   1. Load config (COINEXT__BINANCE__*, COINEXT__REDIS__URL, RiskLimits) via coinext_config.
    //   2. Start the Prometheus exporter (:9102), OTel tracing, and the control server (:8081).
    //   3. Build the RiskEngine (coinext-risk-engine) with configured RiskLimits + kill-switch, the OMS
    //      (coinext-exec-engine), the persisted SeqCursor + EventStore (coinext-persistence), and the live
    //      BinanceExecutionClient (coinext-adapters-binance); connect() it and take_reports().
    //   4. On startup call ExecutionClient::reconcile(): replay the event log and diff venue truth
    //      (open orders + recent fills) by ClientOrderId before accepting new commands (§7).
    //   5. Loop, draining two sources:
    //        a. inbound StrategyCommands (Submit/Cancel/Modify) decoded from the Redis Envelope bus,
    //           each passed through the synchronous RiskEngine gate (record `risk_denials`); on
    //           Approved, route to the ExecutionClient (record `submit_to_ack_ns`).
    //        b. inbound ExecutionReports from take_reports(): append to the EventStore, fold into the
    //           event-sourced Order/Position FSM, and republish on the Redis bus for the trader/UI.
    //   6. The out-of-band risk-monitor (:9104) can POST the control endpoint to trip the global
    //      kill-switch; once engaged, every subsequent order is Denied (KillSwitchEngaged).
    //   7. On SIGTERM: stop accepting commands, flush the EventStore, disconnect the ExecutionClient.

    eprintln!("coinext-exec-svc: scaffold only — OMS/risk loop not yet implemented; exiting.");
}
