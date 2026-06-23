"""coinext_bus — the Python Redis-Streams bus client.

Python never imports the Rust in-process bus crate (that hot path passes typed ``Arc`` with zero
serialization, ARCHITECTURE.md §6). For cross-service / UI fan-out, Python consumes a **Redis
Streams** bus and decodes the versioned **MessagePack** :class:`~coinext_contracts.Envelope`
(``{schema_version, msg_type, trace_id, ts_init, payload}``). The ``ingestor`` (Rust ``coinext-ingest``)
publishes normalized market data; the ``trader``/``api``/``risk-monitor`` consume it.

``redis`` and ``msgpack`` are optional and guarded — this module imports cleanly without them; the
error is raised only when you actually open a connection / decode a frame.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any

from coinext_contracts import (
    SCHEMA_VERSION,
    Envelope,
    MsgType,
    is_kill_switch,
    kill_switch_payload,
)

# Default stream keys on the bus (mirror the Rust publisher's keyspace).
STREAM_MARKET = "coinext.market"  # quotes/trades/bars/deltas from the ingestor
STREAM_EXEC = "coinext.exec"  # order events / fills from exec-svc
STREAM_CTRL = "coinext.control"  # control-plane commands (kill-switch, etc.); api + risk-monitor publish here


def _require_redis() -> Any:
    try:
        import redis  # type: ignore
    except ImportError as exc:  # pragma: no cover - optional dep
        raise ImportError(
            "redis not installed. Install the bus extra: pip install 'coinext[bus]'"
        ) from exc
    return redis


def _require_msgpack() -> Any:
    try:
        import msgpack  # type: ignore
    except ImportError as exc:  # pragma: no cover - optional dep
        raise ImportError(
            "msgpack not installed. Install the bus extra: pip install 'coinext[bus]'"
        ) from exc
    return msgpack


def decode_envelope(raw: bytes) -> Envelope:
    """Decode a MessagePack frame into an :class:`~coinext_contracts.Envelope`.

    The wire layout is a 5-element array ``[schema_version, msg_type, trace_id, ts_init, payload]``
    (compact, positional — matches the Rust ``coinext_bus::Envelope`` serializer). Raises if the schema
    version disagrees, so a mismatched deploy fails loud instead of silently mis-parsing.
    """
    msgpack = _require_msgpack()
    fields = msgpack.unpackb(raw, raw=True)
    if not isinstance(fields, (list, tuple)) or len(fields) != 5:
        raise ValueError(f"malformed Envelope frame: expected 5 fields, got {fields!r}")
    schema_version, msg_type, trace_id, ts_init, payload = fields
    if schema_version != SCHEMA_VERSION:
        raise ValueError(
            f"Envelope schema mismatch: frame={schema_version} expected={SCHEMA_VERSION}"
        )
    return Envelope(
        schema_version=schema_version,
        msg_type=MsgType(msg_type),
        trace_id=trace_id,
        ts_init=ts_init,
        payload=payload,
    )


def encode_envelope(env: Envelope) -> bytes:
    """Encode an :class:`~coinext_contracts.Envelope` back to a MessagePack frame (symmetry helper)."""
    msgpack = _require_msgpack()
    return msgpack.packb(
        [env.schema_version, int(env.msg_type), env.trace_id, env.ts_init, env.payload],
        use_bin_type=True,
    )


@dataclass
class StreamMessage:
    """One decoded message read off a Redis stream."""

    stream: str
    msg_id: str
    envelope: Envelope


class RedisBusClient:
    """Subscribe-and-consume client for the Redis-Streams bus.

    Uses consumer groups so multiple ``trader``/``api`` replicas can share a stream with
    at-least-once delivery. Connections are opened lazily in :meth:`connect`, so constructing the
    client (e.g. for config wiring) does not require redis to be installed.
    """

    def __init__(
        self, url: str = "redis://redis:6379/0", *, group: str = "coinext", consumer: str = "c0"
    ) -> None:
        self.url = url
        self.group = group
        self.consumer = consumer
        self._client: Any | None = None

    def connect(self) -> None:
        """Open the Redis connection (idempotent)."""
        if self._client is not None:
            return
        redis = _require_redis()
        self._client = redis.Redis.from_url(self.url)

    def ensure_group(self, stream: str) -> None:
        """Create the consumer group on ``stream`` if it does not exist (MKSTREAM)."""
        self.connect()
        assert self._client is not None
        try:
            self._client.xgroup_create(stream, self.group, id="$", mkstream=True)
        except Exception:  # noqa: BLE001 - BUSYGROUP means it already exists; ignore
            # TODO: narrow to redis.exceptions.ResponseError("BUSYGROUP ...") once redis is a dep.
            pass

    def consume(
        self,
        streams: list[str],
        *,
        block_ms: int = 1000,
        count: int = 64,
    ) -> Iterator[StreamMessage]:
        """Yield decoded :class:`StreamMessage` from ``streams`` via the consumer group.

        Blocking read loop intended to be driven by a service main loop. ACKs each message after it
        is yielded; downstream errors should be handled by the consumer (DLQ is TODO).
        """
        self.connect()
        assert self._client is not None
        for stream in streams:
            self.ensure_group(stream)
        keys = {s: ">" for s in streams}
        while True:
            resp = self._client.xreadgroup(
                self.group, self.consumer, keys, count=count, block=block_ms
            )
            if not resp:
                continue
            for stream_key, entries in resp:
                stream_name = stream_key.decode() if isinstance(stream_key, bytes) else stream_key
                for msg_id, data in entries:
                    raw = data.get(b"e") or data.get("e")
                    if raw is None:
                        continue
                    env = decode_envelope(raw)
                    mid = msg_id.decode() if isinstance(msg_id, bytes) else msg_id
                    yield StreamMessage(stream=stream_name, msg_id=mid, envelope=env)
                    self._client.xack(stream_name, self.group, msg_id)

    def publish(self, stream: str, env: Envelope) -> str:
        """Publish an :class:`~coinext_contracts.Envelope` to ``stream`` (mainly for tests/tools)."""
        self.connect()
        assert self._client is not None
        return self._client.xadd(stream, {"e": encode_envelope(env)})

    def subscribe(self, streams: list[str], handler: Callable[[StreamMessage], None]) -> None:
        """Convenience: run :meth:`consume` forever, calling ``handler`` per message."""
        for message in self.consume(streams):
            handler(message)

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None


def encode_payload(payload: dict) -> bytes:
    """MessagePack-encode a control payload map (the body of a CTRL Envelope)."""
    msgpack = _require_msgpack()
    return msgpack.packb(payload, use_bin_type=True)


def decode_payload(env: Envelope) -> dict:
    """MessagePack-decode an Envelope's ``payload`` bytes into a dict (control commands).

    Map keys come back as ``str`` (``raw=False``) so callers can index by ``"kind"`` etc.
    """
    msgpack = _require_msgpack()
    return msgpack.unpackb(env.payload, raw=False)


def build_control_envelope(payload: dict, *, trace_id: bytes | None = None) -> Envelope:
    """Build a ``MsgType.CTRL`` :class:`~coinext_contracts.Envelope` wrapping ``payload``.

    ``trace_id`` is a 16-byte correlation id (random if omitted); ``ts_init`` is wall-clock ns.
    """
    tid = trace_id if trace_id is not None else os.urandom(16)
    return Envelope.of(MsgType.CTRL, tid, time.time_ns(), encode_payload(payload))


class Publisher:
    """Thin control-plane publisher over :class:`RedisBusClient`.

    The api (operator kill-switch) and the risk-monitor (breach trip) both publish ``MsgType.CTRL``
    commands through this. Connections are opened lazily by the underlying client, so constructing a
    ``Publisher`` requires neither redis nor msgpack — they are needed only at :meth:`publish_control`.
    """

    def __init__(self, url: str = "redis://redis:6379/0") -> None:
        self.client = RedisBusClient(url)

    def publish_control(self, stream: str, payload: dict) -> str:
        """Wrap ``payload`` in a CTRL Envelope and publish it to ``stream``; return the message id."""
        env = build_control_envelope(payload)
        return self.client.publish(stream, env)

    def publish_kill_switch(
        self,
        stream: str,
        *,
        engaged: bool,
        reason: str,
        source: str,
        actor: str | None = None,
    ) -> str:
        """Convenience: publish a documented ``CtrlKillSwitch`` command to ``stream``."""
        return self.publish_control(
            stream,
            kill_switch_payload(engaged=engaged, reason=reason, source=source, actor=actor),
        )

    def close(self) -> None:
        self.client.close()


def dispatch_control(envelope: Envelope, on_kill: Callable[[str], None]) -> bool:
    """Decode a CTRL ``envelope`` and, if it is an *engaged* ``CtrlKillSwitch``, call ``on_kill(reason)``.

    Pure dispatch (no I/O beyond msgpack decode). Returns True iff ``on_kill`` was invoked. Ignores
    non-CTRL envelopes and kill-switch *release* (engaged=False) messages so callers can subscribe to
    the control stream and react only to a halt.
    """
    if envelope.msg_type != MsgType.CTRL:
        return False
    payload = decode_payload(envelope)
    if is_kill_switch(payload) and payload.get("engaged"):
        on_kill(str(payload.get("reason", "")))
        return True
    return False


__all__ = [
    "RedisBusClient",
    "Publisher",
    "StreamMessage",
    "decode_envelope",
    "encode_envelope",
    "encode_payload",
    "decode_payload",
    "build_control_envelope",
    "dispatch_control",
    "STREAM_MARKET",
    "STREAM_EXEC",
    "STREAM_CTRL",
]
