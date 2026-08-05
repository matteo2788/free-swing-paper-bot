from __future__ import annotations

import numpy as np
import pandas as pd

from swing_bot.strategy import build_trade_plan, tier_crossings


def test_fresh_crossing_logic() -> None:
    assert tier_crossings(55, 65, 60, 80) == ["B"]
    assert tier_crossings(70, 84, 60, 80) == ["A"]
    assert tier_crossings(55, 84, 60, 80) == ["B", "A"]
    assert tier_crossings(84, 88, 60, 80) == []
    assert tier_crossings(84, 75, 60, 80) == []
    assert tier_crossings(75, 82, 60, 80) == ["A"]


def test_trade_plan_has_entry_stop_and_targets() -> None:
    index = pd.date_range("2026-07-01 09:30", periods=30, freq="15min", tz="America/New_York")
    close = np.linspace(100, 104, 30)
    frame = pd.DataFrame(
        {
            "Open": close - 0.1,
            "High": close + 0.5,
            "Low": close - 0.5,
            "Close": close,
            "Volume": 1_000_000,
        },
        index=index,
    )
    config = {
        "entry_buffer_percent": 0.15,
        "minimum_stop_percent": 0.35,
        "maximum_stop_percent": 4.0,
        "target_1_r": 1.0,
        "target_2_r": 2.0,
    }
    plan = build_trade_plan(frame, current_atr=1.2, paper_config=config)
    assert plan is not None
    assert plan["stop"] < plan["entry_low"] < plan["tp1"] < plan["tp2"]
    assert plan["entry_high"] > plan["entry_low"]
