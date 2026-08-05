from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd

from .data_provider import serializable_number
from .indicators import adx, atr, ema, macd, rsi, session_vwap


@dataclass(frozen=True)
class ContextResult:
    passed: bool
    details: dict[str, Any]
    reason: str | None = None


@dataclass(frozen=True)
class ScoreResult:
    score: int
    tier: str | None
    factors: dict[str, int]
    metrics: dict[str, Any]
    trade_plan: dict[str, float] | None
    trigger_time: str


def evaluate_daily_context(
    ticker: str,
    frame: pd.DataFrame,
    spy_frame: pd.DataFrame,
    config: dict[str, Any],
) -> ContextResult:
    fast = int(config["daily_ema_fast"])
    slow = int(config["daily_ema_slow"])
    lookback = int(config["relative_strength_lookback_days"])
    minimum_rows = max(80, slow + 20, lookback + 20)
    if frame is None or len(frame) < minimum_rows or spy_frame is None or len(spy_frame) < minimum_rows:
        return ContextResult(False, {}, "not_enough_daily_data")

    data = frame.copy().dropna(subset=["Close", "High", "Low", "Volume"])
    benchmark = spy_frame.copy().dropna(subset=["Close"])
    if len(data) < minimum_rows or len(benchmark) < minimum_rows:
        return ContextResult(False, {}, "not_enough_clean_daily_data")

    close = float(data["Close"].iloc[-1])
    ema_fast_value = float(ema(data["Close"], fast).iloc[-1])
    ema_slow_value = float(ema(data["Close"], slow).iloc[-1])
    adx_value = float(adx(data, 14).iloc[-1])
    atr_value = float(atr(data, 14).iloc[-1])
    atr_percent = (atr_value / close) * 100 if close > 0 else math.nan
    avg_volume = float(data["Volume"].tail(20).mean())
    avg_dollar_volume = float((data["Close"] * data["Volume"]).tail(20).mean())

    stock_return = float(data["Close"].iloc[-1] / data["Close"].iloc[-(lookback + 1)] - 1)
    spy_return = float(benchmark["Close"].iloc[-1] / benchmark["Close"].iloc[-(lookback + 1)] - 1)
    relative_strength = (stock_return - spy_return) * 100

    checks = {
        "minimum_price": close >= float(config["min_price"]),
        "above_daily_ema_20": close > ema_fast_value,
        "above_daily_ema_50": close > ema_slow_value,
        "daily_adx": adx_value >= float(config["min_daily_adx"]),
        "positive_relative_strength": relative_strength > 0,
        "atr_percent": float(config["min_atr_percent"]) <= atr_percent <= float(config["max_atr_percent"]),
        "average_volume": avg_volume >= float(config["min_avg_volume_20d"]),
        "average_dollar_volume": avg_dollar_volume >= float(config["min_avg_dollar_volume_20d"]),
    }

    details = {
        "ticker": ticker,
        "close": serializable_number(close),
        "ema_fast": serializable_number(ema_fast_value),
        "ema_slow": serializable_number(ema_slow_value),
        "adx": serializable_number(adx_value),
        "atr_percent": serializable_number(atr_percent),
        "avg_volume_20d": serializable_number(avg_volume, 0),
        "avg_dollar_volume_20d": serializable_number(avg_dollar_volume, 0),
        "relative_strength_percent": serializable_number(relative_strength),
        "checks": checks,
    }
    failed = [name for name, passed in checks.items() if not passed]
    return ContextResult(not failed, details, ",".join(failed) if failed else None)


def regular_session(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty or not isinstance(frame.index, pd.DatetimeIndex):
        return frame
    result = frame.copy()
    if result.index.tz is None:
        result.index = result.index.tz_localize("America/New_York")
    else:
        result.index = result.index.tz_convert("America/New_York")
    return result.between_time("09:30", "15:59").copy()


def closed_intraday_bars(frame: pd.DataFrame, now: datetime, interval_minutes: int = 15) -> pd.DataFrame:
    data = regular_session(frame)
    if data.empty:
        return data
    cutoff = pd.Timestamp(now) - pd.Timedelta(minutes=1)
    bar_ends = data.index + pd.Timedelta(minutes=interval_minutes)
    return data.loc[bar_ends <= cutoff].copy()


def resample_four_hour(frame: pd.DataFrame) -> pd.DataFrame:
    data = regular_session(frame)
    if data.empty:
        return data
    pieces: list[pd.DataFrame] = []
    for _, session in data.groupby(data.index.date):
        session = session.sort_index()
        start = session.index[0].normalize() + pd.Timedelta(hours=9, minutes=30)
        bucket = ((session.index - start).total_seconds() // (4 * 60 * 60)).astype(int)
        grouped = session.groupby(bucket).agg(
            Open=("Open", "first"),
            High=("High", "max"),
            Low=("Low", "min"),
            Close=("Close", "last"),
            Volume=("Volume", "sum"),
        )
        labels = [start + pd.Timedelta(hours=4 * int(value)) for value in grouped.index]
        grouped.index = pd.DatetimeIndex(labels)
        pieces.append(grouped)
    return pd.concat(pieces).sort_index() if pieces else pd.DataFrame()


def four_hour_context_pass(frame: pd.DataFrame, config: dict[str, Any]) -> tuple[bool, dict[str, float]]:
    four_hour = resample_four_hour(frame)
    fast = int(config["four_hour_ema_fast"])
    slow = int(config["four_hour_ema_slow"])
    if len(four_hour) < slow + 5:
        return False, {"reason": "not_enough_4h_data"}
    close = float(four_hour["Close"].iloc[-1])
    fast_value = float(ema(four_hour["Close"], fast).iloc[-1])
    slow_value = float(ema(four_hour["Close"], slow).iloc[-1])
    passed = close > fast_value and fast_value > slow_value
    return passed, {
        "close": round(close, 4),
        "ema_fast": round(fast_value, 4),
        "ema_slow": round(slow_value, 4),
    }


def _rvol_points(value: float) -> int:
    if not np.isfinite(value) or value < 1.0:
        return 0
    if value < 1.25:
        return 5
    if value < 1.5:
        return 10
    if value < 2.0:
        return 15
    return 20


def _momentum_points(rsi_value: float, histogram: pd.Series) -> int:
    points = 0
    if 50 <= rsi_value <= 65:
        points += 8
    elif 45 <= rsi_value < 50 or 65 < rsi_value <= 70:
        points += 4
    if len(histogram) >= 2 and np.isfinite(histogram.iloc[-1]):
        if histogram.iloc[-1] > 0:
            points += 6
        if histogram.iloc[-1] > histogram.iloc[-2]:
            points += 6
    return min(points, 20)


def _vwap_points(data: pd.DataFrame, vwap_values: pd.Series) -> int:
    close = float(data["Close"].iloc[-1])
    current_vwap = float(vwap_values.iloc[-1])
    if not np.isfinite(current_vwap):
        return 0
    if len(data) >= 2:
        previous_close = float(data["Close"].iloc[-2])
        previous_vwap = float(vwap_values.iloc[-2])
        if previous_close <= previous_vwap and close > current_vwap:
            return 20
    if close > current_vwap:
        recent_lows = data["Low"].tail(3)
        if len(recent_lows) == 3 and float(recent_lows.min()) >= current_vwap * 0.997:
            return 18
        return 14
    distance = (current_vwap - close) / current_vwap if current_vwap else 1
    return 6 if distance <= 0.002 else 0


def _structure_points(data: pd.DataFrame, lookback: int, current_atr: float) -> tuple[int, float]:
    if len(data) < lookback + 2:
        return 0, math.nan
    resistance = float(data["High"].iloc[-(lookback + 1) : -1].max())
    current = data.iloc[-1]
    close = float(current["Close"])
    high = float(current["High"])
    low = float(current["Low"])
    if close <= resistance:
        distance = resistance - close
        if current_atr > 0 and distance <= current_atr * 0.10:
            return 4, resistance
        return 0, resistance
    candle_range = max(high - low, 1e-9)
    close_location = (close - low) / candle_range
    breakout_atr = (close - resistance) / max(current_atr, 1e-9)
    points = 8
    points += min(8, max(0, round(breakout_atr * 20)))
    if close_location >= 0.70:
        points += 4
    elif close_location >= 0.55:
        points += 2
    return min(points, 20), resistance


def _clean_structure_points(data: pd.DataFrame, lookback: int, close: float, current_atr: float) -> tuple[int, float | None, float | None]:
    if len(data) < 30 or current_atr <= 0:
        return 0, None, None
    older = data["High"].iloc[-(lookback + 21) : -21] if len(data) > lookback + 21 else data["High"].iloc[:-21]
    candidates = older[older > close]
    if candidates.empty:
        return 20, None, None
    resistance = float(candidates.min())
    room_atr = (resistance - close) / current_atr
    if room_atr >= 2.0:
        points = 20
    elif room_atr >= 1.5:
        points = 15
    elif room_atr >= 1.0:
        points = 10
    elif room_atr >= 0.5:
        points = 5
    else:
        points = 0
    return points, resistance, room_atr


def build_trade_plan(data: pd.DataFrame, current_atr: float, paper_config: dict[str, Any]) -> dict[str, float] | None:
    trigger = data.iloc[-1]
    entry_low = float(trigger["High"])
    entry_high = entry_low * (1 + float(paper_config["entry_buffer_percent"]) / 100)
    structural_stop = float(trigger["Low"]) - current_atr * 0.05
    atr_stop = entry_low - current_atr
    stop = max(structural_stop, atr_stop)

    minimum_risk = entry_low * float(paper_config["minimum_stop_percent"]) / 100
    if entry_low - stop < minimum_risk:
        stop = entry_low - minimum_risk
    risk = entry_low - stop
    risk_percent = (risk / entry_low) * 100 if entry_low else math.inf
    if risk <= 0 or risk_percent > float(paper_config["maximum_stop_percent"]):
        return None

    tp1 = entry_low + risk * float(paper_config["target_1_r"])
    tp2 = entry_low + risk * float(paper_config["target_2_r"])
    return {
        "entry_low": round(entry_low, 4),
        "entry_high": round(entry_high, 4),
        "stop": round(stop, 4),
        "risk_per_share": round(risk, 4),
        "risk_percent": round(risk_percent, 3),
        "tp1": round(tp1, 4),
        "tp2": round(tp2, 4),
    }


def score_trigger(
    intraday_frame: pd.DataFrame,
    context_config: dict[str, Any],
    scoring_config: dict[str, Any],
    paper_config: dict[str, Any],
) -> ScoreResult | None:
    data = regular_session(intraday_frame)
    minimum = max(
        int(scoring_config["overhead_lookback_bars"]) + 25,
        int(scoring_config["macd_slow"]) + int(scoring_config["macd_signal"]) + 10,
        220,
    )
    if len(data) < minimum:
        return None

    context_ok, four_hour_details = four_hour_context_pass(data, context_config)
    if not context_ok:
        return None

    atr_values = atr(data, int(scoring_config["atr_period"]))
    rsi_values = rsi(data["Close"], int(scoring_config["rsi_period"]))
    _, _, histogram = macd(
        data["Close"],
        int(scoring_config["macd_fast"]),
        int(scoring_config["macd_slow"]),
        int(scoring_config["macd_signal"]),
    )
    vwap_values = session_vwap(data)

    current_atr = float(atr_values.iloc[-1])
    current_rsi = float(rsi_values.iloc[-1])
    volume_average = float(data["Volume"].iloc[-(int(scoring_config["rvol_lookback_bars"]) + 1) : -1].mean())
    rvol = float(data["Volume"].iloc[-1]) / volume_average if volume_average > 0 else 0.0

    rvol_score = _rvol_points(rvol)
    structure_score, broken_resistance = _structure_points(
        data,
        int(scoring_config["breakout_lookback_bars"]),
        current_atr,
    )
    vwap_score = _vwap_points(data, vwap_values)
    momentum_score = _momentum_points(current_rsi, histogram)
    close = float(data["Close"].iloc[-1])
    clean_score, overhead, room_atr = _clean_structure_points(
        data,
        int(scoring_config["overhead_lookback_bars"]),
        close,
        current_atr,
    )

    factors = {
        "relative_volume": rvol_score,
        "price_structure": structure_score,
        "vwap": vwap_score,
        "momentum": momentum_score,
        "clean_structure": clean_score,
    }
    total = int(sum(factors.values()))
    if total >= int(scoring_config["a_setup_threshold"]):
        tier = "A"
    elif total >= int(scoring_config["b_setup_threshold"]):
        tier = "B"
    else:
        tier = None

    plan = build_trade_plan(data, current_atr, paper_config) if tier else None
    trigger_time = data.index[-1].isoformat()
    metrics = {
        "rvol": round(rvol, 3),
        "rsi": round(current_rsi, 2),
        "macd_histogram": serializable_number(histogram.iloc[-1]),
        "vwap": serializable_number(vwap_values.iloc[-1]),
        "close": round(close, 4),
        "atr": round(current_atr, 4),
        "broken_resistance": serializable_number(broken_resistance),
        "next_overhead_resistance": serializable_number(overhead),
        "room_in_atr": serializable_number(room_atr),
        "four_hour": four_hour_details,
    }
    return ScoreResult(
        score=total,
        tier=tier,
        factors=factors,
        metrics=metrics,
        trade_plan=plan,
        trigger_time=trigger_time,
    )


def tier_crossings(previous_score: int | None, current_score: int, b_threshold: int, a_threshold: int) -> list[str]:
    previous = previous_score if previous_score is not None else 0
    crossings: list[str] = []
    if previous < b_threshold <= current_score:
        crossings.append("B")
    if previous < a_threshold <= current_score:
        crossings.append("A")
    return crossings
