from __future__ import annotations

import numpy as np
import pandas as pd

from swing_bot.indicators import adx, atr, ema, macd, rsi, session_vwap


def make_frame(rows: int = 120) -> pd.DataFrame:
    index = pd.date_range("2026-01-02 09:30", periods=rows, freq="15min", tz="America/New_York")
    base = np.linspace(100, 120, rows)
    return pd.DataFrame(
        {
            "Open": base - 0.2,
            "High": base + 0.8,
            "Low": base - 0.8,
            "Close": base,
            "Volume": np.linspace(1_000_000, 2_000_000, rows),
        },
        index=index,
    )


def test_core_indicators_produce_values() -> None:
    frame = make_frame()
    assert pd.notna(ema(frame["Close"], 20).iloc[-1])
    assert pd.notna(atr(frame, 14).iloc[-1])
    assert 0 <= rsi(frame["Close"], 14).iloc[-1] <= 100
    _, _, histogram = macd(frame["Close"])
    assert pd.notna(histogram.iloc[-1])
    assert pd.notna(adx(frame, 14).iloc[-1])


def test_session_vwap_is_inside_price_area() -> None:
    frame = make_frame(20)
    values = session_vwap(frame)
    assert len(values) == len(frame)
    assert values.iloc[-1] > frame["Low"].min()
    assert values.iloc[-1] < frame["High"].max()
