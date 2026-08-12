from __future__ import annotations

import numpy as np
import pandas as pd

from swing_bot.quant_alpha import RawAlphaSignal, evaluate_market_regime, rank_alpha_signals


def _daily(close_values: np.ndarray) -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=len(close_values), freq="B")
    close = pd.Series(close_values, index=index, dtype=float)
    return pd.DataFrame(
        {
            "Open": close,
            "High": close + 1.0,
            "Low": close - 1.0,
            "Close": close,
            "Volume": 1_000_000.0,
        },
        index=index,
    )


def _regime_config() -> dict[str, object]:
    return {
        "enabled": True,
        "ema_period": 20,
        "ema_slope_lookback_days": 5,
        "require_positive_ema_slopes": True,
        "atr_period": 14,
        "volatility_lookback_days": 20,
        "downside_lookback_days": 3,
        "downside_threshold_percent": -1.0,
        "vix_expansion_ratio": 1.20,
        "atr_expansion_ratio": 1.25,
    }


def test_market_regime_allows_rising_market() -> None:
    spy = _daily(np.linspace(100.0, 130.0, 80))
    qqq = _daily(np.linspace(100.0, 140.0, 80))
    vix = _daily(np.full(80, 15.0))
    result = evaluate_market_regime(spy, qqq, vix, _regime_config())
    assert result.allow_new_longs


def test_market_regime_blocks_spy_below_ema() -> None:
    values = np.linspace(100.0, 130.0, 80)
    values[-1] = 90.0
    spy = _daily(values)
    qqq = _daily(np.linspace(100.0, 140.0, 80))
    vix = _daily(np.full(80, 15.0))
    result = evaluate_market_regime(spy, qqq, vix, _regime_config())
    assert not result.allow_new_longs
    assert "spy_below_20ema" in result.reasons


def test_missing_gamma_is_unavailable_and_weights_renormalize() -> None:
    raw_signals: list[RawAlphaSignal] = []
    for index in range(6):
        strong = index == 5
        raw_signals.append(
            RawAlphaSignal(
                ticker=f"T{index}",
                trigger_time="2026-08-11T10:00:00-04:00",
                volatility_raw=10.0 if strong else float(index),
                relative_strength_raw=20.0 if strong else float(index),
                volume_raw=10.0 if strong else float(index),
                gamma_raw=None,
                gates={
                    "volatility_transition": strong,
                    "volume_absorption": strong,
                },
                metrics={
                    "atr": 2.0,
                    "anchored_vwap": 100.0,
                    "volume": {"close": 105.0},
                },
            )
        )

    ranked = rank_alpha_signals(
        raw_signals,
        {
            "volatility_weight": 0.30,
            "relative_strength_weight": 0.30,
            "volume_weight": 0.20,
            "gamma_weight": 0.20,
            "relative_strength_z_min": 1.75,
            "watchlist_score": 60,
        },
        {
            "minimum_trade_score": 80,
            "entry_buffer_percent": 0.15,
            "stop_atr_multiple": 1.5,
            "target_1_atr_multiple": 1.5,
            "target_2_atr_multiple": 2.5,
        },
    )

    best = ranked[0]
    assert best["ticker"] == "T5"
    assert best["eligible"]
    assert best["factor_breakdown"]["gamma"]["status"] == "UNAVAILABLE"
    assert best["trade_plan"] is not None
