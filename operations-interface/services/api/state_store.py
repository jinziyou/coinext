"""Local API state store — runs/fills/catalog for the control-plane UI.

Status: partial. Long-term backend remains Postgres.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_lock = threading.RLock()
_positions: list[dict[str, Any]] = []
_fills_cache: list[dict[str, Any]] = []


def _default_runs_path() -> Path:
    raw = os.environ.get("COINEXT__API__RUNS_PATH", "").strip()
    return Path(raw) if raw else Path(".coinext/runs.json")


def _default_fills_path() -> Path:
    raw = os.environ.get("COINEXT__API__FILLS_PATH", "").strip()
    return Path(raw) if raw else Path(".coinext/fills.jsonl")


def _iso_now() -> str:
    return datetime.now(tz=UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_runs() -> list[dict[str, Any]]:
    path = _default_runs_path()
    if not path.is_file():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return list(raw) if isinstance(raw, list) else []


def save_run(run: dict[str, Any]) -> dict[str, Any]:
    """Append or upsert a run record (by ``run_id``) and return the stored row."""
    path = _default_runs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        rows = load_runs()
        rid = run.get("run_id")
        if rid:
            rows = [r for r in rows if r.get("run_id") != rid]
        row = {
            "run_id": rid or f"run-{len(rows) + 1:04d}",
            "strategy_id": run.get("strategy_id", "unknown"),
            "environment": run.get("environment", "backtest"),
            "status": run.get("status", "completed"),
            "started_at": run.get("started_at") or _iso_now(),
            "updated_at": run.get("updated_at") or _iso_now(),
            "pnl": str(run.get("pnl", "0")),
            "pnl_currency": run.get("pnl_currency", "USDT"),
        }
        rows.append(row)
        path.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
        return row


def set_positions(positions: list[dict[str, Any]]) -> None:
    with _lock:
        global _positions
        _positions = list(positions)


def get_positions() -> list[dict[str, Any]]:
    with _lock:
        return list(_positions)


def append_fill(fill: dict[str, Any]) -> None:
    path = _default_fills_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(fill, separators=(",", ":")) + "\n")
        _fills_cache.append(fill)


def load_fills(limit: int = 100) -> list[dict[str, Any]]:
    path = _default_fills_path()
    rows: list[dict[str, Any]] = []
    if path.is_file():
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        except (OSError, json.JSONDecodeError):
            rows = []
    with _lock:
        if _fills_cache and not rows:
            rows = list(_fills_cache)
    return rows[-limit:]


def catalog_from_lake() -> dict[str, Any]:
    """Build a catalog payload from the local Parquet lake when available."""
    lake_root = os.environ.get("COINEXT__DATA__LAKE_ROOT", "data")
    instruments: list[dict[str, Any]] = []
    try:
        from coinext_data import DataLake

        lake = DataLake(lake_root)
        series = lake.list_series()
        by_symbol: dict[str, set[str]] = {}
        for venue, symbol, interval in series:
            key = f"{symbol}.{venue}"
            by_symbol.setdefault(key, set()).add(interval)
        for instrument_id, intervals in sorted(by_symbol.items()):
            instruments.append(
                {
                    "instrument_id": instrument_id,
                    "asset_class": "crypto",
                    "bar_types": sorted(intervals),
                }
            )
    except Exception:
        # Fall back to DataCatalog FS scan without pyarrow coverage.
        try:
            from coinext_data import DataCatalog

            cat = DataCatalog(lake_root)
            for sym in cat.list_symbols("BINANCE"):
                instruments.append(
                    {
                        "instrument_id": f"{sym}.BINANCE",
                        "asset_class": "crypto",
                        "bar_types": ["1m"],
                    }
                )
        except Exception:
            instruments = []

    return {
        "lake_root": lake_root,
        "instruments": instruments,
        "source": "data_lake" if instruments else "empty",
    }
