"""coinext_broker — research-side equity broker ports (scaffold).

Live stock execution is **not** wired into the Kernel yet. This package defines the Python-facing
contracts a future IB / 券商 adapter must satisfy so research code can paper-trade equities against
the same order shapes the backtest path uses.

Status: **scaffold** — paper broker is usable offline; IB module is a config + method skeleton
(no network orders until keys + full fill loop land).

See ``market-data/python/coinext_broker/README.md``.
"""

from __future__ import annotations

from .base import (
    BrokerFill,
    BrokerOrder,
    EquityBroker,
    OrderSide,
    OrderStatus,
    OrderType,
    PaperEquityBroker,
)
from .ib_paper import IbConfig, IbPaperBroker, default_ib_factory, ib_contract_fields
from .replay import (
    PortfolioReplayResult,
    ReplayResult,
    replay_bars,
    replay_from_lake,
    replay_portfolio,
    replay_portfolio_from_lake,
)
from .rules import LimitBand, is_t1_venue, limit_band, price_limit_pct

__all__ = [
    "BrokerFill",
    "BrokerOrder",
    "EquityBroker",
    "IbConfig",
    "IbPaperBroker",
    "LimitBand",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "PaperEquityBroker",
    "PortfolioReplayResult",
    "ReplayResult",
    "default_ib_factory",
    "ib_contract_fields",
    "is_t1_venue",
    "limit_band",
    "price_limit_pct",
    "replay_bars",
    "replay_from_lake",
    "replay_portfolio",
    "replay_portfolio_from_lake",
]
