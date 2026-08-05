from __future__ import annotations

import csv
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_STATE: dict[str, Any] = {
    "version": 1,
    "last_daily_refresh": None,
    "daily_pool": {},
    "scores": {},
    "pending": {},
    "positions": {},
    "last_summary_date": None,
    "last_scan_bar": None,
}

TRADE_FIELDS = [
    "trade_id",
    "ticker",
    "tier",
    "score",
    "signal_time",
    "entry_time",
    "exit_time",
    "entry_price",
    "initial_stop",
    "tp1",
    "tp2",
    "quantity",
    "realized_pnl",
    "return_percent",
    "r_multiple",
    "outcome",
    "holding_trading_days",
]


class Storage:
    def __init__(self, runtime_path: Path, trades_path: Path, events_path: Path, marker_path: Path) -> None:
        self.runtime_path = runtime_path
        self.trades_path = trades_path
        self.events_path = events_path
        self.marker_path = marker_path
        for path in (runtime_path, trades_path, events_path, marker_path):
            path.parent.mkdir(parents=True, exist_ok=True)

    def load_state(self) -> dict[str, Any]:
        if not self.runtime_path.exists():
            return json.loads(json.dumps(DEFAULT_STATE))
        try:
            with self.runtime_path.open("r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            state = json.loads(json.dumps(DEFAULT_STATE))
            if isinstance(loaded, dict):
                state.update(loaded)
            for key in ("daily_pool", "scores", "pending", "positions"):
                if not isinstance(state.get(key), dict):
                    state[key] = {}
            return state
        except (OSError, json.JSONDecodeError):
            return json.loads(json.dumps(DEFAULT_STATE))

    def save_state(self, state: dict[str, Any], *, important: bool = False) -> None:
        payload = json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False)
        self._atomic_write(self.runtime_path, payload + "\n")
        if important:
            self.mark_commit_required()

    def log_event(self, event_type: str, payload: dict[str, Any], *, important: bool = True) -> None:
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "type": event_type,
            "payload": payload,
        }
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")
        if important:
            self.mark_commit_required()

    def append_trade(self, trade: dict[str, Any]) -> None:
        exists = self.trades_path.exists() and self.trades_path.stat().st_size > 0
        with self.trades_path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=TRADE_FIELDS, extrasaction="ignore")
            if not exists:
                writer.writeheader()
            writer.writerow({field: trade.get(field, "") for field in TRADE_FIELDS})
        self.mark_commit_required()

    def mark_commit_required(self) -> None:
        self.marker_path.write_text("1\n", encoding="utf-8")

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        descriptor, temporary = tempfile.mkstemp(prefix=path.name, dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
