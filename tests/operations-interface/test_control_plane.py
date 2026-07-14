"""Control-plane wiring tests: the Redis bus Publisher / control dispatch, the risk-monitor consume
loop, the live node's kill-switch subscriber, and layered config resolution.

These run in the DEPENDENCY-LIGHT default environment (no redis/msgpack/prometheus): every payload
is a plain dict and the MessagePack codec is stubbed via monkeypatch, so the wiring is exercised
without the bus extra installed.
"""

from __future__ import annotations

import importlib.util
import pathlib
import sys
import types

import coinext_bus
import pytest
from coinext_contracts import (
    CTRL_KILL_SWITCH,
    LIVE_POSITION_PNL,
    Envelope,
    MsgType,
    is_kill_switch,
    is_live_position_pnl,
    kill_switch_payload,
    live_position_pnl_payload,
)

_RISK_MONITOR = (
    pathlib.Path(__file__).resolve().parents[2]
    / "risk-portfolio"
    / "services"
    / "risk-monitor"
    / "main.py"
)
_TRADER = (
    pathlib.Path(__file__).resolve().parents[2]
    / "execution-live"
    / "services"
    / "trader"
    / "main.py"
)


def _load_risk_monitor():
    """Load risk-portfolio/services/risk-monitor/main.py by path (it is not an installed package)."""
    spec = importlib.util.spec_from_file_location("coinext_risk_monitor_under_test", _RISK_MONITOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_trader_service():
    """Load execution-live/services/trader/main.py by path (it is not an installed package)."""
    spec = importlib.util.spec_from_file_location("coinext_trader_service_under_test", _TRADER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------------------------------
# coinext_contracts — kill-switch payload contract
# --------------------------------------------------------------------------------------------------


def test_kill_switch_payload_shape():
    p = kill_switch_payload(engaged=True, reason="r", source="api", actor="op")
    assert p == {
        "kind": CTRL_KILL_SWITCH,
        "engaged": True,
        "reason": "r",
        "source": "api",
        "actor": "op",
    }
    assert is_kill_switch(p)
    assert not is_kill_switch({"kind": "SomethingElse"})
    assert not is_kill_switch({})


def test_live_position_pnl_payload_shape_and_wire_scalars():
    """Live telemetry payloads expose the documented kind and stringify numeric money/risk fields."""
    positions = [{"symbol": "ETHUSDT", "qty": "0.50", "side": "long"}]

    payload = live_position_pnl_payload(
        account_id="acct-1",
        environment="sandbox",
        symbol="ETHUSDT",
        venue="BINANCE",
        equity=1000.25,
        gross_exposure=500,
        net_exposure=-125.5,
        realized_pnl=12,
        unrealized_pnl=-3.75,
        positions=positions,
        ts_ns=123456789,
        source="trader-a",
    )

    assert payload == {
        "kind": LIVE_POSITION_PNL,
        "account_id": "acct-1",
        "environment": "sandbox",
        "symbol": "ETHUSDT",
        "venue": "BINANCE",
        "equity": "1000.25",
        "gross_exposure": "500",
        "net_exposure": "-125.5",
        "realized_pnl": "12",
        "unrealized_pnl": "-3.75",
        "positions": positions,
        "ts_ns": 123456789,
        "source": "trader-a",
    }
    assert is_live_position_pnl(payload) is True
    assert is_live_position_pnl({"kind": "OtherTelemetry"}) is False


# --------------------------------------------------------------------------------------------------
# coinext_bus — Publisher + dispatch_control (codec stubbed; no msgpack/redis needed)
# --------------------------------------------------------------------------------------------------


class _FakeRedisClient:
    """Captures publishes; stands in for coinext_bus.RedisBusClient."""

    def __init__(self, url="redis://x"):
        self.url = url
        self.published: list[tuple[str, Envelope]] = []
        self.closed = False

    def publish(self, stream, env):
        self.published.append((stream, env))
        return f"{len(self.published)}-0"

    def close(self):
        self.closed = True


def test_publisher_publish_control_builds_ctrl_envelope(monkeypatch):
    # Stub the msgpack codec so no real msgpack is required.
    monkeypatch.setattr(coinext_bus, "encode_payload", lambda payload: ("ENC", payload))
    fake = _FakeRedisClient()
    pub = coinext_bus.Publisher("redis://x")
    monkeypatch.setattr(pub, "client", fake)

    msg_id = pub.publish_kill_switch(
        "coinext.control", engaged=True, reason="breach", source="api", actor="op"
    )
    assert msg_id == "1-0"
    assert len(fake.published) == 1
    stream, env = fake.published[0]
    assert stream == "coinext.control"
    assert env.msg_type == MsgType.CTRL
    assert len(env.trace_id) == 16  # 16-byte correlation id
    # Payload was encoded from the documented kill-switch map.
    assert env.payload == (
        "ENC",
        kill_switch_payload(engaged=True, reason="breach", source="api", actor="op"),
    )


def test_publisher_publish_live_telemetry_builds_live_payload_envelope(monkeypatch):
    """Publisher sends the live telemetry payload on the live stream and returns the bus message id."""
    monkeypatch.setattr(coinext_bus, "encode_payload", lambda payload: ("ENC", payload))
    monkeypatch.setattr(coinext_bus, "decode_payload", lambda env: env.payload[1])

    class _CaptureClient:
        def __init__(self):
            self.published: list[tuple[str, Envelope]] = []

        def publish(self, stream, env):
            self.published.append((stream, env))
            return "telemetry-42-0"

    fake = _CaptureClient()
    pub = coinext_bus.Publisher("redis://telemetry")
    monkeypatch.setattr(pub, "client", fake)

    msg_id = pub.publish_live_telemetry(
        coinext_bus.STREAM_LIVE,
        account_id="acct-live",
        environment="live",
        symbol="BTCUSDT",
        venue="BINANCE",
        equity=1234.5,
        gross_exposure=600.0,
        net_exposure=100.0,
        realized_pnl=7.25,
        unrealized_pnl=-2.5,
        positions=[{"symbol": "BTCUSDT", "qty": "0.01"}],
        ts_ns=99,
        source="trader",
    )

    assert msg_id == "telemetry-42-0"
    assert len(fake.published) == 1
    stream, env = fake.published[0]
    assert stream == "coinext.live"
    assert stream == coinext_bus.STREAM_LIVE
    assert env.msg_type == MsgType.CMD
    payload = coinext_bus.decode_payload(env)
    assert payload["kind"] == LIVE_POSITION_PNL
    assert payload["account_id"] == "acct-live"
    assert payload["environment"] == "live"
    assert payload["symbol"] == "BTCUSDT"
    assert payload["venue"] == "BINANCE"
    assert payload["equity"] == "1234.5"
    assert payload["gross_exposure"] == "600.0"
    assert payload["net_exposure"] == "100.0"
    assert payload["realized_pnl"] == "7.25"
    assert payload["unrealized_pnl"] == "-2.5"
    assert payload["positions"] == [{"symbol": "BTCUSDT", "qty": "0.01"}]
    assert payload["ts_ns"] == 99
    assert payload["source"] == "trader"


def test_dispatch_control_fires_on_engaged_kill(monkeypatch):
    payload = kill_switch_payload(engaged=True, reason="halt", source="risk-monitor")
    monkeypatch.setattr(coinext_bus, "decode_payload", lambda env: payload)
    env = Envelope.of(MsgType.CTRL, b"\x00" * 16, 0, b"ignored")

    seen: list[str] = []
    fired = coinext_bus.dispatch_control(env, seen.append)
    assert fired is True
    assert seen == ["halt"]


def test_dispatch_control_ignores_release_and_noncontrol(monkeypatch):
    # engaged=False release -> no kill.
    release = kill_switch_payload(engaged=False, reason="resume", source="api")
    monkeypatch.setattr(coinext_bus, "decode_payload", lambda env: release)
    env = Envelope.of(MsgType.CTRL, b"\x00" * 16, 0, b"x")
    seen: list[str] = []
    assert coinext_bus.dispatch_control(env, seen.append) is False
    assert seen == []

    # Non-CTRL envelope -> dispatch never even decodes.
    def _boom(_env):
        raise AssertionError("should not decode a non-CTRL envelope")

    monkeypatch.setattr(coinext_bus, "decode_payload", _boom)
    quote = Envelope.of(MsgType.QUOTE, b"\x00" * 16, 0, b"x")
    assert coinext_bus.dispatch_control(quote, seen.append) is False


# --------------------------------------------------------------------------------------------------
# coinext_cli — kill-switch command posts the API contract without touching the network
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("release", "response_engaged", "expected_payload_engage", "expected_status"),
    [
        pytest.param(False, True, True, "ENGAGED", id="engage"),
        pytest.param(True, False, False, "released", id="release"),
    ],
)
def test_cli_kill_switch_posts_json_header_and_reports_state(
    monkeypatch,
    capsys,
    release,
    response_engaged,
    expected_payload_engage,
    expected_status,
):
    """The CLI sends the control endpoint's JSON/header contract and reports the returned state."""
    import json
    import urllib.request

    import coinext_cli.main as cli

    requests: list[urllib.request.Request] = []

    class _FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "engaged": response_engaged,
                    "reason": "acknowledged by api",
                    "engaged_by": "ops",
                    "ts_changed": "2026-07-09T00:00:00+00:00",
                }
            ).encode("utf-8")

    def _fake_urlopen(req, timeout):
        requests.append(req)
        assert timeout == 10
        return _FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", _fake_urlopen)

    rc = cli._cmd_kill_switch(
        release=release,
        reason="operator requested",
        actor="ops",
        api_base="https://api.example.test/",
        api_key="secret-key",
    )

    assert rc == 0
    assert len(requests) == 1
    req = requests[0]
    assert req.full_url == "https://api.example.test/control/killswitch"
    assert req.get_method() == "POST"
    headers = {name.lower(): value for name, value in req.header_items()}
    assert headers["content-type"] == "application/json"
    assert headers["x-api-key"] == "secret-key"
    assert json.loads(req.data.decode("utf-8")) == {
        "engage": expected_payload_engage,
        "reason": "operator requested",
        "actor": "ops",
    }
    out = capsys.readouterr().out
    assert f"kill-switch {expected_status}" in out
    assert "reason='acknowledged by api'" in out
    assert "by='ops'" in out


def test_cli_kill_switch_missing_api_key_returns_2_without_network(
    monkeypatch,
    capsys,
):
    """Missing API credentials fail closed before constructing a network request."""
    import urllib.request

    import coinext_cli.main as cli

    def _fail_network(*args, **kwargs):
        raise AssertionError("missing-api-key path must not call urlopen")

    monkeypatch.delenv("COINEXT__API__KEY", raising=False)
    monkeypatch.setattr(urllib.request, "urlopen", _fail_network)

    rc = cli._cmd_kill_switch(api_base="https://api.example.test", api_key=None)

    assert rc == 2
    assert "missing API key" in capsys.readouterr().out


# --------------------------------------------------------------------------------------------------
# coinext_kernel — native live-kernel builder boundary
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("env_value", ["sandbox", "live"])
def test_kernel_build_kernel_delegates_live_envs_to_native_builder_with_attribute_config(
    monkeypatch, env_value
):
    """coinext_kernel.build_kernel passes env.value, normalized config, and strategy to coinext_py."""
    import coinext_kernel
    from coinext_kernel import Environment

    strategy = object()
    native_handle = object()
    calls = []

    def _build_kernel(env_arg, config_arg, strategy_arg):
        calls.append((env_arg, config_arg, strategy_arg))
        assert env_arg == env_value
        assert config_arg.symbol == "BTCUSDT"
        assert config_arg.redis.url == "redis://unit:6379/3"
        assert config_arg.risk.limits.max_notional == 12_500
        assert not isinstance(config_arg.redis, dict)
        assert not isinstance(config_arg.risk.limits, dict)
        assert strategy_arg is strategy
        return native_handle

    monkeypatch.setitem(
        sys.modules,
        "coinext_py",
        types.SimpleNamespace(build_kernel=_build_kernel),
    )
    config = {
        "symbol": "BTCUSDT",
        "redis": {"url": "redis://unit:6379/3"},
        "risk": {"limits": {"max_notional": 12_500}},
    }

    handle = coinext_kernel.build_kernel(config, Environment(env_value), strategy=strategy)

    assert handle is native_handle
    assert len(calls) == 1


def test_kernel_build_kernel_rejects_backtest_without_native_import(monkeypatch):
    """Backtests stay on the public backtest API; build_kernel must not touch coinext_py for them."""
    import coinext_kernel
    from coinext_kernel import Environment

    def _unexpected_build(*args, **kwargs):
        raise AssertionError("backtest build_kernel must not call coinext_py.build_kernel")

    monkeypatch.setitem(
        sys.modules,
        "coinext_py",
        types.SimpleNamespace(build_kernel=_unexpected_build),
    )

    with pytest.raises(NotImplementedError, match="backtests"):
        coinext_kernel.build_kernel({}, Environment.BACKTEST, strategy=object())


@pytest.mark.parametrize("env_value", ["sandbox", "live"])
def test_kernel_build_kernel_rejects_live_env_without_strategy(monkeypatch, env_value):
    """Sandbox/live native kernels require a strategy object before coinext_py is consulted."""
    import coinext_kernel

    def _unexpected_build(*args, **kwargs):
        raise AssertionError("missing strategy must fail before coinext_py.build_kernel")

    monkeypatch.setitem(
        sys.modules,
        "coinext_py",
        types.SimpleNamespace(build_kernel=_unexpected_build),
    )

    with pytest.raises(ValueError, match="requires a Strategy instance"):
        coinext_kernel.build_kernel({}, env_value)


def test_live_node_run_builds_native_kernel_and_publishes_callback_snapshot(monkeypatch):
    """TradingNode.run loads config, attaches the native kernel, and publishes callback snapshots."""
    import asyncio

    import coinext_kernel
    from coinext_kernel import Environment
    from coinext_live import TradingNode, TradingNodeConfig

    strategy = object()
    loaded_config = types.SimpleNamespace(name="loaded-run-config")
    native_snapshot = object()
    load_calls = []
    build_calls = []
    publish_calls = []

    def _load_config(*, env, cli_overrides):
        load_calls.append({"env": env, "cli_overrides": cli_overrides})
        return loaded_config

    class _NativeKernel:
        def __init__(self):
            self.run_calls = 0

        def run_with_portfolio_callback(self, callback):
            self.run_calls += 1
            assert node._running is True
            callback(native_snapshot)

    native_kernel = _NativeKernel()

    def _build_kernel(config, env, *, strategy):
        build_calls.append({"config": config, "env": env, "strategy": strategy})
        return native_kernel

    monkeypatch.setitem(
        sys.modules, "coinext_config", types.SimpleNamespace(load_config=_load_config)
    )
    monkeypatch.setattr(coinext_kernel, "build_kernel", _build_kernel)
    monkeypatch.setattr(TradingNode, "warmup", lambda self: [])

    node = TradingNode(
        config=TradingNodeConfig(
            env=Environment.SANDBOX,
            account_id="acct-run",
            symbol="SOLUSDT",
            venue="BINANCE",
            redis_url="redis://unit:6379/10",
            reconcile_on_start=False,
        ),
        strategy=strategy,
    )

    def _publish_native_snapshot(snapshot):
        publish_calls.append({"snapshot": snapshot, "running": node._running})
        return "callback-1-0"

    monkeypatch.setattr(node, "publish_native_snapshot", _publish_native_snapshot)

    asyncio.run(node.run())

    assert load_calls == [
        {
            "env": "sandbox",
            "cli_overrides": {
                "env": "sandbox",
                "symbol": "SOLUSDT",
                "redis": {"url": "redis://unit:6379/10"},
                "venue": {"name": "BINANCE"},
            },
        }
    ]
    assert len(build_calls) == 1
    assert build_calls[0]["config"] is loaded_config
    assert build_calls[0]["env"] is Environment.SANDBOX
    assert build_calls[0]["strategy"] is strategy
    assert node._kernel is native_kernel
    assert native_kernel.run_calls == 1
    assert publish_calls == [{"snapshot": native_snapshot, "running": True}]
    assert node._running is False


@pytest.mark.parametrize("kernel_stop_method", ["request_stop", "stop"])
def test_live_node_stop_signals_native_kernel_and_closes_publisher(kernel_stop_method):
    """TradingNode.stop wakes the attached native kernel and closes telemetry publication."""
    from coinext_kernel import Environment
    from coinext_live import TradingNode, TradingNodeConfig

    class _NativeKernel:
        def __init__(self):
            self.calls: list[str] = []

    class _Publisher:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    kernel = _NativeKernel()
    setattr(kernel, kernel_stop_method, lambda: kernel.calls.append(kernel_stop_method))
    publisher = _Publisher()
    node = TradingNode(config=TradingNodeConfig(env=Environment.SANDBOX), strategy=object())
    node._kernel = kernel
    node._telemetry_publisher = publisher
    node._running = True

    node.stop()

    assert kernel.calls == [kernel_stop_method]
    assert publisher.closed is True
    assert node._running is False


# --------------------------------------------------------------------------------------------------
# coinext_live — control subscriber engages the node kill-switch
# --------------------------------------------------------------------------------------------------


def test_live_on_control_message_engages_kill_switch(monkeypatch):
    from coinext_kernel import Environment
    from coinext_live import TradingNode, TradingNodeConfig, on_control_message

    class _NativeKernel:
        def __init__(self):
            self.request_stop_calls = 0

        def request_stop(self):
            self.request_stop_calls += 1

    class _Publisher:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    kernel = _NativeKernel()
    publisher = _Publisher()
    node = TradingNode(config=TradingNodeConfig(env=Environment.LIVE), strategy=object())
    node._kernel = kernel
    node._telemetry_publisher = publisher
    node._running = True
    assert not node.killed

    payload = kill_switch_payload(engaged=True, reason="global halt", source="api")
    monkeypatch.setattr(coinext_bus, "decode_payload", lambda env: payload)
    env = Envelope.of(MsgType.CTRL, b"\x00" * 16, 0, b"x")

    fired = on_control_message(env, node.engage_kill_switch)
    assert fired is True
    assert node.killed is True
    assert node._kill_reason == "global halt"
    assert kernel.request_stop_calls == 1
    assert publisher.closed is True
    assert node._running is False

    # Idempotent: a second engage does not re-signal the kernel or change reason.
    node.engage_kill_switch("other")
    assert node._kill_reason == "global halt"
    assert kernel.request_stop_calls == 1


def test_live_node_publish_telemetry_wires_config_to_bus_and_closes(monkeypatch):
    """TradingNode publishes snapshots with config-derived routing and closes its cached publisher."""
    from coinext_kernel import Environment
    from coinext_live import TradingNode, TradingNodeConfig

    publishers = []

    class _FakePublisher:
        def __init__(self, url):
            self.url = url
            self.calls = []
            self.closed = False
            publishers.append(self)

        def publish_live_telemetry(self, stream, **payload):
            self.calls.append({"stream": stream, **payload})
            return "7-0"

        def close(self):
            self.closed = True

    monkeypatch.setitem(sys.modules, "coinext_bus", types.SimpleNamespace(Publisher=_FakePublisher))

    node = TradingNode(
        config=TradingNodeConfig(
            env=Environment.SANDBOX,
            account_id="acct-telemetry",
            symbol="SOLUSDT",
            venue="BINANCE",
            redis_url="redis://unit:6379/4",
            telemetry_stream="custom.live",
        ),
        strategy=object(),
    )

    msg_id = node.publish_telemetry(
        equity=10_000.0,
        gross_exposure=2500.0,
        net_exposure=-500.0,
        realized_pnl=11.5,
        unrealized_pnl=-4.25,
        positions=[{"symbol": "SOLUSDT", "qty": "10"}],
        ts_ns=456,
    )

    assert msg_id == "7-0"
    assert len(publishers) == 1
    publisher = publishers[0]
    assert publisher.url == "redis://unit:6379/4"
    assert publisher.calls == [
        {
            "stream": "custom.live",
            "account_id": "acct-telemetry",
            "environment": "sandbox",
            "symbol": "SOLUSDT",
            "venue": "BINANCE",
            "equity": 10_000.0,
            "gross_exposure": 2500.0,
            "net_exposure": -500.0,
            "realized_pnl": 11.5,
            "unrealized_pnl": -4.25,
            "positions": [{"symbol": "SOLUSDT", "qty": "10"}],
            "ts_ns": 456,
            "source": "trader",
        }
    ]

    node.stop()

    assert publisher.closed is True


# --------------------------------------------------------------------------------------------------
# coinext_portfolio -> coinext_live — offline portfolio telemetry adapter
# --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("position", "expected_side", "expected_unrealized", "expected_notional", "expected_row"),
    [
        pytest.param(
            {
                "symbol": "BTCUSDT",
                "venue": "BINANCE",
                "net_qty": 2.0,
                "avg_price": 100.0,
                "mark_price": 115.0,
                "realized_pnl": 3.25,
            },
            "long",
            30.0,
            230.0,
            {
                "symbol": "BTCUSDT",
                "venue": "BINANCE",
                "side": "long",
                "net_qty": "2.0",
                "avg_price": "100.0",
                "mark_price": "115.0",
                "realized_pnl": "3.25",
                "unrealized_pnl": "30.0",
                "notional": "230.0",
            },
            id="long",
        ),
        pytest.param(
            {
                "symbol": "ETHUSDT",
                "venue": "BINANCE",
                "net_qty": -1.5,
                "avg_price": 250.0,
                "mark_price": 200.0,
                "realized_pnl": -4.0,
            },
            "short",
            75.0,
            300.0,
            {
                "symbol": "ETHUSDT",
                "venue": "BINANCE",
                "side": "short",
                "net_qty": "-1.5",
                "avg_price": "250.0",
                "mark_price": "200.0",
                "realized_pnl": "-4.0",
                "unrealized_pnl": "75.0",
                "notional": "300.0",
            },
            id="short",
        ),
        pytest.param(
            {
                "symbol": "XRPUSDT",
                "venue": "COINBASE",
                "net_qty": 0.0,
                "avg_price": 0.25,
                "mark_price": 0.30,
                "realized_pnl": 0.0,
            },
            "flat",
            0.0,
            0.0,
            {
                "symbol": "XRPUSDT",
                "venue": "COINBASE",
                "side": "flat",
                "net_qty": "0.0",
                "avg_price": "0.25",
                "mark_price": "0.3",
                "realized_pnl": "0.0",
                "unrealized_pnl": "0.0",
                "notional": "0.0",
            },
            id="flat",
        ),
    ],
)
def test_position_view_telemetry_row_projects_side_and_wire_safe_numeric_fields(
    position,
    expected_side,
    expected_unrealized,
    expected_notional,
    expected_row,
):
    """Position rows expose signed side and stringified numeric fields for the live telemetry wire."""
    from coinext_portfolio import PositionView

    view = PositionView(**position)

    assert view.side == expected_side
    assert view.unrealized_pnl == pytest.approx(expected_unrealized)
    assert view.notional == pytest.approx(expected_notional)

    row = view.telemetry_row()
    assert row == expected_row
    for key in (
        "net_qty",
        "avg_price",
        "mark_price",
        "realized_pnl",
        "unrealized_pnl",
        "notional",
    ):
        assert isinstance(row[key], str)


def test_portfolio_aggregates_long_and_short_positions_for_live_telemetry():
    """Portfolio aggregate methods compute account telemetry from both long and short exposures."""
    from coinext_portfolio import AccountView, Portfolio, PositionView

    portfolio = Portfolio(
        account=AccountView(cash_balance=1000.0, realized_pnl=12.5),
        positions={
            "BTCUSDT": PositionView(
                symbol="BTCUSDT",
                venue="BINANCE",
                net_qty=2.0,
                avg_price=100.0,
                mark_price=115.0,
                realized_pnl=3.25,
            ),
            "ETHUSDT": PositionView(
                symbol="ETHUSDT",
                venue="BINANCE",
                net_qty=-1.5,
                avg_price=250.0,
                mark_price=200.0,
                realized_pnl=-4.0,
            ),
        },
    )

    assert portfolio.total_equity() == pytest.approx(1117.5)
    assert portfolio.gross_exposure() == pytest.approx(530.0)
    assert portfolio.net_exposure() == pytest.approx(-70.0)
    assert portfolio.unrealized_pnl() == pytest.approx(105.0)
    assert portfolio.realized_pnl() == pytest.approx(12.5)
    assert portfolio.telemetry_positions() == [
        {
            "symbol": "BTCUSDT",
            "venue": "BINANCE",
            "side": "long",
            "net_qty": "2.0",
            "avg_price": "100.0",
            "mark_price": "115.0",
            "realized_pnl": "3.25",
            "unrealized_pnl": "30.0",
            "notional": "230.0",
        },
        {
            "symbol": "ETHUSDT",
            "venue": "BINANCE",
            "side": "short",
            "net_qty": "-1.5",
            "avg_price": "250.0",
            "mark_price": "200.0",
            "realized_pnl": "-4.0",
            "unrealized_pnl": "75.0",
            "notional": "300.0",
        },
    ]


def test_portfolio_from_native_backtest_snapshot_preserves_authoritative_fields():
    """Portfolio.from_native preserves native equity, exposure, and row PnL fields."""
    from coinext_portfolio import Portfolio

    native_snapshot = types.SimpleNamespace(
        cash_balance=1000.0,
        realized_pnl=7.0,
        equity=1300.0,
        gross_exposure=10900.0,
        net_exposure=-700.0,
        unrealized_pnl=300.0,
        positions=[
            ("BTCUSDT", "BINANCE", 0.1, 50000.0, 51000.0, 2.0, 100.0, 5100.0),
            ("ETHUSDT", "BINANCE", -2.0, 3000.0, 2900.0, -1.0, 200.0, 5800.0),
        ],
    )
    result = types.SimpleNamespace(portfolio=native_snapshot)

    portfolio = Portfolio.from_native(result)

    assert portfolio.total_equity() == pytest.approx(1300.0)
    assert portfolio.gross_exposure() == pytest.approx(10900.0)
    assert portfolio.net_exposure() == pytest.approx(-700.0)
    assert portfolio.realized_pnl() == pytest.approx(7.0)
    assert portfolio.unrealized_pnl() == pytest.approx(300.0)
    assert portfolio.telemetry_positions() == [
        {
            "symbol": "BTCUSDT",
            "venue": "BINANCE",
            "side": "long",
            "net_qty": "0.1",
            "avg_price": "50000.0",
            "mark_price": "51000.0",
            "realized_pnl": "2.0",
            "unrealized_pnl": "100.0",
            "notional": "5100.0",
        },
        {
            "symbol": "ETHUSDT",
            "venue": "BINANCE",
            "side": "short",
            "net_qty": "-2.0",
            "avg_price": "3000.0",
            "mark_price": "2900.0",
            "realized_pnl": "-1.0",
            "unrealized_pnl": "200.0",
            "notional": "5800.0",
        },
    ]


def test_live_node_publish_portfolio_projects_real_portfolio_to_fake_bus(monkeypatch):
    """TradingNode.publish_portfolio delegates a real Portfolio snapshot to publish_live_telemetry."""
    from coinext_kernel import Environment
    from coinext_live import TradingNode, TradingNodeConfig
    from coinext_portfolio import AccountView, Portfolio, PositionView

    publishers = []

    class _FakePublisher:
        def __init__(self, url):
            self.url = url
            self.calls = []
            publishers.append(self)

        def publish_live_telemetry(self, stream, **payload):
            self.calls.append({"stream": stream, **payload})
            return "portfolio-1-0"

    monkeypatch.setitem(sys.modules, "coinext_bus", types.SimpleNamespace(Publisher=_FakePublisher))

    portfolio = Portfolio(
        account=AccountView(cash_balance=2500.0, realized_pnl=-8.5),
        positions={
            "SOLUSDT": PositionView(
                symbol="SOLUSDT",
                venue="BINANCE",
                net_qty=10.0,
                avg_price=20.0,
                mark_price=25.0,
                realized_pnl=1.0,
            ),
            "ADAUSDT": PositionView(
                symbol="ADAUSDT",
                venue="BINANCE",
                net_qty=-1.0,
                avg_price=80.0,
                mark_price=75.0,
                realized_pnl=-2.0,
            ),
        },
    )
    node = TradingNode(
        config=TradingNodeConfig(
            env=Environment.SANDBOX,
            account_id="acct-portfolio",
            symbol="SOLUSDT",
            venue="BINANCE",
            redis_url="redis://unit:6379/6",
            telemetry_stream="portfolio.live",
        ),
        strategy=object(),
    )

    msg_id = node.publish_portfolio(portfolio, ts_ns=987654321)

    assert msg_id == "portfolio-1-0"
    assert len(publishers) == 1
    assert publishers[0].url == "redis://unit:6379/6"
    assert publishers[0].calls == [
        {
            "stream": "portfolio.live",
            "account_id": "acct-portfolio",
            "environment": "sandbox",
            "symbol": "SOLUSDT",
            "venue": "BINANCE",
            "equity": 2546.5,
            "gross_exposure": 325.0,
            "net_exposure": 175.0,
            "realized_pnl": -8.5,
            "unrealized_pnl": 55.0,
            "positions": [
                {
                    "symbol": "SOLUSDT",
                    "venue": "BINANCE",
                    "side": "long",
                    "net_qty": "10.0",
                    "avg_price": "20.0",
                    "mark_price": "25.0",
                    "realized_pnl": "1.0",
                    "unrealized_pnl": "50.0",
                    "notional": "250.0",
                },
                {
                    "symbol": "ADAUSDT",
                    "venue": "BINANCE",
                    "side": "short",
                    "net_qty": "-1.0",
                    "avg_price": "80.0",
                    "mark_price": "75.0",
                    "realized_pnl": "-2.0",
                    "unrealized_pnl": "5.0",
                    "notional": "75.0",
                },
            ],
            "ts_ns": 987654321,
            "source": "trader",
        }
    ]


def test_live_node_publish_native_snapshot_adapts_authoritative_native_values_to_bus(monkeypatch):
    """TradingNode.publish_native_snapshot publishes a native portfolio snapshot via the bus contract."""
    from coinext_kernel import Environment
    from coinext_live import TradingNode, TradingNodeConfig

    publishers = []

    class _FakePublisher:
        def __init__(self, url):
            self.url = url
            self.calls = []
            self.closed = False
            publishers.append(self)

        def publish_live_telemetry(self, stream, **payload):
            self.calls.append({"stream": stream, **payload})
            return "native-1-0"

        def close(self):
            self.closed = True

    monkeypatch.setitem(sys.modules, "coinext_bus", types.SimpleNamespace(Publisher=_FakePublisher))

    native_snapshot = types.SimpleNamespace(
        cash_balance=1.0,  # ignored when native equity is authoritative
        realized_pnl=-14.0,
        equity=9200.0,
        gross_exposure=8250.0,
        net_exposure=4150.0,
        unrealized_pnl=125.0,
        positions=[
            ("BTCUSDT", "BINANCE", 0.2, 30000.0, 31000.0, 3.0, 75.0, 6200.0),
            ("ETHUSDT", "OKX", -1.0, 2100.0, 2050.0, -2.0, 50.0, 2050.0),
        ],
    )
    node = TradingNode(
        config=TradingNodeConfig(
            env=Environment.LIVE,
            account_id="acct-native",
            symbol="BTCUSDT",
            venue="COINBASE",
            redis_url="redis://unit:6379/7",
            telemetry_stream="native.live",
        ),
        strategy=object(),
    )

    msg_id = node.publish_native_snapshot(native_snapshot, ts_ns=1122334455)

    assert msg_id == "native-1-0"
    assert len(publishers) == 1
    publisher = publishers[0]
    assert publisher.url == "redis://unit:6379/7"
    assert publisher.calls == [
        {
            "stream": "native.live",
            "account_id": "acct-native",
            "environment": "live",
            "symbol": "BTCUSDT",
            "venue": "COINBASE",
            "equity": 9200.0,
            "gross_exposure": 8250.0,
            "net_exposure": 4150.0,
            "realized_pnl": -14.0,
            "unrealized_pnl": 125.0,
            "positions": [
                {
                    "symbol": "BTCUSDT",
                    "venue": "BINANCE",
                    "side": "long",
                    "net_qty": "0.2",
                    "avg_price": "30000.0",
                    "mark_price": "31000.0",
                    "realized_pnl": "3.0",
                    "unrealized_pnl": "75.0",
                    "notional": "6200.0",
                },
                {
                    "symbol": "ETHUSDT",
                    "venue": "OKX",
                    "side": "short",
                    "net_qty": "-1.0",
                    "avg_price": "2100.0",
                    "mark_price": "2050.0",
                    "realized_pnl": "-2.0",
                    "unrealized_pnl": "50.0",
                    "notional": "2050.0",
                },
            ],
            "ts_ns": 1122334455,
            "source": "trader",
        }
    ]

    node.stop()

    assert publisher.closed is True


def test_live_node_publish_kernel_portfolio_reads_attached_kernel_snapshot(monkeypatch):
    """TradingNode.publish_kernel_portfolio pulls the attached kernel's native snapshot and publishes it."""
    from coinext_kernel import Environment
    from coinext_live import TradingNode, TradingNodeConfig

    publishers = []

    class _FakePublisher:
        def __init__(self, url):
            self.url = url
            self.calls = []
            publishers.append(self)

        def publish_live_telemetry(self, stream, **payload):
            self.calls.append({"stream": stream, **payload})
            return "kernel-1-0"

    class _Kernel:
        def __init__(self):
            self.calls = 0

        def portfolio_snapshot(self):
            self.calls += 1
            return types.SimpleNamespace(
                portfolio=types.SimpleNamespace(
                    cash_balance=500.0,
                    realized_pnl=22.0,
                    equity=1500.0,
                    gross_exposure=480.0,
                    net_exposure=240.0,
                    unrealized_pnl=80.0,
                    positions=[
                        ("SOLUSDT", "BINANCE", 30.0, 10.0, 12.0, 4.0, 60.0, 360.0),
                        ("ADAUSDT", "BINANCE", -50.0, 3.0, 2.4, -1.5, 20.0, 120.0),
                    ],
                )
            )

    monkeypatch.setitem(sys.modules, "coinext_bus", types.SimpleNamespace(Publisher=_FakePublisher))

    node = TradingNode(
        config=TradingNodeConfig(
            env=Environment.SANDBOX,
            account_id="acct-kernel",
            symbol="SOLUSDT",
            venue="BINANCE",
            redis_url="redis://unit:6379/8",
            telemetry_stream="kernel.live",
        ),
        strategy=object(),
    )
    kernel = _Kernel()
    node._kernel = kernel

    msg_id = node.publish_kernel_portfolio(ts_ns=5566778899)

    assert msg_id == "kernel-1-0"
    assert kernel.calls == 1
    assert len(publishers) == 1
    assert publishers[0].url == "redis://unit:6379/8"
    assert publishers[0].calls == [
        {
            "stream": "kernel.live",
            "account_id": "acct-kernel",
            "environment": "sandbox",
            "symbol": "SOLUSDT",
            "venue": "BINANCE",
            "equity": 1500.0,
            "gross_exposure": 480.0,
            "net_exposure": 240.0,
            "realized_pnl": 22.0,
            "unrealized_pnl": 80.0,
            "positions": [
                {
                    "symbol": "SOLUSDT",
                    "venue": "BINANCE",
                    "side": "long",
                    "net_qty": "30.0",
                    "avg_price": "10.0",
                    "mark_price": "12.0",
                    "realized_pnl": "4.0",
                    "unrealized_pnl": "60.0",
                    "notional": "360.0",
                },
                {
                    "symbol": "ADAUSDT",
                    "venue": "BINANCE",
                    "side": "short",
                    "net_qty": "-50.0",
                    "avg_price": "3.0",
                    "mark_price": "2.4",
                    "realized_pnl": "-1.5",
                    "unrealized_pnl": "20.0",
                    "notional": "120.0",
                },
            ],
            "ts_ns": 5566778899,
            "source": "trader",
        }
    ]


@pytest.mark.parametrize("kernel", [None, object()], ids=["no-kernel", "no-snapshot-method"])
def test_live_node_publish_kernel_portfolio_noops_without_snapshot_source(monkeypatch, kernel):
    """TradingNode.publish_kernel_portfolio returns None and does not publish without a snapshot source."""
    from coinext_kernel import Environment
    from coinext_live import TradingNode, TradingNodeConfig

    publishers = []

    class _FakePublisher:
        def __init__(self, url):
            publishers.append((url, self))

    monkeypatch.setitem(sys.modules, "coinext_bus", types.SimpleNamespace(Publisher=_FakePublisher))

    node = TradingNode(
        config=TradingNodeConfig(
            env=Environment.SANDBOX,
            account_id="acct-kernel",
            symbol="SOLUSDT",
            venue="BINANCE",
            redis_url="redis://unit:6379/9",
            telemetry_stream="kernel.live",
        ),
        strategy=object(),
    )
    node._kernel = kernel

    assert node.publish_kernel_portfolio(ts_ns=1) is None
    assert publishers == []


def test_trader_build_node_wires_live_config_and_instantiates_strategy(monkeypatch):
    """The trader service builds a TradingNode with account/runtime config and a real strategy instance."""
    trader = _load_trader_service()
    from coinext_kernel import Environment

    class _FakeTradingNodeConfig:
        def __init__(self, *, env, account_id, symbol, venue, redis_url):
            self.env = env
            self.account_id = account_id
            self.symbol = symbol
            self.venue = venue
            self.redis_url = redis_url

    class _FakeTradingNode:
        def __init__(self, *, config, strategy):
            self.config = config
            self.strategy = strategy

    class _Strategy:
        pass

    monkeypatch.setitem(
        sys.modules,
        "coinext_live",
        types.SimpleNamespace(
            TradingNodeConfig=_FakeTradingNodeConfig,
            TradingNode=_FakeTradingNode,
        ),
    )
    monkeypatch.setitem(sys.modules, "coinext_strategy", types.SimpleNamespace(SmaCross=_Strategy))

    node = trader.build_node(
        trader.TraderConfig(
            account_id="acct-build",
            env="sandbox",
            symbol="XRPUSDT",
            venue="BINANCE",
            strategy="SmaCross",
            redis_url="redis://unit:6379/9",
        )
    )

    assert isinstance(node, _FakeTradingNode)
    assert node.config.account_id == "acct-build"
    assert node.config.env is Environment.SANDBOX
    assert node.config.symbol == "XRPUSDT"
    assert node.config.venue == "BINANCE"
    assert node.config.redis_url == "redis://unit:6379/9"
    assert isinstance(node.strategy, _Strategy)


# --------------------------------------------------------------------------------------------------
# risk-portfolio/services/risk-monitor — consume + fold + trip exactly once
# --------------------------------------------------------------------------------------------------


class _FakeBus:
    """Fake coinext_bus for the consume loop: yields StreamMessages and records kill-switch publishes."""

    def __init__(self, payloads):
        self._payloads = payloads
        self.kills: list[dict] = []

        class _Pub:
            def __init__(_self, url):
                _self.url = url

            def publish_kill_switch(_self, stream, *, engaged, reason, source, actor=None):
                self.kills.append(
                    {"stream": stream, "engaged": engaged, "reason": reason, "source": source}
                )
                return "1-0"

        self.Publisher = _Pub

    # decode_payload(envelope) -> the synthetic dict we stashed on the envelope
    @staticmethod
    def decode_payload(envelope):
        return envelope


def test_risk_monitor_trips_once_on_exposure_breach():
    rm = _load_risk_monitor()
    # Tight limits so a synthetic exposure crosses them.
    limits = rm.RiskLimits(
        max_drawdown_pct=0.20,
        max_gross_exposure=1000.0,
        max_net_exposure=1000.0,
        max_loss_of_day=1_000_000.0,
    )
    sup = rm.RiskSupervisor(limits=limits)
    bus = _FakeBus([])

    # Below limit: no trip.
    assert rm.process_message(sup, {"gross_exposure": 500.0}, bus) is False
    assert sup.tripped is False
    assert bus.kills == []

    # Crosses gross exposure: trips exactly once.
    assert rm.process_message(sup, {"gross_exposure": 5000.0}, bus) is True
    assert sup.tripped is True
    assert len(bus.kills) == 1
    assert bus.kills[0]["engaged"] is True
    assert bus.kills[0]["source"] == "risk-monitor"
    assert "gross_exposure" in bus.kills[0]["reason"]

    # Subsequent breaching messages do NOT publish again (latched).
    assert rm.process_message(sup, {"gross_exposure": 9000.0}, bus) is False
    assert len(bus.kills) == 1


def test_risk_monitor_consume_loop_drives_trip():
    rm = _load_risk_monitor()
    limits = rm.RiskLimits(max_gross_exposure=1000.0)
    sup = rm.RiskSupervisor(limits=limits)

    # Synthetic telemetry: healthy, healthy, then a breach.
    payloads = [
        {"gross_exposure": 100.0},
        {"gross_exposure": 200.0},
        {"gross_exposure": 5000.0},
    ]

    class _Msg:
        def __init__(self, env):
            self.envelope = env

    class _Client:
        def __init__(self, url):
            self.url = url

        def consume(self, streams):
            for p in payloads:
                yield _Msg(p)

    bus = _FakeBus(payloads)
    bus.RedisBusClient = _Client

    rm.consume_loop(bus, sup)
    assert sup.tripped is True
    assert len(bus.kills) == 1


def test_risk_monitor_drawdown_delegates_to_coinext_risk():
    rm = _load_risk_monitor()
    sup = rm.RiskSupervisor(limits=rm.RiskLimits(max_drawdown_pct=0.20))
    sup.state.update_equity(100.0)  # peak 100
    sup.state.update_equity(85.0)  # 15% drawdown -> healthy
    assert sup.evaluate() == []
    sup.state.update_equity(70.0)  # 30% drawdown -> breach
    breaches = sup.evaluate()
    assert any(b.limit == "max_drawdown" for b in breaches)


def test_risk_monitor_run_idle_without_bus(monkeypatch):
    """With no coinext_bus, run() must fall into IDLE mode (and not raise)."""
    import asyncio

    rm = _load_risk_monitor()
    monkeypatch.setattr(rm, "_load_bus", lambda: None)

    async def _drive():
        # run() loops forever in idle mode; cancel it after it yields once.
        task = asyncio.ensure_future(rm.run(poll_interval_s=0.001))
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_drive())


# --------------------------------------------------------------------------------------------------
# coinext_config — layered precedence (cli > env > yaml > defaults)
# --------------------------------------------------------------------------------------------------


def test_load_config_precedence(monkeypatch, tmp_path):
    pytest.importorskip("yaml")  # the yaml layer is exercised here
    from coinext_config import load_config

    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    # base.yaml sets symbol + a risk limit; live.yaml overrides symbol.
    (cfg_dir / "base.yaml").write_text("symbol: BTCUSDT\nrisk:\n  max_orders_per_sec: 5\n")
    (cfg_dir / "live.yaml").write_text("symbol: ETHUSDT\n")

    # Default (no env/cli): file layer wins -> live.yaml symbol, base risk.
    cfg = load_config("live", config_dir=str(cfg_dir))
    assert cfg.symbol == "ETHUSDT"
    assert cfg.risk.max_orders_per_sec == 5

    # env beats yaml.
    monkeypatch.setenv("COINEXT__SYMBOL", "SOLUSDT")
    cfg = load_config("live", config_dir=str(cfg_dir))
    assert cfg.symbol == "SOLUSDT"

    # cli beats env.
    cfg = load_config("live", config_dir=str(cfg_dir), cli_overrides={"symbol": "XRPUSDT"})
    assert cfg.symbol == "XRPUSDT"

    # Unset everything falls back to code default.
    monkeypatch.delenv("COINEXT__SYMBOL", raising=False)
    empty = tmp_path / "empty"
    empty.mkdir()
    cfg = load_config("backtest", config_dir=str(empty))
    assert cfg.symbol == "BTCUSDT"  # RunConfig default


def test_load_config_skips_when_pyyaml_missing(monkeypatch, tmp_path):
    """When PyYAML is absent the file layer is skipped but defaults/env/cli still resolve."""
    cfg_dir = tmp_path / "config"
    cfg_dir.mkdir()
    (cfg_dir / "base.yaml").write_text("symbol: FROM_YAML\n")

    import coinext_config

    real_import = (
        __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__
    )

    def _no_yaml(name, *a, **k):
        if name == "yaml":
            raise ImportError("no yaml")
        return real_import(name, *a, **k)

    monkeypatch.setattr("builtins.__import__", _no_yaml)
    cfg = coinext_config.load_config(
        "backtest", config_dir=str(cfg_dir), cli_overrides={"symbol": "FROM_CLI"}
    )
    assert cfg.symbol == "FROM_CLI"
