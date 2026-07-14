"""coinext_cli.main — the ``coinext`` CLI.

Subcommands map onto the control-plane packages:

* ``backtest``       → run the AUTHORITATIVE ``coinext_backtest`` runner with ``coinext_strategy.SmaCross``
  and print ``coinext_analytics.tear_sheet`` (the canonical end-to-end demo).
* ``backtest-multi`` → run a per-symbol SMA portfolio (``coinext_strategy.MultiSma``) across many
  instruments through one kernel (``coinext_backtest.run_multi``) and print the aggregate tear sheet.
* ``parity``         → run the pre-live promotion gate (``coinext_parity.run_gate``): backtest SmaCross vs a
  slightly-perturbed sandbox session and print ``render_verdict`` (the demo acceptance gate).
* ``testnet-gate``   → the one-command closed loop: real klines → backtest → REAL Binance testnet fills →
  ``coinext_parity`` gate (``--no-testnet`` dry-runs the orchestration without keys).
* ``optimize``       → Optuna walk-forward search (``coinext_optimize``).
* ``screen``         → FAST vectorized SMA-cross sweep (``coinext_screen``, non-authoritative) cross-checked
  against the event-driven runner.
* ``download``       → fetch venue history into the data lake (``coinext_data``); crypto via
  Binance, equity/index via Yahoo Finance using the venue catalog.
* ``download-fx``    → Yahoo FX pairs into ``venue=FX`` (USDCNY/USDHKD for multi-ccy).
* ``paper-equity``   → replay lake bars through ``PaperEquityBroker`` (A-share T+1 / 涨跌停).
* ``ib-status``      → probe IB TWS/Gateway connectivity (optional ``ib_insync``).
* ``venues``         → list registered global venues (crypto + mainstream stock markets).
* ``live``           → start the live/sandbox ``TradingNode`` (``coinext_live``).
* ``reconcile``      → reconcile-on-restart against venue truth (``coinext_live.reconcile``).
* ``catalog``        → inspect the data lake (``coinext_data.DataCatalog``).

Typer is used when installed (rich help + the ``coinext`` console script ``coinext_cli.main:app``). Without
it, an ``argparse`` driver provides the same subcommands so ``python -m coinext_cli.main`` always runs.
The heavy work in each subcommand is imported LOCALLY so ``import coinext_cli.main`` stays light and the
backtest path needs only ``coinext_py`` + the pure-Python packages.
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
    strategy: str = "sma",
    venue: str = "BINANCE",
) -> int:
    """Run a strategy through the Rust kernel and print the tear sheet. Returns an exit code.

    ``--strategy sma`` (default) trades market orders on SMA crossovers; ``--strategy limit-maker``
    rests LIMIT orders that fill on intrabar high/low — the OHLC-aware path (synthetic data uses an
    OHLC series with wicks; the lake serves real OHLC). ``--from-lake`` reads the LOCAL Parquet lake
    (reproducible; run ``coinext download`` first); ``--real`` fetches a fresh window; else synthetic.
    Equity venues (``--venue NYSE`` …) default the instrument to cash equity when using lake/real.
    """
    import coinext_analytics
    import coinext_backtest
    from coinext_strategy import LimitMaker, SmaCross

    if strategy not in ("sma", "limit-maker"):
        print(f"unknown --strategy {strategy!r} (expected 'sma' or 'limit-maker')")
        return 1

    venue_raw = venue.strip() or "BINANCE"
    from coinext_data import (
        instrument_spec,
        is_equity_venue,
        resolve_listing,
        resolve_market_group,
    )

    # Equity CLI defaults: 1m/crypto-shaped interval → 1d when reading lake/real.
    if is_equity_venue(venue_raw) and (from_lake or real) and interval == "1m":
        interval = "1d"
        print("note: equity venue — using interval=1d (pass --interval explicitly to override)")

    # Market groups (A股/美股/港股) and aliases resolve to a concrete lake venue + symbol.
    try:
        if resolve_market_group(venue_raw) is not None or is_equity_venue(venue_raw):
            venue_code, lake_sym = resolve_listing(venue_raw, symbol)
        else:
            venue_code, lake_sym = venue_raw.upper(), symbol
    except (ValueError, KeyError):
        venue_code, lake_sym = venue_raw.upper(), symbol

    spec = instrument_spec(venue_code, lake_sym)
    instrument = coinext_backtest.Instrument.equity() if spec.asset_class == "equity" else None
    if is_equity_venue(venue_code):
        print(
            f"[instrument] {venue_code}/{lake_sym} kind={spec.kind} ccy={spec.currency} "
            f"lot={spec.lot_size} fees={spec.maker_fee:.4f}/{spec.taker_fee:.4f} "
            f"px_prec={spec.price_precision} sz_prec={spec.size_precision}"
        )

    if from_lake:
        from coinext_data import _HAVE_LAKE, DataLake

        if not _HAVE_LAKE:
            print("pyarrow not installed — `--from-lake` needs the lake (`uv pip install pyarrow`)")
            return 1
        # OHLCV so resting limits fill on the real intrabar high/low and against real volume.
        bars = DataLake().read_ohlcv(venue_code, lake_sym, interval)
        if not bars:
            print(
                f"lake empty for {venue_code}/{lake_sym} {interval} — run "
                f"`coinext download --venue {venue_code} --symbols {lake_sym}` first"
            )
            return 1
        print(
            f"[lake] loaded {len(bars)} {venue_code}/{lake_sym} {interval} OHLC bars from the lake"
        )
    elif real:
        from coinext_data import fetch_binance_klines

        if is_equity_venue(venue_code):
            from coinext_data import download_equity_bars

            bars = download_equity_bars(lake_sym, interval, venue=venue_code, days=max(n, 30))
            # download_equity_bars returns full OHLCV; runner accepts that shape.
            print(f"[real] fetched {len(bars)} {venue_code}/{lake_sym} {interval} equity bars")
        else:
            bars = fetch_binance_klines(lake_sym, interval, min(n, 1000))
            print(f"[real] fetched {len(bars)} live {lake_sym} {interval} bars")
    elif strategy == "limit-maker":
        bars = coinext_backtest.synthetic_ohlc_bars(
            n=n
        )  # wicks for the resting limits to fill against
    else:
        bars = coinext_backtest.synthetic_bars(n=n)
    strat = LimitMaker() if strategy == "limit-maker" else SmaCross(fast=fast, slow=slow)
    result = coinext_backtest.run(
        strat,
        symbol=lake_sym,
        venue=venue_code,
        bars=bars,
        instrument=instrument,
        price_precision=spec.price_precision,
        size_precision=spec.size_precision,
        maker_fee=spec.maker_fee,
        taker_fee=spec.taker_fee,
    )
    print(coinext_analytics.tear_sheet(result, bars=bars))
    return 0


def _cmd_backtest_multi(
    symbols: str = "BTCUSDT,ETHUSDT",
    fast: int = 10,
    slow: int = 30,
    n: int = 400,
    from_lake: bool = False,
    interval: str = "1m",
    venue: str = "BINANCE",
    base_ccy: str | None = None,
) -> int:
    """Run a per-symbol SMA portfolio (``MultiSma``) across MANY instruments through one kernel.

    ``--from-lake`` reads each symbol's real OHLC from the lake; otherwise each gets a distinct
    synthetic series (varied period/base) so the symbols are not identical. ``--symbols @default``
    expands the venue's liquid universe. ``--base-ccy USD|CNY|HKD`` converts multi-currency equity
    prices into one settlement currency via :class:`coinext_data.FxBook` before the run.
    """
    import coinext_analytics
    import coinext_backtest
    from coinext_data import (
        filter_trading_bars,
        instrument_spec,
        is_equity_venue,
        resolve_listings,
        revalue_bar_map,
        venue_currency,
    )
    from coinext_strategy import MultiSma

    venue_raw = venue.strip() or "BINANCE"
    if is_equity_venue(venue_raw) and from_lake and interval == "1m":
        interval = "1d"
        print("note: equity venue — using interval=1d (pass --interval explicitly to override)")

    try:
        listings = resolve_listings(venue_raw, symbols)
    except (ValueError, KeyError) as exc:
        print(f"symbols: {exc}")
        return 1

    # Multi-venue groups need one primary venue label for the kernel; symbol keys stay unique
    # across venues via "VENUE:SYM" when more than one concrete venue is present.
    venues_used = {v for v, _ in listings}
    multi_venue = len(venues_used) > 1
    primary_venue = next(iter(venues_used))

    # Per-symbol research instrument specs (fees / whole-share precision).
    inst_overrides: dict[str, dict] = {}
    for vcode, sym in listings:
        key = f"{vcode}:{sym}" if multi_venue else sym
        sp = instrument_spec(vcode, sym)
        inst_overrides[key] = {
            "price_precision": sp.price_precision,
            "size_precision": sp.size_precision,
            "maker_fee": sp.maker_fee,
            "taker_fee": sp.taker_fee,
        }

    bars: dict[str, list] = {}
    symbol_venues: dict[str, str] = {}
    if from_lake:
        from coinext_data import _HAVE_LAKE, DataLake

        if not _HAVE_LAKE:
            print("pyarrow not installed — `--from-lake` needs the lake (`uv pip install pyarrow`)")
            return 1
        lake = DataLake()
        for vcode, sym in listings:
            rows = lake.read_ohlcv(vcode, sym, interval)
            if not rows:
                print(
                    f"lake empty for {vcode}/{sym} {interval} — run "
                    f"`coinext download --venue {vcode} --symbols {sym}` first"
                )
                return 1
            # Calendar hygiene on daily equity series (idempotent if already filtered at download).
            if is_equity_venue(vcode) and interval in ("1d", "5d", "1wk"):
                rows, _st = filter_trading_bars(rows, vcode)
            key = f"{vcode}:{sym}" if multi_venue else sym
            bars[key] = rows
            symbol_venues[key] = vcode
        print(
            f"[lake] loaded {len(listings)} symbols across {sorted(venues_used)} "
            f"of {interval} OHLC from the lake"
        )
    else:
        # Give each symbol a distinct synthetic regime so the portfolio is not N copies of one.
        for i, (vcode, sym) in enumerate(listings):
            key = f"{vcode}:{sym}" if multi_venue else sym
            bars[key] = coinext_backtest.synthetic_bars(
                n=n, base=50_000.0 * (1.0 + 0.2 * i), period=40 + 7 * i
            )
            symbol_venues[key] = vcode

    # Multi-currency revaluation into a single kernel settlement currency.
    ccy_set = {venue_currency(v) for v in venues_used}
    if base_ccy:
        base = base_ccy.strip().upper()
    elif is_equity_venue(venue_raw) and len(ccy_set) > 1:
        base = "USD"
        print(f"note: multi-currency portfolio {sorted(ccy_set)} → auto --base-ccy USD")
    else:
        base = None

    if base is not None and is_equity_venue(venue_raw):
        from coinext_data import load_fx_book

        book = load_fx_book(prefer_lake=True, yahoo_if_empty=False)
        n_lake = sum(1 for _ in getattr(book, "curves", {}) or [])
        bars = revalue_bar_map(bars, symbol_venues=symbol_venues, book=book, base=base)
        print(
            f"[fx] revalued prices into {base} "
            f"(FxBook curves={n_lake}; prefer lake venue=FX then static fallbacks)"
        )

    # Shared fee defaults from primary venue; per-symbol overrides above.
    primary_spec = instrument_spec(primary_venue)
    result = coinext_backtest.run_multi(
        MultiSma(fast=fast, slow=slow),
        bars=bars,
        venue=primary_venue,
        instruments=inst_overrides,
        price_precision=primary_spec.price_precision,
        size_precision=primary_spec.size_precision,
        maker_fee=primary_spec.maker_fee,
        taker_fee=primary_spec.taker_fee,
    )
    labels = [f"{v}/{s}" for v, s in listings]
    ccy_note = f" base={base}" if base else ""
    print(f"[multi] {venue_raw} × {len(listings)} instruments{ccy_note}: {', '.join(labels)}")
    print(coinext_analytics.tear_sheet(result))
    return 0


def _cmd_parity(symbol: str = "BTCUSDT", fast: int = 10, slow: int = 30, n: int = 400) -> int:
    """Run the pre-live promotion gate demo and print the verdict. Returns an exit code.

    Builds a near-identical sandbox session from the SAME backtest (fills nudged +1.5 bps + a tiny
    equity wobble — what a clean testnet recording looks like), then runs ``run_gate``. Exit code is
    0 when the gate PASSES (promote-eligible), 1 when it FAILS (blocked from live).
    """
    import coinext_backtest
    from coinext_parity import SessionResult, render_verdict, run_gate
    from coinext_strategy import SmaCross

    bars = coinext_backtest.synthetic_bars(n=n)

    # Record a "sandbox" session by running the backtest once and perturbing it slightly.
    base = SessionResult.from_backtest(
        coinext_backtest.run(SmaCross(fast=fast, slow=slow), symbol=symbol, bars=bars)
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
    recorded_session: str | None = None,
    record_out: str | None = None,
) -> int:
    """Closed loop: real klines → backtest → sandbox/testnet fills → parity gate.

    Default mode fetches real public klines, runs the Rust-kernel backtest, then submits the same
    signals to Binance spot testnet via the Rust adapter example. ``--recorded-session`` replays a
    previously captured sandbox/testnet fixture offline, preserving the exact bars and fills that
    should feed the parity gate. ``--record-out`` writes the session in the same replayable schema.

    ``--no-testnet`` still synthesizes fills for orchestration smoke tests only; it is never a live
    promotion artifact.
    """
    import json
    import os
    import subprocess
    import tempfile
    from pathlib import Path

    import coinext_backtest
    import coinext_parity
    from coinext_data import fetch_binance_klines
    from coinext_strategy import SmaCross

    # operations-interface/python/coinext_cli/main.py → repo root is parents[3]
    root = Path(__file__).resolve().parents[3]
    interval = "1m"
    recording = None
    if recorded_session:
        if no_testnet:
            print("--recorded-session already supplies sandbox fills; omit --no-testnet")
            return 2
        recording = coinext_parity.load_sandbox_recording(recorded_session)
        symbol = recording.symbol
        interval = recording.interval
        if recording.strategy:
            fast = int(recording.strategy.get("fast", fast))
            slow = int(recording.strategy.get("slow", slow))
            qty = float(recording.strategy.get("qty", qty))
        bars = recording.bars
        print(
            f"[1/4] loaded recorded {recording.environment} session "
            f"({len(bars)} bars, {len(recording.fills)} fill(s))"
        )
    else:
        bars = fetch_binance_klines(symbol, interval, n)
        print(f"[1/4] fetched {len(bars)} real {symbol} {interval} bars")

    bt = coinext_backtest.run(SmaCross(fast=fast, slow=slow, qty=qty), symbol=symbol, bars=bars)
    bt_fills = [(int(ts), int(s), float(q), float(px)) for ts, _sym, s, q, px in bt.fills_log]
    print(f"[2/4] backtest produced {len(bt_fills)} fill(s)")
    if not bt_fills:
        print("no trades generated — widen --n or adjust --fast/--slow")
        return 1

    if recording is not None:
        sandbox_fills = recording.fills
        print(f"[3/4] replaying {len(sandbox_fills)} recorded sandbox fill(s)")
    elif no_testnet:
        sandbox_fills = [(ts, s, q, px * (1.0 + 1.5 / 1e4)) for (ts, s, q, px) in bt_fills]
        print("[3/4] --no-testnet: synthesized sandbox fills (+1.5 bps)")
    else:
        if not (
            os.environ.get("COINEXT__BINANCE__API_KEY")
            and os.environ.get("COINEXT__BINANCE__API_SECRET")
        ):
            print(
                "[3/4] missing COINEXT__BINANCE__API_KEY/SECRET — get spot testnet keys at "
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
                "COINEXT__ORDER__SYMBOL": f"{symbol}.BINANCE",
                "COINEXT__ORDERS_IN": orders_in,
                "COINEXT__FILLS_OUT": fills_out,
            }
            print(f"[3/4] placing {len(orders)} market order(s) on testnet via Rust executor…")
            proc = subprocess.run(
                [
                    "cargo",
                    "run",
                    "--quiet",
                    "--manifest-path",
                    str(root / "market-data/crates/coinext-adapters-binance/Cargo.toml"),
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
    bt_session = coinext_parity.SessionResult.from_fills_and_bars(bt_fills, bars, start)
    sb_session = coinext_parity.SessionResult.from_fills_and_bars(sandbox_fills, bars, start)
    metrics = coinext_parity.parity_metrics(bt_session, sb_session)
    verdict = coinext_parity.evaluate(metrics, coinext_parity.AcceptanceCriterion())
    print("[4/4] parity gate:")
    print(coinext_parity.render_verdict(verdict))
    if record_out:
        environment = (
            "recorded-replay"
            if recording is not None
            else "synthetic-no-testnet"
            if no_testnet
            else "binance-spot-testnet"
        )
        coinext_parity.dump_sandbox_recording(
            record_out,
            symbol=symbol,
            interval=interval,
            starting_balance=start,
            bars=bars,
            fills=sandbox_fills,
            environment=environment,
            strategy={"name": "SmaCross", "fast": fast, "slow": slow, "qty": qty},
        )
        print(f"recorded sandbox session written: {record_out}")
    return 0 if verdict.passed else 1


def _cmd_kill_switch(
    release: bool = False,
    reason: str = "manual operator action",
    actor: str = "cli",
    api_base: str | None = None,
    api_key: str | None = None,
) -> int:
    """Engage or release the platform kill-switch through the API control endpoint."""
    import json
    import os
    import urllib.error
    import urllib.request

    base = (
        api_base
        or os.environ.get("COINEXT__API__BASE_URL")
        or os.environ.get("COINEXT__API_BASE")
        or "http://localhost:8000"
    ).rstrip("/")
    key = api_key or os.environ.get("COINEXT__API__KEY", "")
    if not key:
        print("missing API key: set COINEXT__API__KEY or pass --api-key")
        return 2

    payload = {
        "engage": not release,
        "reason": reason,
        "actor": actor,
    }
    req = urllib.request.Request(
        f"{base}/control/killswitch",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-API-Key": key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310 - operator-supplied api
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        print(f"api rejected kill-switch request: HTTP {exc.code} {detail}")
        return 1
    except urllib.error.URLError as exc:
        print(f"api unreachable at {base}: {exc.reason}")
        return 1

    status = "ENGAGED" if body.get("engaged") else "released"
    print(
        f"kill-switch {status}: reason={body.get('reason')!r} "
        f"by={body.get('engaged_by')!r} at={body.get('ts_changed')!r}"
    )
    return 0


def _cmd_optimize(
    symbol: str = "BTCUSDT",
    trials: int = 50,
    splits: int = 4,
    mode: str = "rolling",
    optuna: bool = False,
    from_lake: bool = False,
    interval: str = "1m",
) -> int:
    """Walk-forward optimize SmaCross params with out-of-sample validation.

    Default is a pure-Python grid search (no extra deps); ``--optuna`` uses Optuna TPE over the same
    objective (needs the ``research`` extra). Either way each evaluation runs the AUTHORITATIVE Rust
    backtest, params are chosen IN-SAMPLE per fold and re-scored OUT-of-sample, and the report shows
    the OOS degradation — the overfitting guard. ``--from-lake`` optimizes over real downloaded
    history; otherwise a synthetic series.
    """
    import coinext_backtest
    from coinext_analytics import compute_metrics
    from coinext_optimize import walk_forward_optimize
    from coinext_strategy import SmaCross

    if from_lake:
        from coinext_data import _HAVE_LAKE, DataLake

        if not _HAVE_LAKE:
            print("pyarrow not installed — `--from-lake` needs the lake (`uv pip install pyarrow`)")
            return 1
        bars = DataLake().read_closes("BINANCE", symbol, interval)
        if not bars:
            print(f"lake empty for {symbol} {interval} — run `coinext download --symbols {symbol}`")
            return 1
        print(f"[lake] optimizing over {len(bars)} {symbol} {interval} bars")
    else:
        # A longer synthetic series so each walk-forward OOS window has room for the slow SMA to
        # warm up and trade (short test windows would otherwise score a degenerate flat Sharpe).
        bars = coinext_backtest.synthetic_bars(n=1200)

    def objective(params: dict[str, Any], window: list[tuple[int, float]]) -> float:
        if params["fast"] >= params["slow"] or len(window) < 2:
            return float("-inf")
        result = coinext_backtest.run(SmaCross(**params), symbol=symbol, bars=window)
        return compute_metrics(list(result.equity_curve)).sharpe

    if optuna:

        def search_space(trial: Any) -> dict[str, int]:
            return {
                "fast": trial.suggest_int("fast", 5, 20),
                "slow": trial.suggest_int("slow", 25, 60),
            }

        report = walk_forward_optimize(
            bars,
            objective,
            search_space=search_space,
            n_splits=splits,
            mode=mode,
            optimizer="optuna",
            n_trials=trials,
        )
    else:
        param_grid = {"fast": [5, 8, 11, 14, 17, 20], "slow": [25, 30, 40, 50, 60]}
        report = walk_forward_optimize(
            bars,
            objective,
            param_grid=param_grid,
            n_splits=splits,
            mode=mode,
            optimizer="grid",
        )

    print(report.render())
    return 0


def _cmd_screen(
    symbol: str = "BTCUSDT", from_lake: bool = False, interval: str = "1m", n: int = 1200
) -> int:
    """FAST vectorized SMA-cross sweep (non-authoritative), then cross-check the best vs the runner.

    The vectorized screen ranks a grid in milliseconds with numpy (no Risk/Exec/Brokerage); the
    advisory ``coinext_parity.cross_check`` then warns if the best params drift from the AUTHORITATIVE
    event-driven backtest. Use the screen to narrow a space, confirm survivors with ``coinext backtest``.
    """
    import coinext_backtest
    from coinext_screen import cross_check_vs_event, sweep_sma_cross

    if from_lake:
        from coinext_data import _HAVE_LAKE, DataLake

        if not _HAVE_LAKE:
            print("pyarrow not installed — `--from-lake` needs the lake (`uv pip install pyarrow`)")
            return 1
        bars = DataLake().read_closes("BINANCE", symbol, interval)
        if not bars:
            print(f"lake empty for {symbol} {interval} — run `coinext download --symbols {symbol}`")
            return 1
        print(f"[lake] screening over {len(bars)} {symbol} {interval} bars")
    else:
        bars = coinext_backtest.synthetic_bars(n=n)

    fasts, slows = [5, 8, 11, 14, 17, 20], [25, 30, 40, 50, 60]
    rows = sweep_sma_cross(bars, fasts, slows)
    print("======== vectorized screen (NON-authoritative, fast) ========")
    print(f"swept {len(rows)} (fast,slow) combos; top by vectorized Sharpe:")
    for r in rows[:5]:
        print(
            f"  fast={r.params['fast']:>3} slow={r.params['slow']:>3}  "
            f"sharpe={r.sharpe:>9.3f}  return={r.total_return * 100:>8.2f}%  trades={r.n_trades}"
        )
    best = rows[0].params
    print(f"cross-checking best {best} vs the AUTHORITATIVE event-driven runner ...")
    warnings = cross_check_vs_event(bars, best["fast"], best["slow"], symbol=symbol)
    if warnings:
        print("  advisory drift (the fast screen is misleading for this strategy):")
        for w in warnings:
            print(f"    ⚠ {w}")
    else:
        print("  no material drift — the screen tracks the event-driven runner here.")
    print("=============================================================")
    print(
        "Confirm survivors with: coinext backtest --fast <f> --slow <s> (the parity-valid runner)"
    )
    return 0


def _cmd_download(
    symbols: str = "BTCUSDT",
    interval: str = "1m",
    days: float = 7.0,
    venue: str = "BINANCE",
    *,
    apply_calendar: bool = True,
    adjust: bool = False,
) -> int:
    """Download REAL venue history into the local Parquet lake (no API key).

    * Crypto (``--venue BINANCE``): public Binance klines REST (paginated past 1000-bar limit).
    * Equity / index (``--venue NYSE|NASDAQ|HKEX|SSE|SZSE|…|INDEX``): Yahoo Finance chart API.
    * Market groups: ``ASHARE``/``A股``, ``US``/``美股``, ``HK``/``港股``, ``ETF`` — multi-venue
      download with auto-routing (A-share codes → SSE/SZSE).

    ``--symbols @default`` expands liquid equities; ``@etf`` expands liquid ETFs.
    Equity default interval is usually ``1d``. See ``coinext venues``.
    """
    from coinext_data import (
        _HAVE_LAKE,
        DataLake,
        download_to_lake,
        get_venue,
        is_equity_venue,
        resolve_listings,
        resolve_market_group,
        suggest_equity_download_defaults,
    )

    if not _HAVE_LAKE:
        print("pyarrow not installed — the data lake needs pyarrow (`uv pip install pyarrow`)")
        return 1
    venue_raw = venue.strip() or "BINANCE"
    group = resolve_market_group(venue_raw)
    info = get_venue(venue_raw)
    # Equity / market-group: rewrite crypto-shaped CLI defaults (1m×7d, BTCUSDT).
    if is_equity_venue(venue_raw):
        interval, days, symbols, notes = suggest_equity_download_defaults(
            venue_raw, interval=interval, days=days, symbols=symbols
        )
        for note in notes:
            print(f"note: {note}")
        if apply_calendar:
            print(
                "note: equity calendar filter on (holidays + flat-halt bars); --no-calendar-filter to disable"
            )
    try:
        listings = resolve_listings(venue_raw, symbols)
    except (ValueError, KeyError) as exc:
        print(f"symbols: {exc}")
        return 1
    lake = DataLake()
    multi = len({v for v, _ in listings}) > 1
    src = (
        "yahoo"
        if (group is not None or (info and info.data_source == "yahoo"))
        else ((info.data_source if info else "binance") or "binance")
    )
    label = ", ".join(f"{v}/{s}" for v, s in listings[:12])
    if len(listings) > 12:
        label += f", … (+{len(listings) - 12})"
    print(
        f"downloading {days}d of {interval} for [{label}] "
        f"venue={venue_raw} source={src} -> {lake.root}/bars ..."
    )
    try:
        if group is not None or multi:
            counts = download_to_lake(
                lake,
                [],
                interval=interval,
                days=days,
                venue=venue_raw,
                listings=listings,
                apply_calendar=apply_calendar,
                adjust=adjust,
            )
        else:
            vcode = listings[0][0]
            syms = [s for _, s in listings]
            counts = download_to_lake(
                lake,
                syms,
                interval=interval,
                days=days,
                venue=vcode,
                apply_calendar=apply_calendar,
                adjust=adjust,
            )
    except (ValueError, RuntimeError, KeyError) as exc:
        print(f"download failed: {exc}")
        return 1
    for key, n in counts.items():
        if "/" in key:
            vcode, sym = key.split("/", 1)
        else:
            vcode, sym = listings[0][0], key
        cov = lake.coverage(vcode, sym, interval)
        a, b = cov.span_utc()
        print(f"  {vcode}/{sym} {interval}: {n} rows  [{a} .. {b}]")
    return 0


def _cmd_download_fx(
    pairs: str = "USDCNY,USDHKD", days: float = 365.0, lake_root: str | None = None
) -> int:
    """Download Yahoo FX pairs into ``venue=FX`` for multi-currency revaluation."""
    from coinext_data import _HAVE_LAKE, DataLake, download_fx_to_lake

    if not _HAVE_LAKE:
        print("pyarrow not installed — the data lake needs pyarrow (`uv pip install pyarrow`)")
        return 1
    pair_list = [p.strip().upper().removesuffix("=X") for p in pairs.split(",") if p.strip()]
    if not pair_list:
        print("no FX pairs given")
        return 1
    lake = DataLake(lake_root)
    print(f"downloading FX {pair_list} days={days:g} -> {lake.root}/bars/venue=FX ...")
    try:
        counts = download_fx_to_lake(lake, pair_list, days=days, pause=0.15)
    except (ValueError, RuntimeError) as exc:
        print(f"download-fx failed: {exc}")
        return 1
    for pair, n in counts.items():
        cov = lake.coverage("FX", pair, "1d")
        a, b = cov.span_utc()
        print(f"  FX/{pair} 1d: {n} rows  [{a} .. {b}]")
    print("use: coinext backtest-multi --base-ccy USD --from-lake ...")
    return 0


def _cmd_paper_equity(
    symbol: str = "600519",
    venue: str = "SSE",
    interval: str = "1d",
    strategy: str = "sma",
    fast: int = 5,
    slow: int = 20,
    qty: float | None = None,
    cash: float = 1_000_000.0,
    multi: bool = False,
) -> int:
    """Replay lake bars through PaperEquityBroker (A-share T+1 / price limits).

    ``--multi`` treats ``--symbols``-style venue groups: pass ``--venue ASHARE --symbol @default``
    to expand defaults, or ``--symbol 600519,000001`` under ASHARE auto-routing.
    """
    from coinext_data import instrument_spec, resolve_listing, resolve_listings

    try:
        from coinext_broker import replay_from_lake, replay_portfolio_from_lake
    except ImportError:
        print("coinext_broker not on PYTHONPATH — ensure market-data/python is importable")
        return 1
    if strategy not in ("sma", "buyhold", "none"):
        print(f"unknown --strategy {strategy!r} (sma|buyhold|none)")
        return 1

    # Multi-listing portfolio on one paper broker.
    if multi or "," in symbol or symbol.strip().startswith("@"):
        try:
            listings = resolve_listings(venue, symbol)
        except (ValueError, KeyError) as exc:
            print(f"listings: {exc}")
            return 1
        starting: dict[str, float] = {}
        for v, s in listings:
            ccy = instrument_spec(v, s).currency
            starting.setdefault(ccy, float(cash))
        try:
            port = replay_portfolio_from_lake(
                listings,
                interval=interval,
                strategy=strategy,
                fast=fast,
                slow=slow,
                qty=qty,
                starting_cash=starting,
            )
        except FileNotFoundError as exc:
            print(exc)
            return 1
        print(port.summary())
        return 0

    try:
        vcode, lake_sym = resolve_listing(venue, symbol)
    except (ValueError, KeyError) as exc:
        print(f"listing: {exc}")
        return 1
    ccy = instrument_spec(vcode, lake_sym).currency
    try:
        result = replay_from_lake(
            vcode,
            lake_sym,
            interval=interval,
            strategy=strategy,
            fast=fast,
            slow=slow,
            qty=qty,
            starting_cash={ccy: float(cash)},
        )
    except FileNotFoundError as exc:
        print(exc)
        return 1
    print(result.summary())
    if result.orders:
        print("  last orders:")
        for o in result.orders[-5:]:
            extra = f" reason={o.reject_reason}" if o.reject_reason else ""
            print(
                f"    {o.client_order_id} {o.side} {o.qty:g} @ {o.avg_price or o.limit_price} "
                f"→ {o.status}{extra}"
            )
    return 0


def _cmd_ib_status(readonly: bool = True) -> int:
    """Probe IB TWS/Gateway: connect, list cash, optional mark for AAPL."""
    try:
        from coinext_broker import IbConfig, IbPaperBroker
    except ImportError:
        print("coinext_broker not importable")
        return 1
    cfg = IbConfig.from_env()
    if readonly:
        cfg = IbConfig(
            host=cfg.host,
            port=cfg.port,
            client_id=cfg.client_id,
            account=cfg.account,
            readonly=True,
            timeout_s=cfg.timeout_s,
            fill_wait_s=cfg.fill_wait_s,
        )
    print(
        f"IB target {cfg.host}:{cfg.port} clientId={cfg.client_id} "
        f"readonly={cfg.readonly} (see docs/IB_PAPER.md)"
    )
    br = IbPaperBroker(config=cfg, mode="ib")
    try:
        br.connect()
    except ImportError as exc:
        print(f"missing dep: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"connect failed: {exc}")
        return 1
    try:
        print(f"cash: {br.cash()}")
        mark = br.req_mark("NASDAQ", "AAPL")
        print(f"NASDAQ/AAPL mark: {mark}")
        print("ib-status: OK")
        return 0
    finally:
        br.disconnect()


def _cmd_venues(family: str | None = None) -> int:
    """List registered venues (crypto + global equity markets)."""
    from coinext_data import (
        default_universe,
        etf_universe,
        format_market_groups,
        format_venue_table,
    )

    fam = family.strip().lower() if family else None
    if fam is not None and fam not in ("crypto", "equity", "index"):
        print(f"unknown --family {family!r} (expected crypto|equity|index)")
        return 1
    print(format_venue_table(family=fam))  # type: ignore[arg-type]
    print()
    print(format_market_groups())
    print()
    print("Default universes (--symbols @default):")
    for code in (
        "BINANCE",
        "NASDAQ",
        "NYSE",
        "HKEX",
        "SSE",
        "SZSE",
        "TSE",
        "LSE",
        "INDEX",
    ):
        uni = default_universe(code)
        if uni:
            print(f"  {code}: {', '.join(uni)}")
    print()
    print("ETF universes (--symbols @etf):")
    for code in ("NYSE", "NASDAQ", "SSE", "SZSE", "HKEX"):
        uni = etf_universe(code)
        if uni:
            print(f"  {code}: {', '.join(uni)}")
    print()
    print("Examples:")
    print("  coinext download --venue BINANCE --symbols BTCUSDT,ETHUSDT --interval 1m --days 7")
    print("  # 美股")
    print("  coinext download --venue NASDAQ --symbols @default --interval 1d --days 365")
    print("  coinext download --venue US --symbols AAPL,JPM,SPY --interval 1d --days 365")
    print("  # 港股")
    print("  coinext download --venue HKEX --symbols 0700,0941 --interval 1d --days 365")
    print("  coinext download --venue 港股 --symbols @default --interval 1d --days 365")
    print("  # A股")
    print("  coinext download --venue SSE --symbols 600519 --interval 1d --days 365")
    print(
        "  coinext download --venue ASHARE --symbols 600519,000001,300750 --interval 1d --days 365"
    )
    print("  # ETF")
    print("  coinext download --venue NYSE --symbols @etf --interval 1d --days 365")
    print("  coinext download --venue SSE --symbols @etf --interval 1d --days 365")
    print("  coinext download --venue HKEX --symbols @etf --interval 1d --days 365")
    print("  coinext download --venue INDEX --symbols @default --interval 1d --days 365")
    print("  coinext backtest --venue NASDAQ --symbol AAPL --from-lake --interval 1d")
    print("  coinext backtest-multi --venue NASDAQ --symbols AAPL,MSFT --from-lake --interval 1d")
    return 0


def _cmd_ingest_trades(
    symbol: str = "BTCUSDT", interval: str = "1m", limit: int = 1000, venue: str = "BINANCE"
) -> int:
    """Ingest public aggregate trades into the local OHLCV lake.

    This is the offline-testable market-data ingestion slice: public aggTrades are normalized into
    interval OHLCV bars and written through the same DataLake that backtests and warm-up consume.
    """
    from coinext_data import _HAVE_LAKE, DataLake, ingest_agg_trades_to_lake

    if not _HAVE_LAKE:
        print("pyarrow not installed — the data lake needs pyarrow (`uv pip install pyarrow`)")
        return 1
    lake = DataLake()
    print(f"ingesting {limit} public trades for {symbol} -> {lake.root}/bars ({interval}) ...")
    counts = ingest_agg_trades_to_lake(
        symbol, interval=interval, limit=limit, venue=venue, lake=lake
    )
    print(
        f"  {symbol} {interval}: {counts['trades']} trades -> "
        f"{counts['bars']} bars; stored rows now {counts['stored_rows']}"
    )
    return 0


def _resolve_run_config(env: str, **cli_overrides: Any):
    """Resolve the layered :class:`coinext_config.RunConfig` for a CLI invocation.

    Routes CLI flags through ``coinext_config.load_config`` so precedence (CLI > env > yaml >
    defaults) lives in ONE place. ``cli_overrides`` (only non-None values) become the highest layer.
    """
    from coinext_config import load_config

    overrides = {k: v for k, v in cli_overrides.items() if v is not None}
    overrides.setdefault("env", env)
    return load_config(env, cli_overrides=overrides)


def _cmd_live(
    env: str = "sandbox",
    symbol: str = "BTCUSDT",
    dry_run: bool = True,
    paper: bool = False,
) -> int:
    """Start the live/sandbox TradingNode.

    * Default ``dry_run=True``: warm-up + reconcile only (safe without venue keys).
    * ``paper=True``: offline LiveKernel (ReplayDataClient + PaperFillExec), no keys.
    * ``--no-dry-run`` without paper: real venue loop (requires API keys + coinext_py).
    """
    import asyncio

    from coinext_kernel import Environment
    from coinext_live import TradingNode, TradingNodeConfig
    from coinext_strategy import SmaCross

    run_config = _resolve_run_config(env, symbol=symbol)
    cfg = TradingNodeConfig(
        env=Environment(env),
        symbol=run_config.symbol,
        dry_run=dry_run and not paper,
        paper=paper,
        redis_url=getattr(run_config, "redis_url", "redis://redis:6379/0"),
    )
    node = TradingNode(config=cfg, strategy=SmaCross(), run_config=run_config)
    print(
        f"TradingNode: env={cfg.env.value} symbol={cfg.symbol} "
        f"dry_run={cfg.dry_run} paper={paper} redis={cfg.redis_url}"
    )
    try:
        import anyio

        anyio.run(node.run)
    except ImportError:
        asyncio.run(node.run())
    mode = "paper" if paper else ("dry-run" if cfg.dry_run else "live")
    print(f"TradingNode finished ({mode})")
    return 0


def _cmd_capture_quotes(
    symbol: str = "BTCUSDT",
    seconds: float = 15.0,
    interval: float = 0.5,
    mode: str = "rest",
    out: str | None = None,
    testnet: bool = False,
) -> int:
    """Capture live bookTicker quotes into a replayable JSON recording (public, no key)."""
    from coinext_data.quote_capture import capture_quotes

    if out is None:
        out = f"data/sample/quotes/BINANCE/{symbol}/capture.json"
    print(f"capturing {symbol} bookTicker mode={mode} for {seconds}s -> {out}")
    try:
        result = capture_quotes(
            symbol,
            seconds=seconds,
            interval=interval,
            mode=mode,
            testnet=testnet,
            out=out,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"capture failed: {exc}")
        return 1
    print(f"captured {result['n']} quotes source={result['source']} path={result['path']}")
    return 0 if result["n"] > 0 else 1


def _cmd_reconcile(symbol: str = "BTCUSDT") -> int:
    """Reconcile-on-restart: local event log vs optional venue open-order fixture."""
    from coinext_kernel import Environment
    from coinext_live import TradingNode, TradingNodeConfig
    from coinext_strategy import SmaCross

    node = TradingNode(
        config=TradingNodeConfig(env=Environment.LIVE, symbol=symbol),
        strategy=SmaCross(),
    )
    report = node.reconcile()
    print(f"reconcile report: {report}")
    return 0 if report.get("reconciled", False) else 1


def _cmd_catalog(venue: str = "BINANCE") -> int:
    """Report coverage (rows + UTC span) for every series in the local Parquet lake.

    Pass ``--venue ALL`` (or empty ``*``) to list every venue partition present in the lake.
    """
    from coinext_data import _HAVE_LAKE, DataLake

    if not _HAVE_LAKE:
        print("pyarrow not installed — the catalog needs the lake (`uv pip install pyarrow`)")
        return 1
    lake = DataLake()
    venue_code = (venue or "BINANCE").strip().upper()
    all_series = lake.list_series()
    if venue_code in ("ALL", "*", "ANY"):
        series = all_series
        label = "ALL"
    else:
        series = [s for s in all_series if s[0] == venue_code]
        label = venue_code
    if not series:
        print(f"{label} ({lake.root}/bars): no series found (lake empty or missing)")
        return 0
    print(f"{label} ({lake.root}/bars):")
    for v, s, i in series:
        cov = lake.coverage(v, s, i)
        a, b = cov.span_utc()
        print(f"  {v}/{s} {i}: {cov.n_rows} rows  [{a} .. {b}]")
    return 0


# --------------------------------------------------------------------------------------------------
# Typer front-end (preferred). Falls back to argparse if Typer is absent.
# --------------------------------------------------------------------------------------------------
def _build_typer_app():

    import typer  # type: ignore

    app = typer.Typer(
        add_completion=False,
        help="Coinext control-plane CLI. ONE strategy/engine across backtest/sandbox/live.",
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
        strategy: str = "sma",
        venue: str = "BINANCE",
    ) -> None:
        """Run a strategy through the Rust kernel and print the tear sheet.

        --strategy sma|limit-maker; --from-lake reads the local Parquet lake; --real fetches a fresh
        window; else synthetic. limit-maker rests LIMIT orders (the OHLC-aware fill path).
        --venue selects lake partition / instrument family (BINANCE, NYSE, HKEX, …).
        """
        raise typer.Exit(
            _cmd_backtest(symbol, fast, slow, n, real, from_lake, interval, strategy, venue)
        )

    @app.command("backtest-multi")
    def backtest_multi(
        symbols: str = "BTCUSDT,ETHUSDT",
        fast: int = 10,
        slow: int = 30,
        n: int = 400,
        from_lake: bool = False,
        interval: str = "1m",
        venue: str = "BINANCE",
        base_ccy: str | None = None,
    ) -> None:
        """Run a per-symbol SMA portfolio across many instruments through one kernel.

        --symbols @default expands the venue liquid universe; --venue selects lake partition.
        --base-ccy USD|CNY|HKD revalues multi-currency equity prices into one settlement ccy.
        """
        raise typer.Exit(
            _cmd_backtest_multi(symbols, fast, slow, n, from_lake, interval, venue, base_ccy)
        )

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
        recorded_session: str | None = None,
        record_out: str | None = None,
    ) -> None:
        """Closed loop: real klines → backtest → sandbox/testnet fills → parity gate."""
        raise typer.Exit(
            _cmd_testnet_gate(symbol, fast, slow, n, qty, no_testnet, recorded_session, record_out)
        )

    @app.command()
    def optimize(
        symbol: str = "BTCUSDT",
        trials: int = 50,
        splits: int = 4,
        mode: str = "rolling",
        optuna: bool = False,
        from_lake: bool = False,
        interval: str = "1m",
    ) -> None:
        """Walk-forward optimize strategy params with OOS validation (grid by default; --optuna)."""
        raise typer.Exit(_cmd_optimize(symbol, trials, splits, mode, optuna, from_lake, interval))

    @app.command()
    def screen(
        symbol: str = "BTCUSDT", from_lake: bool = False, interval: str = "1m", n: int = 1200
    ) -> None:
        """Fast vectorized SMA-cross sweep, then cross-check the best vs the event-driven runner."""
        raise typer.Exit(_cmd_screen(symbol, from_lake, interval, n))

    @app.command()
    def download(
        symbols: str = "BTCUSDT",
        interval: str = "1m",
        days: float = 7.0,
        venue: str = "BINANCE",
        no_calendar_filter: bool = False,
        adjust: bool = False,
    ) -> None:
        """Download REAL venue history into the local Parquet lake (no key).

        Crypto: Binance klines. Equity/index: Yahoo Finance (see `coinext venues`).
        Equity daily bars drop holidays / flat-halt prints unless --no-calendar-filter.
        --adjust stores split/dividend-adjusted OHLC (前复权).
        """
        raise typer.Exit(
            _cmd_download(
                symbols,
                interval,
                days,
                venue,
                apply_calendar=not no_calendar_filter,
                adjust=adjust,
            )
        )

    @app.command()
    def venues(family: str | None = None) -> None:
        """List registered global venues (crypto + mainstream stock markets)."""
        raise typer.Exit(_cmd_venues(family))

    @app.command("download-fx")
    def download_fx(
        pairs: str = "USDCNY,USDHKD",
        days: float = 365.0,
        lake_root: str | None = None,
    ) -> None:
        """Download Yahoo FX pairs into the lake under venue=FX (for --base-ccy revaluation)."""
        raise typer.Exit(_cmd_download_fx(pairs, days, lake_root))

    @app.command("paper-equity")
    def paper_equity(
        symbol: str = "600519",
        venue: str = "SSE",
        interval: str = "1d",
        strategy: str = "sma",
        fast: int = 5,
        slow: int = 20,
        qty: float | None = None,
        cash: float = 1_000_000.0,
        multi: bool = False,
    ) -> None:
        """Replay lake bars through PaperEquityBroker (T+1 / 涨跌停 for A-shares).

        Use --multi or comma/@default symbols for a shared multi-market paper portfolio.
        """
        raise typer.Exit(
            _cmd_paper_equity(symbol, venue, interval, strategy, fast, slow, qty, cash, multi)
        )

    @app.command("ib-status")
    def ib_status(readonly: bool = True) -> None:
        """Probe IB TWS/Gateway connectivity (requires ib_insync + running TWS paper)."""
        raise typer.Exit(_cmd_ib_status(readonly))

    @app.command("ingest-trades")
    def ingest_trades(
        symbol: str = "BTCUSDT",
        interval: str = "1m",
        limit: int = 1000,
        venue: str = "BINANCE",
    ) -> None:
        """Ingest public aggregate trades into the local OHLCV lake."""
        raise typer.Exit(_cmd_ingest_trades(symbol, interval, limit, venue))

    @app.command("kill-switch")
    def kill_switch(
        release: bool = False,
        reason: str = "manual operator action",
        actor: str = "cli",
        api_base: str | None = None,
        api_key: str | None = None,
    ) -> None:
        """Engage/release the platform kill-switch through the API service."""
        raise typer.Exit(_cmd_kill_switch(release, reason, actor, api_base, api_key))

    @app.command()
    def live(
        env: str = "sandbox",
        symbol: str = "BTCUSDT",
        dry_run: bool = True,
        paper: bool = False,
    ) -> None:
        """Start the live/sandbox TradingNode (default dry-run; --paper for offline LiveKernel)."""
        raise typer.Exit(_cmd_live(env, symbol, dry_run, paper))

    @app.command()
    def reconcile(symbol: str = "BTCUSDT") -> None:
        """Reconcile local event log against venue open orders (fixture or empty)."""
        raise typer.Exit(_cmd_reconcile(symbol))

    @app.command("capture-quotes")
    def capture_quotes(
        symbol: str = "BTCUSDT",
        seconds: float = 15.0,
        interval: float = 0.5,
        mode: str = "rest",
        out: str | None = None,
        testnet: bool = False,
    ) -> None:
        """Capture live bookTicker quotes (REST poll or WS) into a JSON recording."""
        raise typer.Exit(_cmd_capture_quotes(symbol, seconds, interval, mode, out, testnet))

    @app.command()
    def catalog(venue: str = "BINANCE") -> None:
        """Inspect the data lake (pass --venue ALL for every partition)."""
        raise typer.Exit(_cmd_catalog(venue))

    return app


def _build_argparse_parser():
    import argparse

    parser = argparse.ArgumentParser(
        prog="coinext",
        description="Coinext control-plane CLI (argparse fallback; install 'typer').",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("backtest", help="Run SmaCross and print the tear sheet.")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--fast", type=int, default=10)
    p.add_argument("--slow", type=int, default=30)
    p.add_argument("--n", type=int, default=400)
    p.add_argument(
        "--real",
        action="store_true",
        help="Use REAL market data (Binance klines or Yahoo equity, no key).",
    )
    p.add_argument("--from-lake", action="store_true", help="Read the local Parquet lake.")
    p.add_argument("--interval", default="1m")
    p.add_argument("--strategy", default="sma", choices=["sma", "limit-maker"])
    p.add_argument(
        "--venue",
        default="BINANCE",
        help="Venue or market group: BINANCE, NASDAQ, HKEX, SSE, ASHARE/A股, US/美股, HK/港股, …",
    )

    p = sub.add_parser("backtest-multi", help="Per-symbol SMA portfolio across many instruments.")
    p.add_argument(
        "--symbols",
        default="BTCUSDT,ETHUSDT",
        help="comma-separated, @default equities, or @etf for liquid ETFs",
    )
    p.add_argument("--fast", type=int, default=10)
    p.add_argument("--slow", type=int, default=30)
    p.add_argument("--n", type=int, default=400)
    p.add_argument("--from-lake", action="store_true", help="Read each symbol's OHLC from the lake")
    p.add_argument("--interval", default="1m")
    p.add_argument(
        "--venue",
        default="BINANCE",
        help="Venue or market group: BINANCE, NASDAQ, HKEX, SSE, ASHARE/A股, US/美股, HK/港股, …",
    )
    p.add_argument(
        "--base-ccy",
        default=None,
        help="Revalue multi-currency equity prices into USD|CNY|HKD before the kernel run",
    )

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
    p.add_argument(
        "--recorded-session",
        default=None,
        help="Replay a captured sandbox/testnet fixture JSON instead of placing orders.",
    )
    p.add_argument(
        "--record-out",
        default=None,
        help="Write the sandbox/testnet session JSON after the gate run.",
    )

    p = sub.add_parser("optimize", help="Walk-forward optimize params with OOS validation.")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--trials", type=int, default=50, help="Optuna trials per fold (--optuna only).")
    p.add_argument("--splits", type=int, default=4)
    p.add_argument("--mode", default="rolling", choices=["rolling", "anchored"])
    p.add_argument("--optuna", action="store_true", help="Use Optuna TPE instead of grid search.")
    p.add_argument("--from-lake", action="store_true", help="Optimize over the local Parquet lake.")
    p.add_argument("--interval", default="1m")

    p = sub.add_parser("screen", help="Fast vectorized sweep + cross-check vs the event runner.")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--from-lake", action="store_true", help="Screen over the local Parquet lake.")
    p.add_argument("--interval", default="1m")
    p.add_argument("--n", type=int, default=1200)

    p = sub.add_parser(
        "download",
        help="Download REAL history into the local Parquet lake (Binance or Yahoo equity).",
    )
    p.add_argument(
        "--symbols",
        default="BTCUSDT",
        help="comma-separated (BTCUSDT,AAPL,0700,600519) or @default / @etf presets",
    )
    p.add_argument("--interval", default="1m")
    p.add_argument("--days", type=float, default=7.0)
    p.add_argument(
        "--venue",
        default="BINANCE",
        help="BINANCE | NYSE|NASDAQ|HKEX|SSE|SZSE | ASHARE/A股 | US/美股 | HK/港股 | ETF",
    )
    p.add_argument(
        "--no-calendar-filter",
        action="store_true",
        help="Keep weekend/holiday/flat-halt equity bars (default: filter them out)",
    )
    p.add_argument(
        "--adjust",
        action="store_true",
        help="Store split/dividend-adjusted equity OHLC (前复权 via Yahoo adjclose)",
    )

    p = sub.add_parser("venues", help="List registered global venues (crypto + stock markets).")
    p.add_argument(
        "--family",
        default=None,
        choices=["crypto", "equity", "index"],
        help="Optional filter by asset family.",
    )

    p = sub.add_parser(
        "download-fx",
        help="Download Yahoo FX pairs into venue=FX (USDCNY/USDHKD for multi-ccy).",
    )
    p.add_argument("--pairs", default="USDCNY,USDHKD", help="comma-separated bare pairs")
    p.add_argument("--days", type=float, default=365.0)
    p.add_argument("--lake-root", default=None)

    p = sub.add_parser(
        "paper-equity",
        help="Replay lake bars through PaperEquityBroker (A-share T+1 / 涨跌停).",
    )
    p.add_argument("--symbol", default="600519")
    p.add_argument("--venue", default="SSE", help="SSE, SZSE, NASDAQ, HKEX, ASHARE, …")
    p.add_argument("--interval", default="1d")
    p.add_argument("--strategy", default="sma", choices=["sma", "buyhold", "none"])
    p.add_argument("--fast", type=int, default=5)
    p.add_argument("--slow", type=int, default=20)
    p.add_argument("--qty", type=float, default=None, help="order size (default: board lot)")
    p.add_argument("--cash", type=float, default=1_000_000.0, help="starting cash in listing ccy")
    p.add_argument(
        "--multi",
        action="store_true",
        help="Multi-symbol portfolio on one paper broker (comma symbols or @default)",
    )

    p = sub.add_parser(
        "ib-status",
        help="Probe IB TWS/Gateway (ib_insync); see docs/IB_PAPER.md",
    )
    p.add_argument(
        "--no-readonly",
        action="store_true",
        help="Connect without forcing readonly (default is readonly)",
    )

    p = sub.add_parser("ingest-trades", help="Aggregate public aggTrades into the local lake.")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--interval", default="1m")
    p.add_argument("--limit", type=int, default=1000)
    p.add_argument("--venue", default="BINANCE")

    p = sub.add_parser("kill-switch", help="Engage/release the platform kill-switch via api.")
    p.add_argument("--release", action="store_true", help="Release instead of engage.")
    p.add_argument("--reason", default="manual operator action")
    p.add_argument("--actor", default="cli")
    p.add_argument("--api-base", default=None)
    p.add_argument("--api-key", default=None)

    p = sub.add_parser("live", help="Start the live/sandbox TradingNode.")
    p.add_argument("--env", default="sandbox")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument(
        "--no-dry-run",
        action="store_true",
        help="Run the full native live loop (requires keys + coinext_py live wiring).",
    )
    p.add_argument(
        "--paper",
        action="store_true",
        help="Offline paper LiveKernel (no venue keys; ReplayDataClient + PaperFillExec).",
    )

    p = sub.add_parser("reconcile", help="Reconcile local event log vs venue open orders.")
    p.add_argument("--symbol", default="BTCUSDT")

    p = sub.add_parser("capture-quotes", help="Capture live bookTicker into a JSON recording.")
    p.add_argument("--symbol", default="BTCUSDT")
    p.add_argument("--seconds", type=float, default=15.0)
    p.add_argument("--interval", type=float, default=0.5)
    p.add_argument("--mode", default="rest", choices=["rest", "ws"])
    p.add_argument("--out", default=None)
    p.add_argument("--testnet", action="store_true")

    p = sub.add_parser("catalog", help="Inspect the data lake.")
    p.add_argument(
        "--venue",
        default="BINANCE",
        help="Venue partition to list, or ALL for every venue present.",
    )

    return parser


def _run_argparse(argv: list[str] | None) -> int:
    parser = _build_argparse_parser()
    ns = parser.parse_args(argv)
    dispatch = {
        "backtest": lambda: _cmd_backtest(
            ns.symbol,
            ns.fast,
            ns.slow,
            ns.n,
            ns.real,
            ns.from_lake,
            ns.interval,
            ns.strategy,
            ns.venue,
        ),
        "backtest-multi": lambda: _cmd_backtest_multi(
            ns.symbols,
            ns.fast,
            ns.slow,
            ns.n,
            ns.from_lake,
            ns.interval,
            ns.venue,
            getattr(ns, "base_ccy", None),
        ),
        "parity": lambda: _cmd_parity(ns.symbol, ns.fast, ns.slow, ns.n),
        "testnet-gate": lambda: _cmd_testnet_gate(
            ns.symbol,
            ns.fast,
            ns.slow,
            ns.n,
            ns.qty,
            ns.no_testnet,
            ns.recorded_session,
            ns.record_out,
        ),
        "optimize": lambda: _cmd_optimize(
            ns.symbol, ns.trials, ns.splits, ns.mode, ns.optuna, ns.from_lake, ns.interval
        ),
        "screen": lambda: _cmd_screen(ns.symbol, ns.from_lake, ns.interval, ns.n),
        "kill-switch": lambda: _cmd_kill_switch(
            ns.release, ns.reason, ns.actor, ns.api_base, ns.api_key
        ),
        "download": lambda: _cmd_download(
            ns.symbols,
            ns.interval,
            ns.days,
            ns.venue,
            apply_calendar=not getattr(ns, "no_calendar_filter", False),
            adjust=getattr(ns, "adjust", False),
        ),
        "download-fx": lambda: _cmd_download_fx(ns.pairs, ns.days, getattr(ns, "lake_root", None)),
        "paper-equity": lambda: _cmd_paper_equity(
            ns.symbol,
            ns.venue,
            ns.interval,
            ns.strategy,
            ns.fast,
            ns.slow,
            ns.qty,
            ns.cash,
            getattr(ns, "multi", False),
        ),
        "ib-status": lambda: _cmd_ib_status(readonly=not getattr(ns, "no_readonly", False)),
        "venues": lambda: _cmd_venues(ns.family),
        "ingest-trades": lambda: _cmd_ingest_trades(ns.symbol, ns.interval, ns.limit, ns.venue),
        "live": lambda: _cmd_live(ns.env, ns.symbol, dry_run=not ns.no_dry_run, paper=ns.paper),
        "reconcile": lambda: _cmd_reconcile(ns.symbol),
        "capture-quotes": lambda: _cmd_capture_quotes(
            ns.symbol, ns.seconds, ns.interval, ns.mode, ns.out, ns.testnet
        ),
        "catalog": lambda: _cmd_catalog(ns.venue),
    }
    return dispatch[ns.command]()


def main(argv: list[str] | None = None) -> int:
    """Module entry point (``python -m coinext_cli.main``). Prefers Typer, falls back to argparse."""
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


# The ``coinext`` console script targets ``coinext_cli.main:app``. Expose a Typer ``app`` when present, else a
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
