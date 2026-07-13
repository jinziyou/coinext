"""SQLite SeqCursor + event log (Python coinext_live.persist)."""

from __future__ import annotations

from pathlib import Path

from coinext_live.persist import SqliteEventLog, SqliteSeqCursor
from coinext_live.reconcile import LocalOrderEvent


def test_seq_cursor_monotonic(tmp_path: Path):
    db = tmp_path / "live.db"
    cur = SqliteSeqCursor(db)
    assert cur.current("strat") == 0
    assert cur.next("strat") == 1
    assert cur.next("strat") == 2
    assert cur.current("strat") == 2
    # Restart from same file
    cur2 = SqliteSeqCursor(db)
    assert cur2.next("strat") == 3


def test_event_log_append_export_reconcile(tmp_path: Path):
    db = tmp_path / "live.db"
    log = SqliteEventLog(db)
    log.append(
        "s1",
        "c1",
        LocalOrderEvent(
            client_order_id="c1",
            event="submitted",
            symbol="BTCUSDT",
            side="buy",
            qty=0.1,
            px=50_000.0,
        ),
        ts_ns=1,
    )
    events = log.load_local_events("s1")
    assert len(events) == 1
    assert events[0].client_order_id == "c1"
    jsonl = log.export_jsonl(tmp_path / "e.jsonl")
    assert jsonl.is_file()
    assert "submitted" in jsonl.read_text()
