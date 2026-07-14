"""Global venue registry — crypto + mainstream equity markets.

Venues are free-form strings in the domain model (``InstrumentId.venue``). This module is the
**research-side catalog**: canonical codes for the data lake partition key, Yahoo Finance
symbology for public equity history, and human metadata for CLI / catalog listing.

First-class **research** markets (download + backtest; live broker adapters still deferred):

* **A-shares (A股)** — ``SSE`` / ``SZSE`` (aliases ``ASHARE``, ``A股``; code auto-routes 6xxxx→SSE)
* **ETFs** — trade on the same venues; ``--symbols @etf`` expands liquid ETF presets
* **US equities (美股)** — ``NYSE`` / ``NASDAQ`` / ``AMEX`` (aliases ``US``, ``美股``)
* **Hong Kong (港股)** — ``HKEX`` (aliases ``HK``, ``港股``)

Live execution adapters remain separate (only Binance is wired today). Equity venues here are
for **data + backtest** until a broker adapter lands.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

AssetFamily = Literal["crypto", "equity", "index"]


@dataclass(frozen=True, slots=True)
class VenueInfo:
    """One trading venue / listing market in the Coinext catalog."""

    code: str
    """Lake / domain venue code (uppercase), e.g. ``NYSE``, ``HKEX``, ``BINANCE``."""

    name: str
    """Human-readable exchange name."""

    region: str
    """Geographic region label (e.g. ``Americas``, ``Asia-Pacific``, ``Europe``)."""

    country: str
    """ISO-ish country or multi-country label."""

    currency: str
    """Primary listing / quote currency (ISO 4217 or crypto quote)."""

    asset_family: AssetFamily
    """High-level asset family for routing downloaders and default instruments."""

    timezone: str
    """IANA timezone of the primary session."""

    mic: str | None = None
    """ISO 10383 MIC when applicable (stock exchanges)."""

    yahoo_suffix: str = ""
    """Yahoo Finance ticker suffix (e.g. ``.HK``, ``.SS``). Empty for US and crypto."""

    data_source: str = "none"
    """Public history source used by ``coinext download``: ``binance`` | ``yahoo`` | ``none``."""

    aliases: tuple[str, ...] = ()
    """Alternate codes accepted by :func:`resolve_venue` (case-insensitive)."""

    notes: str = ""
    """Optional operator note."""


# ---------------------------------------------------------------------------
# Canonical catalog
# ---------------------------------------------------------------------------

_VENUES: tuple[VenueInfo, ...] = (
    # --- Crypto (existing research + live path) ---
    VenueInfo(
        code="BINANCE",
        name="Binance",
        region="Global",
        country="Global",
        currency="USDT",
        asset_family="crypto",
        timezone="UTC",
        data_source="binance",
        notes="Reference crypto adapter; public klines via REST.",
    ),
    VenueInfo(
        code="COINBASE",
        name="Coinbase",
        region="Americas",
        country="US",
        currency="USD",
        asset_family="crypto",
        timezone="America/New_York",
        data_source="none",
        notes="Venue id reserved; no public downloader yet.",
    ),
    VenueInfo(
        code="OKX",
        name="OKX",
        region="Global",
        country="Global",
        currency="USDT",
        asset_family="crypto",
        timezone="UTC",
        data_source="none",
        notes="Venue id reserved; no public downloader yet.",
    ),
    # --- Americas equities ---
    VenueInfo(
        code="NYSE",
        name="New York Stock Exchange",
        region="Americas",
        country="US",
        currency="USD",
        asset_family="equity",
        timezone="America/New_York",
        mic="XNYS",
        yahoo_suffix="",
        data_source="yahoo",
        aliases=("XNYS", "NYSEARCA", "ARCA"),
    ),
    VenueInfo(
        code="NASDAQ",
        name="Nasdaq Stock Market",
        region="Americas",
        country="US",
        currency="USD",
        asset_family="equity",
        timezone="America/New_York",
        mic="XNAS",
        yahoo_suffix="",
        data_source="yahoo",
        aliases=("XNAS", "NASDAQGS", "NASDAQGM", "NASDAQCM"),
    ),
    VenueInfo(
        code="AMEX",
        name="NYSE American (AMEX)",
        region="Americas",
        country="US",
        currency="USD",
        asset_family="equity",
        timezone="America/New_York",
        mic="XASE",
        yahoo_suffix="",
        data_source="yahoo",
        aliases=("XASE", "NYSEAMERICAN", "NYSE MKT"),
        notes="Smaller listings + some ETFs/closed-end funds. Large US ETFs often list on ARCA "
        "(use NYSE venue; ARCA is an NYSE alias).",
    ),
    VenueInfo(
        code="TSX",
        name="Toronto Stock Exchange",
        region="Americas",
        country="CA",
        currency="CAD",
        asset_family="equity",
        timezone="America/Toronto",
        mic="XTSE",
        yahoo_suffix=".TO",
        data_source="yahoo",
        aliases=("XTSE", "TSE_CA"),
    ),
    VenueInfo(
        code="B3",
        name="B3 (Brasil Bolsa Balcão)",
        region="Americas",
        country="BR",
        currency="BRL",
        asset_family="equity",
        timezone="America/Sao_Paulo",
        mic="BVMF",
        yahoo_suffix=".SA",
        data_source="yahoo",
        aliases=("BVMF", "BOVESPA"),
    ),
    # --- Europe equities ---
    VenueInfo(
        code="LSE",
        name="London Stock Exchange",
        region="Europe",
        country="GB",
        currency="GBP",
        asset_family="equity",
        timezone="Europe/London",
        mic="XLON",
        yahoo_suffix=".L",
        data_source="yahoo",
        aliases=("XLON", "LON"),
    ),
    VenueInfo(
        code="XETRA",
        name="Deutsche Börse Xetra",
        region="Europe",
        country="DE",
        currency="EUR",
        asset_family="equity",
        timezone="Europe/Berlin",
        mic="XETR",
        yahoo_suffix=".DE",
        data_source="yahoo",
        aliases=("XETR", "FRA", "FSE"),
    ),
    VenueInfo(
        code="EURONEXT",
        name="Euronext (Paris primary)",
        region="Europe",
        country="EU",
        currency="EUR",
        asset_family="equity",
        timezone="Europe/Paris",
        mic="XPAR",
        yahoo_suffix=".PA",
        data_source="yahoo",
        aliases=("XPAR", "EPA", "PAR"),
        notes="Default Yahoo suffix is Paris (.PA). Amsterdam uses .AS, Brussels .BR, Lisbon .LS.",
    ),
    VenueInfo(
        code="SIX",
        name="SIX Swiss Exchange",
        region="Europe",
        country="CH",
        currency="CHF",
        asset_family="equity",
        timezone="Europe/Zurich",
        mic="XSWX",
        yahoo_suffix=".SW",
        data_source="yahoo",
        aliases=("XSWX", "SWX"),
    ),
    # --- Asia-Pacific equities ---
    VenueInfo(
        code="HKEX",
        name="Hong Kong Exchanges and Clearing",
        region="Asia-Pacific",
        country="HK",
        currency="HKD",
        asset_family="equity",
        timezone="Asia/Hong_Kong",
        mic="XHKG",
        yahoo_suffix=".HK",
        data_source="yahoo",
        aliases=("XHKG", "HKG", "SEHK", "HK", "港股"),
        notes="Yahoo codes are 4-digit zero-padded, e.g. 0700.HK for Tencent. ETFs: 2800, 2828.",
    ),
    VenueInfo(
        code="SSE",
        name="Shanghai Stock Exchange",
        region="Asia-Pacific",
        country="CN",
        currency="CNY",
        asset_family="equity",
        timezone="Asia/Shanghai",
        mic="XSHG",
        yahoo_suffix=".SS",
        data_source="yahoo",
        aliases=("XSHG", "SHA", "SH", "SHSE"),
        notes="A-shares + SSE-listed ETFs (51xxxx/56xxxx/58xxxx). Pair with SZSE under ASHARE.",
    ),
    VenueInfo(
        code="SZSE",
        name="Shenzhen Stock Exchange",
        region="Asia-Pacific",
        country="CN",
        currency="CNY",
        asset_family="equity",
        timezone="Asia/Shanghai",
        mic="XSHE",
        yahoo_suffix=".SZ",
        data_source="yahoo",
        aliases=("XSHE", "SHE", "SZ", "SZSC"),
        notes="A-shares (00xxxx/30xxxx) + SZSE ETFs (15xxxx/16xxxx). Pair with SSE under ASHARE.",
    ),
    VenueInfo(
        code="TSE",
        name="Tokyo Stock Exchange (JPX)",
        region="Asia-Pacific",
        country="JP",
        currency="JPY",
        asset_family="equity",
        timezone="Asia/Tokyo",
        mic="XTKS",
        yahoo_suffix=".T",
        data_source="yahoo",
        aliases=("XTKS", "JPX", "TYO"),
    ),
    VenueInfo(
        code="KRX",
        name="Korea Exchange",
        region="Asia-Pacific",
        country="KR",
        currency="KRW",
        asset_family="equity",
        timezone="Asia/Seoul",
        mic="XKRX",
        yahoo_suffix=".KS",
        data_source="yahoo",
        aliases=("XKRX", "KSE"),
        notes="KOSDAQ listings often use .KQ on Yahoo.",
    ),
    VenueInfo(
        code="TWSE",
        name="Taiwan Stock Exchange",
        region="Asia-Pacific",
        country="TW",
        currency="TWD",
        asset_family="equity",
        timezone="Asia/Taipei",
        mic="XTAI",
        yahoo_suffix=".TW",
        data_source="yahoo",
        aliases=("XTAI", "TAI"),
    ),
    VenueInfo(
        code="SGX",
        name="Singapore Exchange",
        region="Asia-Pacific",
        country="SG",
        currency="SGD",
        asset_family="equity",
        timezone="Asia/Singapore",
        mic="XSES",
        yahoo_suffix=".SI",
        data_source="yahoo",
        aliases=("XSES",),
    ),
    VenueInfo(
        code="ASX",
        name="Australian Securities Exchange",
        region="Asia-Pacific",
        country="AU",
        currency="AUD",
        asset_family="equity",
        timezone="Australia/Sydney",
        mic="XASX",
        yahoo_suffix=".AX",
        data_source="yahoo",
        aliases=("XASX",),
    ),
    VenueInfo(
        code="NSE",
        name="National Stock Exchange of India",
        region="Asia-Pacific",
        country="IN",
        currency="INR",
        asset_family="equity",
        timezone="Asia/Kolkata",
        mic="XNSE",
        yahoo_suffix=".NS",
        data_source="yahoo",
        aliases=("XNSE",),
        notes="BSE listings use .BO on Yahoo; this catalog entry is NSE-primary.",
    ),
    # --- Index composite (major benchmarks; Yahoo caret symbols) ---
    VenueInfo(
        code="INDEX",
        name="Global equity indices (Yahoo caret symbols)",
        region="Global",
        country="Global",
        currency="USD",
        asset_family="index",
        timezone="UTC",
        yahoo_suffix="",
        data_source="yahoo",
        notes=(
            "Use Yahoo index tickers as symbols, e.g. ^GSPC, ^DJI, ^IXIC, ^HSI, ^N225, "
            "^FTSE, ^GDAXI, ^FCHI, 000001.SS."
        ),
    ),
    # --- FX (Yahoo =X pairs; multi-currency equity research) ---
    VenueInfo(
        code="FX",
        name="FX spot pairs (Yahoo =X)",
        region="Global",
        country="Global",
        currency="USD",
        asset_family="index",
        timezone="UTC",
        yahoo_suffix="",
        data_source="yahoo",
        aliases=("FOREX", "CURRENCY"),
        notes="Lake symbols are bare pairs (USDCNY, USDHKD); Yahoo tickers are PAIR=X.",
    ),
)

_BY_CODE: dict[str, VenueInfo] = {v.code: v for v in _VENUES}
_ALIAS_TO_CODE: dict[str, str] = {}
for _v in _VENUES:
    _ALIAS_TO_CODE[_v.code.upper()] = _v.code
    for _a in _v.aliases:
        _ALIAS_TO_CODE[_a.upper()] = _v.code

# Liquid blue-chip / benchmark presets for research downloads and multi-symbol demos.
# Keys are canonical venue codes; values are lake-facing symbols (not always Yahoo form).
DEFAULT_UNIVERSES: dict[str, tuple[str, ...]] = {
    "BINANCE": ("BTCUSDT", "ETHUSDT"),
    # 美股 (US equities)
    "NYSE": ("JPM", "XOM", "BA", "V", "DIS", "JNJ", "WMT"),
    "NASDAQ": ("AAPL", "MSFT", "GOOGL", "NVDA", "AMZN", "META", "TSLA"),
    "AMEX": ("SPY", "GLD"),  # illustrative; large ETFs also list on ARCA/NYSE
    "TSX": ("RY", "SHOP", "ENB"),
    "B3": ("PETR4", "VALE3", "ITUB4"),
    "LSE": ("SHEL", "HSBA", "BP", "AZN"),
    "XETRA": ("SAP", "SIE", "ALV"),
    "EURONEXT": ("MC", "OR", "AIR"),
    "SIX": ("NESN", "ROG", "UBSG"),
    # 港股 (HK equities)
    "HKEX": ("0700", "0941", "1299", "0388", "2318", "0005", "1810"),
    # A股 (mainland China A-shares)
    "SSE": ("600519", "601318", "600036", "601012", "600900", "601166"),
    "SZSE": ("000001", "300750", "002594", "000858", "002415", "300059"),
    "TSE": ("7203", "6758", "9984", "8306"),
    "KRX": ("005930", "000660"),
    "TWSE": ("2330", "2317"),
    "SGX": ("D05", "O39"),
    "ASX": ("BHP", "CBA", "CSL"),
    "NSE": ("RELIANCE", "TCS", "INFY"),
    "INDEX": (
        "^GSPC",  # S&P 500
        "^DJI",  # Dow Jones
        "^IXIC",  # Nasdaq Composite
        "^HSI",  # Hang Seng
        "^N225",  # Nikkei 225
        "^FTSE",  # FTSE 100
        "^GDAXI",  # DAX
        "^FCHI",  # CAC 40
        "000001.SS",  # SSE Composite
        "399001.SZ",  # SZSE Component
    ),
    "FX": ("USDCNY", "USDHKD", "EURUSD"),
}

# Liquid ETF presets (``--symbols @etf``). Same lake partitions as equities on each venue.
ETF_UNIVERSES: dict[str, tuple[str, ...]] = {
    # US ETFs (Yahoo bare tickers; ARCA listings use NYSE venue code)
    "NYSE": ("SPY", "IVV", "VOO", "IWM", "GLD", "SLV", "TLT", "HYG", "EEM", "VTI"),
    "NASDAQ": ("QQQ", "TQQQ", "SQQQ", "IBIT"),
    "AMEX": ("SPY", "GLD"),
    # A-share ETFs (SSE 51/56/58xxxx, SZSE 15/16xxxx)
    "SSE": ("510050", "510300", "510500", "588000", "511010", "512100"),
    "SZSE": ("159915", "159919", "159922", "159901", "159949"),
    # HK ETFs (Tracker Fund, HSCEI, Hang Seng TECH, ChinaAMC CSI 300)
    "HKEX": ("2800", "2828", "3067", "3033", "2801"),
}

# Virtual market groups → one or more concrete venues (CLI / multi-download helpers).
# Keys are uppercase group codes; aliases map via MARKET_GROUP_ALIASES.
MARKET_GROUPS: dict[str, tuple[str, ...]] = {
    "ASHARE": ("SSE", "SZSE"),  # A股
    "US": ("NASDAQ", "NYSE", "AMEX"),  # 美股
    "HK": ("HKEX",),  # 港股 (also a direct HKEX alias)
    "ETF": ("NYSE", "NASDAQ", "SSE", "SZSE", "HKEX"),  # cross-market ETF download target
}

MARKET_GROUP_ALIASES: dict[str, str] = {
    "ASHARE": "ASHARE",
    "ASHARES": "ASHARE",
    "A": "ASHARE",
    "CN": "ASHARE",
    "A股": "ASHARE",
    "沪深": "ASHARE",
    "US": "US",
    "USA": "US",
    "美股": "US",
    "HK": "HK",
    "港股": "HK",
    "HONGKONG": "HK",
    "ETF": "ETF",
    "ETFS": "ETF",
}

# Compact sample-lake set: core equities + one ETF per focus market + indices + FX.
SAMPLE_EQUITY_SERIES: tuple[tuple[str, str], ...] = (
    ("NASDAQ", "AAPL"),
    ("NYSE", "JPM"),
    ("NYSE", "SPY"),  # US ETF
    ("HKEX", "0700"),
    ("HKEX", "2800"),  # HK ETF (Tracker Fund)
    ("SSE", "600519"),
    ("SSE", "510300"),  # A-share ETF (CSI 300)
    ("SZSE", "000001"),
    ("TSE", "7203"),
    ("LSE", "SHEL"),
    ("INDEX", "^GSPC"),
    ("INDEX", "^HSI"),
    ("FX", "USDCNY"),
    ("FX", "USDHKD"),
)

# Split/dividend-adjusted daily series (interval=1d_adj) for offline 前复权 demos.
SAMPLE_ADJ_SERIES: tuple[tuple[str, str], ...] = (
    ("SSE", "600519"),
    ("NASDAQ", "AAPL"),
    ("HKEX", "0700"),
)

# Default FX pairs for multi-currency equity research (Yahoo =X).
DEFAULT_FX_PAIRS: tuple[str, ...] = ("USDCNY", "USDHKD", "EURUSD")


def all_venues() -> list[VenueInfo]:
    """Return the full venue catalog in declaration order."""
    return list(_VENUES)


def equity_venues() -> list[VenueInfo]:
    """Mainstream stock exchanges (asset_family == equity)."""
    return [v for v in _VENUES if v.asset_family == "equity"]


def default_universe(venue: str) -> tuple[str, ...]:
    """Return the liquid default symbol list for ``venue`` (empty if none registered).

    Market groups expand to the concatenation of member default universes (deduped, stable order).
    """
    group = resolve_market_group(venue)
    if group is not None:
        seen: set[str] = set()
        out: list[str] = []
        for vcode in MARKET_GROUPS[group]:
            for s in DEFAULT_UNIVERSES.get(vcode, ()):
                if s not in seen:
                    seen.add(s)
                    out.append(s)
        return tuple(out)
    info = get_venue(venue)
    if info is None:
        return ()
    return DEFAULT_UNIVERSES.get(info.code, ())


def etf_universe(venue: str) -> tuple[str, ...]:
    """Return liquid ETF symbols for ``venue`` (or market group); empty if none registered."""
    group = resolve_market_group(venue)
    if group is not None:
        seen: set[str] = set()
        out: list[str] = []
        for vcode in MARKET_GROUPS[group]:
            for s in ETF_UNIVERSES.get(vcode, ()):
                if s not in seen:
                    seen.add(s)
                    out.append(s)
        return tuple(out)
    info = get_venue(venue)
    if info is None:
        return ()
    return ETF_UNIVERSES.get(info.code, ())


def resolve_market_group(code: str) -> str | None:
    """Return canonical market-group code (``ASHARE`` / ``US`` / ``HK`` / ``ETF``) or ``None``."""
    raw = code.strip()
    if not raw:
        return None
    # Chinese aliases are exact-match; Latin aliases are case-insensitive.
    if raw in MARKET_GROUP_ALIASES:
        return MARKET_GROUP_ALIASES[raw]
    key = raw.upper()
    return MARKET_GROUP_ALIASES.get(key)


def expand_venues(code: str) -> list[str]:
    """Expand a venue or market-group code to one or more concrete venue codes.

    Examples: ``ASHARE`` → ``[SSE, SZSE]``, ``美股`` → ``[NASDAQ, NYSE, AMEX]``,
    ``HKEX`` → ``[HKEX]``.
    """
    group = resolve_market_group(code)
    if group is not None:
        return list(MARKET_GROUPS[group])
    info = get_venue(code)
    if info is not None:
        return [info.code]
    # Unknown free-form label — pass through uppercased for crypto-style partitions.
    return [code.strip().upper()]


def infer_ashare_venue(symbol: str) -> str:
    """Infer SSE vs SZSE from a mainland A-share / ETF numeric code.

    * ``6xxxxx`` / ``5xxxxx`` (incl. STAR + SSE ETFs) → ``SSE``
    * ``0xxxxx`` / ``3xxxxx`` / ``1xxxxx`` (SZSE stocks + ETFs) → ``SZSE``

    Raises ``ValueError`` when the code cannot be classified.
    """
    raw = symbol.strip().upper()
    # Strip Yahoo suffix if present.
    for suf in (".SS", ".SZ"):
        if raw.endswith(suf):
            raw = raw[: -len(suf)]
            break
    if not raw.isdigit() or len(raw) > 6:
        raise ValueError(f"cannot infer A-share venue from symbol {symbol!r}")
    body = raw.zfill(6)
    lead = body[0]
    if lead in ("5", "6"):
        return "SSE"
    if lead in ("0", "1", "2", "3"):
        return "SZSE"
    raise ValueError(f"cannot infer A-share venue from code {body!r}")


def resolve_listing(venue: str, symbol: str) -> tuple[str, str]:
    """Map ``(venue_or_group, symbol)`` to a concrete ``(venue_code, lake_symbol)``.

    * Concrete venues: normalize symbol via :func:`lake_symbol`.
    * Prefixed tickers (``sh600519``, ``sz000001``, ``hk0700``, ``600519.SS``) carry their own
      venue and win when the group is compatible.
    * ``ASHARE`` / ``A股``: auto-route by numeric code to SSE or SZSE.
    * ``US`` / ``美股``: keep symbol as-is under ``NASDAQ`` unless already a known
      NYSE/AMEX default/ETF listing (best-effort; pass an explicit venue for certainty).
    * ``HK`` / ``港股``: ``HKEX``.
    * ``ETF`` group: try A-share inference first, else require a concrete venue.
    """
    raw = symbol.strip()
    if not raw:
        raise ValueError("empty symbol")
    inferred, body = parse_user_symbol(raw)
    group = resolve_market_group(venue)

    # Prefixed / suffixed ticker already identifies the exchange (sh/sz/hk, .SS/.SZ/.HK).
    if inferred is not None and inferred != "INDEX":
        if group is None:
            # Explicit exchange in the ticker wins over a mismatched --venue (common when pasting
            # sh600519 while --venue is still the CLI default or a sibling A-share board).
            return inferred, lake_symbol(inferred, body)
        members = set(MARKET_GROUPS.get(group, ()))
        if inferred in members or group in ("ETF", "ASHARE", "US", "HK"):
            # ASHARE only accepts SSE/SZSE; US only US boards; etc.
            if group == "ASHARE" and inferred not in ("SSE", "SZSE"):
                pass  # fall through to numeric inference on body
            elif group == "US" and inferred not in ("NYSE", "NASDAQ", "AMEX"):
                pass
            elif group == "HK" and inferred != "HKEX":
                pass
            else:
                return inferred, lake_symbol(inferred, body)

    if group is None:
        info = resolve_venue(venue)
        return info.code, lake_symbol(info.code, body)

    if group == "HK":
        return "HKEX", lake_symbol("HKEX", body)

    if group == "ASHARE":
        vcode = infer_ashare_venue(body)
        return vcode, lake_symbol(vcode, body)

    if group == "US":
        # Prefer an explicit match against known NYSE/AMEX universes; default NASDAQ.
        for vcode in ("NYSE", "AMEX", "NASDAQ"):
            known = set(DEFAULT_UNIVERSES.get(vcode, ())) | set(ETF_UNIVERSES.get(vcode, ()))
            if body in known:
                return vcode, lake_symbol(vcode, body)
        return "NASDAQ", lake_symbol("NASDAQ", body)

    if group == "ETF":
        # Numeric mainland codes → A-share ETF venue; HK 4-digit pure numeric → HKEX;
        # letter tickers → US (NYSE/NASDAQ via known lists).
        bare = body.split(".")[0] if "." in body else body
        if bare.isdigit():
            if len(bare) >= 5 or len(bare) == 6:
                try:
                    vcode = infer_ashare_venue(bare)
                    return vcode, lake_symbol(vcode, bare)
                except ValueError:
                    pass
            # HK ETF codes are typically 4 digits (2800, 3067).
            if len(bare.lstrip("0") or "0") <= 4 and len(bare) <= 4:
                return "HKEX", lake_symbol("HKEX", bare)
        for vcode in ("NYSE", "NASDAQ", "AMEX"):
            if body in ETF_UNIVERSES.get(vcode, ()) or body in DEFAULT_UNIVERSES.get(vcode, ()):
                return vcode, lake_symbol(vcode, body)
        raise ValueError(
            f"cannot route ETF symbol {symbol!r} under market group ETF; "
            "pass an explicit venue (NYSE, NASDAQ, SSE, SZSE, HKEX)"
        )

    raise ValueError(f"unhandled market group {group!r}")


def resolve_symbols(venue: str, symbols: str | list[str] | None) -> list[str]:
    """Parse a CLI-style symbols argument into a non-empty lake-facing list.

    Accepts:

    * ``None`` / ``""`` / ``"@default"`` / ``"@liquid"`` → :func:`default_universe`
    * ``"@etf"`` / ``"@etfs"`` → :func:`etf_universe`
    * comma-separated string (``"AAPL,MSFT"``)
    * already-split list

    For market groups (``ASHARE``, ``US``, …) symbols are normalized but **not**
    re-routed per code — use :func:`resolve_listings` when each symbol may land on a
    different concrete venue.

    Raises ``ValueError`` when the result would be empty.
    """
    venues = expand_venues(venue)
    venue_code = venues[0] if venues else "BINANCE"
    # Prefer concrete primary for lake_symbol padding rules when group is single-venue.
    if resolve_market_group(venue) == "HK":
        venue_code = "HKEX"
    elif resolve_market_group(venue) == "ASHARE":
        venue_code = "SSE"  # padding/suffix-neutral for 6-digit codes
    elif resolve_market_group(venue) == "US":
        venue_code = "NASDAQ"
    elif resolve_market_group(venue) is None:
        info = get_venue(venue)
        venue_code = info.code if info else venue.strip().upper() or "BINANCE"

    if symbols is None:
        raw_items: list[str] = []
    elif isinstance(symbols, str):
        raw_items = [s.strip() for s in symbols.split(",") if s.strip()]
    else:
        raw_items = [str(s).strip() for s in symbols if str(s).strip()]

    if not raw_items or (
        len(raw_items) == 1 and raw_items[0].lower() in ("@default", "@liquid", "default", "liquid")
    ):
        uni = default_universe(venue)
        if not uni:
            raise ValueError(
                f"no default universe for venue {venue!r}; pass --symbols explicitly"
            )
        return [lake_symbol(venue_code, s) for s in uni]

    if len(raw_items) == 1 and raw_items[0].lower() in ("@etf", "@etfs", "etf", "etfs"):
        uni = etf_universe(venue)
        if not uni:
            raise ValueError(
                f"no ETF universe for venue {venue!r}; known ETF venues: "
                f"{', '.join(sorted(ETF_UNIVERSES))}"
            )
        return [lake_symbol(venue_code, s) for s in uni]

    return [lake_symbol(venue_code, s) for s in raw_items]


def resolve_listings(
    venue: str, symbols: str | list[str] | None
) -> list[tuple[str, str]]:
    """Like :func:`resolve_symbols` but returns ``[(venue_code, lake_symbol), ...]``.

    Expands ``@default`` / ``@etf`` across market-group member venues so multi-venue
    downloads (A股 = SSE+SZSE, 美股 = NASDAQ+NYSE+AMEX) write correct partitions.
    """
    group = resolve_market_group(venue)

    if symbols is None:
        raw_items: list[str] = []
    elif isinstance(symbols, str):
        raw_items = [s.strip() for s in symbols.split(",") if s.strip()]
    else:
        raw_items = [str(s).strip() for s in symbols if str(s).strip()]

    preset: str | None = None
    if not raw_items or (
        len(raw_items) == 1 and raw_items[0].lower() in ("@default", "@liquid", "default", "liquid")
    ):
        preset = "default"
    elif len(raw_items) == 1 and raw_items[0].lower() in ("@etf", "@etfs", "etf", "etfs"):
        preset = "etf"

    if preset is not None:
        member_venues = expand_venues(venue)
        out: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for vcode in member_venues:
            uni = etf_universe(vcode) if preset == "etf" else default_universe(vcode)
            for s in uni:
                pair = (vcode, lake_symbol(vcode, s))
                if pair not in seen:
                    seen.add(pair)
                    out.append(pair)
        if not out:
            kind = "ETF" if preset == "etf" else "default"
            raise ValueError(f"no {kind} universe for venue {venue!r}")
        return out

    # Explicit symbol list — route each through resolve_listing when group/venue is set.
    out = []
    seen_pairs: set[tuple[str, str]] = set()
    for s in raw_items:
        if group is not None:
            pair = resolve_listing(venue, s)
        else:
            info = resolve_venue(venue)
            pair = (info.code, lake_symbol(info.code, s))
        if pair not in seen_pairs:
            seen_pairs.add(pair)
            out.append(pair)
    if not out:
        raise ValueError("empty symbol list")
    return out


def resolve_venue(code: str) -> VenueInfo:
    """Resolve a venue code or alias (case-insensitive). Raises ``KeyError`` if unknown.

    Market-group labels (``ASHARE``, ``US``, ``美股``, …) are **not** venues — use
    :func:`expand_venues` / :func:`resolve_listings` for those.
    """
    raw = code.strip()
    if not raw:
        raise KeyError("empty venue code")
    # Prefer Chinese exact alias on venues (e.g. 港股 → HKEX) before uppercasing.
    if raw in _ALIAS_TO_CODE:
        return _BY_CODE[_ALIAS_TO_CODE[raw]]
    key = raw.upper()
    canonical = _ALIAS_TO_CODE.get(key)
    if canonical is None:
        # Helpful hint when user passed a market group.
        group = resolve_market_group(code)
        if group is not None:
            members = ", ".join(MARKET_GROUPS[group])
            raise KeyError(
                f"{code!r} is a market group (members: {members}); "
                f"use expand_venues/resolve_listings or a concrete venue code"
            )
        known = ", ".join(v.code for v in _VENUES)
        raise KeyError(f"unknown venue {code!r}; known: {known}")
    return _BY_CODE[canonical]


def get_venue(code: str) -> VenueInfo | None:
    """Like :func:`resolve_venue` but returns ``None`` for unknown codes (and market groups)."""
    try:
        return resolve_venue(code)
    except KeyError:
        return None


def is_equity_venue(code: str) -> bool:
    """True when ``code`` is an equity/index venue **or** an equity market group (A股/美股/港股/ETF)."""
    group = resolve_market_group(code)
    if group is not None:
        return True  # all defined groups are equity-side research markets
    info = get_venue(code)
    return info is not None and info.asset_family in ("equity", "index")


def yahoo_symbol(venue: str, symbol: str) -> str:
    """Map a lake ``(venue, symbol)`` pair to a Yahoo Finance ticker.

    * If ``symbol`` already contains a Yahoo suffix (``.`` or leading ``^``), it is returned as-is
      (after stripping whitespace). This lets callers pass full Yahoo tickers (e.g. ``0700.HK``,
      ``^GSPC``) under any equity/index venue.
    * Chinese prefixes (``sh600519``, ``hk0700``) are stripped before suffixing.
    * Otherwise the venue's ``yahoo_suffix`` is appended. HKEX numeric codes are zero-padded to
      4 digits (``700`` → ``0700.HK``).
    * Market groups are resolved via :func:`resolve_listing` first.
    """
    if not symbol.strip():
        raise ValueError("empty symbol")

    if resolve_market_group(venue) is not None:
        vcode, lake_sym = resolve_listing(venue, symbol)
        return yahoo_symbol(vcode, lake_sym)

    inferred, body = parse_user_symbol(symbol)
    # Already a Yahoo-style ticker (suffix kept by parse only for non-mapped suffixes).
    raw_upper = symbol.strip().upper()
    if raw_upper.startswith("^"):
        return raw_upper
    # If user passed 600519.SS etc., rebuild from inferred venue for consistency.
    if inferred in ("SSE", "SZSE", "HKEX") and "." in raw_upper:
        info = resolve_venue(inferred)
        b = body
        if info.code == "HKEX" and b.isdigit():
            b = b.zfill(4)
        if info.code in ("SSE", "SZSE") and b.isdigit():
            b = b.zfill(6)
        return f"{b}{info.yahoo_suffix}"

    info = resolve_venue(venue)
    if info.asset_family == "crypto":
        raise ValueError(
            f"venue {info.code} is crypto — use the Binance downloader, not Yahoo symbology"
        )

    # Prefer body after prefix strip.
    if body.startswith("^") or ("." in body and not body[0].isdigit()):
        return body

    # FX pairs: USDCNY → USDCNY=X
    if info.code == "FX":
        code = body.removesuffix("=X")
        return f"{code}=X"

    # HKEX: pad pure-numeric codes to 4 digits.
    if info.code == "HKEX" and body.isdigit():
        body = body.zfill(4)
    # SSE/SZSE: pad pure-numeric A-share codes to 6 digits.
    if info.code in ("SSE", "SZSE") and body.isdigit():
        body = body.zfill(6)
    return f"{body}{info.yahoo_suffix}"


def lake_symbol(venue: str, symbol: str) -> str:
    """Normalize a user-facing symbol for the data-lake partition key.

    Strips a matching venue Yahoo suffix when present so partitions stay clean
    (``0700.HK`` on HKEX → ``0700``). Accepts Chinese-style prefixes
    (``sh600519``, ``sz000001``, ``hk0700``). Index caret symbols are kept as-is.
    """
    _inferred, body = parse_user_symbol(symbol)
    if body.startswith("^"):
        return body
    info = get_venue(venue)
    if info is not None and info.code == "FX":
        return body.removesuffix("=X")
    if info is not None and info.yahoo_suffix and body.endswith(info.yahoo_suffix.upper()):
        body = body[: -len(info.yahoo_suffix)]
    if info is not None and info.code == "HKEX" and body.isdigit():
        body = body.zfill(4)
    if info is not None and info.code in ("SSE", "SZSE") and body.isdigit():
        body = body.zfill(6)
    return body


# ---------------------------------------------------------------------------
# Symbol classification + research instrument defaults
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    """Research-side instrument defaults for backtests on a given venue.

    ``lot_size`` is advisory (A-share board lot is typically 100); the sim still uses
    ``size_precision`` whole-share increments unless a broker adapter enforces lots.
    """

    venue: str
    currency: str
    price_precision: int = 2
    size_precision: int = 0  # whole shares for equities/ETFs
    maker_fee: float = 0.0002
    taker_fee: float = 0.0005
    lot_size: int = 1
    asset_class: str = "equity"
    kind: str = "equity"  # "equity" | "etf" | "index" | "crypto"
    notes: str = ""


# Exchange-prefix aliases used by Chinese data vendors / portals (case-insensitive).
_SYMBOL_PREFIX_TO_VENUE: dict[str, str] = {
    "SH": "SSE",
    "SS": "SSE",
    "SHA": "SSE",
    "SZ": "SZSE",
    "SHE": "SZSE",
    "HK": "HKEX",
    "HKG": "HKEX",
}


def parse_user_symbol(symbol: str) -> tuple[str | None, str]:
    """Parse a user-facing ticker into ``(inferred_venue | None, body)``.

    Accepts:

    * bare codes: ``AAPL``, ``600519``, ``0700``
    * Yahoo: ``600519.SS``, ``0700.HK``, ``^GSPC``
    * Chinese prefixes: ``sh600519``, ``sz000001``, ``hk0700``, ``sh.600519``
    """
    raw = symbol.strip()
    if not raw:
        raise ValueError("empty symbol")
    if raw.startswith("^"):
        return "INDEX", raw.upper()

    # Yahoo-style SUFFIX first (600519.SS / 0700.HK).
    upper = raw.upper()
    if "." in upper and not upper.startswith("."):
        body, _, suf = upper.rpartition(".")
        if suf in ("SS", "SH"):
            return "SSE", body
        if suf == "SZ":
            return "SZSE", body
        if suf == "HK":
            return "HKEX", body.zfill(4) if body.isdigit() else body
        if suf in ("L", "T", "TO", "DE", "PA", "AX", "NS", "KS", "TW", "SI", "SA", "SW"):
            # Leave body; caller still has venue context for these.
            return None, body

    # Prefix forms: sh600519 / sz.000001 / hk0700
    low = raw.lower().replace(" ", "")
    for pref, vcode in (
        ("sh.", "SSE"),
        ("ss.", "SSE"),
        ("sz.", "SZSE"),
        ("hk.", "HKEX"),
        ("sh", "SSE"),
        ("ss", "SSE"),
        ("sz", "SZSE"),
        ("hk", "HKEX"),
    ):
        if low.startswith(pref) and len(low) > len(pref):
            body = low[len(pref) :].lstrip(".").upper()
            if body:
                if vcode == "HKEX" and body.isdigit():
                    body = body.zfill(4)
                if vcode in ("SSE", "SZSE") and body.isdigit():
                    body = body.zfill(6)
                return vcode, body

    return None, upper


def is_etf_symbol(venue: str, symbol: str) -> bool:
    """Heuristic ETF detector for research routing / instrument notes.

    * Listed in :data:`ETF_UNIVERSES` for the (resolved) venue → True
    * A-share numeric: ``51/56/58xxxx`` (SSE) or ``15/16xxxx`` (SZSE) → True
    * Cross-listed known ETF ticker under a compatible market group → True
    """
    try:
        if resolve_market_group(venue) is not None or get_venue(venue) is not None:
            vcode, lake_sym = resolve_listing(venue, symbol)
        else:
            inferred, body = parse_user_symbol(symbol)
            vcode = inferred or venue.strip().upper()
            lake_sym = body
    except (ValueError, KeyError):
        inferred, body = parse_user_symbol(symbol)
        vcode = inferred or venue.strip().upper()
        lake_sym = body

    if lake_sym in ETF_UNIVERSES.get(vcode, ()):
        return True

    members = set(expand_venues(venue)) if (
        get_venue(venue) is not None or resolve_market_group(venue) is not None
    ) else {vcode}
    for uv, syms in ETF_UNIVERSES.items():
        if lake_sym in syms and (uv == vcode or uv in members or resolve_market_group(venue) == "ETF"):
            return True

    if vcode in ("SSE", "SZSE") or resolve_market_group(venue) == "ASHARE":
        if lake_sym.isdigit():
            b = lake_sym.zfill(6)
            if b.startswith(("51", "56", "58", "15", "16")):
                return True
    return False


def instrument_spec(venue: str, symbol: str | None = None) -> InstrumentSpec:
    """Return research backtest defaults (fees, precision, currency) for ``venue`` [/ ``symbol``].

    Fees are approximate friction for event-driven research — not a brokerage schedule.
    """
    group = resolve_market_group(venue)
    info_in = get_venue(venue)

    if group == "HK" or (info_in is not None and info_in.code == "HKEX"):
        vcode = "HKEX"
    elif group == "ASHARE":
        if symbol:
            try:
                vcode = resolve_listing(venue, symbol)[0]
            except (ValueError, KeyError):
                vcode = "SSE"
        else:
            vcode = "SSE"
    elif group == "US":
        if symbol:
            try:
                vcode = resolve_listing(venue, symbol)[0]
            except (ValueError, KeyError):
                vcode = "NASDAQ"
        else:
            vcode = "NASDAQ"
    elif group == "ETF":
        if symbol:
            try:
                vcode = resolve_listing(venue, symbol)[0]
            except (ValueError, KeyError):
                vcode = "NYSE"
        else:
            vcode = "NYSE"
    else:
        vcode = info_in.code if info_in is not None else (venue.strip().upper() or "BINANCE")

    info = get_venue(vcode)
    currency = info.currency if info is not None else "USD"

    if info is not None and info.asset_family == "index":
        kind = "index"
    elif symbol and is_etf_symbol(vcode, symbol):
        kind = "etf"
    elif info is not None and info.asset_family == "equity":
        kind = "equity"
    elif group is not None:
        kind = "equity"
    else:
        kind = "crypto"

    # Market-specific research friction (maker ≈ passive, taker ≈ aggressive / all-in estimate).
    if vcode in ("SSE", "SZSE"):
        # A-share: ~2.5 bps commission + sell-side stamp duty folded into taker.
        return InstrumentSpec(
            venue=vcode,
            currency="CNY",
            price_precision=2,
            size_precision=0,
            maker_fee=0.00025,
            taker_fee=0.00075,
            lot_size=100,
            asset_class="equity",
            kind=kind if kind in ("equity", "etf") else "equity",
            notes="A-share board lot typically 100; stamp duty modeled in taker_fee",
        )
    if vcode == "HKEX":
        return InstrumentSpec(
            venue=vcode,
            currency="HKD",
            price_precision=3,
            size_precision=0,
            maker_fee=0.0003,
            taker_fee=0.0013,
            lot_size=100,
            asset_class="equity",
            kind=kind if kind in ("equity", "etf") else "equity",
            notes="HK stamp duty ~10 bps folded into taker_fee; board lots vary by name",
        )
    if vcode in ("NYSE", "NASDAQ", "AMEX"):
        return InstrumentSpec(
            venue=vcode,
            currency="USD",
            price_precision=2,
            size_precision=0,
            maker_fee=0.0,
            taker_fee=0.0001,
            lot_size=1,
            asset_class="equity",
            kind=kind if kind in ("equity", "etf") else "equity",
            notes="Near-zero commission model; SEC/finra fees ignored",
        )
    if vcode == "INDEX" or (info is not None and info.asset_family == "index"):
        return InstrumentSpec(
            venue=vcode,
            currency=currency,
            price_precision=2,
            size_precision=0,
            maker_fee=0.0,
            taker_fee=0.0,
            lot_size=1,
            asset_class="equity",
            kind="index",
            notes="Index series are not tradeable; fees zeroed for research overlays",
        )
    if info is not None and info.asset_family == "equity":
        return InstrumentSpec(
            venue=vcode,
            currency=currency,
            price_precision=2,
            size_precision=0,
            maker_fee=0.0002,
            taker_fee=0.0005,
            lot_size=1,
            asset_class="equity",
            kind="equity",
        )
    # Crypto / unknown
    return InstrumentSpec(
        venue=vcode,
        currency=currency if info is not None else "USDT",
        price_precision=2,
        size_precision=3,
        maker_fee=0.0002,
        taker_fee=0.0004,
        lot_size=1,
        asset_class="spot",
        kind="crypto",
    )


def suggest_equity_download_defaults(
    venue: str, *, interval: str, days: float, symbols: str
) -> tuple[str, float, str, list[str]]:
    """Nudge crypto-shaped CLI defaults toward equity research when ``venue`` is equity-side.

    When the caller still has Binance-shaped defaults (``interval=1m``, ``days=7``,
    ``symbols=BTCUSDT``), rewrite them for daily multi-month equity history.
    Returns ``(interval, days, symbols, notes)``.
    """
    notes: list[str] = []
    if not is_equity_venue(venue):
        return interval, days, symbols, notes

    out_interval, out_days, out_symbols = interval, days, symbols
    if out_symbols in ("BTCUSDT", "btcusdt") and (venue.strip().upper() != "BINANCE"):
        out_symbols = "@default"
        notes.append("symbols BTCUSDT → @default (equity venue)")
    # Only rewrite when both look like the CLI crypto defaults (avoid overriding explicit choices).
    if out_interval == "1m" and out_days == 7.0:
        out_interval = "1d"
        out_days = 365.0
        notes.append("interval/days 1m×7d → 1d×365d for equity/ETF research")
    elif out_interval == "1m":
        out_interval = "1d"
        notes.append("interval 1m → 1d (Yahoo intraday history is limited)")
    return out_interval, out_days, out_symbols, notes


def format_venue_table(*, family: AssetFamily | None = None) -> str:
    """Plain-text table for CLI ``coinext venues``."""
    rows = [v for v in _VENUES if family is None or v.asset_family == family]
    headers = ("CODE", "NAME", "REGION", "CCY", "FAMILY", "SOURCE", "YAHOO")
    cols = [
        (
            v.code,
            v.name,
            v.region,
            v.currency,
            v.asset_family,
            v.data_source,
            v.yahoo_suffix or ("(none)" if v.asset_family != "crypto" else "n/a"),
        )
        for v in rows
    ]
    widths = [len(h) for h in headers]
    for row in cols:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt(row: tuple[str, ...]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))

    lines = [fmt(headers), fmt(tuple("-" * w for w in widths))]
    lines.extend(fmt(r) for r in cols)
    return "\n".join(lines)


def format_market_groups() -> str:
    """Plain-text summary of A股 / 美股 / 港股 / ETF market groups for CLI help."""
    lines = [
        "Market groups (research multi-venue shortcuts):",
        "  ASHARE / A股 / A     → SSE + SZSE     (--symbols @default | @etf; auto-route codes)",
        "  US / 美股           → NASDAQ + NYSE + AMEX",
        "  HK / 港股           → HKEX",
        "  ETF                → NYSE + NASDAQ + SSE + SZSE + HKEX (@etf presets)",
        "",
        "ETF presets (--symbols @etf) on concrete venues:",
    ]
    for code in ("NYSE", "NASDAQ", "SSE", "SZSE", "HKEX"):
        uni = ETF_UNIVERSES.get(code, ())
        if uni:
            lines.append(f"  {code}: {', '.join(uni)}")
    return "\n".join(lines)


__all__ = [
    "AssetFamily",
    "DEFAULT_FX_PAIRS",
    "DEFAULT_UNIVERSES",
    "ETF_UNIVERSES",
    "InstrumentSpec",
    "MARKET_GROUPS",
    "MARKET_GROUP_ALIASES",
    "SAMPLE_ADJ_SERIES",
    "SAMPLE_EQUITY_SERIES",
    "VenueInfo",
    "all_venues",
    "default_universe",
    "equity_venues",
    "etf_universe",
    "expand_venues",
    "format_market_groups",
    "format_venue_table",
    "get_venue",
    "infer_ashare_venue",
    "instrument_spec",
    "is_equity_venue",
    "is_etf_symbol",
    "lake_symbol",
    "parse_user_symbol",
    "resolve_listing",
    "resolve_listings",
    "resolve_market_group",
    "resolve_symbols",
    "resolve_venue",
    "suggest_equity_download_defaults",
    "yahoo_symbol",
]
