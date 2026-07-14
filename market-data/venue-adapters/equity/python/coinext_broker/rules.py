"""A-share rules re-export — canonical implementation lives in ``coinext_data.ashare_rules``.

Keep this thin so paper and research share one definition with Kernel parity docs.
"""

from __future__ import annotations

from coinext_data.ashare_rules import (
    LIMIT_PCT_CHINEXT_STAR,
    LIMIT_PCT_MAIN,
    LIMIT_PCT_ST,
    LimitBand,
    T1_VENUES,
    is_t1_venue,
    limit_band,
    price_limit_pct,
    resolve_prev_close,
    round_tick,
    trade_date_from_ns,
)

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
