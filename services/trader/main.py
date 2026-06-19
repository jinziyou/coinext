"""services/trader — thin live-trading process wrapper (one process per account).

ARCHITECTURE.md §7 (Live): ``coinext_live`` builds the **same** ``RunConfig`` the backtest uses, but with
``Environment::Live`` — the Kernel injects a ``LiveClock``, the ``BinanceDataClient``, and the
``BinanceExecutionClient`` behind byte-identical ports; nothing else changes (the parity invariant,
§1). This wrapper is intentionally thin: it builds a single ``coinext_live.TradingNode`` for **one
account**, wires the strategy, and runs it. Everything load-bearing lives in the Rust core +
``coinext_live``.

**One process per account.** Each account (set of API keys / sub-account) gets its **own** trader
process. This isolates blast radius (a crash, a kill-switch trip, a reconcile stall affects one
account only), keeps the deterministic single-threaded core per node, and sidesteps the open
SeqCursor-namespacing-across-accounts question (ARCHITECTURE.md §11). Horizontal scale = more trader
processes, coordinated only via the Redis bus.

Live data path: a standalone Rust ``ingestor`` normalizes venue WS frames and republishes them on
the Redis bus; this node's DataEngine consumes them. **Warm-up is always served from the LOCAL
HistoryReader** (identical to backtest), never live REST at handler time — so indicators are
byte-identical across backtest and live (§7, §10). On restart, ``reconcile()`` replays the event log
and diffs venue truth.

Canonical deployment (service/port table): built from ``deploy/docker/trader.Dockerfile``, runs the
Python ``coinext_live`` runtime, exposes Prometheus metrics on **:9103**. Config via ``COINEXT__*`` env (see
``.env.example``); the account is selected by ``COINEXT__TRADER__ACCOUNT_ID``.

Design note: ``coinext_live`` (which pulls in the compiled ``coinext_py`` extension) is imported **lazily and
guarded**, so this module imports for inspection/tests without the Rust build present.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

logger = logging.getLogger("coinext.trader")


@dataclass(frozen=True)
class TraderConfig:
    """Per-account live-trading configuration, resolved from ``COINEXT__*`` env (see ``.env.example``)."""

    account_id: str = "default"
    env: str = "live"            # backtest | sandbox | live (this wrapper is for sandbox/live)
    symbol: str = "BTCUSDT"
    venue: str = "BINANCE"
    strategy: str = "SmaCross"   # name resolved against coinext_strategy
    redis_url: str = "redis://redis:6379/0"
    metrics_port: int = 9103

    @classmethod
    def from_env(cls) -> "TraderConfig":
        return cls(
            account_id=os.environ.get("COINEXT__TRADER__ACCOUNT_ID", cls.account_id),
            env=os.environ.get("COINEXT__ENV", cls.env),
            symbol=os.environ.get("COINEXT__TRADER__SYMBOL", cls.symbol),
            venue=os.environ.get("COINEXT__TRADER__VENUE", cls.venue),
            strategy=os.environ.get("COINEXT__TRADER__STRATEGY", cls.strategy),
            redis_url=os.environ.get("COINEXT__REDIS__URL", cls.redis_url),
            metrics_port=int(os.environ.get("COINEXT__TRADER__METRICS_PORT", str(cls.metrics_port))),
        )


def _load_live():
    """Import the ``coinext_live`` live runtime lazily.

    Raises a clear setup error if the runtime / compiled ``coinext_py`` extension is unavailable, since a
    trader process cannot do anything useful without it.
    """
    try:
        import coinext_live  # noqa: WPS433 - intentional lazy import

        return coinext_live
    except ImportError as exc:  # pragma: no cover - environment-dependent
        raise RuntimeError(
            "coinext_live runtime unavailable. Build the extension with "
            "`uvx maturin develop --manifest-path crates/coinext-py/Cargo.toml --features python` "
            "and install the coinext_live package."
        ) from exc


def _build_strategy(name: str):
    """Resolve a strategy class from ``coinext_strategy`` by name and instantiate it with defaults.

    TODO: feed strategy parameters from layered config (coinext_config) instead of constructor defaults.
    """
    import coinext_strategy  # noqa: WPS433 - lazy; only needed when actually running

    try:
        strategy_cls = getattr(coinext_strategy, name)
    except AttributeError as exc:  # pragma: no cover - config error
        raise RuntimeError(f"unknown strategy {name!r} (not found in coinext_strategy)") from exc
    return strategy_cls()


def build_node(cfg: TraderConfig):
    """Build (but do not start) the single-account ``coinext_live.TradingNode``.

    Separated from :func:`run` so tests / tooling can construct and inspect the node without entering
    its run loop. The exact ``TradingNode`` constructor surface is owned by ``coinext_live``; the call
    below reflects the expected shape and is a TODO until that API is finalized.
    """
    coinext_live = _load_live()
    strategy = _build_strategy(cfg.strategy)

    # TODO: replace with the finalized coinext_live.TradingNode builder once its config surface lands.
    # The node injects a LiveClock + Binance Data/Exec clients behind identical ports (parity, §1).
    node = coinext_live.TradingNode(  # type: ignore[attr-defined]
        account_id=cfg.account_id,
        environment=cfg.env,
        symbol=cfg.symbol,
        venue=cfg.venue,
        strategy=strategy,
        redis_url=cfg.redis_url,
    )
    return node


def run(cfg: TraderConfig | None = None) -> None:
    """Build and run the live node for ONE account until shutdown.

    Lifecycle (delegated to ``coinext_live.TradingNode``): connect data/exec clients → ``reconcile()``
    against venue truth → warm up indicators from the LOCAL HistoryReader → enter the deterministic
    core loop. The out-of-band ``risk-monitor`` and the in-core risk gate can trip the kill-switch at
    any time; this node honours it.
    """
    cfg = cfg or TraderConfig.from_env()
    _maybe_start_metrics_server(cfg.metrics_port)
    logger.info(
        "starting trader account=%s env=%s strategy=%s %s.%s",
        cfg.account_id,
        cfg.env,
        cfg.strategy,
        cfg.symbol,
        cfg.venue,
    )
    node = build_node(cfg)

    # TODO: real run/shutdown handling (signal traps, graceful stop, reconcile-on-restart) lives in
    # coinext_live.TradingNode.run(); this wrapper just drives it.
    node.run()  # type: ignore[attr-defined]


def _maybe_start_metrics_server(port: int) -> None:
    """Start the Prometheus metrics endpoint on :9103 if ``prometheus_client`` is installed.

    Exposes the node SLO histograms (``strategy_dispatch_ns``, ``submit_to_ack_ns``, ``ws_reconnects``,
    ``risk_denials`` — ARCHITECTURE.md §8) once wired through coinext_live.
    """
    try:  # pragma: no cover - optional dependency
        from prometheus_client import start_http_server  # noqa: WPS433

        start_http_server(port)
        logger.info("trader metrics on :%d", port)
    except ImportError:
        logger.info("prometheus_client not installed; metrics endpoint disabled")


def main() -> None:
    """Console entrypoint (also the Docker CMD target). One process == one account."""
    logging.basicConfig(
        level=os.environ.get("COINEXT__LOG__LEVEL", "info").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    run(TraderConfig.from_env())


if __name__ == "__main__":
    main()
