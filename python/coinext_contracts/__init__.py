"""coinext_contracts — the Python contract surface.

Re-exports the compiled domain/runtime types from ``coinext_py`` (the source of truth), and defines the
MessagePack ``Envelope`` schema + ``MsgType`` registry that the Redis-Streams bus uses so the Rust
side and Python side (``coinext_bus``) agree on the wire format. Port ``Protocol`` definitions describe
the seams the Python control plane implements/consumes.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Protocol, runtime_checkable

# Wire schema version — MUST match coinext_bus::Envelope::SCHEMA_VERSION on the Rust side.
SCHEMA_VERSION = 1


class MsgType(IntEnum):
    """Payload kind tag in the Envelope (must match coinext_ports::MsgType discriminants)."""

    QUOTE = 0
    TRADE = 1
    BAR = 2
    DELTA = 3
    ORDER_EVENT = 4
    FILL = 5
    TIMER = 6
    CMD = 7
    CTRL = 8


@dataclass(frozen=True)
class Envelope:
    """Versioned cross-service frame. Mirrors coinext_bus::Envelope."""

    schema_version: int
    msg_type: MsgType
    trace_id: bytes  # 16 bytes
    ts_init: int
    payload: bytes  # MessagePack-encoded domain object

    @staticmethod
    def of(msg_type: MsgType, trace_id: bytes, ts_init: int, payload: bytes) -> Envelope:
        return Envelope(SCHEMA_VERSION, msg_type, trace_id, ts_init, payload)


@runtime_checkable
class StrategyProtocol(Protocol):
    """The handler surface the Rust PyStrategyAdapter calls."""

    def on_bar(self, bar, ctx) -> None: ...


# Re-export the compiled runtime if available (it is the source of truth).
try:  # pragma: no cover - import guard
    import coinext_py  # noqa: F401

    HAVE_NATIVE = True
except ImportError:
    HAVE_NATIVE = False


__all__ = ["SCHEMA_VERSION", "MsgType", "Envelope", "StrategyProtocol", "HAVE_NATIVE"]
