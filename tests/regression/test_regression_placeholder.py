"""Pinned-statistics regression gate (LEAN-style determinism check).

VeloxQuant's core invariant is that the event-driven backtest is **deterministic** (see
``docs/ARCHITECTURE.md`` §1-2: the synchronous core merge-sorts events by ``ts_event``; there is no
RNG and ``synthetic_bars`` is a closed-form series). This test enforces two things:

1. **Bit-for-bit reproducibility** — running the SAME ``SmaCross`` over the SAME bars twice yields
   an identical equity curve and identical fill count.
2. **Pinned final equity** — the run's ``final_equity`` matches a value pinned in a small JSON
   golden file. The golden is *created on first run* (so the gate self-bootstraps), then compared
   on every subsequent run within a tight tolerance. A drift larger than the tolerance means the
   engine economics or matching changed and must be reviewed (delete the golden to re-pin
   intentionally).

This is the placeholder for the broader regression suite; extend with more strategies / fixtures.
The whole module is skipped if the compiled ``qv_py`` extension is not built.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Skip the entire module unless the Rust extension is available (same guard as tests/parity etc.).
pytest.importorskip(
    "qv_py",
    reason=(
        "build the extension: "
        "uvx maturin develop --manifest-path crates/qv-py/Cargo.toml --features python"
    ),
)

from qv_backtest import run, synthetic_bars  # noqa: E402
from qv_strategy import SmaCross  # noqa: E402

# --- Fixed fixture (any change here is an intentional re-pin) ---
_FIXTURE = {
    "bars": 300,
    "fast": 10,
    "slow": 30,
    "qty": 0.5,
    "starting_balance": 100_000.0,
}

# Golden file lives NEXT TO this test (committed; not under the gitignored /data path).
_GOLDEN_PATH = Path(__file__).with_name("regression_golden.json")

# Absolute tolerance on final_equity (quote ccy). Tight: determinism means this should be ~0, but a
# small band absorbs trivial float-display rounding without masking real economic drift.
_FINAL_EQUITY_ABS_TOL = 1e-6


def _run_fixed():
    """Run the pinned strategy over the pinned synthetic bars and return the BacktestResult."""
    bars = synthetic_bars(_FIXTURE["bars"])
    strategy = SmaCross(_FIXTURE["fast"], _FIXTURE["slow"], _FIXTURE["qty"])
    return run(strategy, bars=bars, starting_balance=_FIXTURE["starting_balance"])


def test_backtest_is_bit_for_bit_reproducible():
    """Same inputs -> identical equity curve and identical fills (no RNG, deterministic core)."""
    a = _run_fixed()
    b = _run_fixed()

    assert list(a.equity_curve) == list(b.equity_curve), "equity curve diverged between runs"
    assert a.fills == b.fills, "fill count diverged between runs"
    assert a.orders_submitted == b.orders_submitted
    assert a.final_equity == b.final_equity


def test_final_equity_matches_pinned_golden():
    """Compare final_equity to a pinned value; create the golden on first run if missing."""
    result = _run_fixed()
    actual = float(result.final_equity)

    if not _GOLDEN_PATH.exists():
        # First run: self-bootstrap the golden so the gate is active from here on.
        _GOLDEN_PATH.write_text(
            json.dumps(
                {
                    "fixture": _FIXTURE,
                    "final_equity": actual,
                    "fills": int(result.fills),
                    "orders_submitted": int(result.orders_submitted),
                    "_note": "Auto-pinned on first run. Delete to intentionally re-pin.",
                },
                indent=2,
            )
            + "\n"
        )
        pytest.skip(f"pinned regression golden created at {_GOLDEN_PATH.name}; re-run to compare")

    golden = json.loads(_GOLDEN_PATH.read_text())
    expected = float(golden["final_equity"])

    assert actual == pytest.approx(expected, abs=_FINAL_EQUITY_ABS_TOL), (
        f"final_equity drifted from pinned golden: {actual} vs {expected} "
        f"(tol={_FINAL_EQUITY_ABS_TOL}). If intentional, delete {_GOLDEN_PATH.name} to re-pin."
    )
    # Discrete counts must match exactly (no tolerance).
    assert int(result.fills) == int(golden["fills"])
    assert int(result.orders_submitted) == int(golden["orders_submitted"])
