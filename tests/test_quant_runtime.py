from __future__ import annotations

from pathlib import Path

import pandas as pd

from swing_bot.quant_runtime import QuantPaperTradingEngine
from swing_bot.storage import Storage


class _Clock:
    def trading_days_between(self, *_args: object, **_kwargs: object) -> int:
        return 0


class _Alerter:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def position_event_alert(self, title: str, _position: dict, message: str, _color: int) -> bool:
        self.events.append((title, message))
        return True


def _storage(tmp_path: Path) -> Storage:
    return Storage(
        tmp_path / "state.json",
        tmp_path / "trades.csv",
        tmp_path / "events.jsonl",
        tmp_path / "commit_required",
    )


def _config() -> dict[str, object]:
    return {
        "starting_account_value": 10000.0,
        "risk_per_trade_percent": 2.0,
        "max_position_notional_percent": 100.0,
        "target_1_exit_fraction": 0.50,
        "target_2_exit_fraction": 0.25,
        "target_1_atr_multiple": 1.5,
        "target_2_atr_multiple": 2.5,
        "stop_atr_multiple": 1.5,
        "trailing_atr_multiple": 2.0,
        "slippage_percent": 0.05,
        "transaction_fee_percent": 0.01,
        "early_invalidation_bars": 3,
        "atr_period": 14,
        "time_stop_trading_days": 5,
    }


def _position(entry_time: pd.Timestamp) -> dict:
    return {
        "trade_id": "TEST-1",
        "ticker": "TEST",
        "tier": "A",
        "score": 90,
        "alpha_score": 90.0,
        "signal_time": entry_time.isoformat(),
        "entry_time": entry_time.isoformat(),
        "entry_reference_price": 100.0,
        "entry_price": 100.0,
        "entry_vwap": 100.0,
        "signal_atr": 2.0,
        "initial_stop": 97.0,
        "stop": 97.0,
        "tp1": 103.0,
        "tp2": 105.0,
        "quantity": 10,
        "remaining_quantity": 10,
        "target_1_quantity": 5,
        "target_2_quantity": 2,
        "initial_notional": 1000.0,
        "initial_risk_dollars": 30.0,
        "account_value_at_entry": 10000.0,
        "cash_unused": 9000.0,
        "slippage_percent": 0.05,
        "transaction_fee_percent": 0.01,
        "entry_fee": 0.0,
        "fees_paid": 0.0,
        "realized_pnl": 0.0,
        "stage": 0,
        "trailing_stop": None,
        "highest_since_entry": 100.0,
        "last_processed_bar": entry_time.isoformat(),
        "last_price": 100.0,
        "last_return_percent": 0.0,
        "last_r_multiple": 0.0,
        "exits": [],
    }


def test_early_vwap_invalidation_closes_position(tmp_path: Path) -> None:
    index = pd.date_range("2026-08-11 10:00", periods=2, freq="5min", tz="America/New_York")
    frame = pd.DataFrame(
        {
            "Open": [100.0, 100.0],
            "High": [101.0, 100.2],
            "Low": [99.5, 99.0],
            "Close": [100.0, 99.5],
            "Volume": [1000.0, 800.0],
        },
        index=index,
    )
    alerter = _Alerter()
    engine = QuantPaperTradingEngine(_config(), _Clock(), _storage(tmp_path), alerter)
    position = _position(index[0])
    state = {
        "positions": {"TEST-1": position},
        "pending": {},
        "paper_account_value": 10000.0,
        "paper_account_starting_value": 10000.0,
        "performance": {},
    }

    changed = engine._process_positions(state, {"TEST": frame})

    assert changed
    assert not state["positions"]
    assert state["performance"]["closed_trades"] == 1
    assert alerter.events
    assert "EARLY VWAP INVALIDATION" in alerter.events[-1][1]


def test_runner_chandelier_trail_ratchets_up(tmp_path: Path) -> None:
    index = pd.date_range("2026-08-11 10:00", periods=22, freq="5min", tz="America/New_York")
    closes = [100.0 + (i * 0.08) for i in range(21)] + [108.0]
    frame = pd.DataFrame(
        {
            "Open": closes,
            "High": [value + 0.5 for value in closes[:-1]] + [110.0],
            "Low": [value - 0.5 for value in closes[:-1]] + [107.0],
            "Close": closes,
            "Volume": [1000.0] * 22,
        },
        index=index,
    )
    alerter = _Alerter()
    engine = QuantPaperTradingEngine(_config(), _Clock(), _storage(tmp_path), alerter)
    position = _position(index[0])
    position.update(
        {
            "stage": 2,
            "remaining_quantity": 3,
            "stop": 100.0,
            "trailing_stop": 100.0,
            "highest_since_entry": 105.0,
            "last_processed_bar": index[-2].isoformat(),
        }
    )
    state = {
        "positions": {"TEST-1": position},
        "pending": {},
        "paper_account_value": 10000.0,
        "paper_account_starting_value": 10000.0,
        "performance": {},
    }

    changed = engine._process_positions(state, {"TEST": frame})

    assert not changed
    updated = state["positions"]["TEST-1"]
    assert updated["highest_since_entry"] == 110.0
    assert updated["trailing_stop"] > 100.0
    assert updated["stop"] == updated["trailing_stop"]
