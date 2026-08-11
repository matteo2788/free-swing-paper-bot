from __future__ import annotations

from typing import Any

import pandas as pd

from swing_bot.paper import PaperTradingEngine
from swing_bot.responsive_runtime import ResponsivePaperTradingEngine, _parse_minutes


class RecordingAlerter:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []
        self.position_events: list[dict[str, Any]] = []
        self.catchups: list[list[dict[str, Any]]] = []

    def entry_alert(self, position: dict[str, Any]) -> bool:
        self.entries.append(position)
        return True

    def position_event_alert(
        self,
        title: str,
        position: dict[str, Any],
        message: str,
        color: int,
    ) -> bool:
        self.position_events.append(
            {
                "title": title,
                "position": position,
                "message": message,
                "color": color,
            }
        )
        return True

    def paper_catchup_alert(self, events: list[dict[str, Any]]) -> bool:
        self.catchups.append(events)
        return True


def _position(trade_id: str = "ABC-1") -> dict[str, Any]:
    return {
        "trade_id": trade_id,
        "ticker": "ABC",
        "entry_time": "2026-08-11T10:00:00-04:00",
        "entry_price": 100.0,
        "quantity": 10,
        "remaining_quantity": 10,
        "realized_pnl": 0.0,
        "last_return_percent": 0.0,
        "last_r_multiple": 0.0,
        "exits": [],
    }


def test_multiple_recovered_events_become_one_catchup(monkeypatch: Any) -> None:
    def fake_process(
        engine: PaperTradingEngine,
        state: dict[str, Any],
        intraday_frames: dict[str, pd.DataFrame],
    ) -> bool:
        opened = _position()
        engine.alerter.entry_alert(opened)

        stopped = _position()
        stopped["remaining_quantity"] = 0
        stopped["realized_pnl"] = -10.0
        stopped["last_return_percent"] = -1.0
        stopped["last_r_multiple"] = -1.0
        stopped["exits"] = [
            {
                "time": "2026-08-11T10:10:00-04:00",
                "reason": "STOP LOSS",
                "price": 99.0,
                "quantity": 10,
                "pnl": -10.0,
            }
        ]
        engine.alerter.position_event_alert(
            "❌ STOP-LOSS HIT",
            stopped,
            "Recovered stop",
            0xE74C3C,
        )
        return True

    monkeypatch.setattr(PaperTradingEngine, "process", fake_process)
    alerter = RecordingAlerter()
    engine = ResponsivePaperTradingEngine({}, object(), object(), alerter)  # type: ignore[arg-type]

    changed = engine.process({}, {})

    assert changed is True
    assert alerter.entries == []
    assert alerter.position_events == []
    assert len(alerter.catchups) == 1
    assert [event["kind"] for event in alerter.catchups[0]] == ["entry", "position"]


def test_single_recovered_event_stays_normal(monkeypatch: Any) -> None:
    def fake_process(
        engine: PaperTradingEngine,
        state: dict[str, Any],
        intraday_frames: dict[str, pd.DataFrame],
    ) -> bool:
        engine.alerter.entry_alert(_position("XYZ-1"))
        return True

    monkeypatch.setattr(PaperTradingEngine, "process", fake_process)
    alerter = RecordingAlerter()
    engine = ResponsivePaperTradingEngine({}, object(), object(), alerter)  # type: ignore[arg-type]

    changed = engine.process({}, {})

    assert changed is True
    assert len(alerter.entries) == 1
    assert alerter.catchups == []


def test_monitor_interval_parser() -> None:
    assert _parse_minutes("5m") == 5
    assert _parse_minutes("10") == 10
    assert _parse_minutes("bad", 7) == 7
