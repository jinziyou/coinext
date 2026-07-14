"""FX helpers — pair download and FxBook for multi-currency research.

Status: verified.
"""

from __future__ import annotations

import bisect
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .venues import DEFAULT_FX_PAIRS, get_venue, instrument_spec

if TYPE_CHECKING:
    from .lake import DataLake

_NS_PER_S = 1_000_000_000

# Yahoo Finance FX tickers (no suffix) → (base, quote) meaning 1 base = rate quote.
# e.g. USDCNY=X → 1 USD = r CNY.
_YAHOO_FX: dict[str, tuple[str, str]] = {
    "USDCNY": ("USD", "CNY"),
    "USDHKD": ("USD", "HKD"),
    "USDCNH": ("USD", "CNY"),  # treat offshore CNH ≈ CNY for research
    "EURUSD": ("EUR", "USD"),
    "GBPUSD": ("GBP", "USD"),
    "USDJPY": ("USD", "JPY"),
    "CNYHKD": ("CNY", "HKD"),
    "HKDCNY": ("HKD", "CNY"),
}

# Static fallback rates when no series loaded (approx mid-2024/25 levels; research only).
_FALLBACK: dict[tuple[str, str], float] = {
    ("USD", "USD"): 1.0,
    ("CNY", "CNY"): 1.0,
    ("HKD", "HKD"): 1.0,
    ("USD", "CNY"): 7.25,
    ("CNY", "USD"): 1.0 / 7.25,
    ("USD", "HKD"): 7.80,
    ("HKD", "USD"): 1.0 / 7.80,
    ("CNY", "HKD"): 7.80 / 7.25,
    ("HKD", "CNY"): 7.25 / 7.80,
    ("EUR", "USD"): 1.08,
    ("USD", "EUR"): 1.0 / 1.08,
    ("GBP", "USD"): 1.27,
    ("USD", "GBP"): 1.0 / 1.27,
    ("USD", "JPY"): 150.0,
    ("JPY", "USD"): 1.0 / 150.0,
}


@dataclass
class FxCurve:
    """Time series of FX rates for one directed pair (base→quote)."""

    base: str
    quote: str
    # Sorted (ts_ns, rate) where rate = units of quote per 1 base.
    points: list[tuple[int, float]] = field(default_factory=list)

    def add(self, ts_ns: int, rate: float) -> None:
        if rate <= 0:
            return
        self.points.append((int(ts_ns), float(rate)))
        self.points.sort(key=lambda p: p[0])

    def rate_asof(self, ts_ns: int) -> float | None:
        if not self.points:
            return None
        ts = list(zip(*self.points, strict=False))[0]
        i = bisect.bisect_right(ts, int(ts_ns)) - 1
        if i < 0:
            return self.points[0][1]  # before first: clamp
        return self.points[i][1]


@dataclass
class FxBook:
    """Collection of FX curves with inverse synthesis and fallbacks."""

    curves: dict[tuple[str, str], FxCurve] = field(default_factory=dict)
    use_fallback: bool = True

    @classmethod
    def with_defaults(cls) -> FxBook:
        book = cls()
        for (b, q), r in _FALLBACK.items():
            if b == q:
                continue
            book.curves[(b, q)] = FxCurve(b, q, [(0, r)])
        return book

    def set_rate(self, base: str, quote: str, ts_ns: int, rate: float) -> None:
        base, quote = base.upper(), quote.upper()
        key = (base, quote)
        if key not in self.curves:
            self.curves[key] = FxCurve(base, quote)
        self.curves[key].add(ts_ns, rate)
        # Maintain inverse.
        inv = (quote, base)
        if inv not in self.curves:
            self.curves[inv] = FxCurve(quote, base)
        self.curves[inv].add(ts_ns, 1.0 / rate)

    def rate(self, base: str, quote: str, ts_ns: int | None = None) -> float:
        """Units of ``quote`` per 1 ``base`` at ``ts_ns`` (or latest / fallback)."""
        base, quote = base.upper(), quote.upper()
        if base == quote:
            return 1.0
        ts = int(ts_ns) if ts_ns is not None else 2**62
        curve = self.curves.get((base, quote))
        if curve is not None:
            r = curve.rate_asof(ts)
            if r is not None:
                return r
        # Try triangulation via USD.
        if base != "USD" and quote != "USD":
            try:
                b_usd = self.rate(base, "USD", ts)
                usd_q = self.rate("USD", quote, ts)
                return b_usd * usd_q
            except KeyError:
                pass
        if self.use_fallback and (base, quote) in _FALLBACK:
            return _FALLBACK[(base, quote)]
        raise KeyError(f"no FX rate for {base}/{quote}")

    def load_yahoo(
        self,
        pairs: Sequence[str] | None = None,
        *,
        days: float = 365.0,
        pause: float = 0.15,
        apply_calendar: bool = False,
    ) -> dict[str, int]:
        """Download FX pairs from Yahoo (``USDCNY=X`` style) into the book.

        ``pairs`` entries are bare codes like ``USDCNY`` or full ``USDCNY=X``.
        Returns ``{pair: n_bars}``. FX trades nearly 24/5 — calendar filter is off by default.
        """
        import time

        from .equity_download import download_equity_bars
        from .venues import DEFAULT_FX_PAIRS

        pairs = list(pairs) if pairs is not None else list(DEFAULT_FX_PAIRS)
        out: dict[str, int] = {}
        for i, raw in enumerate(pairs):
            code = raw.strip().upper().removesuffix("=X")
            if code not in _YAHOO_FX:
                raise ValueError(f"unknown FX pair {raw!r}; known: {sorted(_YAHOO_FX)}")
            b, q = _YAHOO_FX[code]
            ticker = f"{code}=X"
            rows = download_equity_bars(
                code,
                "1d",
                venue="FX",
                days=days,
                ticker=ticker,
                apply_calendar=apply_calendar,
            )
            for ts, _o, _h, _lo, c, _v in rows:
                self.set_rate(b, q, ts, c)
            out[code] = len(rows)
            if pause and i + 1 < len(pairs):
                time.sleep(pause)
        return out

    def load_from_lake(
        self,
        lake: Any | None = None,
        pairs: Sequence[str] | None = None,
        *,
        interval: str = "1d",
        lake_root: str | None = None,
    ) -> dict[str, int]:
        """Load FX OHLCV closes from the Parquet lake (venue=FX).

        Returns ``{pair: n_rows}``. Missing series are skipped.
        """
        from .lake import DataLake
        from .venues import DEFAULT_FX_PAIRS

        lake = lake or DataLake(lake_root)
        pairs = list(pairs) if pairs is not None else list(DEFAULT_FX_PAIRS)
        out: dict[str, int] = {}
        for raw in pairs:
            code = raw.strip().upper().removesuffix("=X")
            if code not in _YAHOO_FX:
                continue
            b, q = _YAHOO_FX[code]
            rows = lake.read_ohlcv("FX", code, interval)
            for row in rows:
                ts, c = int(row[0]), float(row[4] if len(row) >= 5 else row[1])
                self.set_rate(b, q, ts, c)
            if rows:
                out[code] = len(rows)
        return out

    @classmethod
    def from_lake(
        cls,
        lake: Any | None = None,
        pairs: Sequence[str] | None = None,
        *,
        lake_root: str | None = None,
        use_fallback: bool = True,
    ) -> FxBook:
        """Build a book from lake FX series, falling back to static mid rates."""
        book = cls.with_defaults() if use_fallback else cls(use_fallback=False)
        book.load_from_lake(lake, pairs, lake_root=lake_root)
        return book

    def convert_amount(
        self, amount: float, from_ccy: str, to_ccy: str, ts_ns: int | None = None
    ) -> float:
        return float(amount) * self.rate(from_ccy, to_ccy, ts_ns)


def convert_bars(
    bars: Iterable[tuple],
    book: FxBook,
    *,
    quote: str,
    base: str,
) -> list[tuple]:
    """Convert OHLC(V) prices from ``quote`` currency into ``base`` currency.

    Each bar's OHLC is multiplied by ``rate(quote→base)`` as-of the bar timestamp.
    Volume is left unchanged (share count). Close-only rows are supported.
    """
    quote, base = quote.upper(), base.upper()
    out: list[tuple] = []
    for row in bars:
        ts = int(row[0])
        fx = book.rate(quote, base, ts)
        if len(row) == 2:
            out.append((ts, float(row[1]) * fx))
        elif len(row) == 5:
            ts, o, h, lo, c = row
            out.append((int(ts), o * fx, h * fx, lo * fx, c * fx))
        elif len(row) >= 6:
            ts, o, h, lo, c, v = row[0], row[1], row[2], row[3], row[4], row[5]
            out.append((int(ts), o * fx, h * fx, lo * fx, c * fx, float(v)))
        else:
            raise ValueError(f"unsupported bar shape len={len(row)}")
    return out


def venue_currency(venue: str) -> str:
    """Primary listing currency for a venue (from the catalog / instrument_spec)."""
    info = get_venue(venue)
    if info is not None:
        return info.currency
    return instrument_spec(venue).currency


def revalue_bar_map(
    bars: Mapping[str, list[tuple]],
    *,
    symbol_venues: Mapping[str, str],
    book: FxBook,
    base: str = "USD",
) -> dict[str, list[tuple]]:
    """Convert each series in ``bars`` from its venue currency into ``base``.

    ``symbol_venues`` maps bar-dict keys → concrete venue codes (e.g. ``SSE:600519`` → ``SSE``).
    """
    base = base.upper()
    out: dict[str, list[tuple]] = {}
    for key, series in bars.items():
        vcode = symbol_venues.get(key)
        if vcode is None and ":" in key:
            vcode = key.split(":", 1)[0]
        if vcode is None:
            vcode = "NASDAQ"
        q = venue_currency(vcode)
        out[key] = convert_bars(series, book, quote=q, base=base) if q != base else list(series)
    return out


def mark_portfolio_value(
    positions: Mapping[str, float],
    marks: Mapping[str, float],
    *,
    symbol_venues: Mapping[str, str],
    book: FxBook,
    base: str = "USD",
    cash: float = 0.0,
    cash_ccy: str | None = None,
    ts_ns: int | None = None,
) -> float:
    """Mark-to-market portfolio in ``base`` currency.

    ``positions`` / ``marks`` are in local listing units; FX converts notionals.
    """
    base = base.upper()
    total = book.convert_amount(cash, cash_ccy or base, base, ts_ns)
    for sym, qty in positions.items():
        px = marks.get(sym)
        if px is None:
            continue
        vcode = symbol_venues.get(sym, "NASDAQ")
        q = venue_currency(vcode)
        notional_local = float(qty) * float(px)
        total += book.convert_amount(notional_local, q, base, ts_ns)
    return total


def yahoo_fx_ticker(pair: str) -> str:
    """``USDCNY`` → ``USDCNY=X``."""
    code = pair.strip().upper().removesuffix("=X")
    if code not in _YAHOO_FX:
        raise ValueError(f"unknown FX pair {pair!r}; known: {sorted(_YAHOO_FX)}")
    return f"{code}=X"


def download_fx_to_lake(
    lake: DataLake | Any,
    pairs: Sequence[str] | None = None,
    *,
    days: float = 365.0,
    interval: str = "1d",
    pause: float = 0.15,
    apply_calendar: bool = False,
) -> dict[str, int]:
    """Download Yahoo FX pairs into the lake under ``venue=FX``.

    Returns ``{pair: rows_written}``. Also suitable for seeding ``data/sample``.
    """
    from .equity_download import download_equity_to_lake

    pairs = list(pairs) if pairs is not None else list(DEFAULT_FX_PAIRS)
    # download_equity_to_lake normalizes via lake_symbol(FX, …) → bare pair.
    return download_equity_to_lake(
        lake,
        [p.strip().upper().removesuffix("=X") for p in pairs],
        interval=interval,
        venue="FX",
        days=days,
        pause=pause,
        apply_calendar=apply_calendar,
    )


def load_fx_book(
    *,
    lake_root: str | None = None,
    pairs: Sequence[str] | None = None,
    prefer_lake: bool = True,
    yahoo_if_empty: bool = False,
    days: float = 365.0,
) -> FxBook:
    """Preferred research constructor: lake first, optional Yahoo fill-in, then fallbacks."""
    book = FxBook.with_defaults()
    if prefer_lake:
        loaded = book.load_from_lake(pairs=pairs, lake_root=lake_root)
    else:
        loaded = {}
    if yahoo_if_empty and not loaded:
        try:
            book.load_yahoo(pairs, days=days)
        except Exception:
            pass  # keep fallbacks
    return book


__all__ = [
    "DEFAULT_FX_PAIRS",
    "FxBook",
    "FxCurve",
    "convert_bars",
    "download_fx_to_lake",
    "load_fx_book",
    "mark_portfolio_value",
    "revalue_bar_map",
    "venue_currency",
    "yahoo_fx_ticker",
]
