"""services/risk-monitor — out-of-band global risk supervisor.

ARCHITECTURE.md §7–§8: the per-order ``coinext-risk-engine`` gate lives *inside* each trading node's
synchronous core and is the first line of defense. This service is the **second, out-of-band** line:
a standalone process that watches *all* PnL / position / fill telemetry on the Redis-Streams bus
(``coinext_bus``, decoding the MessagePack ``Envelope`` — §6) and enforces **account-wide** limits the
in-core gate cannot see in isolation:

* **max drawdown**            — peak-to-trough equity decline across the account,
* **gross / net exposure**    — sum of abs(notional) (gross) and signed notional (net) per instrument,
* **loss-of-day**             — realized + unrealized PnL since the session boundary.

On a breach it **trips the global kill-switch** by publishing a ``CtrlKillSwitch`` command on the
control stream. Every ``trader`` process's in-core risk gate honours it atomically, halting new order
routing platform-wide. Because it is out-of-band, a crash or deadlock in a trading node cannot
silence it.

Canonical deployment (service/port table): built from ``deploy/docker/risk-monitor.Dockerfile``,
exposes Prometheus metrics on **:9104**. Config via ``COINEXT__REDIS__URL`` and the ``COINEXT__RISK__*`` keys
(shared with the in-core gate — see ``.env.example``).

Design note: ``coinext_bus`` (and ``redis`` / ``msgpack`` / ``prometheus_client``) are imported **lazily
and guarded**, so this module imports and the limit math is unit-testable without the bus, the
extension, or a running Redis.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger("coinext.risk_monitor")

# --------------------------------------------------------------------------------------------------
# Config (COINEXT__SECTION__KEY convention; same keys the in-core RiskEngine reads — defense in depth)
# --------------------------------------------------------------------------------------------------


def _env_float(key: str, default: float) -> float:
    raw = os.environ.get(key)
    try:
        return float(raw) if raw is not None else default
    except ValueError:  # pragma: no cover - defensive
        logger.warning("invalid float for %s=%r; using default %s", key, raw, default)
        return default


@dataclass(frozen=True)
class RiskLimits:
    """Account-wide limits. Sourced from ``COINEXT__RISK__*`` env (see ``.env.example``)."""

    max_drawdown_pct: float = 0.20  # trip if equity falls 20% below its session peak
    max_gross_exposure: float = 1_000_000.0  # sum of |notional| across all instruments
    max_net_exposure: float = 500_000.0  # |signed notional| (directional)
    max_loss_of_day: float = 50_000.0  # absolute PnL loss since session start

    @classmethod
    def from_env(cls) -> RiskLimits:
        return cls(
            max_drawdown_pct=_env_float("COINEXT__RISK__MAX_DRAWDOWN_PCT", cls.max_drawdown_pct),
            max_gross_exposure=_env_float(
                "COINEXT__RISK__MAX_GROSS_EXPOSURE", cls.max_gross_exposure
            ),
            max_net_exposure=_env_float("COINEXT__RISK__MAX_NET_EXPOSURE", cls.max_net_exposure),
            max_loss_of_day=_env_float("COINEXT__RISK__MAX_LOSS_OF_DAY", cls.max_loss_of_day),
        )


# --------------------------------------------------------------------------------------------------
# Rolling account state folded from bus telemetry
# --------------------------------------------------------------------------------------------------


@dataclass
class AccountState:
    """Running account aggregate folded from position / PnL envelopes on the bus."""

    equity: float = 0.0
    session_peak_equity: float = 0.0
    day_start_equity: float = 0.0
    gross_exposure: float = 0.0
    net_exposure: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl: float = 0.0

    def update_equity(self, equity: float) -> None:
        self.equity = equity
        self.session_peak_equity = max(self.session_peak_equity, equity)
        if self.day_start_equity == 0.0:
            self.day_start_equity = equity

    def snapshot(self) -> Any:
        """Project this account state onto a ``coinext_risk.PortfolioSnapshot`` (the shared view)."""
        from coinext_risk import PortfolioSnapshot  # local import: keeps coinext_risk optional

        return PortfolioSnapshot(
            ts_ns=0, equity=self.equity, peak_equity=self.session_peak_equity
        )

    @property
    def drawdown_pct(self) -> float:
        """Observed peak-to-trough decline (for reporting). The *breach decision* lives in
        ``coinext_risk.MaxDrawdown`` — this matches its ``(peak - equity) / peak`` definition."""
        if self.session_peak_equity <= 0.0:
            return 0.0
        return (self.session_peak_equity - self.equity) / self.session_peak_equity

    @property
    def loss_of_day(self) -> float:
        """Positive number = loss since session start."""
        return max(0.0, self.day_start_equity - self.equity)


@dataclass
class Breach:
    """A single tripped limit, attached to the kill-switch reason."""

    limit: str
    observed: float
    threshold: float

    def __str__(self) -> str:  # pragma: no cover - formatting only
        return f"{self.limit}: observed={self.observed:.2f} threshold={self.threshold:.2f}"


@dataclass
class RiskSupervisor:
    """Pure limit-evaluation core (no I/O) — unit-testable in isolation."""

    limits: RiskLimits = field(default_factory=RiskLimits.from_env)
    state: AccountState = field(default_factory=AccountState)
    tripped: bool = False

    def evaluate(self) -> list[Breach]:
        """Return the list of breached limits for the current account state (empty == healthy).

        The drawdown circuit-breaker is delegated to ``coinext_risk.MaxDrawdown`` — the single source
        of truth for that math (see the reconciliation note in ``coinext_risk``). The account-wide
        exposure / loss-of-day limits the protections pipeline does not model are evaluated here.
        """
        from coinext_risk import MaxDrawdown, ProtectionConfig  # local import: optional dep

        breaches: list[Breach] = []
        dd = MaxDrawdown(ProtectionConfig(max_drawdown_pct=self.limits.max_drawdown_pct))
        if dd.evaluate(self.state.snapshot()).tripped:
            breaches.append(
                Breach("max_drawdown", self.state.drawdown_pct, self.limits.max_drawdown_pct)
            )
        if self.state.gross_exposure > self.limits.max_gross_exposure:
            breaches.append(
                Breach("gross_exposure", self.state.gross_exposure, self.limits.max_gross_exposure)
            )
        if abs(self.state.net_exposure) > self.limits.max_net_exposure:
            breaches.append(
                Breach("net_exposure", abs(self.state.net_exposure), self.limits.max_net_exposure)
            )
        if self.state.loss_of_day > self.limits.max_loss_of_day:
            breaches.append(
                Breach("loss_of_day", self.state.loss_of_day, self.limits.max_loss_of_day)
            )
        return breaches

    def fold_envelope(self, payload: dict) -> None:
        """Fold one decoded bus payload (position / PnL / account snapshot) into the running state.

        TODO: match against the concrete coinext_contracts payload schemas (FILL / position snapshot /
        account event). The shape below is a representative placeholder.
        """
        if "equity" in payload:
            self.state.update_equity(float(payload["equity"]))
        if "gross_exposure" in payload:
            self.state.gross_exposure = float(payload["gross_exposure"])
        if "net_exposure" in payload:
            self.state.net_exposure = float(payload["net_exposure"])
        if "realized_pnl" in payload:
            self.state.realized_pnl = float(payload["realized_pnl"])
        if "unrealized_pnl" in payload:
            self.state.unrealized_pnl = float(payload["unrealized_pnl"])


# --------------------------------------------------------------------------------------------------
# Bus wiring (lazy / guarded)
# --------------------------------------------------------------------------------------------------

REDIS_URL = os.environ.get("COINEXT__REDIS__URL", "redis://redis:6379/0")
STREAM_TELEMETRY = "coinext.live"  # position / PnL telemetry consumed here
STREAM_CONTROL = "coinext.control"  # CtrlKillSwitch published here on a breach
METRICS_PORT = int(os.environ.get("COINEXT__RISK_MONITOR__METRICS_PORT", "9104"))


def _load_bus():
    """Import ``coinext_bus`` lazily; return None when the bus client is unavailable."""
    try:
        import coinext_bus  # noqa: WPS433 - intentional lazy import

        return coinext_bus
    except ImportError:  # pragma: no cover - environment-dependent
        return None


def _breach_reason(breaches: list[Breach]) -> str:
    """The human-readable kill-switch reason for a set of ``breaches``."""
    return "risk-monitor breach: " + "; ".join(str(b) for b in breaches)


def _trip_kill_switch(bus, breaches: list[Breach]) -> None:
    """Publish a global ``CtrlKillSwitch`` (engaged) command in response to ``breaches``.

    Builds a real CtrlKillSwitch command (a ``MsgType.CTRL`` Envelope) via ``coinext_bus.Publisher``
    and publishes it on ``STREAM_CONTROL``. Every ``trader``'s in-core gate honours it.
    """
    reason = _breach_reason(breaches)
    if bus is None:  # pragma: no cover - environment-dependent
        logger.critical("KILL-SWITCH (no bus to publish): %s", reason)
        return
    try:  # pragma: no cover - requires a running redis
        publisher = bus.Publisher(REDIS_URL)
        publisher.publish_kill_switch(
            STREAM_CONTROL, engaged=True, reason=reason, source="risk-monitor"
        )
        logger.critical("KILL-SWITCH ENGAGED and published: %s", reason)
    except Exception as exc:  # noqa: BLE001 - last line of defense: log loudly, never swallow silently
        logger.exception("failed to publish kill-switch (%s): %s", reason, exc)


def process_message(supervisor: RiskSupervisor, payload: dict, bus) -> bool:
    """Fold one telemetry ``payload`` into ``supervisor``, evaluate, and trip on the FIRST breach.

    Pure-ish: the only side effect is publishing the kill-switch (via ``bus``) the single time the
    supervisor latches ``tripped``. Returns True iff the kill-switch was tripped by THIS message, so
    the consume loop never double-publishes. Unit-testable with a fake bus.
    """
    supervisor.fold_envelope(payload)
    if supervisor.tripped:
        return False
    breaches = supervisor.evaluate()
    if breaches:
        _trip_kill_switch(bus, breaches)
        supervisor.tripped = True
        return True
    return False


# --------------------------------------------------------------------------------------------------
# Async supervisory loop
# --------------------------------------------------------------------------------------------------


def consume_loop(bus, supervisor: RiskSupervisor) -> None:
    """Blocking consume loop: read telemetry off STREAM_TELEMETRY, fold, evaluate, trip on breach.

    Drives ``bus.RedisBusClient.consume`` (consumer-group, at-least-once). Each :class:`StreamMessage`
    carries a decoded Envelope; we msgpack-decode its payload and hand it to :func:`process_message`,
    which trips the kill-switch exactly once on the first breach. Separated from :func:`run` so it is
    testable with a fake bus that yields synthetic StreamMessages.
    """
    client = bus.RedisBusClient(REDIS_URL)
    for message in client.consume([STREAM_TELEMETRY]):  # pragma: no branch
        try:
            payload = bus.decode_payload(message.envelope)
        except Exception as exc:  # noqa: BLE001 - a malformed frame must not kill the supervisor
            logger.warning("skipping undecodable telemetry frame: %s", exc)
            continue
        process_message(supervisor, payload, bus)


async def run(poll_interval_s: float = 1.0) -> None:
    """Main supervisory loop: consume telemetry, evaluate limits, trip on breach.

    Control flow:

      1. consume STREAM_TELEMETRY via ``coinext_bus.RedisBusClient`` (consumer group),
      2. for each Envelope: msgpack-decode payload → ``supervisor.fold_envelope(payload)``,
      3. ``breaches = supervisor.evaluate()``,
      4. on the FIRST breach: publish a real CtrlKillSwitch on STREAM_CONTROL and latch ``tripped``,
      5. export gauges/counters to Prometheus on :9104.

    When ``coinext_bus`` (or redis/msgpack) is unavailable the supervisor falls back to an IDLE
    keep-alive so the container stays up and /metrics is scrapeable. The blocking consume loop runs
    in a worker thread so this coroutine stays cancellable.
    """
    bus = _load_bus()
    supervisor = RiskSupervisor()
    _maybe_start_metrics_server()

    if bus is None or not hasattr(bus, "RedisBusClient"):
        logger.warning(
            "coinext_bus unavailable; risk-monitor running in IDLE stub mode (no telemetry consumed). "
            "Limits=%s",
            supervisor.limits,
        )
        # Idle keep-alive so the container stays up and /metrics is scrapeable.
        while True:  # pragma: no cover - long-running
            await asyncio.sleep(poll_interval_s)

    logger.info("risk-monitor consuming telemetry on %s; limits=%s", STREAM_TELEMETRY, supervisor.limits)
    await asyncio.to_thread(consume_loop, bus, supervisor)  # pragma: no cover - long-running


def _maybe_start_metrics_server() -> None:
    """Start the Prometheus metrics endpoint on :9104 if ``prometheus_client`` is installed.

    Exported series (planned): ``risk_drawdown_pct``, ``risk_gross_exposure``,
    ``risk_net_exposure``, ``risk_loss_of_day``, ``risk_killswitch_trips_total``.
    """
    try:  # pragma: no cover - optional dependency
        from prometheus_client import start_http_server  # noqa: WPS433

        start_http_server(METRICS_PORT)
        logger.info("risk-monitor metrics on :%d", METRICS_PORT)
    except ImportError:
        logger.info("prometheus_client not installed; metrics endpoint disabled")


def main() -> None:
    """Console entrypoint (also the Docker CMD target)."""
    logging.basicConfig(
        level=os.environ.get("COINEXT__LOG__LEVEL", "info").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    logger.info("starting risk-monitor (out-of-band global supervisor)")
    asyncio.run(run())


if __name__ == "__main__":
    main()
