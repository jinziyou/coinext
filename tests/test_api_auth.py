"""Security tests for the control-plane API (services/api/app.py).

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

_APP_PATH = pathlib.Path(__file__).resolve().parents[1] / "services" / "api" / "app.py"


def _load_app_module():
    """Load services/api/app.py by path (it is not an installed package)."""
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


def test_killswitch_authorized_with_api_key(app_module):
    client = TestClient(app_module.app)
    resp = client.post(
        "/control/killswitch",
        json={"engage": True, "reason": "test", "actor": "op"},
        headers={"X-API-Key": "s3cret-key"},
    )
    # The correct key authorizes the request: it must get *past* the auth gate (never 401/403).
    # Downstream the endpoint either mirrors state locally (200, no bus) or surfaces a bus/redis
    # error (503) — both mean auth succeeded, which is what this security test asserts.
    assert resp.status_code not in (401, 403)
    if resp.status_code == 200:
        body = resp.json()
        assert body["engaged"] is True
        assert body["engaged_by"] == "op"


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
