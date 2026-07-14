"""coinext_broker — Equity paper / IB paper brokers (research path).

Status: partial. See root ARCHITECTURE.md and docs/STATUS.md.
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
