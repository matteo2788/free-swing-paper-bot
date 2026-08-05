from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.astype(float).ewm(span=period, adjust=False, min_periods=period).mean()


def true_range(frame: pd.DataFrame) -> pd.Series:
    high = frame["High"].astype(float)
    low = frame["Low"].astype(float)
    close = frame["Close"].astype(float)
    previous_close = close.shift(1)
    return pd.concat(
        [(high - low), (high - previous_close).abs(), (low - previous_close).abs()],
        axis=1,
    ).max(axis=1)


def atr(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    tr = true_range(frame)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.astype(float).diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    average_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    average_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = average_gain / average_loss.replace(0, np.nan)
    result = 100 - (100 / (1 + rs))
    result = result.where(average_loss != 0, 100.0)
    return result.clip(0, 100)


def macd(
    series: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> tuple[pd.Series, pd.Series, pd.Series]:
    fast_line = ema(series, fast)
    slow_line = ema(series, slow)
    macd_line = fast_line - slow_line
    signal_line = macd_line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def adx(frame: pd.DataFrame, period: int = 14) -> pd.Series:
    high = frame["High"].astype(float)
    low = frame["Low"].astype(float)

    up_move = high.diff()
    down_move = -low.diff()
    plus_dm = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
        index=frame.index,
        dtype=float,
    )
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
        index=frame.index,
        dtype=float,
    )

    atr_values = atr(frame, period)
    plus_di = 100 * plus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr_values
    minus_di = 100 * minus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / atr_values
    denominator = (plus_di + minus_di).replace(0, np.nan)
    dx = 100 * (plus_di - minus_di).abs() / denominator
    return dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def session_vwap(frame: pd.DataFrame) -> pd.Series:
    if frame.empty:
        return pd.Series(index=frame.index, dtype=float)
    typical = (frame["High"] + frame["Low"] + frame["Close"]) / 3.0
    volume = frame["Volume"].astype(float)
    if not isinstance(frame.index, pd.DatetimeIndex):
        raise ValueError("VWAP requires a DatetimeIndex")
    session_keys = pd.Series(frame.index.date, index=frame.index)
    cumulative_pv = (typical * volume).groupby(session_keys).cumsum()
    cumulative_volume = volume.groupby(session_keys).cumsum().replace(0, np.nan)
    return cumulative_pv / cumulative_volume


def clean_ohlcv(frame: pd.DataFrame) -> pd.DataFrame:
    required = ["Open", "High", "Low", "Close", "Volume"]
    if frame is None or frame.empty:
        return pd.DataFrame(columns=required)
    result = frame.copy()
    result.columns = [str(column).title() for column in result.columns]
    missing = [column for column in required if column not in result.columns]
    if missing:
        raise ValueError(f"OHLCV data is missing columns: {missing}")
    result = result[required].apply(pd.to_numeric, errors="coerce")
    result = result.dropna(subset=["Open", "High", "Low", "Close"])
    result["Volume"] = result["Volume"].fillna(0)
    result = result[~result.index.duplicated(keep="last")].sort_index()
    return result
