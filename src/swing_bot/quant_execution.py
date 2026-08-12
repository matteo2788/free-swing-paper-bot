from __future__ import annotations

import math
from typing import Any


def apply_long_entry_slippage(price: float, slippage_percent: float) -> float:
    return round(float(price) * (1.0 + float(slippage_percent) / 100.0), 6)


def apply_long_exit_slippage(price: float, slippage_percent: float) -> float:
    return round(float(price) * (1.0 - float(slippage_percent) / 100.0), 6)


def transaction_fee(notional: float, fee_percent: float) -> float:
    return round(max(0.0, float(notional)) * max(0.0, float(fee_percent)) / 100.0, 6)


def risk_parity_quantity(
    account_value: float,
    risk_percent: float,
    entry_price: float,
    stop_price: float,
    max_notional_percent: float = 100.0,
    fee_percent: float = 0.0,
) -> int:
    account = float(account_value)
    entry = float(entry_price)
    stop = float(stop_price)
    risk_per_share = entry - stop
    if account <= 0 or entry <= 0 or risk_per_share <= 0:
        return 0

    risk_budget = account * float(risk_percent) / 100.0
    quantity_by_risk = math.floor(risk_budget / risk_per_share)

    max_notional = account * float(max_notional_percent) / 100.0
    entry_cost_per_share = entry * (1.0 + max(0.0, float(fee_percent)) / 100.0)
    quantity_by_notional = math.floor(max_notional / entry_cost_per_share)
    return max(0, min(quantity_by_risk, quantity_by_notional))


def update_performance_statistics(
    previous: dict[str, Any] | None,
    *,
    realized_pnl: float,
    account_value: float,
    starting_account_value: float,
) -> dict[str, Any]:
    stats = dict(previous or {})
    closed = int(stats.get("closed_trades", 0)) + 1
    wins = int(stats.get("wins", 0)) + (1 if realized_pnl > 0 else 0)
    losses = int(stats.get("losses", 0)) + (1 if realized_pnl < 0 else 0)
    breakeven = int(stats.get("breakeven", 0)) + (1 if realized_pnl == 0 else 0)
    gross_profit = float(stats.get("gross_profit", 0.0)) + max(0.0, realized_pnl)
    gross_loss = float(stats.get("gross_loss", 0.0)) + max(0.0, -realized_pnl)
    total_pnl = float(stats.get("total_pnl", 0.0)) + realized_pnl

    current_equity = float(account_value)
    peak_equity = max(
        float(stats.get("peak_equity", starting_account_value)),
        current_equity,
        float(starting_account_value),
    )
    drawdown_dollars = max(0.0, peak_equity - current_equity)
    drawdown_percent = (drawdown_dollars / peak_equity * 100.0) if peak_equity > 0 else 0.0
    max_drawdown_dollars = max(
        float(stats.get("max_drawdown_dollars", 0.0)),
        drawdown_dollars,
    )
    max_drawdown_percent = max(
        float(stats.get("max_drawdown_percent", 0.0)),
        drawdown_percent,
    )

    if gross_loss > 0:
        profit_factor: float | None = gross_profit / gross_loss
    elif gross_profit > 0:
        profit_factor = None
    else:
        profit_factor = 0.0

    return {
        "closed_trades": closed,
        "wins": wins,
        "losses": losses,
        "breakeven": breakeven,
        "win_rate_percent": round((wins / closed) * 100.0, 3),
        "expectancy_dollars": round(total_pnl / closed, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "profit_factor": round(profit_factor, 4) if profit_factor is not None else None,
        "total_pnl": round(total_pnl, 2),
        "starting_account_value": round(float(starting_account_value), 2),
        "current_account_value": round(current_equity, 2),
        "peak_equity": round(peak_equity, 2),
        "max_drawdown_dollars": round(max_drawdown_dollars, 2),
        "max_drawdown_percent": round(max_drawdown_percent, 3),
    }
