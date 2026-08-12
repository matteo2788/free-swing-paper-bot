from __future__ import annotations

from swing_bot.quant_execution import (
    apply_long_entry_slippage,
    apply_long_exit_slippage,
    risk_parity_quantity,
    update_performance_statistics,
)


def test_risk_parity_quantity_uses_two_percent_budget() -> None:
    assert risk_parity_quantity(10000, 2.0, 100.0, 97.0) == 66


def test_slippage_is_applied_against_long_position() -> None:
    assert apply_long_entry_slippage(100.0, 0.05) == 100.05
    assert apply_long_exit_slippage(100.0, 0.05) == 99.95


def test_performance_statistics_track_expectancy_and_drawdown() -> None:
    first = update_performance_statistics(
        None,
        realized_pnl=100.0,
        account_value=10100.0,
        starting_account_value=10000.0,
    )
    second = update_performance_statistics(
        first,
        realized_pnl=-200.0,
        account_value=9900.0,
        starting_account_value=10000.0,
    )
    assert second["closed_trades"] == 2
    assert second["win_rate_percent"] == 50.0
    assert second["expectancy_dollars"] == -50.0
    assert second["profit_factor"] == 0.5
    assert second["max_drawdown_percent"] > 0
