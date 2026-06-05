"""qv_cli.main — the ``qv`` CLI.

Subcommands map onto the control-plane packages:

* ``backtest``  → run the AUTHORITATIVE ``qv_backtest`` runner with ``qv_strategy.SmaCross`` and
  print ``qv_analytics.tear_sheet`` (the canonical end-to-end demo).
* ``parity``    → run the pre-live promotion gate (``qv_parity.run_gate``): backtest SmaCross vs a
  slightly-perturbed sandbox session and print ``render_verdict`` (the demo acceptance gate).
* ``optimize``  → Optuna walk-forward search (``qv_optimize``).
* ``download``  → fetch venue history into the data lake (``qv_data``).
* ``live``      → start the live/sandbox ``TradingNode`` (``qv_live``).
* ``reconcile`` → reconcile-on-restart against venue truth (``qv_live.reconcile``).
* ``catalog``   → inspect the data lake (``qv_data.DataCatalog``).

Typer is used when installed (rich help + the ``qv`` console script ``qv_cli.main:app``). Without
it, an ``argparse`` driver provides the same subcommands so ``python -m qv_cli.main`` always runs.
The heavy work in each subcommand is imported LOCALLY so ``import qv_cli.main`` stays light and the
backtest path needs only ``qv_py`` + the pure-Python packages.
"""

from __future__ import annotations

import sys
from typing import Any


# --------------------------------------------------------------------------------------------------
# Shared command implementations (provider-agnostic: called by both the Typer and argparse fronts).
# --------------------------------------------------------------------------------------------------
def _cmd_backtest(
    symbol: str = "BTCUSDT",
    fast: int = 10,
    slow: int = 30,
    n: int = 400,
    real: bool = False,
    from_lake: bool = False,
    interval: str = "1m",
) -> int:
    """Run SmaCross through the Rust kernel and print the tear sheet. Returns an exit code.

    ``--from-lake`` reads the LOCAL Parquet lake (reproducible; run ``qv download`` first);
    ``--real`` fetches a fresh 1000-bar window from Binance; otherwise uses synthetic bars.
    """
    import qv_analytics
    import qv_backtest
    from qv_strategy import SmaCross

    if from_lake:
        from qv_data import _HAVE_LAKE, DataLake

        if not _HAVE_LAKE:
            print("pyarrow not installed — `--from-lake` needs the lake (`uv pip install pyarrow`)")
            return 1
        bars = DataLake().read_closes("BINANCE", symbol, interval)
        if not bars:
            print(
                f"lake empty for {symbol} {interval} — run `qv download --symbols {symbol}` first"
            )
            return 1
        print(f"[lake] loaded {len(bars)} {symbol} {interval} bars from {DataLake().root}/bars")
    elif real:
        from qv_data import fetch_binance_klines

        bars = fetch_binance_klines(symbol, interval, min(n, 1000))
        print(f"[real] fetched {len(bars)} live {symbol} {interval} bars")
    else:
        bars = qv_backtest.synthetic_bars(n=n)
    strategy = SmaCross(fast=fast, slow=slow)
    result = qv_backtest.run(strategy, symbol=symbol, bars=bars)
    print(qv_analytics.tear_sheet(result))
    return 0


def _cmd_parity(symbol: str = "BTCUSDT", fast: int = 10, slow: int = 30, n: int = 400) -> int:
    """Run the pre-live promotion gate demo and print the verdict. Returns an exit code.

    Builds a near-identical sandbox session from the SAME backtest (fills nudged +1.5 bps + a tiny
    equity wobble — what a clean testnet recording looks like), then runs ``run_gate``. Exit code is
    0 when the gate PASSES (promote-eligible), 1 when it FAILS (blocked from live).
    """
    import qv_backtest
    from qv_parity import SessionResult, render_verdict, run_gate
    from qv_strategy import SmaCross

    bars = qv_backtest.synthetic_bars(n=n)

    # Record a "sandbox" session by running the backtest once and perturbing it slightly.
    base = SessionResult.from_backtest(
        qv_backtest.run(SmaCross(fast=fast, slow=slow), symbol=symbol, bars=bars)
    )
    sandbox = SessionResult(
        equity_curve=[
            (ts, eq * (1.0 + 1e-5 * (1 if i % 2 == 0 else -1)))
            for i, (ts, eq) in enumerate(base.equity_curve)
        ],
        fills=[(ts, side, qty, px * (1.0 + 1.5 / 1e4)) for (ts, side, qty, px) in base.fills],
    )

    verdict = run_gate(lambda: SmaCross(fast=fast, slow=slow), bars, sandbox, symbol=symbol)
    print(render_verdict(verdict))
    return 0 if verdict.passed else 1


def _cmd_testnet_gate(
    symbol: str = "BTCUSDT",
    fast: int = 10,
    slow: int = 30,
    n: int = 120,
    qty: float = 0.001,
    no_testnet: bool = False,
) -> int:
    """The ONE-COMMAND closed loop: real klines → backtest → REAL testnet fills → parity gate.

    1. Fetch REAL Binance klines (public REST, no key).
    2. Run SmaCross through the Rust kernel (the authoritative backtest) to get the signal fills.
    3. Place those same orders as MARKET orders on Binance SPOT TESTNET via the Rust
       ``testnet_orders`` example (needs VQ__BINANCE__API_KEY/SECRET); capture the REAL fills.
    4. Rebuild the SANDBOX session (backtest signal timestamps + real testnet fill prices) and the
       backtest session with the IDENTICAL reconstruction, then run the parity gate.

    ``--no-testnet`` synthesizes the sandbox (backtest prices nudged a few bps) so the whole loop is
    runnable without keys to validate the orchestration. With keys, it executes on real testnet.
    """
    import json
    import os
    import subprocess
    import tempfile
    from pathlib import Path

    import qv_backtest
    import qv_parity
    from qv_data import fetch_binance_klines
    from qv_strategy import SmaCross

    root = Path(__file__).resolve().parents[2]
    bars = fetch_binance_klines(symbol, "1m", n)
    print(f"[1/4] fetched {len(bars)} real {symbol} 1m bars")

    bt = qv_backtest.run(SmaCross(fast=fast, slow=slow, qty=qty), symbol=symbol, bars=bars)
    bt_fills = [(int(ts), int(s), float(q), float(px)) for ts, s, q, px in bt.fills_log]
    print(f"[2/4] backtest produced {len(bt_fills)} fill(s)")
    if not bt_fills:
        print("no trades generated — widen --n or adjust --fast/--slow")
        return 1

    if no_testnet:
        sandbox_fills = [(ts, s, q, px * (1.0 + 1.5 / 1e4)) for (ts, s, q, px) in bt_fills]
        print("[3/4] --no-testnet: synthesized sandbox fills (+1.5 bps)")
    else:
        if not (
            os.environ.get("VQ__BINANCE__API_KEY") and os.environ.get("VQ__BINANCE__API_SECRET")
        ):
            print(
                "[3/4] missing VQ__BINANCE__API_KEY/SECRET — get spot testnet keys at "
                "https://testnet.binance.vision/ (GitHub login), or pass --no-testnet to dry-run."
            )
            return 2
        with tempfile.TemporaryDirectory() as td:
            orders_in = os.path.join(td, "orders.json")
            fills_out = os.path.join(td, "fills.json")
            orders = [{"side": "buy" if s > 0 else "sell", "qty": q} for (_, s, q, _) in bt_fills]
            Path(orders_in).write_text(json.dumps(orders))
            env = {
                **os.environ,
                "VQ__ORDER__SYMBOL": f"{symbol}.BINANCE",
                "VQ__ORDERS_IN": orders_in,
                "VQ__FILLS_OUT": fills_out,
            }
            print(f"[3/4] placing {len(orders)} market order(s) on testnet via Rust executor…")
            proc = subprocess.run(
                [
                    "cargo",
                    "run",
                    "--quiet",
                    "--manifest-path",
                    str(root / "crates/qv-adapters/binance/Cargo.toml"),
                    "--example",
                    "testnet_orders",
                ],
                env=env,
                cwd=str(root),
                check=False,
            )
            if proc.returncode != 0 or not os.path.exists(fills_out):
                print(f"testnet executor failed (exit {proc.returncode})")
                return 1
            recs = json.loads(Path(fills_out).read_text())
        sandbox_fills = []
        for (ts, s, q, _), rec in zip(bt_fills, recs, strict=False):
            if isinstance(rec, dict) and "px" in rec:
                sandbox_fills.append((ts, s, q, float(rec["px"])))
            else:
                print(f"  warn: order at ts={ts} had no fill ({rec}); skipping")
        if not sandbox_fills:
            print("no testnet fills captured")
            return 1

    start = bt.starting_equity
    bt_session = qv_parity.SessionResult.from_fills_and_bars(bt_fills, bars, start)
    sb_session = qv_parity.SessionResult.from_fills_and_bars(sandbox_fills, bars, start)
    metrics = qv_parity.parity_metrics(bt_session, sb_session)
    verdict = qv_parity.evaluate(metrics, qv_parity.AcceptanceCriterion())
    print("[4/4] parity gate:")
    print(qv_parity.render_verdict(verdict))
    return 0 if verdict.passed else 1


def _cmd_optimize(symbol: str = "BTCUSDT", trials: int = 50, splits: int = 4) -> int:
    """Walk-forward optimize SmaCross params (requires the ``research`` extra for optuna)."""
    import qv_backtest
    from qv_analytics import compute_metrics
    from qv_optimize import OptimizeNode
    from qv_strategy import SmaCross

    bars = qv_backtest.synthetic_bars()

    def search_space(trial: Any) -> dict[str, int]:
        return {
            "fast": trial.suggest_int("fast", 5, 20),
            "slow": trial.suggest_int("slow", 25, 60),
        }

    def objective(params: dict[str, Any], window: list[tuple[int, float]]) -> float:
        if params["fast"] >= params["slow"] or len(window) < 2:
            return float("-inf")
        result = qv_backtest.run(SmaCross(**params), symbol=symbol, bars=window)
        return compute_metrics(list(result.equity_curve)).sharpe

    node = OptimizeNode(bars=bars, search_space=search_space, objective=objective, n_splits=splits)
    res = node.run(n_trials=trials)
    print(f"best params: {res.best_params}  best sharpe (cv): {res.best_value:.3f}")
    return 0


def _cmd_download(
    symbols: str = "BTCUSDT", interval: str = "1m", days: float = 7.0, venue: str = "BINANCE"
) -> int:
    """Download REAL venue history (public Binance REST, no key) into the local Parquet lake.

    Pages past the 1000-bar request limit to pull ``--days`` of history for each ``--symbols``,
    writing partitioned Parquet (deduped/idempotent). Then prints per-symbol coverage.
    """
    from qv_data import _HAVE_LAKE, DataLake, download_to_lake

    if not _HAVE_LAKE:
        print("pyarrow not installed — the data lake needs pyarrow (`uv pip install pyarrow`)")
        return 1
    lake = DataLake()
    syms = [s.strip() for s in symbols.split(",") if s.strip()]
    print(f"downloading {days}d of {interval} for {syms} -> {lake.root}/bars ...")
    counts = download_to_lake(lake, syms, interval=interval, days=days, venue=venue)
    for sym, n in counts.items():
        cov = lake.coverage(venue, sym, interval)
        a, b = cov.span_utc()
        print(f"  {sym} {interval}: {n} rows  [{a} .. {b}]")
    return 0


def _cmd_live(env: str = "sandbox", symbol: str = "BTCUSDT") -> int:
    """Start the live/sandbox TradingNode. STUB: builds the node and reports intent."""
    from qv_kernel import Environment
    from qv_live import TradingNode, TradingNodeConfig
    from qv_strategy import SmaCross

    cfg = TradingNodeConfig(env=Environment(env), symbol=symbol)
    node = TradingNode(config=cfg, strategy=SmaCross())
    print(f"[stub] TradingNode ready: env={cfg.env.value} symbol={cfg.symbol}; run() is a stub")
    # TODO: anyio.run(node.run) once the native live loop is wired.
    _ = node
    return 0


def _cmd_reconcile(symbol: str = "BTCUSDT") -> int:
    """Reconcile-on-restart against venue truth. STUB: prints the (empty) diff."""
    from qv_kernel import Environment
    from qv_live import TradingNode, TradingNodeConfig
    from qv_strategy import SmaCross

    node = TradingNode(
        config=TradingNodeConfig(env=Environment.LIVE, symbol=symbol),
        strategy=SmaCross(),
    )
    print(f"[stub] reconcile report: {node.reconcile()}")
    return 0


def _cmd_catalog(venue: str = "BINANCE") -> int:
    """Report coverage (rows + UTC span) for every series in the local Parquet lake."""
    from qv_data import _HAVE_LAKE, DataLake

    if not _HAVE_LAKE:
        print("pyarrow not installed — the catalog needs the lake (`uv pip install pyarrow`)")
        return 1
    lake = DataLake()
    series = [s for s in lake.list_series() if s[0] == venue]
    if not series:
        print(f"{venue} ({lake.root}/bars): no series found (lake empty or missing)")
        return 0
    print(f"{venue} ({lake.root}/bars):")
    for v, s, i in series:
        cov = lake.coverage(v, s, i)
        a, b = cov.span_utc()
        print(f"  {s} {i}: {cov.n_rows} rows  [{a} .. {b}]")
    return 0


# --------------------------------------------------------------------------------------------------
# Typer front-end (preferred). Falls back to argparse if Typer is absent.
# --------------------------------------------------------------------------------------------------
def _build_typer_app():
    import typer  # type: ignore

    app = typer.Typer(
        add_completion=False,
        help="VeloxQuant control-plane CLI. ONE strategy/engine across backtest/sandbox/live.",
    )

    @app.command()
    def backtest(
        symbol: str = "BTCUSDT",
        fast: int = 10,
        slow: int = 30,
        n: int = 400,
        real: bool = False,
        from_lake: bool = False,
        interval: str = "1m",
    ) -> None:
        """Run SmaCross through the Rust kernel and print the tear sheet.

        --from-lake reads the local Parquet lake; --real fetches a fresh window; else synthetic.
        """
        raise typer.Exit(_cmd_backtest(symbol, fast, slow, n, real, from_lake, interval))

    @app.command()
    def parity(symbol: str = "BTCUSDT", fast: int = 10, slow: int = 30, n: int = 400) -> None:
        """Run the pre-live promotion gate (backtest vs sandbox) and print the verdict."""
        raise typer.Exit(_cmd_parity(symbol, fast, slow, n))

    @app.command("testnet-gate")
    def testnet_gate(
        symbol: str = "BTCUSDT",
        fast: int = 10,
        slow: int = 30,
        n: int = 120,
        qty: float = 0.001,
        no_testnet: bool = False,
    ) -> None:
        """One-command closed loop: real klines → backtest → REAL testnet fills → parity gate."""
        raise typer.Exit(_cmd_testnet_gate(symbol, fast, slow, n, qty, no_testnet))

    @app.command()
    def optimize(symbol: str = "BTCUSDT", trials: int = 50, splits: int = 4) -> None:
        """Walk-forward optimize strategy parameters (Optuna)."""
        raise typer.Exit(_cmd_optimize(symbol, trials, splits))

    @app.command()
    def download(
        symbols: str = "BTCUSDT", interval: str = "1m", days: float = 7.0, venue: str = "BINANCE"
    ) -> None:
        """Download REAL venue history into the local Parquet lake (paginated, no key)."""
        raise typer.Exit(_cmd_download(symbols, interval, days, venue))

    @app.command()
    def live(env: str = "sandbox", symbol: str = "BTCUSDT") -> None:
        """Start the live/sandbox TradingNode."""
        raise typer.Exit(_cmd_live(env, symbol))

    @app.command()
    def reconcile(symbol: str = "BTCUSDT") -> None:
        """Reconcile local state against venue truth."""
        raise typer.Exit(_cmd_reconcile(symbol))

    @app.command()
    def catalog(venue: str = "BINANCE") -> None:
        """Inspect the data lake."""
        raise typer.Exit(_cmd_catalog(venue))

    return app


def _build_argparse_parser():
    import argparse

    parser = argparse.ArgumentParser(
        prog="qv", description="VeloxQuant control-plane CLI (argparse fallback; install 'typer')."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("backtest", help="Run SmaCross and print the tear sheet.")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--fast", type=int, default=10)
    p.add_argument("--slow", type=int, default=30)
    p.add_argument("--n", type=int, default=400)
    p.add_argument("--real", action="store_true", help="Use REAL Binance klines (no key).")
    p.add_argument("--from-lake", action="store_true", help="Read the local Parquet lake.")
    p.add_argument("--interval", default="1m")

    p = sub.add_parser("parity", help="Run the pre-live promotion gate (backtest vs sandbox).")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--fast", type=int, default=10)
    p.add_argument("--slow", type=int, default=30)
    p.add_argument("--n", type=int, default=400)

    p = sub.add_parser(
        "testnet-gate", help="One-command loop: real data → backtest → testnet → gate."
    )
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--fast", type=int, default=10)
    p.add_argument("--slow", type=int, default=30)
    p.add_argument("--n", type=int, default=120)
    p.add_argument("--qty", type=float, default=0.001)
    p.add_argument(
        "--no-testnet", action="store_true", help="Dry-run: synthesize the sandbox (no key)."
    )

    p = sub.add_parser("optimize", help="Walk-forward optimize (Optuna).")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--trials", type=int, default=50)
    p.add_argument("--splits", type=int, default=4)

    p = sub.add_parser("download", help="Download REAL history into the local Parquet lake.")
    p.add_argument("--symbols", default="BTCUSDT", help="comma-separated, e.g. BTCUSDT,ETHUSDT")
    p.add_argument("--interval", default="1m")
    p.add_argument("--days", type=float, default=7.0)
    p.add_argument("--venue", default="BINANCE")

    p = sub.add_parser("live", help="Start the live/sandbox TradingNode.")
    p.add_argument("--env", default="sandbox")
    p.add_argument("--symbol", default="BTCUSDT")

    p = sub.add_parser("reconcile", help="Reconcile local state against venue truth.")
    p.add_argument("--symbol", default="BTCUSDT")

    p = sub.add_parser("catalog", help="Inspect the data lake.")
    p.add_argument("--venue", default="BINANCE")

    return parser


def _run_argparse(argv: list[str] | None) -> int:
    parser = _build_argparse_parser()
    ns = parser.parse_args(argv)
    dispatch = {
        "backtest": lambda: _cmd_backtest(
            ns.symbol, ns.fast, ns.slow, ns.n, ns.real, ns.from_lake, ns.interval
        ),
        "parity": lambda: _cmd_parity(ns.symbol, ns.fast, ns.slow, ns.n),
        "testnet-gate": lambda: _cmd_testnet_gate(
            ns.symbol, ns.fast, ns.slow, ns.n, ns.qty, ns.no_testnet
        ),
        "optimize": lambda: _cmd_optimize(ns.symbol, ns.trials, ns.splits),
        "download": lambda: _cmd_download(ns.symbols, ns.interval, ns.days, ns.venue),
        "live": lambda: _cmd_live(ns.env, ns.symbol),
        "reconcile": lambda: _cmd_reconcile(ns.symbol),
        "catalog": lambda: _cmd_catalog(ns.venue),
    }
    return dispatch[ns.command]()


def main(argv: list[str] | None = None) -> int:
    """Module entry point (``python -m qv_cli.main``). Prefers Typer, falls back to argparse."""
    try:
        import typer  # noqa: F401
    except ImportError:
        return _run_argparse(argv)
    # Typer drives sys.argv itself; route module-style invocation through it too.
    app = _build_typer_app()
    if argv is not None:
        import typer

        return typer.main.get_command(app).main(args=argv, standalone_mode=False) or 0
    app()
    return 0


# The ``qv`` console script targets ``qv_cli.main:app``. Expose a Typer ``app`` when present, else a
# tiny callable that runs the argparse fallback so the entry point never dangles.
try:  # pragma: no cover - import guard
    import typer  # noqa: F401

    app = _build_typer_app()
except ImportError:  # pragma: no cover - fallback path

    def app() -> None:  # type: ignore[misc]
        """Argparse fallback exposed under the ``app`` name (no Typer installed)."""
        raise SystemExit(main(sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
