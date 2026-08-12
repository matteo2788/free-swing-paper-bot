from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .data_provider import serializable_number
from .indicators import atr, ema
from .strategy import regular_session


@dataclass(frozen=True)
class RegimeResult:
    allow_new_longs: bool
    reasons: list[str]
    metrics: dict[str, Any]


@dataclass(frozen=True)
class RawAlphaSignal:
    ticker: str
    trigger_time: str
    volatility_raw: float
    relative_strength_raw: float
    volume_raw: float
    gamma_raw: float | None
    gates: dict[str, bool]
    metrics: dict[str, Any]


def _normal_cdf(value: float) -> float:
    return 0.5 * (1.0 + math.erf(value / math.sqrt(2.0)))


def _cross_sectional_z(values: dict[str, float]) -> dict[str, float]:
    if not values:
        return {}
    array = np.array(list(values.values()), dtype=float)
    mean = float(np.mean(array))
    std = float(np.std(array, ddof=0))
    if not np.isfinite(std) or std < 1e-9:
        return {ticker: 0.0 for ticker in values}
    return {ticker: (float(value) - mean) / std for ticker, value in values.items()}


def _ema_slope_percent(close: pd.Series, period: int, lookback: int) -> tuple[float, float]:
    values = ema(close.astype(float), period).dropna()
    if len(values) <= lookback:
        return math.nan, math.nan
    current = float(values.iloc[-1])
    previous = float(values.iloc[-(lookback + 1)])
    slope = ((current / previous) - 1.0) * 100.0 if previous else math.nan
    return current, slope


def evaluate_market_regime(
    spy: pd.DataFrame,
    qqq: pd.DataFrame,
    vix: pd.DataFrame | None,
    config: dict[str, Any],
) -> RegimeResult:
    if not bool(config.get("enabled", True)):
        return RegimeResult(True, [], {"enabled": False})

    ema_period = int(config.get("ema_period", 20))
    slope_lookback = int(config.get("ema_slope_lookback_days", 5))
    volatility_lookback = int(config.get("volatility_lookback_days", 20))
    downside_lookback = int(config.get("downside_lookback_days", 3))

    minimum = max(ema_period + slope_lookback + 5, volatility_lookback + 20, downside_lookback + 5)
    if spy is None or qqq is None or len(spy) < minimum or len(qqq) < minimum:
        return RegimeResult(False, ["insufficient_macro_data"], {"enabled": True, "minimum_rows": minimum})

    spy_data = spy.dropna(subset=["Close", "High", "Low"]).copy()
    qqq_data = qqq.dropna(subset=["Close"]).copy()
    if len(spy_data) < minimum or len(qqq_data) < minimum:
        return RegimeResult(False, ["insufficient_clean_macro_data"], {"enabled": True})

    spy_close = float(spy_data["Close"].iloc[-1])
    qqq_close = float(qqq_data["Close"].iloc[-1])
    spy_ema, spy_slope = _ema_slope_percent(spy_data["Close"], ema_period, slope_lookback)
    qqq_ema, qqq_slope = _ema_slope_percent(qqq_data["Close"], ema_period, slope_lookback)

    spy_atr = atr(spy_data, int(config.get("atr_period", 14))).dropna()
    atr_ratio = math.nan
    if len(spy_atr) >= volatility_lookback + 1:
        prior_atr = float(spy_atr.iloc[-(volatility_lookback + 1) : -1].mean())
        atr_ratio = float(spy_atr.iloc[-1]) / prior_atr if prior_atr > 0 else math.nan

    vix_close = math.nan
    vix_ratio = math.nan
    if vix is not None and not vix.empty and "Close" in vix:
        vix_close_series = vix["Close"].astype(float).dropna()
        if len(vix_close_series) >= volatility_lookback + 1:
            vix_close = float(vix_close_series.iloc[-1])
            prior_vix = float(vix_close_series.iloc[-(volatility_lookback + 1) : -1].mean())
            vix_ratio = vix_close / prior_vix if prior_vix > 0 else math.nan

    downside_return = (spy_close / float(spy_data["Close"].iloc[-(downside_lookback + 1)]) - 1.0) * 100.0
    volatility_expanding_down = (
        downside_return <= float(config.get("downside_threshold_percent", -1.0))
        and (
            (np.isfinite(vix_ratio) and vix_ratio >= float(config.get("vix_expansion_ratio", 1.20)))
            or (np.isfinite(atr_ratio) and atr_ratio >= float(config.get("atr_expansion_ratio", 1.25)))
        )
    )

    reasons: list[str] = []
    if not np.isfinite(spy_ema) or spy_close <= spy_ema:
        reasons.append("spy_below_20ema")
    if bool(config.get("require_positive_ema_slopes", True)):
        if not np.isfinite(spy_slope) or spy_slope <= 0:
            reasons.append("spy_20ema_slope_nonpositive")
        if not np.isfinite(qqq_slope) or qqq_slope <= 0:
            reasons.append("qqq_20ema_slope_nonpositive")
    if volatility_expanding_down:
        reasons.append("broad_volatility_expanding_on_downside")

    metrics = {
        "spy_close": round(spy_close, 4),
        "spy_ema20": serializable_number(spy_ema),
        "spy_ema20_slope_percent": serializable_number(spy_slope),
        "qqq_close": round(qqq_close, 4),
        "qqq_ema20": serializable_number(qqq_ema),
        "qqq_ema20_slope_percent": serializable_number(qqq_slope),
        "spy_atr_expansion_ratio": serializable_number(atr_ratio),
        "vix_close": serializable_number(vix_close),
        "vix_expansion_ratio": serializable_number(vix_ratio),
        "spy_downside_return_percent": round(downside_return, 4),
        "volatility_expanding_down": bool(volatility_expanding_down),
    }
    return RegimeResult(not reasons, reasons, metrics)


def sector_relative_performance(stock: pd.DataFrame, sector: pd.DataFrame) -> dict[str, float | None]:
    if stock is None or sector is None or stock.empty or sector.empty:
        return {"relative_5d_percent": None, "relative_20d_percent": None}

    stock_close = stock["Close"].astype(float).dropna()
    sector_close = sector["Close"].astype(float).dropna()
    output: dict[str, float | None] = {}
    for days, key in ((5, "relative_5d_percent"), (20, "relative_20d_percent")):
        if len(stock_close) < days + 1 or len(sector_close) < days + 1:
            output[key] = None
            continue
        stock_return = float(stock_close.iloc[-1] / stock_close.iloc[-(days + 1)] - 1.0)
        sector_return = float(sector_close.iloc[-1] / sector_close.iloc[-(days + 1)] - 1.0)
        output[key] = round((stock_return - sector_return) * 100.0, 6)
    return output


def _bandwidth_percentile(series: pd.Series, lookback: int, index: int = -2) -> float:
    clean = series.dropna()
    if len(clean) < lookback + abs(index):
        return math.nan
    target = float(clean.iloc[index])
    end = len(clean) + index + 1 if index < 0 else index + 1
    start = max(0, end - lookback)
    window = clean.iloc[start:end]
    if window.empty:
        return math.nan
    return float((window <= target).mean() * 100.0)


def _squeeze_metrics(data: pd.DataFrame, config: dict[str, Any]) -> dict[str, Any]:
    bb_period = int(config.get("bollinger_period", 20))
    bb_std = float(config.get("bollinger_std", 2.0))
    kc_period = int(config.get("keltner_period", 20))
    kc_multiplier = float(config.get("keltner_atr_multiplier", 1.5))
    bandwidth_lookback = int(config.get("squeeze_lookback_bars", 50))

    close = data["Close"].astype(float)
    bb_mid = close.rolling(bb_period).mean()
    bb_sigma = close.rolling(bb_period).std(ddof=0)
    bb_upper = bb_mid + bb_std * bb_sigma
    bb_lower = bb_mid - bb_std * bb_sigma

    kc_mid = ema(close, kc_period)
    kc_atr = atr(data, kc_period)
    kc_upper = kc_mid + kc_multiplier * kc_atr
    kc_lower = kc_mid - kc_multiplier * kc_atr

    squeeze_on = (bb_upper < kc_upper) & (bb_lower > kc_lower)
    bandwidth = ((bb_upper - bb_lower) / bb_mid.replace(0, np.nan)) * 100.0

    momentum_lookback = int(config.get("momentum_lookback_bars", 20))
    momentum_smoothing = int(config.get("momentum_smoothing_bars", 5))
    raw_momentum = close - close.rolling(momentum_lookback).mean()
    smoothed_momentum = raw_momentum.ewm(span=momentum_smoothing, adjust=False, min_periods=momentum_smoothing).mean()

    if len(data) < max(bb_period, kc_period, bandwidth_lookback) + 5:
        return {"qualifies": False, "raw": 0.0}

    previous_bw_pct = _bandwidth_percentile(bandwidth, bandwidth_lookback, index=-2)
    current_bw = float(bandwidth.iloc[-1])
    previous_bw = float(bandwidth.iloc[-2])
    released = bool(squeeze_on.iloc[-2]) and not bool(squeeze_on.iloc[-1])
    expanding = np.isfinite(current_bw) and np.isfinite(previous_bw) and previous_bw > 0 and current_bw > previous_bw
    current_momentum = float(smoothed_momentum.iloc[-1])
    previous_momentum = float(smoothed_momentum.iloc[-2])
    momentum_cross = np.isfinite(current_momentum) and np.isfinite(previous_momentum) and previous_momentum <= 0 < current_momentum

    percentile_limit = float(config.get("squeeze_bandwidth_percentile_max", 10.0))
    compressed = np.isfinite(previous_bw_pct) and previous_bw_pct <= percentile_limit
    qualifies = released and expanding and momentum_cross and compressed

    current_atr = float(atr(data, int(config.get("atr_period", 14))).iloc[-1])
    compression_strength = max(0.0, (percentile_limit - previous_bw_pct) / max(percentile_limit, 1e-9)) if np.isfinite(previous_bw_pct) else 0.0
    expansion_strength = max(0.0, (current_bw / previous_bw) - 1.0) if np.isfinite(current_bw) and np.isfinite(previous_bw) and previous_bw > 0 else 0.0
    momentum_atr = max(0.0, current_momentum / current_atr) if np.isfinite(current_momentum) and np.isfinite(current_atr) and current_atr > 0 else 0.0
    raw = compression_strength + 2.0 * expansion_strength + momentum_atr
    return {
        "qualifies": bool(qualifies),
        "raw": float(raw),
        "squeeze_released": bool(released),
        "bandwidth_expanding": bool(expanding),
        "momentum_cross_above_zero": bool(momentum_cross),
        "previous_bandwidth_percentile": serializable_number(previous_bw_pct),
        "current_bandwidth_percent": serializable_number(current_bw),
        "previous_bandwidth_percent": serializable_number(previous_bw),
        "smoothed_momentum": serializable_number(current_momentum),
        "atr": serializable_number(current_atr),
    }


def _find_previous_swing_high(data: pd.DataFrame, lookback: int, wing: int) -> int | None:
    if len(data) < wing * 2 + 5:
        return None
    high = data["High"].astype(float).to_numpy()
    start = max(wing, len(data) - lookback)
    stop = len(data) - wing - 1
    candidates: list[int] = []
    for idx in range(start, max(start, stop + 1)):
        left = high[idx - wing : idx]
        right = high[idx + 1 : idx + wing + 1]
        if len(left) < wing or len(right) < wing:
            continue
        if high[idx] > float(np.max(left)) and high[idx] >= float(np.max(right)):
            candidates.append(idx)
    if candidates:
        return candidates[-1]

    fallback_stop = max(1, len(data) - max(wing, 3))
    fallback_start = max(0, fallback_stop - lookback)
    if fallback_stop <= fallback_start:
        return None
    relative = int(np.argmax(high[fallback_start:fallback_stop]))
    return fallback_start + relative


def anchored_vwap_from_previous_swing_high(frame: pd.DataFrame, config: dict[str, Any]) -> dict[str, Any]:
    data = regular_session(frame)
    if data.empty:
        return {"value": None, "anchor_time": None}
    lookback = int(config.get("swing_high_lookback_bars", 80))
    wing = int(config.get("swing_high_wing_bars", 3))
    anchor_idx = _find_previous_swing_high(data, lookback, wing)
    if anchor_idx is None:
        return {"value": None, "anchor_time": None}

    anchored = data.iloc[anchor_idx:]
    typical = (anchored["High"].astype(float) + anchored["Low"].astype(float) + anchored["Close"].astype(float)) / 3.0
    volume = anchored["Volume"].astype(float)
    total_volume = float(volume.sum())
    if total_volume <= 0:
        return {"value": None, "anchor_time": data.index[anchor_idx].isoformat()}
    value = float((typical * volume).sum() / total_volume)
    return {
        "value": round(value, 6),
        "anchor_time": data.index[anchor_idx].isoformat(),
        "anchor_price": round(float(data["High"].iloc[anchor_idx]), 6),
    }


def _volume_absorption_metrics(data: pd.DataFrame, config: dict[str, Any]) -> dict[str, Any]:
    lookback = int(config.get("rvol_lookback_bars", 20))
    if len(data) < lookback + 5:
        return {"qualifies": False, "raw": 0.0}

    prior_volume = data["Volume"].astype(float).iloc[-(lookback + 1) : -1]
    average_volume = float(prior_volume.mean())
    current_volume = float(data["Volume"].iloc[-1])
    rvol = current_volume / average_volume if average_volume > 0 else 0.0
    anchored = anchored_vwap_from_previous_swing_high(data, config)
    anchored_vwap = anchored.get("value")
    close = float(data["Close"].iloc[-1])
    above = anchored_vwap is not None and close > float(anchored_vwap)
    distance_percent = ((close / float(anchored_vwap)) - 1.0) * 100.0 if anchored_vwap not in (None, 0) else math.nan
    qualifies = rvol >= float(config.get("rvol_min", 2.5)) and above
    raw = math.log1p(max(0.0, rvol))
    if np.isfinite(distance_percent):
        raw += max(-2.0, min(2.0, distance_percent / 2.0))
    if not above:
        raw *= -1.0

    return {
        "qualifies": bool(qualifies),
        "raw": float(raw),
        "rvol": round(rvol, 4),
        "anchored_vwap": anchored_vwap,
        "anchor_time": anchored.get("anchor_time"),
        "anchor_price": anchored.get("anchor_price"),
        "close": round(close, 4),
        "above_anchored_vwap": bool(above),
        "distance_above_anchored_vwap_percent": serializable_number(distance_percent),
    }


def load_gamma_snapshot(config: dict[str, Any], root: Path) -> dict[str, Any]:
    if not bool(config.get("enabled", False)):
        return {"available": False, "reason": "disabled", "tickers": {}}
    path = root / str(config.get("snapshot_path", "data/options_gamma.json"))
    if not path.exists():
        return {"available": False, "reason": "snapshot_missing", "tickers": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"available": False, "reason": "snapshot_invalid", "tickers": {}}
    if not isinstance(payload, dict):
        return {"available": False, "reason": "snapshot_invalid", "tickers": {}}

    as_of = payload.get("as_of")
    if as_of:
        try:
            stamp = pd.Timestamp(as_of)
            if stamp.tzinfo is None:
                stamp = stamp.tz_localize("UTC")
            age_minutes = (pd.Timestamp.now(tz="UTC") - stamp.tz_convert("UTC")).total_seconds() / 60.0
            if age_minutes > float(config.get("max_age_minutes", 60)):
                return {"available": False, "reason": "snapshot_stale", "tickers": {}}
        except (TypeError, ValueError):
            return {"available": False, "reason": "snapshot_invalid_time", "tickers": {}}

    tickers = payload.get("tickers", {})
    if not isinstance(tickers, dict):
        tickers = {}
    return {
        "available": bool(tickers),
        "reason": None if tickers else "snapshot_empty",
        "as_of": as_of,
        "tickers": tickers,
    }


def build_raw_alpha_signal(
    ticker: str,
    intraday_frame: pd.DataFrame,
    daily_details: dict[str, Any],
    alpha_config: dict[str, Any],
    gamma_snapshot: dict[str, Any],
) -> RawAlphaSignal | None:
    data = regular_session(intraday_frame)
    if data.empty or len(data) < int(alpha_config.get("minimum_intraday_bars", 220)):
        return None

    squeeze = _squeeze_metrics(data, alpha_config)
    volume = _volume_absorption_metrics(data, alpha_config)
    relative_5d = daily_details.get("relative_5d_percent")
    relative_20d = daily_details.get("relative_20d_percent")
    if relative_5d is None or relative_20d is None:
        return None
    relative_raw = (float(relative_5d) + float(relative_20d)) / 2.0

    gamma_record = {}
    if gamma_snapshot.get("available"):
        gamma_record = gamma_snapshot.get("tickers", {}).get(ticker, {}) or {}
    gamma_raw: float | None = None
    if isinstance(gamma_record, dict):
        candidate = gamma_record.get("gamma_acceleration_score")
        try:
            gamma_raw = float(candidate) if candidate is not None else None
        except (TypeError, ValueError):
            gamma_raw = None

    metrics = {
        "squeeze": squeeze,
        "relative_5d_percent": round(float(relative_5d), 6),
        "relative_20d_percent": round(float(relative_20d), 6),
        "sector": daily_details.get("sector"),
        "sector_etf": daily_details.get("sector_etf", "SPY"),
        "volume": volume,
        "atr": squeeze.get("atr"),
        "anchored_vwap": volume.get("anchored_vwap"),
        "gamma": {
            "status": "CONFIRMED" if gamma_raw is not None else "UNAVAILABLE",
            "gamma_acceleration_score": gamma_raw,
            "call_oi_ratio": gamma_record.get("call_oi_ratio") if isinstance(gamma_record, dict) else None,
            "overhead_strike": gamma_record.get("overhead_strike") if isinstance(gamma_record, dict) else None,
            "snapshot_as_of": gamma_snapshot.get("as_of"),
            "reason": None if gamma_raw is not None else gamma_snapshot.get("reason", "ticker_missing"),
        },
    }
    return RawAlphaSignal(
        ticker=ticker,
        trigger_time=data.index[-1].isoformat(),
        volatility_raw=float(squeeze.get("raw", 0.0)),
        relative_strength_raw=float(relative_raw),
        volume_raw=float(volume.get("raw", 0.0)),
        gamma_raw=gamma_raw,
        gates={
            "volatility_transition": bool(squeeze.get("qualifies", False)),
            "volume_absorption": bool(volume.get("qualifies", False)),
        },
        metrics=metrics,
    )


def _quant_trade_plan(raw: RawAlphaSignal, alpha_config: dict[str, Any], paper_config: dict[str, Any]) -> dict[str, float] | None:
    current_atr = raw.metrics.get("atr")
    close = raw.metrics.get("volume", {}).get("close")
    if current_atr is None or close is None:
        return None
    atr_value = float(current_atr)
    entry_low = float(close)
    if atr_value <= 0 or entry_low <= 0:
        return None

    entry_high = entry_low * (1.0 + float(paper_config.get("entry_buffer_percent", 0.15)) / 100.0)
    stop_multiple = float(paper_config.get("stop_atr_multiple", 1.5))
    tp1_multiple = float(paper_config.get("target_1_atr_multiple", 1.5))
    tp2_multiple = float(paper_config.get("target_2_atr_multiple", 2.5))
    stop = entry_low - stop_multiple * atr_value
    return {
        "entry_low": round(entry_low, 4),
        "entry_high": round(entry_high, 4),
        "stop": round(stop, 4),
        "risk_per_share": round(entry_low - stop, 4),
        "risk_percent": round(((entry_low - stop) / entry_low) * 100.0, 4),
        "tp1": round(entry_low + tp1_multiple * atr_value, 4),
        "tp2": round(entry_low + tp2_multiple * atr_value, 4),
        "atr": round(atr_value, 6),
        "entry_vwap": round(float(raw.metrics.get("anchored_vwap") or entry_low), 6),
    }


def rank_alpha_signals(
    raw_signals: list[RawAlphaSignal],
    alpha_config: dict[str, Any],
    paper_config: dict[str, Any],
) -> list[dict[str, Any]]:
    if not raw_signals:
        return []

    volatility_z = _cross_sectional_z({item.ticker: item.volatility_raw for item in raw_signals})
    relative_z = _cross_sectional_z({item.ticker: item.relative_strength_raw for item in raw_signals})
    volume_z = _cross_sectional_z({item.ticker: item.volume_raw for item in raw_signals})
    gamma_values = {item.ticker: float(item.gamma_raw) for item in raw_signals if item.gamma_raw is not None and np.isfinite(item.gamma_raw)}
    gamma_z = _cross_sectional_z(gamma_values)

    weights = {
        "volatility": float(alpha_config.get("volatility_weight", 0.30)),
        "relative_strength": float(alpha_config.get("relative_strength_weight", 0.30)),
        "volume": float(alpha_config.get("volume_weight", 0.20)),
        "gamma": float(alpha_config.get("gamma_weight", 0.20)),
    }
    relative_min = float(alpha_config.get("relative_strength_z_min", 1.75))

    output: list[dict[str, Any]] = []
    for raw in raw_signals:
        z_values: dict[str, float | None] = {
            "volatility": volatility_z.get(raw.ticker, 0.0),
            "relative_strength": relative_z.get(raw.ticker, 0.0),
            "volume": volume_z.get(raw.ticker, 0.0),
            "gamma": gamma_z.get(raw.ticker) if raw.gamma_raw is not None else None,
        }

        numerator = 0.0
        denominator = 0.0
        for name, weight in weights.items():
            z_value = z_values[name]
            if z_value is None:
                continue
            numerator += weight * float(z_value)
            denominator += weight
        composite_z = numerator / denominator if denominator > 0 else -math.inf
        alpha_score = _normal_cdf(composite_z) * 100.0 if np.isfinite(composite_z) else 0.0

        gamma_available = z_values["gamma"] is not None
        gamma_positive = (raw.gamma_raw or 0.0) > 0 if gamma_available else True
        gates = {
            **raw.gates,
            "relative_strength": float(z_values["relative_strength"] or 0.0) >= relative_min,
            "gamma_positive_when_available": gamma_positive,
        }
        eligible = all(gates.values())

        factor_breakdown = {
            "volatility": {
                "raw": round(raw.volatility_raw, 6),
                "z": round(float(z_values["volatility"] or 0.0), 4),
                "weight": weights["volatility"],
                "status": "CONFIRMED",
            },
            "relative_strength": {
                "raw": round(raw.relative_strength_raw, 6),
                "z": round(float(z_values["relative_strength"] or 0.0), 4),
                "weight": weights["relative_strength"],
                "status": "CONFIRMED",
            },
            "volume": {
                "raw": round(raw.volume_raw, 6),
                "z": round(float(z_values["volume"] or 0.0), 4),
                "weight": weights["volume"],
                "status": "CONFIRMED",
            },
            "gamma": {
                "raw": round(float(raw.gamma_raw), 6) if raw.gamma_raw is not None else None,
                "z": round(float(z_values["gamma"]), 4) if z_values["gamma"] is not None else None,
                "weight": weights["gamma"],
                "status": "CONFIRMED" if gamma_available else "UNAVAILABLE",
            },
        }

        plan = _quant_trade_plan(raw, alpha_config, paper_config) if eligible else None
        tier = "A" if eligible and alpha_score >= float(paper_config.get("minimum_trade_score", 80)) else ("B" if alpha_score >= float(alpha_config.get("watchlist_score", 60)) else None)
        output.append(
            {
                "ticker": raw.ticker,
                "score": int(round(alpha_score)),
                "alpha_score": round(alpha_score, 3),
                "composite_z": round(composite_z, 4),
                "tier": tier,
                "factors": {
                    "volatility": int(round(max(0.0, min(100.0, _normal_cdf(float(z_values["volatility"] or 0.0)) * 100.0)))),
                    "relative_strength": int(round(max(0.0, min(100.0, _normal_cdf(float(z_values["relative_strength"] or 0.0)) * 100.0)))),
                    "volume": int(round(max(0.0, min(100.0, _normal_cdf(float(z_values["volume"] or 0.0)) * 100.0)))),
                    "gamma": int(round(_normal_cdf(float(z_values["gamma"])) * 100.0)) if z_values["gamma"] is not None else 0,
                },
                "factor_breakdown": factor_breakdown,
                "gates": gates,
                "eligible": bool(eligible),
                "metrics": raw.metrics,
                "trade_plan": plan,
                "trigger_time": raw.trigger_time,
            }
        )

    output.sort(
        key=lambda item: (
            bool(item["eligible"]),
            float(item["alpha_score"]),
            float(item["composite_z"]),
            item["ticker"],
        ),
        reverse=True,
    )
    return output
