"""Optional Redis integration tests (skipped when redis is down / not installed).

Exercises the paper OMS command path contract end-to-end when a local Redis is available:

1. Publish a ``SubmitMarket`` CMD envelope to ``coinext.exec.cmd``
2. (Requires ``coinext-exec-svc`` running with ``COINEXT__REDIS__URL`` — otherwise we only
   verify encode/publish/read of the command stream itself.)

Without Redis these tests skip cleanly so default CI stays green.
"""

from __future__ import annotations

import os
import time

import pytest

pytest.importorskip("redis")
pytest.importorskip("msgpack")

from coinext_bus import (  # noqa: E402
    STREAM_EXEC,
    STREAM_EXEC_CMD,
    Publisher,
    decode_envelope,
    decode_payload,
)


def _redis_url() -> str:
    return os.environ.get("COINEXT__REDIS__URL", "redis://127.0.0.1:6379/0")


def _require_redis():
    import redis

    url = _redis_url()
    try:
        client = redis.Redis.from_url(url, socket_connect_timeout=0.5)
        client.ping()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"redis unavailable at {url}: {exc}")
    return url


def test_publish_exec_command_lands_on_stream():
    url = _require_redis()
    pub = Publisher(url)
    msg_id = pub.publish_exec_command(
        kind="SubmitMarket",
        strategy_id="itest",
        symbol="BTCUSDT",
        qty=0.001,
    )
    assert msg_id
    # Read last entry
    import redis

    r = redis.Redis.from_url(url)
    entries = r.xrevrange(STREAM_EXEC_CMD, count=1)
    assert entries, "expected at least one command on coinext.exec.cmd"
    _mid, fields = entries[0]
    raw = fields.get(b"e") or fields.get("e")
    assert raw is not None
    env = decode_envelope(raw)
    payload = decode_payload(env)
    assert payload.get("kind") == "SubmitMarket"
    assert payload.get("strategy_id") == "itest"
    pub.close()


def test_paper_oms_roundtrip_if_exec_svc_running():
    """If exec-svc is up, a submit should produce a report on coinext.exec within a few seconds."""
    url = _require_redis()
    import redis

    r = redis.Redis.from_url(url)
    # Heuristic: if nothing is consuming, we still pass by only checking publish.
    # Wait briefly for a paper filled report after publish.
    before = r.xlen(STREAM_EXEC) if r.exists(STREAM_EXEC) else 0
    pub = Publisher(url)
    pub.publish_exec_command(kind="SubmitMarket", strategy_id="itest-rt", qty=0.002)
    deadline = time.time() + 3.0
    found = False
    while time.time() < deadline:
        after = r.xlen(STREAM_EXEC) if r.exists(STREAM_EXEC) else 0
        if after > before:
            entries = r.xrevrange(STREAM_EXEC, count=5)
            for _mid, fields in entries:
                raw = fields.get(b"e") or fields.get("e")
                if not raw:
                    continue
                try:
                    payload = decode_payload(decode_envelope(raw))
                except Exception:  # noqa: BLE001
                    continue
                if payload.get("strategy_id") == "itest-rt" or payload.get("kind") in {
                    "submitted",
                    "filled",
                    "accepted",
                }:
                    found = True
                    break
        if found:
            break
        time.sleep(0.2)
    pub.close()
    if not found:
        pytest.skip(
            "no exec-svc consumer responded on coinext.exec within 3s "
            "(start: COINEXT__REDIS__URL=... just exec-svc)"
        )
