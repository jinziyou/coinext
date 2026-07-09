"""Derivatives instrument foundation: equity / futures / options trade through the Rust kernel.

Phase 1 models the three asset classes as tradeable priced instruments — you feed the contract's own
price series and PnL scales by the contract multiplier. Expiry settlement / exercise / greeks are
later phases. Requires the compiled coinext_py extension.
"""

from __future__ import annotations

import pytest

pytest.importorskip("coinext_py", reason="build coinext_py: uvx maturin develop --features python")

import coinext_backtest as bt  # noqa: E402
from coinext_strategy import Strategy  # noqa: E402

BASE, STEP = 1_700_000_000_000_000_000, 60_000_000_000
EXPIRY = 2_000_000_000_000_000_000


class BuyHold(Strategy):
    def __init__(self):
        self.done = False

    def on_bar(self, bar, ctx):
        if not self.done:
            self.done = True
            ctx.submit_market("buy", 1.0)


def _rising_bars(n=11):
    # close climbs 100 -> 100+n-1, so a 1-unit long gains (n-1) per unit of multiplier.
    return [(BASE + i * STEP, 100.0 + i) for i in range(n)]


def test_future_pnl_scales_with_multiplier():
    bars = _rising_bars()
    spot = bt.run(BuyHold(), bars=bars)
    fut = bt.run(
        BuyHold(),
        bars=bars,
        instrument=bt.Instrument.future(multiplier=10.0, expiry_ns=EXPIRY, underlying="BTCUSDT"),
    )
    spot_pnl = spot.final_equity - 100_000.0
    fut_pnl = fut.final_equity - 100_000.0
    assert spot_pnl > 0
    # PnL and fees both scale by the multiplier.
    assert fut_pnl == pytest.approx(spot_pnl * 10.0, rel=1e-6)


def test_option_contract_trades_with_multiplier():
    bars = _rising_bars()
    spot_pnl = bt.run(BuyHold(), bars=bars).final_equity - 100_000.0
    # Distinct option symbol so its underlying ("BTCUSDT") isn't the option itself; with no
    # underlying feed in this single-instrument run, expiry settles to the option's own mark -> the
    # multiplier relationship is preserved (settled PnL == the mark-to-market PnL, scaled 100x).
    opt = bt.run(
        BuyHold(),
        bars=bars,
        symbol="BTC-C-50000",
        instrument=bt.Instrument.option(
            strike=50_000, right="call", expiry_ns=EXPIRY, underlying="BTCUSDT", multiplier=100.0
        ),
    )
    assert opt.orders_denied == 0
    assert (opt.final_equity - 100_000.0) == pytest.approx(spot_pnl * 100.0, rel=1e-6)


def test_future_settles_at_expiry():
    # A future expiring within the run cash-settles: the open position closes (one extra fill) at
    # the final mark, and the prior unrealized gain becomes realized.
    base, step = BASE, STEP
    bars = [(base + i * step, 100.0 + i) for i in range(6)]  # 100 -> 105
    expiry = base + 6 * step + 1  # just past the last bar
    res = bt.run(
        BuyHold(),
        bars=bars,
        instrument=bt.Instrument.future(multiplier=10.0, expiry_ns=expiry, underlying="SPX"),
    )
    assert res.orders_submitted == 1
    assert res.fills == 2  # the entry + the settlement fill
    assert res.realized_pnl > 0  # settled to mark 105 above the ~100 entry, x10 multiplier


def test_equity_trades_like_spot():
    bars = _rising_bars()
    spot = bt.run(BuyHold(), bars=bars).final_equity
    eq = bt.run(BuyHold(), bars=bars, instrument=bt.Instrument.equity()).final_equity
    assert eq == pytest.approx(spot)  # equity multiplier is 1


def test_option_spec_validates_right():
    with pytest.raises(ValueError):
        bt.Instrument.option(strike=100, right="sideways", expiry_ns=EXPIRY, underlying="BTCUSDT")


def test_future_requires_expiry_at_the_bridge():
    # The Rust bridge rejects a future built without an expiry (defensive; the helper requires it).
    with pytest.raises((ValueError, TypeError)):
        import coinext_py

        coinext_py.run_backtest(
            BuyHold(),
            "ESZ5",
            "CME",
            100_000.0,
            [(BASE, 100.0, 100.0, 100.0, 100.0, 0.0)],
            asset_class="future",
            multiplier=50.0,
            # expiry_ns omitted -> error
        )
