"""Security tests for the control-plane API (operations-interface/api/service/api/app.py).

Asserts the trading-control surface is not reachable unauthenticated:

* mutating/control endpoints (POST /control/killswitch, POST /backtest) reject requests with no /
  wrong ``X-API-Key`` (401), and authorize past the auth gate when the key matches;
* the read-only liveness probe stays open and answers at both ``/health`` and ``/healthz`` (the path
  the Docker / docker-compose healthcheck probes);
* CORS never defaults to ``*``.

The API stack (fastapi/starlette/httpx) is an optional extra; skip cleanly when it is absent so the
core analytics test run stays dependency-light. The CI gate installs the `api` extra and runs these.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("starlette.testclient")

from starlette.testclient import TestClient  # noqa: E402

_APP_PATH = (
    pathlib.Path(__file__).resolve().parents[2]
    / "operations-interface"
    / "api"
    / "service"
    / "api"
    / "app.py"
)


def _load_app_module():
    """Load operations-interface/api/service/api/app.py by path (it is not an installed package)."""
    spec = importlib.util.spec_from_file_location("coinext_api_app_under_test", _APP_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Register before exec so dataclass/pydantic forward refs resolve cleanly.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def app_module(monkeypatch: pytest.MonkeyPatch):
    """Fresh import of the app with a known API key configured."""
    monkeypatch.setenv("COINEXT__API__KEY", "s3cret-key")
    monkeypatch.delenv("COINEXT__API__CORS_ORIGINS", raising=False)
    return _load_app_module()


class _FakeWebSocket:
    def __init__(self):
        self.sent: list[dict[str, object]] = []

    async def send_json(self, event):
        self.sent.append(event)


def test_killswitch_rejected_without_api_key(app_module):
    client = TestClient(app_module.app)
    resp = client.post("/control/killswitch", json={"engage": True, "reason": "test"})
    assert resp.status_code == 401


def test_killswitch_rejected_with_wrong_api_key(app_module):
    client = TestClient(app_module.app)
    resp = client.post(
        "/control/killswitch",
        json={"engage": True, "reason": "test"},
        headers={"X-API-Key": "wrong"},
    )
    assert resp.status_code == 401


def test_killswitch_authorized_with_api_key_records_event_and_state(
    app_module, monkeypatch: pytest.MonkeyPatch
):
    """An authorized killswitch call publishes, records an operator event, and updates read state.

    The fake ``coinext_bus`` module avoids redis/msgpack while still exercising the endpoint's
    observable contract: accepted POSTs publish one CtrlKillSwitch command, append one event for the
    UI timeline, and make GET /control/killswitch reflect the requested state.
    """
    import types

    published: list[dict[str, object]] = []

    class _FakePublisher:
        def __init__(self, url):
            self.url = url

        def publish_kill_switch(self, stream, *, engaged, reason, source, actor=None):
            published.append(
                {
                    "stream": stream,
                    "engaged": engaged,
                    "reason": reason,
                    "source": source,
                    "actor": actor,
                }
            )
            return "1-0"

    fake_bus = types.ModuleType("coinext_bus")
    fake_bus.Publisher = _FakePublisher  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "coinext_bus", fake_bus)

    client = TestClient(app_module.app)
    resp = client.post(
        "/control/killswitch",
        json={"engage": True, "reason": "test", "actor": "op"},
        headers={"X-API-Key": "s3cret-key"},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["engaged"] is True
    assert body["engaged_by"] == "op"
    assert body["reason"] == "test"
    assert published == [
        {
            "stream": app_module.STREAM_CONTROL,
            "engaged": True,
            "reason": "test",
            "source": "api",
            "actor": "op",
        }
    ]

    state_resp = client.get("/control/killswitch")
    assert state_resp.status_code == 200
    assert state_resp.json()["engaged"] is True
    assert state_resp.json()["engaged_by"] == "op"
    assert state_resp.json()["reason"] == "test"

    events_resp = client.get("/control/events")
    assert events_resp.status_code == 200
    events = events_resp.json()
    assert len(events) == 1
    event = events[0]
    assert event["seq"] == 1
    assert event["engaged"] is True
    assert event["source"] == "api"
    assert event["actor"] == "op"
    assert event["reason"] == "test"
    assert event["msg_id"] == "1-0"


def test_control_stream_projects_risk_monitor_killswitch_once(app_module):
    """Risk-monitor CtrlKillSwitch frames update the API projection and are deduped by msg_id."""
    import types

    from coinext_contracts import kill_switch_payload

    payload = kill_switch_payload(
        engaged=True,
        reason="max exposure breached",
        source="risk-monitor",
    )
    clients: list[object] = []

    class _FakeRedisBusClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.streams: list[str] = []
            self.closed = False
            clients.append(self)

        def consume(self, streams):
            self.streams = list(streams)
            yield types.SimpleNamespace(envelope=dict(payload), msg_id="risk-1")
            yield types.SimpleNamespace(envelope=dict(payload), msg_id="risk-1")

        def close(self):
            self.closed = True

    fake_bus = types.SimpleNamespace(
        RedisBusClient=_FakeRedisBusClient,
        decode_payload=lambda envelope: envelope,
    )

    app_module._consume_control_stream(fake_bus)

    assert len(clients) == 1
    assert clients[0].streams == [app_module.STREAM_CONTROL]
    assert clients[0].closed is True

    client = TestClient(app_module.app)
    state_resp = client.get("/control/killswitch")
    assert state_resp.status_code == 200
    state = state_resp.json()
    assert state["engaged"] is True
    assert state["engaged_by"] == "risk-monitor"
    assert state["reason"] == "max exposure breached"

    events_resp = client.get("/control/events")
    assert events_resp.status_code == 200
    events = events_resp.json()
    assert len(events) == 1
    event = events[0]
    assert event["seq"] == 1
    assert event["engaged"] is True
    assert event["source"] == "risk-monitor"
    assert event["actor"] is None
    assert event["reason"] == "max exposure breached"
    assert event["msg_id"] == "risk-1"


def test_live_stream_consumes_bus_telemetry_and_normalizes_event(
    app_module, monkeypatch: pytest.MonkeyPatch
):
    """Redis live telemetry is decoded, normalized, sent once, and the client is closed."""
    import asyncio
    import types

    clients: list[object] = []

    class _FakeRedisBusClient:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs
            self.streams: list[str] = []
            self.block_ms: int | None = None
            self.count: int | None = None
            self.closed = False
            clients.append(self)

        def consume(self, streams, *, block_ms, count):
            self.streams = list(streams)
            self.block_ms = block_ms
            self.count = count
            yield types.SimpleNamespace(
                envelope={"source": "live-runtime", "equity": "1000.00"},
                msg_id="live-1",
                stream=app_module.STREAM_LIVE,
            )

        def close(self):
            self.closed = True

    fake_bus = types.SimpleNamespace(
        RedisBusClient=_FakeRedisBusClient,
        decode_payload=lambda envelope: dict(envelope),
    )
    monkeypatch.setattr(app_module, "_load_bus", lambda: fake_bus)
    ws = _FakeWebSocket()

    streamed = asyncio.run(app_module._bus_live_stream(ws))

    assert streamed is True
    assert len(clients) == 1
    client = clients[0]
    assert client.streams == [app_module.STREAM_LIVE]
    assert client.block_ms == 1000
    assert client.count == 1
    assert client.closed is True
    assert ws.sent == [
        {
            "source": "live-runtime",
            "equity": "1000.00",
            "type": "position_update",
            "msg_id": "live-1",
            "stream": app_module.STREAM_LIVE,
        }
    ]


def test_live_stream_returns_false_without_bus_and_sends_nothing(
    app_module, monkeypatch: pytest.MonkeyPatch
):
    """Missing bus support returns False so /ws/live can fall back to the synthetic stub."""
    import asyncio

    monkeypatch.setattr(app_module, "_load_bus", lambda: None)
    ws = _FakeWebSocket()

    streamed = asyncio.run(app_module._bus_live_stream(ws))

    assert streamed is False
    assert ws.sent == []


def test_live_stream_returns_false_and_closes_when_redis_consume_fails(
    app_module, monkeypatch: pytest.MonkeyPatch
):
    """Redis availability errors are contained and leave the API able to use the stub stream."""
    import asyncio
    import types

    clients: list[object] = []

    class _FailingRedisBusClient:
        def __init__(self, *args, **kwargs):
            self.closed = False
            clients.append(self)

        def consume(self, streams, *, block_ms, count):
            raise RuntimeError("redis unavailable")

        def close(self):
            self.closed = True

    fake_bus = types.SimpleNamespace(
        RedisBusClient=_FailingRedisBusClient,
        decode_payload=lambda envelope: envelope,
    )
    monkeypatch.setattr(app_module, "_load_bus", lambda: fake_bus)
    ws = _FakeWebSocket()

    streamed = asyncio.run(app_module._bus_live_stream(ws))

    assert streamed is False
    assert len(clients) == 1
    assert clients[0].closed is True
    assert ws.sent == []


def test_backtest_requires_api_key(app_module):
    client = TestClient(app_module.app)
    # No key -> 401 before any backtest work (which would otherwise 503 without coinext_py).
    resp = client.post("/backtest", json={})
    assert resp.status_code == 401


def test_health_open_and_aliased(app_module):
    client = TestClient(app_module.app)
    for path in ("/health", "/healthz"):
        resp = client.get(path)
        assert resp.status_code == 200, path
        assert resp.json()["status"] == "ok"


def test_get_killswitch_stays_open(app_module):
    client = TestClient(app_module.app)
    resp = client.get("/control/killswitch")
    assert resp.status_code == 200


def test_fail_closed_when_key_unset(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("COINEXT__API__KEY", raising=False)
    module = _load_app_module()
    client = TestClient(module.app)
    # Unconfigured key -> control endpoint fails closed (503), never silently open.
    resp = client.post(
        "/control/killswitch",
        json={"engage": True},
        headers={"X-API-Key": "anything"},
    )
    assert resp.status_code == 503


def test_cors_does_not_default_to_wildcard(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COINEXT__API__KEY", "s3cret-key")
    monkeypatch.delenv("COINEXT__API__CORS_ORIGINS", raising=False)
    module = _load_app_module()
    client = TestClient(module.app)
    resp = client.get("/health", headers={"Origin": "https://evil.example"})
    # With no configured origins, the middleware must not echo an allow-origin for an arbitrary host.
    assert resp.headers.get("access-control-allow-origin") != "*"
    assert resp.headers.get("access-control-allow-origin") != "https://evil.example"
