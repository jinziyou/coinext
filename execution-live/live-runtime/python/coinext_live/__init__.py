"""coinext_live — the TradingNode (live / sandbox runtime).

Builds the SAME ``RunConfig`` as the backtest, but tells the Kernel to inject ``Environment::Live``
(or ``Sandbox``) pieces: a ``LiveClock``, the ``BinanceDataClient``, and the
``BinanceExecutionClient`` — behind byte-identical ports, so the OMS / Risk / Portfolio / Strategy
above are unchanged (ARCHITECTURE.md §1, §7). NOTHING else changes vs backtest.

Key live-only responsibilities (partly wired here; native I/O still lives in Rust):

* **Warm-up from the LOCAL HistoryReader** — indicators are warmed from the lake, never via live
  REST at handler time, so they are byte-identical to backtest (ARCHITECTURE.md §7, §10).
* **Dual fill path** — fills/acks arrive on the WS user-stream (fast) with a REST poll loop
  (fallback). Both fold into the event-sourced Order/Position.
* **Portfolio telemetry** — native ``PortfolioSnapshot`` values are adapted via
  ``publish_native_snapshot`` / ``publish_kernel_portfolio`` and emitted on ``coinext.live``.
* **Reconcile-on-restart** — :meth:`reconcile` replays the local event log and diffs it against
  venue truth before trading resumes.

The Binance clients live in Rust (``coinext-adapters/binance``); this node only orchestrates lifecycle.
Async is via ``anyio`` (the ``live`` extra); imports are deferred so this module loads without it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import import_module
from typing import Any

from coinext_kernel import Environment


def _load_bus():
    """Import ``coinext_bus`` lazily; live telemetry is optional until the run loop starts."""
    try:
        import coinext_bus  # noqa: WPS433 - optional runtime dependency
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise ImportError(
            "coinext_bus unavailable. Install the bus extra so TradingNode can publish live telemetry."
        ) from exc
    return coinext_bus


@dataclass
class TradingNodeConfig:
    """Live node wiring derived from a ``coinext_config.RunConfig``."""

    env: Environment = Environment.LIVE
    account_id: str = "default"
    symbol: str = "BTCUSDT"
    venue: str = "BINANCE"
    redis_url: str = "redis://redis:6379/0"
    telemetry_stream: str = "coinext.live"
    warmup_bars: int = 200  # how many local bars to warm indicators with before going live
    reconcile_on_start: bool = True
    rest_poll_secs: float = 5.0  # REST fill fallback cadence

    def __post_init__(self) -> None:
        if not isinstance(self.env, Environment):
            self.env = Environment(self.env)


@dataclass
class TradingNode:
    """Live runtime orchestrator.

    Holds the strategy, the native Kernel handle, and the local
    :class:`~coinext_data.HistoryReader` for warm-up. Venue I/O lives in Rust behind ``coinext_py``;
    this object owns lifecycle and telemetry publication.
    """

    config: TradingNodeConfig
    strategy: Any
    run_config: Any = None  # a coinext_config.RunConfig
    _kernel: Any = field(default=None, init=False, repr=False)
    _running: bool = field(default=False, init=False, repr=False)
    _killed: bool = field(default=False, init=False, repr=False)
    _kill_reason: str | None = field(default=None, init=False, repr=False)
    _telemetry_publisher: Any = field(default=None, init=False, repr=False)

    # --- kill-switch ----------------------------------------------------------------------------
    @property
    def killed(self) -> bool:
        """True once the global kill-switch has engaged this node (no new order routing)."""
        return self._killed

    def engage_kill_switch(self, reason: str = "") -> None:
        """Engage this node's kill-switch and request a graceful stop.

        Wired to the control stream via :func:`on_control_message`. Sets the node kill flag and tears
        the run loop down. If a native kernel handle is attached, also request its stop signal so the
        Rust loop wakes, disconnects its ports, and returns. Idempotent.
        """
        if self._killed:
            return
        self._killed = True
        self._kill_reason = reason
        self.stop()

    # --- lifecycle ------------------------------------------------------------------------------
    def warmup(self) -> list[tuple[int, float]]:
        """Load warm-up bars from the LOCAL data lake and prime the strategy's indicators.

        Identical mechanism to backtest warm-up — this is the parity guarantee for indicator state.
        """
        from coinext_data import (  # local import: keeps coinext_data optional at import
            BarSpec,
            HistoryReader,
        )

        reader = HistoryReader()
        spec = BarSpec(symbol=self.config.symbol)
        # TODO: derive end_ns from the LiveClock at start; for now read the tail of the lake.
        bars = reader.warmup_bars(spec, end_ns=2**63 - 1, n=self.config.warmup_bars)
        # TODO: feed bars through the strategy's on_bar with a warmup ctx (no orders emitted).
        return bars

    def reconcile(self) -> dict[str, Any]:
        """Reconcile-on-restart: replay the local event log and diff against venue truth.

        Returns a diff report (missing fills, orphan orders, position mismatch). On disagreement the
        node must NOT resume trading until the operator resolves it. ARCHITECTURE.md §7, §11.
        """
        # TODO: read append-only OrderEvent store (coinext-persistence) + query Binance REST for open
        # orders / positions / balances, then compute the diff.
        return {"reconciled": False, "missing_fills": [], "orphan_orders": [], "note": "stub"}

    def publish_telemetry(
        self,
        *,
        equity: object,
        gross_exposure: object,
        net_exposure: object,
        realized_pnl: object,
        unrealized_pnl: object,
        positions: list[dict[str, object]] | None = None,
        ts_ns: int | None = None,
    ) -> str:
        """Publish one live account/position/PnL snapshot onto the cross-process bus.

        The native live loop owns the authoritative portfolio state; when it folds a fill/mark update,
        it calls this hook with that already-computed snapshot. Python only serializes the contract and
        publishes it to ``coinext.live`` for API fan-out and the out-of-band risk-monitor.
        """
        if self._telemetry_publisher is None:
            bus = _load_bus()
            self._telemetry_publisher = bus.Publisher(self.config.redis_url)
        return self._telemetry_publisher.publish_live_telemetry(
            self.config.telemetry_stream,
            account_id=self.config.account_id,
            environment=self.config.env.value,
            symbol=self.config.symbol,
            venue=self.config.venue,
            equity=equity,
            gross_exposure=gross_exposure,
            net_exposure=net_exposure,
            realized_pnl=realized_pnl,
            unrealized_pnl=unrealized_pnl,
            positions=positions,
            ts_ns=ts_ns,
            source="trader",
        )

    def publish_portfolio(self, portfolio: Any, *, ts_ns: int | None = None) -> str:
        """Project a ``coinext_portfolio.Portfolio`` facade and publish it as live telemetry."""
        return self.publish_telemetry(
            equity=portfolio.total_equity(),
            gross_exposure=portfolio.gross_exposure(),
            net_exposure=portfolio.net_exposure(),
            realized_pnl=portfolio.realized_pnl(),
            unrealized_pnl=portfolio.unrealized_pnl(),
            positions=portfolio.telemetry_positions(),
            ts_ns=ts_ns,
        )

    def publish_native_snapshot(self, native: Any, *, ts_ns: int | None = None) -> str:
        """Adapt a native Rust portfolio snapshot and publish it on the live telemetry stream."""
        from coinext_portfolio import Portfolio  # local import: keeps portfolio facade optional

        return self.publish_portfolio(Portfolio.from_native(native), ts_ns=ts_ns)

    def publish_kernel_portfolio(self, *, ts_ns: int | None = None) -> str | None:
        """Publish ``self._kernel.portfolio_snapshot()`` when a native kernel handle is attached.

        Returns ``None`` instead of fabricating telemetry if the node has not reached the native-kernel
        phase yet, or if the current handle does not expose the snapshot API.
        """
        kernel = self._kernel
        snapshot = getattr(kernel, "portfolio_snapshot", None) if kernel is not None else None
        if not callable(snapshot):
            return None
        return self.publish_native_snapshot(snapshot(), ts_ns=ts_ns)

    def _effective_run_config(self) -> Any:
        """Return the explicit ``RunConfig`` or load one from layered config plus node overrides."""
        if self.run_config is not None:
            return self.run_config
        load_config = import_module("coinext_config").load_config

        return load_config(
            env=self.config.env.value,
            cli_overrides={
                "env": self.config.env.value,
                "symbol": self.config.symbol,
                "redis": {"url": self.config.redis_url},
                "venue": {"name": self.config.venue},
            },
        )

    async def run(self) -> None:
        """Run the live node until the native kernel returns or raises.

        Lifecycle:

        1. (optional) :meth:`reconcile` against venue truth.
        2. :meth:`warmup` indicators from the local lake.
        3. Build the native Kernel for ``LIVE``/``SANDBOX`` via ``coinext_py.build_kernel``.
        4. Hand control to the Rust core; each authoritative fill/mark fold calls back with a native
           ``PortfolioSnapshot`` that this node publishes to ``coinext.live``.
        """
        if self.config.reconcile_on_start:
            self.reconcile()
        self.warmup()

        from coinext_kernel import build_kernel  # local import: compiled extension is optional

        self._kernel = build_kernel(
            self._effective_run_config(),
            self.config.env,
            strategy=self.strategy,
        )
        run_with_callback = getattr(self._kernel, "run_with_portfolio_callback", None)
        if not callable(run_with_callback):
            raise RuntimeError("native kernel handle does not expose run_with_portfolio_callback")

        self._running = True
        try:
            run_with_callback(lambda snapshot: self.publish_native_snapshot(snapshot))
        finally:
            self._running = False

    def stop(self) -> None:
        """Request graceful shutdown: signal native kernel, stop Python lifecycle, close publisher."""
        kernel = self._kernel
        request_stop = getattr(kernel, "request_stop", None) if kernel is not None else None
        if not callable(request_stop) and kernel is not None:
            request_stop = getattr(kernel, "stop", None)
        if callable(request_stop):
            request_stop()
        self._running = False
        publisher = self._telemetry_publisher
        close = getattr(publisher, "close", None)
        if callable(close):
            close()


# --------------------------------------------------------------------------------------------------
# Control-stream subscriber — engages this node's kill-switch on a CtrlKillSwitch command.
# --------------------------------------------------------------------------------------------------


def on_control_message(envelope: Any, on_kill: Callable[[str], None]) -> bool:
    """Dispatch one control-stream Envelope: engage the kill hook on a ``CtrlKillSwitch`` (engaged).

    Thin wrapper over ``coinext_bus.dispatch_control`` so the live node depends on the bus only at
    call time (and the dispatch stays unit-testable). Returns True iff ``on_kill`` fired.
    """
    from coinext_bus import dispatch_control  # local import: keeps coinext_bus optional at import

    return dispatch_control(envelope, on_kill)


def subscribe_control(node: TradingNode, url: str = "redis://redis:6379/0") -> None:
    """Subscribe to the control stream and engage ``node``'s kill-switch on a ``CtrlKillSwitch``.

    Blocking loop intended to run on its own thread/task beside the live node. Requires the bus
    extra (redis/msgpack); imported lazily so this module loads without them.
    """
    from coinext_bus import STREAM_CTRL, RedisBusClient  # local import: bus is optional

    client = RedisBusClient(url)
    for message in client.consume([STREAM_CTRL]):  # pragma: no cover - requires a running redis
        on_control_message(message.envelope, node.engage_kill_switch)


__all__ = [
    "TradingNode",
    "TradingNodeConfig",
    "on_control_message",
    "subscribe_control",
]
