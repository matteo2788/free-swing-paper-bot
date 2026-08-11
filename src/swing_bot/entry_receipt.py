from __future__ import annotations

from typing import Any

from .responsive_runtime import ResponsiveDiscordAlerter


def _money(value: Any) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def _percent(value: float) -> str:
    return f"{value:+.2f}%"


def _price_move_percent(entry: float, target: float) -> float:
    if entry <= 0:
        return 0.0
    return ((target - entry) / entry) * 100.0


def _entry_receipt_alert(self: ResponsiveDiscordAlerter, position: dict[str, Any]) -> bool:
    ticker = str(position.get("ticker", "UNKNOWN"))
    entry = float(position.get("entry_price", 0.0))
    quantity = int(position.get("quantity", 0))
    invested = float(position.get("initial_notional", entry * quantity))

    account_value = float(
        position.get(
            "account_value_at_entry",
            invested + float(position.get("cash_unused", 0.0)),
        )
    )
    cash_left = float(position.get("cash_unused", max(0.0, account_value - invested)))
    allocation = float(
        position.get(
            "allocation_percent",
            (invested / account_value * 100.0) if account_value > 0 else 0.0,
        )
    )

    stop = float(position.get("stop", position.get("initial_stop", 0.0)))
    tp1 = float(position.get("tp1", 0.0))
    tp2 = float(position.get("tp2", 0.0))
    stop_risk = float(position.get("initial_risk_dollars", max(0.0, entry - stop) * quantity))
    stop_risk_account_percent = (stop_risk / account_value * 100.0) if account_value > 0 else 0.0

    tp1_qty = int(position.get("target_1_quantity", 0))
    tp2_qty = int(position.get("target_2_quantity", 0))
    runner_qty = max(0, quantity - tp1_qty - tp2_qty)

    tp1_profit = max(0.0, tp1 - entry) * tp1_qty
    tp2_profit = max(0.0, tp2 - entry) * tp2_qty

    score = int(position.get("score", 0))
    trade_id = str(position.get("trade_id", "N/A"))

    receipt = (
        f"**Ticker:** `{ticker}`\n"
        f"**Shares:** `{quantity}`\n"
        f"**Price / share:** `{_money(entry)}`\n"
        f"**Money invested:** `{_money(invested)}`\n"
        f"**Account used:** `{allocation:.2f}%`"
    )

    account = (
        f"**Account before entry:** `{_money(account_value)}`\n"
        f"**Cash left uninvested:** `{_money(cash_left)}`\n"
        f"**Position value at fill:** `{_money(invested)}`\n"
        f"**Account equity at entry:** `{_money(account_value)}`"
    )

    plan = (
        f"🛑 **Stop Loss:** `{_money(stop)}` "
        f"({_percent(_price_move_percent(entry, stop))})\n"
        f"↳ Max planned loss: **{_money(stop_risk)}** "
        f"({stop_risk_account_percent:.2f}% of account)\n\n"
        f"🎯 **TP1:** `{_money(tp1)}` "
        f"({_percent(_price_move_percent(entry, tp1))})\n"
        f"↳ Planned exit: **{tp1_qty} shares** • approx. **+{_money(tp1_profit)}**\n\n"
        f"🎯 **TP2:** `{_money(tp2)}` "
        f"({_percent(_price_move_percent(entry, tp2))})\n"
        f"↳ Planned exit: **{tp2_qty} shares** • approx. **+{_money(tp2_profit)}**\n\n"
        f"🏃 **Runner after TP2:** `{runner_qty} shares`"
    )

    setup = (
        f"**Setup score:** `{score}/100`\n"
        f"**Sizing:** `ALL-IN PAPER ACCOUNT`\n"
        f"**Trade ID:** `{trade_id}`"
    )

    return self.send_embed(
        title=f"🧾 PAPER TRADE RECEIPT • {ticker}",
        description=(
            "**Simulated fill confirmed.** The bot selected this as the strongest available "
            "A-quality setup and opened the one allowed paper position."
        ),
        color=0x2ECC71,
        fields=[
            {"name": "📦 Trade", "value": receipt, "inline": False},
            {"name": "💵 Account", "value": account, "inline": False},
            {"name": "🗺️ Trade Plan", "value": plan, "inline": False},
            {"name": "📊 Setup", "value": setup, "inline": False},
            {
                "name": "🕒 Timing",
                "value": self._timing_field(position.get("entry_time")),
                "inline": False,
            },
        ],
        footer=(
            "Paper trading only • Cash left means uninvested simulated cash • "
            "Account equity includes the paper position"
        ),
    )


def install_entry_receipt() -> None:
    """Replace the normal paper-entry alert with the detailed receipt format."""
    ResponsiveDiscordAlerter.entry_alert = _entry_receipt_alert  # type: ignore[method-assign]
