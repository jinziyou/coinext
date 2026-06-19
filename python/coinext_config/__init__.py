"""coinext_config — layered configuration for the Coinext control plane.

Resolution order (highest precedence first), per ``docs/ARCHITECTURE.md`` §1/§7 + ``.env.example``::

    CLI flags  >  env (COINEXT__SECTION__KEY)  >  config/*.yaml files  >  built-in defaults

The same ``RunConfig`` is built for every :class:`~coinext_kernel.Environment` — only the Kernel-injected
Clock / Cache / Data+Exec clients differ between backtest, sandbox, and live (the parity invariant).
The ``BrokerageModel`` economics carried under ``VenueConfig`` are SHARED between backtest and live
so the two agree on venue *economics*, not just order flow (LEAN's keystone, ARCHITECTURE.md §5).

Optional deps are guarded: pydantic is used for validation when present, otherwise a stdlib
``dataclasses`` fallback provides the same field surface. PyYAML is loaded lazily and only when a
config file is actually read. ``from coinext_config import load_config`` imports cleanly with NO heavy
deps installed.
"""

from __future__ import annotations

import os
from typing import Any

# --------------------------------------------------------------------------------------------------
# Optional pydantic — validation when available, dataclasses fallback otherwise.
# --------------------------------------------------------------------------------------------------
try:  # pragma: no cover - import guard
    from pydantic import BaseModel, Field

    _HAVE_PYDANTIC = True
except ImportError:  # pragma: no cover - fallback path
    _HAVE_PYDANTIC = False

    from dataclasses import dataclass

    def Field(default=None, **_kwargs):  # type: ignore[no-redef] # noqa: N802
        """Minimal ``pydantic.Field`` shim so model definitions are import-compatible."""
        return default

    class BaseModel:  # type: ignore[no-redef]
        """Tiny ``pydantic.BaseModel`` stand-in backed by ``dataclasses``.

        Subclasses are turned into dataclasses on definition (via ``__init_subclass__``) so the
        field surface and ``__init__(**kwargs)`` behaviour match the pydantic path closely enough
        for the scaffold. TODO: when pydantic is a hard dep, delete this fallback.
        """

        def __init_subclass__(cls, **kwargs: Any) -> None:
            super().__init_subclass__(**kwargs)
            # Promote bare-annotation defaults onto the class so dataclass picks them up.
            dataclass(cls)  # type: ignore[arg-type]

        def model_dump(self) -> dict[str, Any]:
            return dict(self.__dict__)


# The env-var prefix and section separator, per .env.example.
ENV_PREFIX = "COINEXT__"
SECTION_SEP = "__"

# Known config environments (mirror config/{backtest,sandbox,live}.yaml + base.yaml).
ENVIRONMENTS = ("backtest", "sandbox", "live")


class BinanceConfig(BaseModel):
    """Binance reference-adapter settings (ARCHITECTURE.md §5, §7).

    Public market-data streams need no keys; trading (sandbox/live) does. ``testnet=True`` selects
    the sandbox endpoints — the SAME ``ExecutionClient`` port, only different base URLs.
    """

    api_key: str = Field(default="")
    api_secret: str = Field(default="")
    testnet: bool = Field(default=True)
    # TODO: ws_base / rest_base overrides; recv_window; rate-limit weights per coinext-network.


class VenueConfig(BaseModel):
    """Per-venue economics + identity.

    The fee/slippage/latency fields parameterize the ``BrokerageModel`` that is SHARED by the
    SimulatedExchange (backtest) and the live adapter — so backtest and live agree on economics.
    """

    name: str = Field(default="BINANCE")
    price_precision: int = Field(default=2)
    size_precision: int = Field(default=3)
    maker_fee: float = Field(default=0.0002)
    taker_fee: float = Field(default=0.0004)
    latency_ns: int = Field(default=1_000_000)  # simulated ack/fill latency on the time-frontier
    # TODO: slippage model params; partial-fill / queue-position fidelity knobs (coinext-sim).


class RiskConfig(BaseModel):
    """Pre-trade risk gate + defense-in-depth limits (ARCHITECTURE.md §8, .env.example COINEXT__RISK__).

    The per-order Rust ``RiskEngine`` reads the same numbers; the out-of-band ``risk-monitor``
    watches PnL/positions and can trip ``kill_switch`` globally.
    """

    max_order_notional: float = Field(default=50_000.0)
    max_position_notional: float = Field(default=250_000.0)
    max_gross_exposure: float = Field(default=1_000_000.0)
    max_orders_per_sec: int = Field(default=20)
    kill_switch: bool = Field(default=False)


class RunConfig(BaseModel):
    """The single top-level config the Kernel consumes for ANY environment.

    Identical across backtest / sandbox / live — the Kernel swaps Clock + Cache + clients, not this.
    """

    env: str = Field(default="backtest")
    symbol: str = Field(default="BTCUSDT")
    starting_balance: float = Field(default=100_000.0)
    redis_url: str = Field(default="redis://redis:6379/0")
    postgres_dsn: str = Field(default="postgresql://coinext:coinext@postgres:5432/coinext")
    data_lake_root: str = Field(default="/data")
    otel_endpoint: str = Field(default="http://otel-collector:4317")
    log_level: str = Field(default="info")
    venue: VenueConfig = Field(default_factory=VenueConfig) if _HAVE_PYDANTIC else Field()
    risk: RiskConfig = Field(default_factory=RiskConfig) if _HAVE_PYDANTIC else Field()
    binance: BinanceConfig = Field(default_factory=BinanceConfig) if _HAVE_PYDANTIC else Field()

    # The dataclass fallback cannot express ``default_factory`` through the Field() shim, so ensure
    # the nested models exist after construction in both paths.
    def __post_init__(self) -> None:  # pragma: no cover - dataclass fallback only
        if not isinstance(getattr(self, "venue", None), VenueConfig):
            self.venue = VenueConfig()
        if not isinstance(getattr(self, "risk", None), RiskConfig):
            self.risk = RiskConfig()
        if not isinstance(getattr(self, "binance", None), BinanceConfig):
            self.binance = BinanceConfig()


# --------------------------------------------------------------------------------------------------
# Layer readers.
# --------------------------------------------------------------------------------------------------
def _read_yaml(path: str) -> dict[str, Any]:
    """Read one YAML config file into a nested dict. Returns ``{}`` if absent or PyYAML missing."""
    if not os.path.exists(path):
        return {}
    try:  # lazy, optional — config files are advisory in the scaffold
        import yaml  # type: ignore
    except ImportError:  # pragma: no cover - PyYAML not installed
        # TODO: a tiny stdlib YAML-subset reader could remove the soft dep entirely.
        return {}
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _yaml_layers(env: str, config_dir: str) -> dict[str, Any]:
    """Merge ``config/base.yaml`` then ``config/{env}.yaml`` (env wins)."""
    merged = _deep_merge({}, _read_yaml(os.path.join(config_dir, "base.yaml")))
    merged = _deep_merge(merged, _read_yaml(os.path.join(config_dir, f"{env}.yaml")))
    return merged


def _env_layer() -> dict[str, Any]:
    """Project ``COINEXT__SECTION__KEY`` environment variables into a nested dict.

    ``COINEXT__RISK__MAX_ORDERS_PER_SEC=20`` -> ``{"risk": {"max_orders_per_sec": "20"}}``.
    ``COINEXT__ENV=live`` -> ``{"env": "live"}`` (no section).
    """
    out: dict[str, Any] = {}
    for raw_key, value in os.environ.items():
        if not raw_key.startswith(ENV_PREFIX):
            continue
        parts = raw_key[len(ENV_PREFIX) :].lower().split(SECTION_SEP)
        cursor = out
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = value
    return out


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``overlay`` onto ``base`` (overlay wins). Returns ``base`` (mutated)."""
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _coerce_scalars(d: dict[str, Any]) -> dict[str, Any]:
    """Best-effort coerce string scalars (from env/yaml) into bool/int/float in place."""
    for key, value in list(d.items()):
        if isinstance(value, dict):
            _coerce_scalars(value)
        elif isinstance(value, str):
            d[key] = _coerce_str(value)
    return d


def _coerce_str(s: str) -> Any:
    low = s.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(s)
    except ValueError:
        pass
    try:
        return float(s)
    except ValueError:
        return s


def _model_fields(model_cls: type) -> set[str]:
    """Field names a model accepts, for both the pydantic and dataclasses backends."""
    fields = getattr(model_cls, "model_fields", None)  # pydantic v2
    if fields:
        return set(fields.keys())
    dc_fields = getattr(model_cls, "__dataclass_fields__", None)  # dataclasses fallback
    if dc_fields:
        return set(dc_fields.keys())
    return set(getattr(model_cls, "__annotations__", {}).keys())


def _filter_known(model_cls: type, data: dict[str, Any]) -> dict[str, Any]:
    """Drop keys the model does not declare (the canonical yaml carries richer sections than the
    scaffold sub-models, e.g. ``venue.markets`` / ``brokerage.slippage_bps`` — ignore them)."""
    allowed = _model_fields(model_cls)
    return {k: v for k, v in data.items() if k in allowed}


def _build_runconfig(merged: dict[str, Any]) -> RunConfig:
    """Construct a :class:`RunConfig` from a merged dict, building nested sub-models.

    Maps the canonical yaml/env section layout (``brokerage``/``data``/``redis``/... — see
    ``config/base.yaml``) onto the flat ``RunConfig`` + sub-model fields. Unknown keys are dropped
    so extra config sections never break construction on either backend.
    """
    # VenueConfig pulls identity from `venue` and economics from the shared `brokerage` section.
    # `brokerage` is the base; explicit `venue.*` (where CLI/env land) wins on key collisions.
    venue_data: dict[str, Any] = dict(merged.get("brokerage", {}))  # maker_fee/taker_fee/latency_ns
    venue_data.update(merged.get("venue", {}))
    venue = VenueConfig(**_filter_known(VenueConfig, venue_data))

    risk = RiskConfig(**_filter_known(RiskConfig, merged.get("risk", {})))
    # `binance` keys may also arrive under the `venue` section (api_key/api_secret/testnet).
    binance_data: dict[str, Any] = dict(merged.get("binance", {}))
    for key in ("api_key", "api_secret", "testnet"):
        if key in merged.get("venue", {}):
            binance_data.setdefault(key, merged["venue"][key])
    binance = BinanceConfig(**_filter_known(BinanceConfig, binance_data))

    # Flatten the infra sections into RunConfig's flat fields (best-effort, all optional).
    top: dict[str, Any] = {}
    for key in ("env", "symbol", "starting_balance"):
        if key in merged:
            top[key] = merged[key]
    _map(top, "redis_url", merged, "redis", "url")
    _map(top, "postgres_dsn", merged, "postgres", "dsn")
    _map(top, "data_lake_root", merged, "data", "lake_root")
    _map(top, "otel_endpoint", merged, "otel", "endpoint")
    _map(top, "log_level", merged, "log", "level")
    top = _filter_known(RunConfig, top)
    return RunConfig(venue=venue, risk=risk, binance=binance, **top)


def _map(dst: dict[str, Any], dst_key: str, src: dict[str, Any], section: str, key: str) -> None:
    """Copy ``src[section][key]`` to ``dst[dst_key]`` if present."""
    sect = src.get(section)
    if isinstance(sect, dict) and key in sect:
        dst[dst_key] = sect[key]


def load_config(
    env: str | None = None,
    *,
    config_dir: str = "config",
    cli_overrides: dict[str, Any] | None = None,
) -> RunConfig:
    """Resolve the layered config into a :class:`RunConfig`.

    Precedence (high to low): ``cli_overrides`` > env (``COINEXT__*``) > ``config/{base,env}.yaml`` >
    code defaults. ``env`` defaults to ``$COINEXT__ENV`` or ``"backtest"``.

    The CLI (``coinext_cli``) passes already-parsed flags as ``cli_overrides`` (a nested dict mirroring
    the section layout) so the precedence rule stays in ONE place.
    """
    env = env or os.environ.get(f"{ENV_PREFIX}ENV", "backtest")

    merged: dict[str, Any] = {}
    _deep_merge(merged, _yaml_layers(env, config_dir))  # lowest (above defaults)
    _deep_merge(merged, _env_layer())  # env beats files
    if cli_overrides:
        _deep_merge(merged, cli_overrides)  # CLI wins
    merged.setdefault("env", env)
    _coerce_scalars(merged)
    return _build_runconfig(merged)


__all__ = [
    "RunConfig",
    "RiskConfig",
    "VenueConfig",
    "BinanceConfig",
    "load_config",
    "ENV_PREFIX",
    "ENVIRONMENTS",
]
