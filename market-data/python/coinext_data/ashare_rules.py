"""A-share market rules helpers (lot sizes, limit bands for research).

Status: verified; Kernel T+1/涨跌停 live in OMS.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from .calendar import previous_session_date, session_date

_NS_PER_S = 1_000_000_000

T1_VENUES: frozenset[str] = frozenset({"SSE", "SZSE"})

# Documented board heuristics (Kernel must match).
LIMIT_PCT_MAIN = 0.10
LIMIT_PCT_CHINEXT_STAR = 0.20
LIMIT_PCT_ST = 0.05


def is_t1_venue(venue: str) -> bool:
    return venue.strip().upper() in T1_VENUES


def trade_date_from_ns(ts_ns: int, venue: str = "SSE") -> dt.date:
    """Session-local calendar date for T+1 / 涨跌停 day keys."""
    return session_date(int(ts_ns), venue)


def price_limit_pct(venue: str, symbol: str) -> float | None:
    """Daily limit fraction or ``None`` if the venue has no A-share band."""
    if not is_t1_venue(venue):
        return None
    s = symbol.strip().upper()
    if s.startswith("*ST") or s.startswith("ST"):
        return LIMIT_PCT_ST
    body = s
    for suf in (".SS", ".SZ"):
        if body.endswith(suf):
            body = body[: -len(suf)]
    if body.isdigit():
        b = body.zfill(6)
        if b.startswith(("300", "301", "688", "689")):
            return LIMIT_PCT_CHINEXT_STAR
        return LIMIT_PCT_MAIN
    return LIMIT_PCT_MAIN


def round_tick(px: float, decimals: int = 2) -> float:
    """A-share style price rounding (0.01 CNY)."""
    return round(float(px), decimals)


@dataclass(frozen=True, slots=True)
class LimitBand:
    prev_close: float
    pct: float

    @property
    def up(self) -> float:
        return round_tick(self.prev_close * (1.0 + self.pct))

    @property
    def down(self) -> float:
        return round_tick(self.prev_close * (1.0 - self.pct))

    def clamp(self, price: float) -> float:
        return min(self.up, max(self.down, float(price)))

    def allows(self, price: float, *, side: str) -> bool:
        px = float(price)
        if side == "buy":
            return px <= self.up + 1e-9
        return px >= self.down - 1e-9


def limit_band(venue: str, symbol: str, prev_close: float) -> LimitBand | None:
    pct = price_limit_pct(venue, symbol)
    if pct is None or prev_close <= 0:
        return None
    return LimitBand(prev_close=float(prev_close), pct=pct)


def resolve_prev_close(
    closes_by_session: dict[dt.date, float],
    session_day: dt.date,
    venue: str = "SSE",
    *,
    max_lookback: int = 30,
) -> float | None:
    """Last close strictly before ``session_day``, walking **trading days** only.

    ``closes_by_session`` maps session dates → last print that day. Holidays/weekends without
    prints are skipped automatically; if a holiday had a stale print it is still skipped when
    looking for the previous *trading* day via :func:`previous_session_date`.
    """
    d = previous_session_date(venue, session_day)
    for _ in range(max_lookback):
        if d is None:
            return None
        if d in closes_by_session:
            return closes_by_session[d]
        d = previous_session_date(venue, d)
    return None


__all__ = [
    "LIMIT_PCT_CHINEXT_STAR",
    "LIMIT_PCT_MAIN",
    "LIMIT_PCT_ST",
    "LimitBand",
    "T1_VENUES",
    "is_t1_venue",
    "limit_band",
    "price_limit_pct",
    "resolve_prev_close",
    "round_tick",
    "trade_date_from_ns",
]
