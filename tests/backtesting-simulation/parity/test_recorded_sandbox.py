"""Recorded sandbox/testnet replay tests for the parity gate and CLI.

These tests exercise the offline recorded-session path: no network, no live testnet credentials, and no
implementation-source assertions. Tests that drive ``coinext_backtest`` skip when the Rust extension is
not available, matching the nearby parity tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from coinext_parity import (
    AcceptanceCriterion,
    SessionResult,
    load_sandbox_recording,
    render_verdict,
    run_gate,
)

_FIXTURE = Path(__file__).with_name("fixtures") / "recorded_sandbox_sma_cross.json"
_EXPECTED_STRATEGY = {"name": "SmaCross", "fast": 10, "slow": 30, "qty": 0.001}


def _require_backtest() -> None:
    """Skip only tests that need the compiled coinext_backtest/Rust bridge."""
    pytest.importorskip("coinext_backtest")


def _forbid_network_klines(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the CLI recorded-session tests fail if they regress into the network fetch path."""
    coinext_data = pytest.importorskip("coinext_data")

    def _unexpected_fetch(*_args: object, **_kwargs: object) -> list[tuple[int, float]]:
        pytest.fail("recorded-session path attempted to fetch live klines")

    monkeypatch.setattr(coinext_data, "fetch_binance_klines", _unexpected_fetch)


def test_load_sandbox_recording_replays_fixture_session():
    recording = load_sandbox_recording(_FIXTURE)

    assert recording.symbol == "BTCUSDT"
    assert recording.interval == "1m"
    assert recording.environment == "fixture-synthetic-sandbox"
    assert recording.strategy == _EXPECTED_STRATEGY
    assert recording.starting_balance == pytest.approx(100_000.0)
    assert len(recording.bars) == 120
    assert len(recording.fills) == 4
    assert all(
        b_ts < a_ts
        for (b_ts, _), (a_ts, _) in zip(recording.bars, recording.bars[1:], strict=False)
    )

    session = recording.to_session()

    assert isinstance(session, SessionResult)
    assert len(session.equity_curve) == len(recording.bars)
    assert len(session.fills) == len(recording.fills)
    assert session.equity_curve[0][1] == pytest.approx(recording.starting_balance)
    assert session.final_return() != pytest.approx(0.0)
    # Replaying snaps fill timestamps to the bar grid, but preserves the externally relevant fill
    # side/quantity/price values captured from the sandbox session.
    assert [(s, q, px) for _ts, s, q, px in session.fills] == [
        (s, q, px) for _ts, s, q, px in recording.fills
    ]
    assert {ts for ts, *_ in session.fills}.issubset({ts for ts, _close in recording.bars})


def test_recorded_sandbox_fixture_passes_parity_gate_for_matching_sma_metadata():
    _require_backtest()
    from coinext_strategy import SmaCross

    recording = load_sandbox_recording(_FIXTURE)
    assert recording.strategy == _EXPECTED_STRATEGY

    verdict = run_gate(
        lambda: SmaCross(
            fast=int(recording.strategy["fast"]),
            slow=int(recording.strategy["slow"]),
            qty=float(recording.strategy["qty"]),
        ),
        recording.bars,
        recording.to_session(),
        AcceptanceCriterion(),
        symbol=recording.symbol,
        starting_balance=recording.starting_balance,
    )

    assert verdict.passed, render_verdict(verdict)
    assert verdict.metrics.signal_timing_agreement == pytest.approx(1.0)
    assert verdict.metrics.fill_price_deviation_bps <= AcceptanceCriterion().max_fill_dev_bps
    assert verdict.metrics.equity_correlation >= AcceptanceCriterion().min_equity_corr
    assert verdict.metrics.return_diff <= AcceptanceCriterion().max_return_diff


def test_cli_recorded_session_replays_without_network(monkeypatch, capsys):
    _require_backtest()
    _forbid_network_klines(monkeypatch)
    from coinext_cli.main import _cmd_testnet_gate

    rc = _cmd_testnet_gate(recorded_session=str(_FIXTURE))

    out = capsys.readouterr().out
    assert rc == 0
    assert "loaded recorded" in out
    assert "replaying" in out
    assert "recorded sandbox fill" in out
    assert "[4/4] parity gate" in out


def test_cli_record_out_roundtrips_recorded_bars_and_fills(tmp_path, monkeypatch, capsys):
    _require_backtest()
    _forbid_network_klines(monkeypatch)
    from coinext_cli.main import _cmd_testnet_gate

    original = load_sandbox_recording(_FIXTURE)
    out_path = tmp_path / "roundtrip_recording.json"

    rc = _cmd_testnet_gate(recorded_session=str(_FIXTURE), record_out=str(out_path))

    out = capsys.readouterr().out
    assert rc == 0
    assert "recorded sandbox session written" in out
    replayed = load_sandbox_recording(out_path)
    assert replayed.environment == "recorded-replay"
    assert replayed.symbol == original.symbol
    assert replayed.interval == original.interval
    assert replayed.starting_balance == pytest.approx(original.starting_balance)
    assert replayed.strategy == original.strategy
    assert replayed.bars == original.bars
    assert replayed.fills == original.fills
