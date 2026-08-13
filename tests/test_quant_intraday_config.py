from __future__ import annotations

from pathlib import Path

import yaml


def test_quant_intraday_window_is_supported_and_long_enough() -> None:
    settings_path = Path(__file__).resolve().parents[1] / "config" / "settings.yaml"
    settings = yaml.safe_load(settings_path.read_text(encoding="utf-8"))

    period = str(settings["system"]["data_period_intraday"])
    interval = str(settings["system"]["trigger_interval"])
    minimum_bars = int(settings["alpha"]["minimum_intraday_bars"])

    # yfinance 1.5.x documents 1mo as a valid period while 60d is not a
    # documented period string. 1mo of 15m regular-session bars also provides
    # comfortably more than the 220 bars required by the alpha engine.
    assert interval == "15m"
    assert period == "1mo"
    assert minimum_bars <= 220
