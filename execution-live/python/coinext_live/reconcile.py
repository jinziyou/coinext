"""Reconcile helpers — offline/file and SQLite-backed position/fill diffs.

Status: partial. Live venue reconcile still evolving.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class LocalOrderEvent:
    client_order_id: str
    event: str
    symbol: str = ""
    side: str = ""
    qty: float = 0.0
    px: float = 0.0
    ts_ns: int = 0
    venue_order_id: str = ""

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> LocalOrderEvent:
        return cls(
            client_order_id=str(raw.get("client_order_id", "")),
            event=str(raw.get("event", "")).lower(),
            symbol=str(raw.get("symbol", "")),
            side=str(raw.get("side", "")).lower(),
            qty=float(raw.get("qty") or 0.0),
            px=float(raw.get("px") or 0.0),
            ts_ns=int(raw.get("ts_ns") or 0),
            venue_order_id=str(raw.get("venue_order_id", "")),
        )


@dataclass
class VenueOpenOrder:
    """Venue-reported open order (from REST or a test fixture)."""

    client_order_id: str
    symbol: str = ""
    side: str = ""
    qty: float = 0.0
    px: float = 0.0
    venue_order_id: str = ""


@dataclass
class ReconcileReport:
    reconciled: bool
    missing_fills: list[dict[str, Any]] = field(default_factory=list)
    orphan_orders: list[dict[str, Any]] = field(default_factory=list)
    local_open: list[dict[str, Any]] = field(default_factory=list)
    venue_open: list[dict[str, Any]] = field(default_factory=list)
    local_events: int = 0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "reconciled": self.reconciled,
            "missing_fills": self.missing_fills,
            "orphan_orders": self.orphan_orders,
            "local_open": self.local_open,
            "venue_open": self.venue_open,
            "local_events": self.local_events,
            "note": self.note,
        }


def default_event_log_path() -> Path | None:
    raw = os.environ.get("COINEXT__PERSIST__EVENT_LOG", "").strip()
    return Path(raw) if raw else None


def append_event(path: str | Path, event: LocalOrderEvent | dict[str, Any]) -> None:
    """Append one event as a JSON line (creates parent dirs)."""
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(event, LocalOrderEvent):
        payload = {
            "client_order_id": event.client_order_id,
            "event": event.event,
            "symbol": event.symbol,
            "side": event.side,
            "qty": event.qty,
            "px": event.px,
            "ts_ns": event.ts_ns,
            "venue_order_id": event.venue_order_id,
        }
    else:
        payload = dict(event)
    with dest.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, separators=(",", ":")) + "\n")


def load_events(path: str | Path | None) -> list[LocalOrderEvent]:
    if path is None:
        return []
    p = Path(path)
    if not p.is_file():
        return []
    out: list[LocalOrderEvent] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        out.append(LocalOrderEvent.from_dict(json.loads(line)))
    return out


def open_orders_from_events(events: list[LocalOrderEvent]) -> dict[str, LocalOrderEvent]:
    """Fold the event log into currently-open client orders (submitted without fill/cancel)."""
    open_map: dict[str, LocalOrderEvent] = {}
    for ev in events:
        coid = ev.client_order_id
        if not coid:
            continue
        if ev.event in {"submitted", "accepted", "partially_filled"}:
            open_map[coid] = ev
        elif ev.event in {"filled", "canceled", "cancelled", "rejected", "expired"}:
            open_map.pop(coid, None)
    return open_map


def diff_open_orders(
    local: dict[str, LocalOrderEvent],
    venue: list[VenueOpenOrder],
) -> ReconcileReport:
    """Compare local open set vs venue open set by ``client_order_id``."""
    venue_map = {v.client_order_id: v for v in venue if v.client_order_id}
    missing = []  # on venue, not local
    orphans = []  # local, not on venue
    for coid, v in venue_map.items():
        if coid not in local:
            missing.append(
                {
                    "client_order_id": coid,
                    "symbol": v.symbol,
                    "side": v.side,
                    "qty": v.qty,
                    "px": v.px,
                    "venue_order_id": v.venue_order_id,
                }
            )
    for coid, loc in local.items():
        if coid not in venue_map:
            orphans.append(
                {
                    "client_order_id": coid,
                    "symbol": loc.symbol,
                    "side": loc.side,
                    "qty": loc.qty,
                    "px": loc.px,
                    "venue_order_id": loc.venue_order_id,
                }
            )
    ok = not missing and not orphans
    return ReconcileReport(
        reconciled=ok,
        missing_fills=missing,  # name kept for API stability; holds venue-only opens
        orphan_orders=orphans,
        local_open=[
            {
                "client_order_id": c,
                "symbol": e.symbol,
                "side": e.side,
                "qty": e.qty,
                "px": e.px,
            }
            for c, e in local.items()
        ],
        venue_open=[
            {
                "client_order_id": v.client_order_id,
                "symbol": v.symbol,
                "side": v.side,
                "qty": v.qty,
                "px": v.px,
            }
            for v in venue
        ],
        note="local event-log vs venue open-order set",
    )


def reconcile_from_paths(
    event_log: str | Path | None,
    venue_open: list[VenueOpenOrder] | None = None,
    *,
    venue_fixture: str | Path | None = None,
) -> ReconcileReport:
    """Load local log (+ optional venue JSON fixture) and produce a reconcile report."""
    events = load_events(event_log)
    local_open = open_orders_from_events(events)
    venue_list = list(venue_open or [])
    if venue_fixture is not None:
        vp = Path(venue_fixture)
        if vp.is_file():
            raw = json.loads(vp.read_text(encoding="utf-8"))
            for row in raw.get("open_orders") or raw if isinstance(raw, list) else []:
                if isinstance(row, dict):
                    venue_list.append(
                        VenueOpenOrder(
                            client_order_id=str(row.get("client_order_id", "")),
                            symbol=str(row.get("symbol", "")),
                            side=str(row.get("side", "")),
                            qty=float(row.get("qty") or 0.0),
                            px=float(row.get("px") or 0.0),
                            venue_order_id=str(row.get("venue_order_id", "")),
                        )
                    )
    report = diff_open_orders(local_open, venue_list)
    report.local_events = len(events)
    if venue_open is None and venue_fixture is None:
        report.note = (
            "local-only reconcile (no venue snapshot); set venue fixture or inject open orders "
            "for a full diff"
        )
        # Without venue truth, we only report local open state — not a hard fail.
        report.reconciled = True
        report.orphan_orders = []
        report.missing_fills = []
    return report
