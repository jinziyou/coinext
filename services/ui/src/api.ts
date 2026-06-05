// VeloxQuant — typed fetch client for the `api` service.
//
// The `api` service (FastAPI, canonical port 8000) is the read-side projection
// over the Redis-Streams Envelope bus (docs/ARCHITECTURE.md §6/§8): the trader /
// exec-svc / risk-monitor publish state, the api caches it, and this UI polls it.
// The only mutating call is the kill-switch, which the api forwards to the
// exec-svc control plane (port 8081) to trip the atomic kill-switch in
// qv-risk-engine.
//
// Base URL: VITE_API_BASE (injected at build/run time) || http://localhost:8000.
// Set VITE_API_BASE=/api to route through the dev proxy in vite.config.ts.

export const API_BASE: string =
  (import.meta.env.VITE_API_BASE as string | undefined) ??
  "http://localhost:8000";

// ---------------------------------------------------------------------------
// Wire types — mirror the api service response models.
//
// NOTE: all monetary / quantity / price fields cross the JSON boundary as
// strings, preserving the fixed-precision integer domain (no f64 in the domain;
// see ARCHITECTURE §4). The UI treats them as opaque display strings and does
// NOT do float math on them.
// ---------------------------------------------------------------------------

export type Environment = "backtest" | "sandbox" | "live";

export type RunStatus =
  | "pending"
  | "running"
  | "paused"
  | "completed"
  | "failed"
  | "killed";

/** A trading/backtest run as surfaced by GET /runs. */
export interface Run {
  run_id: string;
  strategy_id: string;
  environment: Environment;
  status: RunStatus;
  started_at: string | null; // ISO-8601
  updated_at: string | null; // ISO-8601
  /** Realized + unrealized PnL for the run, in the account currency (string). */
  pnl?: string;
  pnl_currency?: string;
}

export type PositionSide = "long" | "short" | "flat";

/** A live position with mark-sourced PnL as surfaced by GET /positions. */
export interface Position {
  instrument_id: string;
  venue: string;
  side: PositionSide;
  /** Absolute position size (non-negative fixed-precision quantity, string). */
  quantity: string;
  avg_px: string;
  /** Latest mark price from the Cache (string). */
  mark_px: string;
  unrealized_pnl: string;
  realized_pnl: string;
  currency: string;
  ts_last: string | null; // ISO-8601
}

export type OrderSide = "buy" | "sell";
export type LiquiditySide = "maker" | "taker" | "unknown";

/** An execution fill as surfaced by GET /fills. */
export interface Fill {
  fill_id: string;
  client_order_id: string;
  venue_order_id: string | null;
  instrument_id: string;
  side: OrderSide;
  last_qty: string;
  last_px: string;
  commission: string;
  commission_currency: string;
  liquidity: LiquiditySide;
  ts_event: string; // ISO-8601 (venue/sim event time)
}

/**
 * Latency SLO snapshot as surfaced by GET /latency. Values are the histogram
 * percentiles the platform tracks (ARCHITECTURE §8): submit_to_ack_ns,
 * strategy_dispatch_ns, ingest_to_publish_ns, etc. Reported in nanoseconds.
 */
export interface LatencyMetric {
  name: string; // e.g. "submit_to_ack_ns"
  p50_ns: number;
  p95_ns: number;
  p99_ns: number;
  count: number;
}

export interface LatencySnapshot {
  metrics: LatencyMetric[];
  ts_snapshot: string | null; // ISO-8601
}

/** Current kill-switch state as surfaced by GET /control/killswitch. */
export interface KillSwitchState {
  engaged: boolean;
  engaged_by: string | null;
  reason: string | null;
  ts_changed: string | null; // ISO-8601
}

export interface KillSwitchRequest {
  /** true => engage (halt all routing), false => disengage. */
  engage: boolean;
  reason: string;
  /** Operator identity for the audit trail. */
  actor?: string;
}

// ---------------------------------------------------------------------------
// HTTP plumbing
// ---------------------------------------------------------------------------

export class ApiError extends Error {
  constructor(
    public status: number,
    public statusText: string,
    public body?: unknown,
  ) {
    super(`api ${status} ${statusText}`);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { "content-type": "application/json", ...(init?.headers ?? {}) },
    ...init,
  });
  if (!res.ok) {
    let body: unknown = undefined;
    try {
      body = await res.json();
    } catch {
      // non-JSON error body; ignore
    }
    throw new ApiError(res.status, res.statusText, body);
  }
  // 204 No Content guard
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

// ---------------------------------------------------------------------------
// Endpoints — keep names/paths aligned with the `api` service.
// ---------------------------------------------------------------------------

export const api = {
  /** GET /runs — list of runs across environments. */
  getRuns: () => request<Run[]>("/runs"),

  /** GET /positions — live positions with mark-sourced PnL. */
  getPositions: () => request<Position[]>("/positions"),

  /** GET /fills — recent execution fills (most recent first). */
  getFills: () => request<Fill[]>("/fills"),

  /** GET /latency — latency SLO histogram snapshot. */
  getLatency: () => request<LatencySnapshot>("/latency"),

  /** GET /control/killswitch — current global kill-switch state. */
  getKillSwitch: () => request<KillSwitchState>("/control/killswitch"),

  /**
   * POST /control/killswitch — engage/disengage the global kill-switch.
   * Forwarded by the api to exec-svc's control plane (port 8081) which trips
   * the atomic kill-switch in qv-risk-engine; all order routing is denied
   * while engaged (risk_denials metric increments).
   */
  setKillSwitch: (req: KillSwitchRequest) =>
    request<KillSwitchState>("/control/killswitch", {
      method: "POST",
      body: JSON.stringify(req),
    }),
};
