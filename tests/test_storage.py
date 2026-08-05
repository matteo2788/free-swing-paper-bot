from __future__ import annotations

from pathlib import Path

from swing_bot.storage import Storage


def test_storage_round_trip(tmp_path: Path) -> None:
    storage = Storage(
        tmp_path / "state.json",
        tmp_path / "trades.csv",
        tmp_path / "events.jsonl",
        tmp_path / "commit_required",
    )
    state = storage.load_state()
    state["scores"]["TEST"] = {"score": 82}
    storage.save_state(state)
    loaded = storage.load_state()
    assert loaded["scores"]["TEST"]["score"] == 82


def test_important_event_creates_commit_marker(tmp_path: Path) -> None:
    marker = tmp_path / "commit_required"
    storage = Storage(
        tmp_path / "state.json",
        tmp_path / "trades.csv",
        tmp_path / "events.jsonl",
        marker,
    )
    storage.log_event("test", {"ok": True})
    assert marker.exists()
