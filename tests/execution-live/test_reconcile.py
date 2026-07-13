"""File-backed live reconcile (no venue network)."""

from __future__ import annotations

from pathlib import Path

from coinext_live.reconcile import (
    LocalOrderEvent,
    VenueOpenOrder,
    append_event,
    diff_open_orders,
    load_events,
    open_orders_from_events,
    reconcile_from_paths,
)


def test_event_log_fold_and_diff(tmp_path: Path):
    log = tmp_path / "events.jsonl"
    append_event(
        log,
        LocalOrderEvent(
            client_order_id="c1",
            event="submitted",
            symbol="BTCUSDT",
            side="buy",
            qty=0.1,
            px=50_000.0,
        ),
    )
    append_event(
        log,
        LocalOrderEvent(client_order_id="c2", event="submitted", symbol="BTCUSDT", side="sell"),
    )
    append_event(log, LocalOrderEvent(client_order_id="c2", event="filled", symbol="BTCUSDT"))

    events = load_events(log)
    open_map = open_orders_from_events(events)
    assert set(open_map) == {"c1"}

    # Venue agrees on c1 → reconciled
    report = diff_open_orders(
        open_map,
        [VenueOpenOrder(client_order_id="c1", symbol="BTCUSDT", side="buy", qty=0.1)],
    )
    assert report.reconciled is True
    assert report.orphan_orders == []
    assert report.missing_fills == []

    # Venue missing c1 → orphan local
    report2 = diff_open_orders(open_map, [])
    assert report2.reconciled is False
    assert report2.orphan_orders[0]["client_order_id"] == "c1"


def test_reconcile_from_paths_local_only(tmp_path: Path):
    log = tmp_path / "events.jsonl"
    append_event(log, {"client_order_id": "x", "event": "submitted", "symbol": "ETHUSDT"})
    report = reconcile_from_paths(log)
    assert report.local_events == 1
    assert report.reconciled is True  # local-only is soft-pass
    assert len(report.local_open) == 1
