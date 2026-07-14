"""coinext_derivatives — Black-Scholes / Greeks / IV facades.

Status: verified. See root ARCHITECTURE.md and docs/STATUS.md.
"""

from __future__ import annotations

from typing import NamedTuple

try:
    import coinext_py  # the compiled Rust extension (maturin develop)
except ImportError as exc:  # pragma: no cover - surfaced as a clear setup error
    raise ImportError(
        "coinext_py extension not built. Run: "
        "uvx maturin develop --manifest-path foundation/crates/coinext-py/Cargo.toml --features python"
    ) from exc


class Greeks(NamedTuple):
    """First-order greeks. ``vega`` is per 1.0 of vol (÷100 for per-1%), ``theta`` per year."""

    delta: float
    gamma: float
    vega: float
    theta: float
    rho: float


def _is_call(right: str) -> bool:
    r = right.lower()
    if r in ("call", "c"):
        return True
    if r in ("put", "p"):
        return False
    raise ValueError(f"right must be 'call' or 'put', got {right!r}")


def bs_price(
    spot: float, strike: float, t_years: float, rate: float, vol: float, right: str = "call"
) -> float:
    """Black–Scholes premium of a European option."""
    return coinext_py.bs_price(spot, strike, t_years, rate, vol, _is_call(right))


def greeks(
    spot: float, strike: float, t_years: float, rate: float, vol: float, right: str = "call"
) -> Greeks:
    """The five greeks as a :class:`Greeks` namedtuple."""
    return Greeks(*coinext_py.bs_greeks(spot, strike, t_years, rate, vol, _is_call(right)))


def implied_vol(
    market_price: float,
    spot: float,
    strike: float,
    t_years: float,
    rate: float,
    right: str = "call",
) -> float | None:
    """Volatility that reprices ``market_price``; ``None`` if below intrinsic / unbracketed."""
    return coinext_py.implied_vol(market_price, spot, strike, t_years, rate, _is_call(right))


__all__ = ["Greeks", "bs_price", "greeks", "implied_vol"]
