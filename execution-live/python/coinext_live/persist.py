"""Live runtime persistence helpers (file/SQLite paths).

Status: partial.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from coinext_live.reconcile import LocalOrderEvent


def default_db_path() -> Path:
    raw = os.environ.get("COINEXT__PERSIST__DB", "").strip()
    return Path(raw) if raw else Path(".coinext/live.db")


@dataclass
class SqliteSeqCursor:
    """Monotonic per-strategy sequence (mirrors Rust ``SqliteSeqCursor``)."""

    path: str | Path
    _lock: threading.RLock = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS seq_cursor (
                    strategy_id TEXT PRIMARY KEY,
                    seq         INTEGER NOT NULL
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def next(self, namespace: str) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT seq FROM seq_cursor WHERE strategy_id = ?", (namespace,)
            ).fetchone()
            nxt = int(row[0]) + 1 if row else 1
            conn.execute(
                "INSERT INTO seq_cursor(strategy_id, seq) VALUES(?, ?) "
                "ON CONFLICT(strategy_id) DO UPDATE SET seq = excluded.seq",
                (namespace, nxt),
            )
            conn.commit()
            return nxt

    def current(self, namespace: str) -> int:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT seq FROM seq_cursor WHERE strategy_id = ?", (namespace,)
            ).fetchone()
            return int(row[0]) if row else 0


@dataclass
class SqliteEventLog:
    """Append-only order events (compatible layout with Rust ``order_events``)."""

    path: str | Path
    _lock: threading.RLock = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS order_events (
                    strategy_id     TEXT    NOT NULL,
                    client_order_id TEXT    NOT NULL,
                    seq             INTEGER NOT NULL,
                    ts              INTEGER NOT NULL,
                    event_json      TEXT    NOT NULL,
                    PRIMARY KEY (client_order_id, seq)
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_order_events_strategy "
                "ON order_events (strategy_id, client_order_id, seq)"
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def append(
        self,
        strategy_id: str,
        client_order_id: str,
        event: dict[str, Any] | LocalOrderEvent,
        ts_ns: int = 0,
    ) -> int:
        if isinstance(event, LocalOrderEvent):
            payload = {
                "client_order_id": event.client_order_id,
                "event": event.event,
                "symbol": event.symbol,
                "side": event.side,
                "qty": event.qty,
                "px": event.px,
                "ts_ns": event.ts_ns or ts_ns,
                "venue_order_id": event.venue_order_id,
            }
        else:
            payload = dict(event)
        blob = json.dumps(payload, separators=(",", ":"))
        with self._lock, self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(MAX(seq) + 1, 0) FROM order_events WHERE client_order_id = ?",
                (client_order_id,),
            ).fetchone()
            seq = int(row[0]) if row else 0
            conn.execute(
                "INSERT INTO order_events(strategy_id, client_order_id, seq, ts, event_json) "
                "VALUES(?, ?, ?, ?, ?)",
                (strategy_id, client_order_id, seq, int(ts_ns), blob),
            )
            conn.commit()
            return seq

    def load_local_events(self, strategy_id: str | None = None) -> list[LocalOrderEvent]:
        with self._lock, self._connect() as conn:
            if strategy_id:
                rows = conn.execute(
                    "SELECT event_json FROM order_events WHERE strategy_id = ? "
                    "ORDER BY client_order_id, seq",
                    (strategy_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT event_json FROM order_events ORDER BY client_order_id, seq"
                ).fetchall()
        out: list[LocalOrderEvent] = []
        for (blob,) in rows:
            raw = json.loads(blob)
            out.append(LocalOrderEvent.from_dict(raw))
        return out

    def export_jsonl(self, path: str | Path, strategy_id: str | None = None) -> Path:
        """Write events as JSONL for the file-based reconcile path."""
        dest = Path(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        events = self.load_local_events(strategy_id)
        with dest.open("w", encoding="utf-8") as fh:
            for ev in events:
                fh.write(
                    json.dumps(
                        {
                            "client_order_id": ev.client_order_id,
                            "event": ev.event,
                            "symbol": ev.symbol,
                            "side": ev.side,
                            "qty": ev.qty,
                            "px": ev.px,
                            "ts_ns": ev.ts_ns,
                            "venue_order_id": ev.venue_order_id,
                        },
                        separators=(",", ":"),
                    )
                    + "\n"
                )
        return dest
