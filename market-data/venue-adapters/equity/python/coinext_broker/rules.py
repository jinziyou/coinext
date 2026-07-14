"""A-share market rules: T+1 sellability + daily price limits (涨跌停).

Session dates use **venue-local** calendars (Asia/Shanghai for A-shares) via
:func:`coinext_data.calendar.session_date` — not UTC midnight.

Research-grade approximations — not a full exchange rule engine.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

_NS_PER_S = 1_000_000_000

# Venues that enforce cash T+1 (cannot sell shares bought the same session day).
_T1_VENUES = frozenset({"SSE", "SZSE"})


def is_t1_venue(venue: str) -> bool:
    return venue.strip().upper() in _T1_VENUES


def trade_date_from_ns(ts_ns: int, venue: str = "SSE") -> dt.date:
    """Local **session** calendar date for a bar/order timestamp.

    Defaults to A-share (Asia/Shanghai). Pass ``venue`` for US/HK session TZ.
    """
    try:
        from coinext_data.calendar import session_date

        return session_date(int(ts_ns), venue)
    except Exception:
        # Offline fallback if coinext_data is not on path (should not happen in-repo).
        return dt.datetime.fromtimestamp(int(ts_ns) // _NS_PER_S, tz=dt.UTC).date()


def price_limit_pct(venue: str, symbol: str) -> float | None:
    """Return daily limit fraction (e.g. 0.10 = ±10%) or ``None`` if no limit.

    A-share heuristics (aligned with Kernel OMS):
    * ST / *ST names → 5%
    * STAR (688) / ChiNext (300) → 20%
    * Main board + most ETFs → 10%
    * Non-A venues → ``None``
    """
    v = venue.strip().upper()
    if v not in _T1_VENUES:
        return None
    s = symbol.strip().upper()
    if s.startswith("*ST") or s.startswith("ST"):
        return 0.05
    body = s
    for suf in (".SS", ".SZ"):
        if body.endswith(suf):
            body = body[: -len(suf)]
    if body.isdigit():
        b = body.zfill(6)
        if b.startswith(("300", "301", "688", "689")):
            return 0.20
        return 0.10
    return 0.10


def _round_tick(px: float, decimals: int = 2) -> float:
    """A-share style price rounding (0.01 CNY)."""
    return round(float(px), decimals)


@dataclass(frozen=True, slots=True)
class LimitBand:
    prev_close: float
    pct: float

    @property
    def up(self) -> float:
        return _round_tick(self.prev_close * (1.0 + self.pct))

    @property
    def down(self) -> float:
        return _round_tick(self.prev_close * (1.0 - self.pct))

    def clamp(self, price: float) -> float:
        return min(self.up, max(self.down, float(price)))

    def allows(self, price: float, *, side: str) -> bool:
        """Buy cannot be above up-limit; sell not below down-limit (at-limit OK)."""
        px = float(price)
        if side == "buy":
            return px <= self.up + 1e-9
        return px >= self.down - 1e-9


def limit_band(venue: str, symbol: str, prev_close: float) -> LimitBand | None:
    pct = price_limit_pct(venue, symbol)
    if pct is None or prev_close <= 0:
        return None
    return LimitBand(prev_close=float(prev_close), pct=pct)


__all__ = [
    "LimitBand",
    "is_t1_venue",
    "limit_band",
    "price_limit_pct",
    "trade_date_from_ns",
]
