"""Option pricing (Black–Scholes) bridged to Python — the same coinext-derivatives the Rust core uses.

Phase 3 of derivatives: price options, compute greeks, back out implied vol. Requires coinext_py.
"""

from __future__ import annotations

import pytest

pytest.importorskip("coinext_py", reason="build coinext_py: uvx maturin develop --features python")

from coinext_derivatives import Greeks, bs_price, greeks, implied_vol  # noqa: E402

# Reference: S=K=100, T=1, r=5%, vol=20%.
ATM = dict(spot=100.0, strike=100.0, t_years=1.0, rate=0.05, vol=0.2)


def test_textbook_prices():
    assert bs_price(**ATM, right="call") == pytest.approx(10.4506, abs=1e-3)
    assert bs_price(**ATM, right="put") == pytest.approx(5.5735, abs=1e-3)


def test_put_call_parity():
    c = bs_price(**ATM, right="call")
    p = bs_price(**ATM, right="put")
    import math

    parity = ATM["spot"] - ATM["strike"] * math.exp(-ATM["rate"] * ATM["t_years"])
    assert (c - p) == pytest.approx(parity, abs=1e-6)


def test_greeks_shape_and_relations():
    c = greeks(**ATM, right="call")
    p = greeks(**ATM, right="put")
    assert isinstance(c, Greeks)
    assert c.delta == pytest.approx(0.6368, abs=1e-3)
    assert (c.delta - p.delta) == pytest.approx(1.0, abs=1e-9)  # call − put delta = 1
    assert c.gamma == pytest.approx(p.gamma)  # gamma/vega identical call vs put
    assert c.vega == pytest.approx(p.vega)
    assert c.gamma > 0 and c.vega > 0


def test_implied_vol_round_trips():
    for right, vol in [("call", 0.15), ("put", 0.30), ("call", 0.45)]:
        args = {**ATM, "vol": vol}
        px = bs_price(**args, right=right)
        iv = implied_vol(px, ATM["spot"], ATM["strike"], ATM["t_years"], ATM["rate"], right)
        assert iv == pytest.approx(vol, abs=1e-4)


def test_implied_vol_below_intrinsic_is_none():
    # A call quoted below its intrinsic (deep ITM, price < S−K) has no real implied vol.
    iv = implied_vol(1.0, spot=200.0, strike=100.0, t_years=1.0, rate=0.0, right="call")
    assert iv is None


def test_bad_right_raises():
    with pytest.raises(ValueError):
        bs_price(**ATM, right="straddle")


def test_expired_option_prices_intrinsic():
    assert bs_price(spot=120, strike=100, t_years=0.0, rate=0.05, vol=0.2, right="call") == 20.0
    assert bs_price(spot=120, strike=100, t_years=0.0, rate=0.05, vol=0.2, right="put") == 0.0
