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


# --------------------------------------------------------------------------------------------------
# Control-plane payloads carried under MsgType.CTRL.
# --------------------------------------------------------------------------------------------------
#
# A CTRL Envelope's ``payload`` is a MessagePack map. The ``kind`` key tags the concrete control
# command so consumers can dispatch. The kill-switch command — the platform-wide trading halt the
# operator (api) and the out-of-band risk-monitor both publish — has this documented shape::
#
#     {
#       "kind":    "CtrlKillSwitch",   # CTRL_KILL_SWITCH discriminant
#       "engaged": bool,               # True halts new order routing; False releases it
#       "reason":  str,                # human-readable audit reason
#       "source":  str,                # who published it ("api" | "risk-monitor" | ...)
#       "actor":   str | None,         # operator identity for the audit trail (optional)
#     }
#
# Keep this map shape stable: it is the cross-service contract honoured by every ``trader`` process's
# in-core risk gate (via the Rust side) and by the Python ``coinext_live`` control subscriber.
CTRL_KILL_SWITCH = "CtrlKillSwitch"  # payload["kind"] tag for the kill-switch command


def kill_switch_payload(
    *, engaged: bool, reason: str, source: str, actor: str | None = None
) -> dict[str, object]:
    """Build the documented ``CtrlKillSwitch`` payload map (the body of a MsgType.CTRL Envelope)."""
    return {
        "kind": CTRL_KILL_SWITCH,
        "engaged": bool(engaged),
        "reason": reason,
        "source": source,
        "actor": actor,
    }


def is_kill_switch(payload: dict) -> bool:
    """True if ``payload`` is a ``CtrlKillSwitch`` command (regardless of engaged/released)."""
    return isinstance(payload, dict) and payload.get("kind") == CTRL_KILL_SWITCH


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


__all__ = [
    "SCHEMA_VERSION",
    "MsgType",
    "Envelope",
    "StrategyProtocol",
    "HAVE_NATIVE",
    "CTRL_KILL_SWITCH",
    "kill_switch_payload",
    "is_kill_switch",
]
