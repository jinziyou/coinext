"""Multi-instrument backtests: many symbols through ONE Rust kernel via ``run_backtest_multi``.

Proves the bridge wires the already-multi-instrument core to Python: each symbol's bars reach
``on_bar`` tagged with ``bar.symbol``, orders target the right instrument, positions are isolated,
and a portfolio run is equivalent to running each symbol standalone. Requires the compiled
``qv_py`` extension.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PYTHON_ROOT = Path(__file__).resolve().parents[1] / "python"
if str(_PYTHON_ROOT) not in sys.path:
    sys.path.insert(0, str(_PYTHON_ROOT))

pytest.importorskip("qv_py", reason="build qv_py: uvx maturin develop --features python")

import qv_backtest as bt  # noqa: E402
from qv_strategy import MultiSma, SmaCross, Strategy  # noqa: E402

STEP, BASE = 60_000_000_000, 1_700_000_000_000_000_000


def _flat(price: float, n: int = 10) -> list[tuple[int, float]]:
    return [(BASE + i * STEP, price) for i in range(n)]


def test_multi_instrument_position_isolation():
    # Buy 1 AAA on its first bar and 2 BBB on its first; positions must stay per-symbol independent.
    class Recorder(Strategy):
        def __init__(self):
            self.seen: set[str] = set()
            self.pos: dict[str, float] = {}
            self.bought: set[str] = set()

        def on_bar(self, bar, ctx):
            self.seen.add(bar.symbol)
            self.pos[bar.symbol] = ctx.position(bar.symbol)
            if bar.symbol not in self.bought:
                self.bought.add(bar.symbol)
                ctx.submit_market("buy", 1.0 if bar.symbol == "AAA" else 2.0, bar.symbol)

    rec = Recorder()
    res = bt.run_multi(rec, bars={"AAA": _flat(100.0), "BBB": _flat(200.0)})

    assert rec.seen == {"AAA", "BBB"}  # both symbols dispatched to on_bar
    assert rec.pos["AAA"] == pytest.approx(1.0)
    assert rec.pos["BBB"] == pytest.approx(2.0)  # independent size, independent position
    assert res.fills == 2
    assert res.orders_denied == 0


def test_portfolio_equals_sum_of_single_runs():
    # A multi-instrument MultiSma run must produce exactly the union of two standalone SmaCross runs
    # (same engines, isolated per-symbol state) — the strongest equivalence check.
    a = bt.synthetic_bars(400)
    b = bt.synthetic_bars(400, period=55, amplitude=2000.0)

    multi = bt.run_multi(MultiSma(10, 30, 0.5), bars={"AAA": a, "BBB": b})
    single_a = bt.run(SmaCross(10, 30, 0.5), bars=a)
    single_b = bt.run(SmaCross(10, 30, 0.5), bars=b)

    assert multi.fills == single_a.fills + single_b.fills
    assert multi.orders_submitted == single_a.orders_submitted + single_b.orders_submitted
    assert multi.fills > 0


def test_run_multi_rejects_empty():
    with pytest.raises(ValueError):
        bt.run_multi(MultiSma(), bars={})


def test_order_to_unknown_symbol_is_dropped():
    # A strategy that targets a symbol not registered as an instrument: the intent is dropped (no
    # crash, no fill) rather than misrouted.
    class BadTarget(Strategy):
        def __init__(self):
            self.done = False

        def on_bar(self, bar, ctx):
            if not self.done:
                self.done = True
                ctx.submit_market("buy", 1.0, "NOPE")  # not in `bars`

    res = bt.run_multi(BadTarget(), bars={"AAA": _flat(100.0)})
    assert res.fills == 0


def test_run_multi_accepts_mixed_close_and_ohlc():
    # One symbol close-only, one OHLC — both normalize and run together.
    ohlc = [(BASE + i * STEP, 100.0, 101.0, 99.0, 100.0) for i in range(10)]
    res = bt.run_multi(MultiSma(3, 5, 0.1), bars={"AAA": _flat(100.0), "BBB": ohlc})
    assert res.starting_equity == pytest.approx(100_000.0)
