"""coinext_live — the TradingNode (live / sandbox runtime).

Builds the SAME ``RunConfig`` as the backtest, but tells the Kernel to inject ``Environment::Live``
(or ``Sandbox``) pieces: a ``LiveClock``, the ``BinanceDataClient``, and the
``BinanceExecutionClient`` — behind byte-identical ports, so the OMS / Risk / Portfolio / Strategy
above are unchanged (ARCHITECTURE.md §1, §7). NOTHING else changes vs backtest.

Key live-only responsibilities (all stubbed here):

* **Warm-up from the LOCAL HistoryReader** — indicators are warmed from the lake, never via live
  REST at handler time, so they are byte-identical to backtest (ARCHITECTURE.md §7, §10).
* **Dual fill path** — fills/acks arrive on the WS user-stream (fast) with a REST poll loop
  (fallback). Both fold into the event-sourced Order/Position.
* **Reconcile-on-restart** — :meth:`reconcile` replays the local event log and diffs it against
  venue truth before trading resumes.

The Binance clients live in Rust (``coinext-adapters/binance``); this node only orchestrates lifecycle.
Async is via ``anyio`` (the ``live`` extra); imports are deferred so this module loads without it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from coinext_kernel import Environment


@dataclass
class TradingNodeConfig:
    """Live node wiring derived from a ``coinext_config.RunConfig``."""

    env: Environment = Environment.LIVE
    symbol: str = "BTCUSDT"
    warmup_bars: int = 200  # how many local bars to warm indicators with before going live
    reconcile_on_start: bool = True
    rest_poll_secs: float = 5.0  # REST fill fallback cadence


@dataclass
class TradingNode:
    """Live runtime orchestrator.

    Holds the strategy, the Kernel handle, the local :class:`~coinext_data.HistoryReader` for warm-up,
    and (in a real build) the Rust-side Binance clients. ``run()`` is an async stub documenting the
    lifecycle; the actual I/O loops live on Tokio behind ``coinext_py``.
    """

    config: TradingNodeConfig
    strategy: Any
    run_config: Any = None  # a coinext_config.RunConfig
    _kernel: Any = field(default=None, init=False, repr=False)
    _running: bool = field(default=False, init=False, repr=False)
    _killed: bool = field(default=False, init=False, repr=False)
    _kill_reason: str | None = field(default=None, init=False, repr=False)

    # --- kill-switch ----------------------------------------------------------------------------
    @property
    def killed(self) -> bool:
        """True once the global kill-switch has engaged this node (no new order routing)."""
        return self._killed

    def engage_kill_switch(self, reason: str = "") -> None:
        """Engage this node's kill-switch and request a graceful stop.

        Wired to the control stream via :func:`on_control_message`. Sets the kill flag (the in-core
        gate / OMS reads it to deny new orders) and tears the run loop down. Idempotent.

        TODO: once the native run loop is wired, also signal ``coinext_py`` so the Rust core's atomic
        kill-switch flips in lock-step.
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

    async def run(self) -> None:
        """Run the live node until stopped.

        Lifecycle:

        1. (optional) :meth:`reconcile` against venue truth.
        2. :meth:`warmup` indicators from the local lake.
        3. Build the Kernel for ``LIVE``/``SANDBOX`` (injects LiveClock + Binance clients).
        4. Hand control to the Rust core: WS market data + WS user-stream fills drive the SAME
           synchronous handlers; a REST poll loop is the fill fallback; TimerEvents come from the
           LiveClock.

        This is a STUB: it documents the sequence and yields control once. The real loop runs on
        Tokio behind ``coinext_py`` and only returns on shutdown / kill-switch.
        """
        if self.config.reconcile_on_start:
            self.reconcile()
        self.warmup()

        # TODO: self._kernel = coinext_kernel.build_kernel(self.run_config, self.config.env)
        # TODO: await the native run loop; bridge KeyboardInterrupt / kill-switch to graceful stop.
        self._running = True
        try:
            import anyio  # type: ignore

            await anyio.sleep(0)  # placeholder yield; replaced by the native run future
        except ImportError as exc:  # pragma: no cover - live extra not installed
            # No anyio: nothing to await. Surfaced clearly when actually wiring the live loop.
            raise ImportError(
                "anyio not installed. Install the live extra: pip install 'coinext[live]'"
            ) from exc
        finally:
            self._running = False

    def stop(self) -> None:
        """Request a graceful shutdown (cancel timers, flush, disconnect clients). TODO: wire."""
        self._running = False


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
