"""Exchange trading calendars + bar hygiene for equity research.

Filters weekend / holiday sessions and likely halt bars (null already dropped by the Yahoo
parser; zero-volume flat prints are treated as halted). Used by the equity downloader and
optional lake re-reads so multi-market backtests do not see non-sessions.

Calendars are **research-grade**: weekend rules are exact; holiday tables cover 2018–2028 for
CN/HK and rule-based federal-style holidays for US. They are not a substitute for a licensed
exchange calendar feed.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Iterable, Sequence

from .venues import expand_venues, get_venue, resolve_market_group

_NS_PER_S = 1_000_000_000

# Bar row shapes accepted by filter helpers (close-only, OHLC, OHLCV).
BarLike = tuple


# ---------------------------------------------------------------------------
# Holiday tables (YYYY-MM-DD). CN/HK from public exchange announcements (approx).
# ---------------------------------------------------------------------------

# Mainland China (SSE/SZSE) closed days 2018–2028 (weekends excluded separately).
_CN_HOLIDAYS: frozenset[str] = frozenset(
    {
        # 2018
        "2018-01-01",
        "2018-02-15",
        "2018-02-16",
        "2018-02-17",
        "2018-02-18",
        "2018-02-19",
        "2018-02-20",
        "2018-02-21",
        "2018-04-05",
        "2018-04-06",
        "2018-04-07",
        "2018-04-30",
        "2018-05-01",
        "2018-06-18",
        "2018-09-24",
        "2018-10-01",
        "2018-10-02",
        "2018-10-03",
        "2018-10-04",
        "2018-10-05",
        # 2019
        "2019-01-01",
        "2019-02-04",
        "2019-02-05",
        "2019-02-06",
        "2019-02-07",
        "2019-02-08",
        "2019-04-05",
        "2019-05-01",
        "2019-05-02",
        "2019-05-03",
        "2019-06-07",
        "2019-09-13",
        "2019-10-01",
        "2019-10-02",
        "2019-10-03",
        "2019-10-04",
        "2019-10-07",
        # 2020
        "2020-01-01",
        "2020-01-24",
        "2020-01-25",
        "2020-01-26",
        "2020-01-27",
        "2020-01-28",
        "2020-01-29",
        "2020-01-30",
        "2020-04-06",
        "2020-05-01",
        "2020-05-04",
        "2020-05-05",
        "2020-06-25",
        "2020-06-26",
        "2020-10-01",
        "2020-10-02",
        "2020-10-05",
        "2020-10-06",
        "2020-10-07",
        "2020-10-08",
        # 2021
        "2021-01-01",
        "2021-02-11",
        "2021-02-12",
        "2021-02-15",
        "2021-02-16",
        "2021-02-17",
        "2021-04-05",
        "2021-05-03",
        "2021-05-04",
        "2021-05-05",
        "2021-06-14",
        "2021-09-20",
        "2021-09-21",
        "2021-10-01",
        "2021-10-04",
        "2021-10-05",
        "2021-10-06",
        "2021-10-07",
        # 2022
        "2022-01-03",
        "2022-01-31",
        "2022-02-01",
        "2022-02-02",
        "2022-02-03",
        "2022-02-04",
        "2022-04-04",
        "2022-04-05",
        "2022-05-02",
        "2022-05-03",
        "2022-05-04",
        "2022-06-03",
        "2022-09-12",
        "2022-10-03",
        "2022-10-04",
        "2022-10-05",
        "2022-10-06",
        "2022-10-07",
        # 2023
        "2023-01-02",
        "2023-01-23",
        "2023-01-24",
        "2023-01-25",
        "2023-01-26",
        "2023-01-27",
        "2023-04-05",
        "2023-05-01",
        "2023-05-02",
        "2023-05-03",
        "2023-06-22",
        "2023-06-23",
        "2023-09-29",
        "2023-10-02",
        "2023-10-03",
        "2023-10-04",
        "2023-10-05",
        "2023-10-06",
        # 2024
        "2024-01-01",
        "2024-02-09",
        "2024-02-12",
        "2024-02-13",
        "2024-02-14",
        "2024-02-15",
        "2024-02-16",
        "2024-04-04",
        "2024-04-05",
        "2024-05-01",
        "2024-05-02",
        "2024-05-03",
        "2024-06-10",
        "2024-09-16",
        "2024-09-17",
        "2024-10-01",
        "2024-10-02",
        "2024-10-03",
        "2024-10-04",
        "2024-10-07",
        # 2025
        "2025-01-01",
        "2025-01-28",
        "2025-01-29",
        "2025-01-30",
        "2025-01-31",
        "2025-02-03",
        "2025-02-04",
        "2025-04-04",
        "2025-05-01",
        "2025-05-02",
        "2025-05-05",
        "2025-05-31",
        "2025-10-01",
        "2025-10-02",
        "2025-10-03",
        "2025-10-06",
        "2025-10-07",
        "2025-10-08",
        # 2026 (announced / typical pattern — refine when official notice lands)
        "2026-01-01",
        "2026-01-02",
        "2026-02-16",
        "2026-02-17",
        "2026-02-18",
        "2026-02-19",
        "2026-02-20",
        "2026-02-23",
        "2026-04-06",
        "2026-05-01",
        "2026-05-04",
        "2026-05-05",
        "2026-06-19",
        "2026-09-25",
        "2026-10-01",
        "2026-10-02",
        "2026-10-05",
        "2026-10-06",
        "2026-10-07",
        # 2027
        "2027-01-01",
        "2027-02-05",
        "2027-02-08",
        "2027-02-09",
        "2027-02-10",
        "2027-02-11",
        "2027-02-12",
        "2027-04-05",
        "2027-05-03",
        "2027-05-04",
        "2027-05-05",
        "2027-06-09",
        "2027-09-15",
        "2027-10-01",
        "2027-10-04",
        "2027-10-05",
        "2027-10-06",
        "2027-10-07",
        # 2028
        "2028-01-03",
        "2028-01-25",
        "2028-01-26",
        "2028-01-27",
        "2028-01-28",
        "2028-01-31",
        "2028-02-01",
        "2028-04-04",
        "2028-05-01",
        "2028-05-02",
        "2028-05-03",
        "2028-05-29",
        "2028-10-02",
        "2028-10-03",
        "2028-10-04",
        "2028-10-05",
        "2028-10-06",
    }
)

# Hong Kong (HKEX) — major holidays 2018–2028 (subset; good enough for research hygiene).
_HK_HOLIDAYS: frozenset[str] = frozenset(
    {
        "2018-01-01",
        "2018-02-16",
        "2018-02-19",
        "2018-03-30",
        "2018-04-02",
        "2018-04-05",
        "2018-05-01",
        "2018-05-22",
        "2018-06-18",
        "2018-07-02",
        "2018-09-25",
        "2018-10-01",
        "2018-10-17",
        "2018-12-25",
        "2018-12-26",
        "2019-01-01",
        "2019-02-05",
        "2019-02-06",
        "2019-02-07",
        "2019-04-05",
        "2019-04-19",
        "2019-04-22",
        "2019-05-01",
        "2019-05-13",
        "2019-06-07",
        "2019-07-01",
        "2019-10-01",
        "2019-10-07",
        "2019-12-25",
        "2019-12-26",
        "2020-01-01",
        "2020-01-27",
        "2020-01-28",
        "2020-04-10",
        "2020-04-13",
        "2020-04-30",
        "2020-05-01",
        "2020-06-25",
        "2020-07-01",
        "2020-10-01",
        "2020-10-02",
        "2020-10-26",
        "2020-12-25",
        "2021-01-01",
        "2021-02-12",
        "2021-02-15",
        "2021-04-02",
        "2021-04-05",
        "2021-04-06",
        "2021-05-19",
        "2021-06-14",
        "2021-07-01",
        "2021-09-22",
        "2021-10-01",
        "2021-10-14",
        "2021-12-27",
        "2022-01-31",
        "2022-02-01",
        "2022-02-02",
        "2022-02-03",
        "2022-04-05",
        "2022-04-15",
        "2022-04-18",
        "2022-05-02",
        "2022-05-09",
        "2022-06-03",
        "2022-07-01",
        "2022-09-12",
        "2022-10-04",
        "2022-12-26",
        "2022-12-27",
        "2023-01-02",
        "2023-01-23",
        "2023-01-24",
        "2023-01-25",
        "2023-04-05",
        "2023-04-07",
        "2023-04-10",
        "2023-05-01",
        "2023-05-26",
        "2023-06-22",
        "2023-07-03",
        "2023-09-30",
        "2023-10-02",
        "2023-10-23",
        "2023-12-25",
        "2023-12-26",
        "2024-01-01",
        "2024-02-12",
        "2024-02-13",
        "2024-03-29",
        "2024-04-01",
        "2024-04-04",
        "2024-05-01",
        "2024-05-15",
        "2024-06-10",
        "2024-07-01",
        "2024-09-18",
        "2024-10-01",
        "2024-10-11",
        "2024-12-25",
        "2024-12-26",
        "2025-01-01",
        "2025-01-29",
        "2025-01-30",
        "2025-01-31",
        "2025-04-04",
        "2025-04-18",
        "2025-04-21",
        "2025-05-01",
        "2025-05-05",
        "2025-05-31",
        "2025-07-01",
        "2025-10-01",
        "2025-10-07",
        "2025-12-25",
        "2025-12-26",
        "2026-01-01",
        "2026-02-17",
        "2026-02-18",
        "2026-02-19",
        "2026-04-03",
        "2026-04-06",
        "2026-05-01",
        "2026-05-25",
        "2026-06-19",
        "2026-07-01",
        "2026-10-01",
        "2026-10-19",
        "2026-12-25",
        "2027-01-01",
        "2027-02-08",
        "2027-02-09",
        "2027-03-26",
        "2027-03-29",
        "2027-04-05",
        "2027-05-03",
        "2027-06-09",
        "2027-07-01",
        "2027-10-01",
        "2027-12-27",
        "2028-01-03",
        "2028-01-26",
        "2028-01-27",
        "2028-04-14",
        "2028-04-17",
        "2028-05-01",
        "2028-05-24",
        "2028-06-28",
        "2028-10-02",
        "2028-12-25",
        "2028-12-26",
    }
)


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> dt.date:
    """Return the n-th weekday (Mon=0) in month. n=1 first, n=-1 last."""
    if n > 0:
        d = dt.date(year, month, 1)
        while d.weekday() != weekday:
            d += dt.timedelta(days=1)
        d += dt.timedelta(weeks=n - 1)
        return d
    # last
    if month == 12:
        d = dt.date(year + 1, 1, 1) - dt.timedelta(days=1)
    else:
        d = dt.date(year, month + 1, 1) - dt.timedelta(days=1)
    while d.weekday() != weekday:
        d -= dt.timedelta(days=1)
    return d


def _us_holidays(year: int) -> set[str]:
    """US equity market holidays (NYSE-style, observed on nearest weekday)."""
    out: set[dt.date] = set()

    def observed(d: dt.date) -> dt.date:
        if d.weekday() == 5:  # Sat → Fri
            return d - dt.timedelta(days=1)
        if d.weekday() == 6:  # Sun → Mon
            return d + dt.timedelta(days=1)
        return d

    out.add(observed(dt.date(year, 1, 1)))  # New Year
    out.add(_nth_weekday(year, 1, 0, 3))  # MLK 3rd Mon Jan
    out.add(_nth_weekday(year, 2, 0, 3))  # Presidents 3rd Mon Feb
    # Good Friday: Friday before Easter (Anonymous Gregorian algorithm)
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    easter = dt.date(year, month, day)
    out.add(easter - dt.timedelta(days=2))  # Good Friday
    out.add(_nth_weekday(year, 5, 0, -1))  # Memorial last Mon May
    if year >= 2021:
        out.add(observed(dt.date(year, 6, 19)))  # Juneteenth
    out.add(observed(dt.date(year, 7, 4)))  # Independence
    out.add(_nth_weekday(year, 9, 0, 1))  # Labor 1st Mon Sep
    out.add(_nth_weekday(year, 11, 3, 4))  # Thanksgiving 4th Thu Nov
    out.add(observed(dt.date(year, 12, 25)))  # Christmas
    return {d.isoformat() for d in out}


def _us_holiday_set(year_from: int = 2018, year_to: int = 2028) -> frozenset[str]:
    s: set[str] = set()
    for y in range(year_from, year_to + 1):
        s |= _us_holidays(y)
    return frozenset(s)


_US_HOLIDAYS = _us_holiday_set()


@dataclass(frozen=True, slots=True)
class SessionHours:
    """Regular cash-session wall-clock hours in the venue timezone (local)."""

    open_hm: tuple[int, int]
    close_hm: tuple[int, int]
    timezone: str
    lunch_break: tuple[tuple[int, int], tuple[int, int]] | None = None


# Regular sessions (local wall clock).
_SESSION: dict[str, SessionHours] = {
    "SSE": SessionHours((9, 30), (15, 0), "Asia/Shanghai", ((11, 30), (13, 0))),
    "SZSE": SessionHours((9, 30), (15, 0), "Asia/Shanghai", ((11, 30), (13, 0))),
    "HKEX": SessionHours((9, 30), (16, 0), "Asia/Hong_Kong", ((12, 0), (13, 0))),
    "NYSE": SessionHours((9, 30), (16, 0), "America/New_York"),
    "NASDAQ": SessionHours((9, 30), (16, 0), "America/New_York"),
    "AMEX": SessionHours((9, 30), (16, 0), "America/New_York"),
}


@dataclass(frozen=True, slots=True)
class TradingCalendar:
    """One exchange (or market-group primary) trading calendar."""

    code: str
    holidays: frozenset[str]
    session: SessionHours | None = None

    def is_weekend(self, day: dt.date) -> bool:
        return day.weekday() >= 5

    def is_holiday(self, day: dt.date) -> bool:
        return day.isoformat() in self.holidays

    def is_trading_day(self, day: dt.date) -> bool:
        return not self.is_weekend(day) and not self.is_holiday(day)

    def trading_days(self, start: dt.date, end: dt.date) -> list[dt.date]:
        if end < start:
            return []
        out: list[dt.date] = []
        d = start
        while d <= end:
            if self.is_trading_day(d):
                out.append(d)
            d += dt.timedelta(days=1)
        return out


def calendar_for(venue: str) -> TradingCalendar:
    """Resolve a venue or market group to a :class:`TradingCalendar`."""
    group = resolve_market_group(venue)
    if group == "ASHARE":
        code = "SSE"
    elif group == "US":
        code = "NYSE"
    elif group == "HK":
        code = "HKEX"
    elif group == "ETF":
        code = "NYSE"
    else:
        info = get_venue(venue)
        code = info.code if info else venue.strip().upper()
        # Map multi-venue aliases already expanded.
        if code in expand_venues(venue):
            code = expand_venues(venue)[0]

    if code in ("SSE", "SZSE"):
        return TradingCalendar(code, _CN_HOLIDAYS, _SESSION.get(code))
    if code == "HKEX":
        return TradingCalendar(code, _HK_HOLIDAYS, _SESSION.get(code))
    if code in ("NYSE", "NASDAQ", "AMEX"):
        return TradingCalendar(code, _US_HOLIDAYS, _SESSION.get(code))
    # Default: weekends only (no holiday table).
    info = get_venue(code)
    sess = _SESSION.get(code)
    return TradingCalendar(code, frozenset(), sess or (
        SessionHours((0, 0), (23, 59), info.timezone) if info else None
    ))


def _bar_date_utc(ts_ns: int) -> dt.date:
    return dt.datetime.fromtimestamp(int(ts_ns) // _NS_PER_S, tz=dt.UTC).date()


def _is_flat_halt(row: Sequence) -> bool:
    """True when OHLCV looks like an untraded / halted daily print (vol=0 and flat OHLC)."""
    if len(row) < 6:
        return False
    _ts, o, h, lo, c, v = row[0], row[1], row[2], row[3], row[4], row[5]
    try:
        if float(v) > 0:
            return False
        return float(o) == float(h) == float(lo) == float(c)
    except (TypeError, ValueError):
        return False


def _is_zero_volume(row: Sequence) -> bool:
    if len(row) < 6:
        return False
    try:
        return float(row[5]) == 0.0
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True, slots=True)
class FilterStats:
    """How many bars a filter pass removed."""

    input_rows: int
    output_rows: int
    dropped_weekend: int = 0
    dropped_holiday: int = 0
    dropped_halt: int = 0
    dropped_zero_volume: int = 0

    @property
    def dropped(self) -> int:
        return self.input_rows - self.output_rows


def filter_trading_bars(
    bars: Iterable[BarLike],
    venue: str,
    *,
    drop_holidays: bool = True,
    drop_weekends: bool = True,
    drop_flat_halts: bool = True,
    drop_zero_volume: bool = False,
) -> tuple[list, FilterStats]:
    """Filter a bar series for equity-session hygiene.

    Parameters
    ----------
    bars:
        Close-only ``(ts, c)``, OHLC, or OHLCV rows (``ts`` = bar close ns).
    venue:
        Concrete venue or market group (selects holiday calendar).
    drop_flat_halts:
        Drop OHLCV rows with volume==0 and o==h==l==c (typical halt / untraded day).
    drop_zero_volume:
        Drop any OHLCV row with volume==0 (stricter; may remove valid limit-up days with
        reported zero in some vendors — off by default).
    """
    cal = calendar_for(venue)
    out: list = []
    n_we = n_hol = n_halt = n_zv = 0
    rows = list(bars)
    for row in rows:
        if not row:
            continue
        ts = int(row[0])
        day = _bar_date_utc(ts)
        if drop_weekends and cal.is_weekend(day):
            n_we += 1
            continue
        if drop_holidays and cal.is_holiday(day):
            n_hol += 1
            continue
        if drop_flat_halts and _is_flat_halt(row):
            n_halt += 1
            continue
        if drop_zero_volume and _is_zero_volume(row):
            n_zv += 1
            continue
        out.append(row)
    stats = FilterStats(
        input_rows=len(rows),
        output_rows=len(out),
        dropped_weekend=n_we,
        dropped_holiday=n_hol,
        dropped_halt=n_halt,
        dropped_zero_volume=n_zv,
    )
    return out, stats


def is_trading_day(venue: str, day: dt.date | str) -> bool:
    """Convenience: is ``day`` a trading day on ``venue``?"""
    if isinstance(day, str):
        day = dt.date.fromisoformat(day)
    return calendar_for(venue).is_trading_day(day)


def session_hours(venue: str) -> SessionHours | None:
    """Regular cash session hours for ``venue``, or ``None`` if unknown."""
    return calendar_for(venue).session


def _hm_to_minutes(hm: tuple[int, int]) -> int:
    return hm[0] * 60 + hm[1]


def bar_local_time(ts_ns: int, timezone: str) -> dt.datetime:
    """Convert bar close ns (UTC) to a timezone-aware local datetime."""
    from zoneinfo import ZoneInfo

    utc = dt.datetime.fromtimestamp(int(ts_ns) // _NS_PER_S, tz=dt.UTC)
    return utc.astimezone(ZoneInfo(timezone))


def in_session(ts_ns: int, venue: str, *, include_lunch_break: bool = False) -> bool:
    """True when ``ts_ns`` falls inside the regular cash session (local wall clock).

    Lunch break (A股 11:30–13:00, 港股 12:00–13:00) is **excluded** unless
    ``include_lunch_break=True``. Unknown venues return True (no filter).
    """
    cal = calendar_for(venue)
    sess = cal.session
    if sess is None:
        return True
    local = bar_local_time(ts_ns, sess.timezone)
    if not cal.is_trading_day(local.date()):
        return False
    mins = local.hour * 60 + local.minute
    open_m = _hm_to_minutes(sess.open_hm)
    close_m = _hm_to_minutes(sess.close_hm)
    if mins < open_m or mins > close_m:
        return False
    if not include_lunch_break and sess.lunch_break is not None:
        lb0, lb1 = sess.lunch_break
        if _hm_to_minutes(lb0) <= mins < _hm_to_minutes(lb1):
            return False
    return True


def filter_session_bars(
    bars: Iterable[BarLike],
    venue: str,
    *,
    include_lunch_break: bool = False,
) -> tuple[list, FilterStats]:
    """Keep only bars whose close timestamp is inside the regular session.

    Also applies weekend/holiday filter via :func:`filter_trading_bars` first.
    Useful for **intraday** equity series (1m/5m/1h). Daily bars usually pass unchanged
    (close stamps fall near session end).
    """
    # Calendar day hygiene first.
    day_ok, day_stats = filter_trading_bars(
        bars, venue, drop_flat_halts=False, drop_zero_volume=False
    )
    out: list = []
    dropped_session = 0
    for row in day_ok:
        if in_session(int(row[0]), venue, include_lunch_break=include_lunch_break):
            out.append(row)
        else:
            dropped_session += 1
    stats = FilterStats(
        input_rows=day_stats.input_rows,
        output_rows=len(out),
        dropped_weekend=day_stats.dropped_weekend,
        dropped_holiday=day_stats.dropped_holiday,
        dropped_halt=day_stats.dropped_halt,
        dropped_zero_volume=day_stats.dropped_zero_volume + dropped_session,
    )
    return out, stats


__all__ = [
    "FilterStats",
    "SessionHours",
    "TradingCalendar",
    "bar_local_time",
    "calendar_for",
    "filter_session_bars",
    "filter_trading_bars",
    "in_session",
    "is_trading_day",
    "session_hours",
]
