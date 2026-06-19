// Coinext operator dashboard shell.
//
// Panels (docs/ARCHITECTURE.md §7/§8):
//   - Runs                : GET /runs
//   - Live Positions/PnL  : GET /positions (mark-sourced unrealized PnL)
//   - Fills               : GET /fills
//   - Latency             : GET /latency (SLO histograms: submit_to_ack_ns, ...)
//   - Kill-Switch         : GET/POST /control/killswitch (guarded, confirm dialog)
//
// All data is fetched via @tanstack/react-query against the `api` service. This
// is a SCAFFOLD: panels render real fetched data but styling/UX is intentionally
// minimal. TODO markers note where richer operator tooling will land.
import { useState } from "react";
import {
  useMutation,
  useQuery,
  useQueryClient,
} from "@tanstack/react-query";
import {
  api,
  ApiError,
  API_BASE,
  type Fill,
  type KillSwitchState,
  type LatencySnapshot,
  type Position,
  type Run,
} from "./api";

// --- formatting helpers (display-only; never used for domain math) ----------

function fmtTs(ts: string | null | undefined): string {
  if (!ts) return "—";
  const d = new Date(ts);
  return Number.isNaN(d.getTime()) ? ts : d.toLocaleTimeString();
}

function fmtNs(ns: number): string {
  if (ns >= 1_000_000) return `${(ns / 1_000_000).toFixed(2)} ms`;
  if (ns >= 1_000) return `${(ns / 1_000).toFixed(2)} µs`;
  return `${ns} ns`;
}

function pnlClass(v: string | undefined): string {
  if (!v) return "";
  // Leading '-' is sufficient to colorize; we deliberately do NOT parseFloat
  // domain money (fixed-precision integer strings; see ARCHITECTURE §4).
  return v.trim().startsWith("-") ? "neg" : "pos";
}

// --- generic panel chrome ---------------------------------------------------

function Panel(props: {
  title: string;
  isFetching?: boolean;
  error?: unknown;
  children: React.ReactNode;
  actions?: React.ReactNode;
}) {
  return (
    <section className="panel">
      <header className="panel-head">
        <h2>{props.title}</h2>
        <div className="panel-head-right">
          {props.isFetching ? <span className="dot live" title="refreshing" /> : null}
          {props.actions}
        </div>
      </header>
      <div className="panel-body">
        {props.error ? <ErrorBanner error={props.error} /> : props.children}
      </div>
    </section>
  );
}

function ErrorBanner({ error }: { error: unknown }) {
  const msg =
    error instanceof ApiError
      ? `${error.status} ${error.statusText}`
      : error instanceof Error
        ? error.message
        : String(error);
  return <div className="error">Failed to load: {msg}</div>;
}

function Empty({ label }: { label: string }) {
  return <div className="empty">{label}</div>;
}

// --- panels -----------------------------------------------------------------

function RunsPanel() {
  const q = useQuery<Run[]>({
    queryKey: ["runs"],
    queryFn: api.getRuns,
    refetchInterval: 5_000,
  });
  return (
    <Panel title="Runs" isFetching={q.isFetching} error={q.error}>
      {q.data && q.data.length > 0 ? (
        <table>
          <thead>
            <tr>
              <th>Run</th>
              <th>Strategy</th>
              <th>Env</th>
              <th>Status</th>
              <th className="num">PnL</th>
              <th>Updated</th>
            </tr>
          </thead>
          <tbody>
            {q.data.map((r) => (
              <tr key={r.run_id}>
                <td className="mono">{r.run_id}</td>
                <td>{r.strategy_id}</td>
                <td>
                  <span className={`tag env-${r.environment}`}>{r.environment}</span>
                </td>
                <td>
                  <span className={`tag status-${r.status}`}>{r.status}</span>
                </td>
                <td className={`num ${pnlClass(r.pnl)}`}>
                  {r.pnl ? `${r.pnl} ${r.pnl_currency ?? ""}` : "—"}
                </td>
                <td>{fmtTs(r.updated_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : q.isLoading ? (
        <Empty label="Loading runs…" />
      ) : (
        <Empty label="No runs" />
      )}
    </Panel>
  );
}

function PositionsPanel() {
  const q = useQuery<Position[]>({
    queryKey: ["positions"],
    queryFn: api.getPositions,
    refetchInterval: 2_000,
  });
  return (
    <Panel title="Live Positions / PnL" isFetching={q.isFetching} error={q.error}>
      {q.data && q.data.length > 0 ? (
        <table>
          <thead>
            <tr>
              <th>Instrument</th>
              <th>Side</th>
              <th className="num">Qty</th>
              <th className="num">Avg</th>
              <th className="num">Mark</th>
              <th className="num">uPnL</th>
              <th className="num">rPnL</th>
            </tr>
          </thead>
          <tbody>
            {q.data.map((p) => (
              <tr key={`${p.venue}:${p.instrument_id}`}>
                <td className="mono">{p.instrument_id}</td>
                <td>
                  <span className={`tag side-${p.side}`}>{p.side}</span>
                </td>
                <td className="num">{p.quantity}</td>
                <td className="num">{p.avg_px}</td>
                <td className="num">{p.mark_px}</td>
                <td className={`num ${pnlClass(p.unrealized_pnl)}`}>
                  {p.unrealized_pnl} {p.currency}
                </td>
                <td className={`num ${pnlClass(p.realized_pnl)}`}>
                  {p.realized_pnl} {p.currency}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : q.isLoading ? (
        <Empty label="Loading positions…" />
      ) : (
        <Empty label="Flat — no open positions" />
      )}
    </Panel>
  );
}

function FillsPanel() {
  const q = useQuery<Fill[]>({
    queryKey: ["fills"],
    queryFn: api.getFills,
    refetchInterval: 2_000,
  });
  return (
    <Panel title="Fills" isFetching={q.isFetching} error={q.error}>
      {q.data && q.data.length > 0 ? (
        <table>
          <thead>
            <tr>
              <th>Time</th>
              <th>Instrument</th>
              <th>Side</th>
              <th className="num">Qty</th>
              <th className="num">Px</th>
              <th>Liq</th>
              <th className="num">Fee</th>
            </tr>
          </thead>
          <tbody>
            {q.data.map((f) => (
              <tr key={f.fill_id}>
                <td>{fmtTs(f.ts_event)}</td>
                <td className="mono">{f.instrument_id}</td>
                <td>
                  <span className={`tag side-${f.side === "buy" ? "long" : "short"}`}>
                    {f.side}
                  </span>
                </td>
                <td className="num">{f.last_qty}</td>
                <td className="num">{f.last_px}</td>
                <td>{f.liquidity}</td>
                <td className="num">
                  {f.commission} {f.commission_currency}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : q.isLoading ? (
        <Empty label="Loading fills…" />
      ) : (
        <Empty label="No fills yet" />
      )}
    </Panel>
  );
}

function LatencyPanel() {
  const q = useQuery<LatencySnapshot>({
    queryKey: ["latency"],
    queryFn: api.getLatency,
    refetchInterval: 5_000,
  });
  return (
    <Panel title="Latency (SLO)" isFetching={q.isFetching} error={q.error}>
      {q.data && q.data.metrics.length > 0 ? (
        <table>
          <thead>
            <tr>
              <th>Metric</th>
              <th className="num">p50</th>
              <th className="num">p95</th>
              <th className="num">p99</th>
              <th className="num">n</th>
            </tr>
          </thead>
          <tbody>
            {q.data.metrics.map((m) => (
              <tr key={m.name}>
                <td className="mono">{m.name}</td>
                <td className="num">{fmtNs(m.p50_ns)}</td>
                <td className="num">{fmtNs(m.p95_ns)}</td>
                <td className="num">{fmtNs(m.p99_ns)}</td>
                <td className="num">{m.count}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : q.isLoading ? (
        <Empty label="Loading latency…" />
      ) : (
        <Empty label="No latency samples" />
      )}
    </Panel>
  );
}

// --- guarded kill-switch ----------------------------------------------------

function KillSwitchPanel() {
  const qc = useQueryClient();
  const state = useQuery<KillSwitchState>({
    queryKey: ["killswitch"],
    queryFn: api.getKillSwitch,
    refetchInterval: 3_000,
  });

  const [confirmOpen, setConfirmOpen] = useState(false);
  const [reason, setReason] = useState("");

  const engaged = state.data?.engaged ?? false;
  // When disengaged we offer to ENGAGE (halt); when engaged we offer to RELEASE.
  const nextEngage = !engaged;

  const mutation = useMutation({
    mutationFn: () =>
      api.setKillSwitch({
        engage: nextEngage,
        reason: reason.trim() || (nextEngage ? "operator halt" : "operator release"),
        actor: "ui-operator",
      }),
    onSuccess: (next) => {
      qc.setQueryData(["killswitch"], next);
      void qc.invalidateQueries({ queryKey: ["killswitch"] });
      setConfirmOpen(false);
      setReason("");
    },
  });

  return (
    <Panel
      title="Kill-Switch"
      isFetching={state.isFetching}
      error={state.error}
      actions={
        <span className={`tag ${engaged ? "status-killed" : "status-running"}`}>
          {engaged ? "ENGAGED" : "armed"}
        </span>
      }
    >
      <div className="killswitch">
        <p className="muted">
          Trips the atomic global kill-switch in <code>coinext-risk-engine</code> via
          the exec-svc control plane. While engaged, all order routing is denied.
        </p>

        {engaged && (state.data?.reason || state.data?.engaged_by) ? (
          <p className="muted small">
            Engaged by <strong>{state.data?.engaged_by ?? "?"}</strong>
            {state.data?.reason ? ` — “${state.data.reason}”` : ""}
            {state.data?.ts_changed ? ` (${fmtTs(state.data.ts_changed)})` : ""}
          </p>
        ) : null}

        {!confirmOpen ? (
          <button
            className={nextEngage ? "btn danger" : "btn warn"}
            onClick={() => setConfirmOpen(true)}
            disabled={state.isLoading}
          >
            {nextEngage ? "ENGAGE KILL-SWITCH" : "Release kill-switch"}
          </button>
        ) : (
          // Confirm dialog (guard): require an explicit second action + reason.
          <div className="confirm">
            <p className="confirm-q">
              {nextEngage
                ? "Halt ALL order routing across every live run?"
                : "Re-enable order routing?"}
            </p>
            <input
              type="text"
              placeholder="reason (audit trail)"
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              autoFocus
            />
            <div className="confirm-actions">
              <button
                className={nextEngage ? "btn danger" : "btn warn"}
                onClick={() => mutation.mutate()}
                disabled={mutation.isPending}
              >
                {mutation.isPending
                  ? "Working…"
                  : nextEngage
                    ? "Confirm: ENGAGE"
                    : "Confirm: release"}
              </button>
              <button
                className="btn ghost"
                onClick={() => {
                  setConfirmOpen(false);
                  setReason("");
                }}
                disabled={mutation.isPending}
              >
                Cancel
              </button>
            </div>
            {mutation.error ? <ErrorBanner error={mutation.error} /> : null}
          </div>
        )}
      </div>
    </Panel>
  );
}

// --- shell ------------------------------------------------------------------

export function App() {
  return (
    <div className="app">
      <header className="app-head">
        <div className="brand">
          <span className="brand-mark">CX</span>
          <span className="brand-name">Coinext</span>
          <span className="brand-sub">operator dashboard</span>
        </div>
        <div className="app-head-right">
          <span className="muted small mono">api: {API_BASE}</span>
        </div>
      </header>

      <main className="grid">
        <div className="col col-wide">
          <RunsPanel />
          <PositionsPanel />
          <FillsPanel />
        </div>
        <div className="col col-narrow">
          <KillSwitchPanel />
          <LatencyPanel />
        </div>
      </main>

      <footer className="app-foot">
        <span className="muted small">
          Scaffold — read-only cockpit over the api service. See
          docs/ARCHITECTURE.md §8.
        </span>
      </footer>
    </div>
  );
}
