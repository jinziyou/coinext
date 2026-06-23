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

import coinext_bus
import pytest
from coinext_contracts import (
    CTRL_KILL_SWITCH,
    Envelope,
    MsgType,
    is_kill_switch,
    kill_switch_payload,
)

_RISK_MONITOR = pathlib.Path(__file__).resolve().parents[1] / "services" / "risk-monitor" / "main.py"


def _load_risk_monitor():
    """Load services/risk-monitor/main.py by path (it is not an installed package)."""
    spec = importlib.util.spec_from_file_location("coinext_risk_monitor_under_test", _RISK_MONITOR)
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
    assert env.payload == ("ENC", kill_switch_payload(
        engaged=True, reason="breach", source="api", actor="op"
    ))


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
# coinext_live — control subscriber engages the node kill-switch
# --------------------------------------------------------------------------------------------------


def test_live_on_control_message_engages_kill_switch(monkeypatch):
    from coinext_kernel import Environment
    from coinext_live import TradingNode, TradingNodeConfig, on_control_message

    node = TradingNode(
        config=TradingNodeConfig(env=Environment.LIVE), strategy=object()
    )
    node._running = True
    assert not node.killed

    payload = kill_switch_payload(engaged=True, reason="global halt", source="api")
    monkeypatch.setattr(coinext_bus, "decode_payload", lambda env: payload)
    env = Envelope.of(MsgType.CTRL, b"\x00" * 16, 0, b"x")

    fired = on_control_message(env, node.engage_kill_switch)
    assert fired is True
    assert node.killed is True
    assert node._kill_reason == "global halt"
    assert node._running is False  # engaging the kill-switch requested a graceful stop

    # Idempotent: a second engage does not change reason.
    node.engage_kill_switch("other")
    assert node._kill_reason == "global halt"


# --------------------------------------------------------------------------------------------------
# services/risk-monitor — consume + fold + trip exactly once
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
    (cfg_dir / "base.yaml").write_text(
        "symbol: BTCUSDT\nrisk:\n  max_orders_per_sec: 5\n"
    )
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

    real_import = __builtins__["__import__"] if isinstance(__builtins__, dict) else __builtins__.__import__

    def _no_yaml(name, *a, **k):
        if name == "yaml":
            raise ImportError("no yaml")
        return real_import(name, *a, **k)

    monkeypatch.setattr("builtins.__import__", _no_yaml)
    cfg = coinext_config.load_config(
        "backtest", config_dir=str(cfg_dir), cli_overrides={"symbol": "FROM_CLI"}
    )
    assert cfg.symbol == "FROM_CLI"
