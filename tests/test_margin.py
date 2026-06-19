"""Margin / leverage / liquidation (Phase 4 of derivatives).

`leverage` gates initial margin at submit; `maintenance_margin_rate` arms mark-to-market
liquidation. Both opt-in (0 = fully funded, no liquidation). Requires the compiled coinext_py extension.
"""

from __future__ import annotations

import pytest

pytest.importorskip("coinext_py", reason="build coinext_py: uvx maturin develop --features python")

import coinext_backtest as bt  # noqa: E402
from coinext_strategy import Strategy  # noqa: E402

BASE, STEP = 1_700_000_000_000_000_000, 60_000_000_000


class BuyOnce(Strategy):
    def __init__(self, qty: float):
        self.qty = qty
        self.done = False

    def on_bar(self, bar, ctx):
        if not self.done:
            self.done = True
            ctx.submit_market("buy", self.qty)


def _bars(prices):
    return [(BASE + i * STEP, float(p)) for i, p in enumerate(prices)]


def test_leverage_denies_an_overleveraged_order():
    bars = _bars([100.0] * 4)
    # 1000 equity at 2x -> max notional 2000. qty 30 @ 100 = 3000 notional (margin 1500 > 1000).
    res = bt.run(BuyOnce(30.0), bars=bars, starting_balance=1000.0, leverage=2.0)
    assert res.orders_submitted == 1
    assert res.orders_denied == 1
    assert res.fills == 0


def test_leverage_allows_within_the_limit():
    bars = _bars([100.0] * 4)
    # qty 15 @ 100 = 1500 notional -> margin 750 < 1000 equity -> allowed.
    res = bt.run(BuyOnce(15.0), bars=bars, starting_balance=1000.0, leverage=2.0)
    assert res.orders_denied == 0
    assert res.fills == 1


def test_maintenance_margin_liquidates():
    # Buy 1 @ ~50k with 10k; the price dips to 44k (equity ~4k < maint 44k×0.1 = 4.4k) -> liquidate.
    # The later recovery to 50k can't save the already-flattened account.
    bars = _bars([50_000, 44_000, 50_000])
    res = bt.run(BuyOnce(1.0), bars=bars, starting_balance=10_000.0, maintenance_margin_rate=0.1)
    assert res.fills == 2  # entry + forced liquidation close
    assert res.final_equity < 5_000.0  # the loss is locked in


def test_no_liquidation_without_a_rate_recovers():
    # The IDENTICAL dip-and-recover, no maintenance rate -> the position rides through and recovers.
    bars = _bars([50_000, 44_000, 50_000])
    res = bt.run(BuyOnce(1.0), bars=bars, starting_balance=10_000.0)
    assert res.fills == 1  # just the entry; never force-closed (spot has no expiry settlement)
    assert res.final_equity > 9_000.0  # recovered to ~breakeven
