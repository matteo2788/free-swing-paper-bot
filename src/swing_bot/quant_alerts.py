from __future__ import annotations

from typing import Any

from .responsive_runtime import ResponsiveDiscordAlerter


def _money(value: Any) -> str:
    try:
        return f"${float(value):,.2f}"
    except (TypeError, ValueError):
        return "$0.00"


def _z(value: Any) -> str:
    if value is None:
        return "UNAVAILABLE"
    try:
        return f"{float(value):+.2f}σ"
    except (TypeError, ValueError):
        return "UNAVAILABLE"


class QuantDiscordAlerter(ResponsiveDiscordAlerter):
    """Quant-specific Discord output with factor transparency and performance analytics."""

    def setup_alert(self, ticker: str, result: dict[str, Any], crossing: str) -> bool:
        score = float(result.get("alpha_score", result.get("score", 0)))
        breakdown = result.get("factor_breakdown", {})
        gates = result.get("gates", {})
        plan = result.get("trade_plan") or {}
        tier = "QUANT A" if crossing == "A" else "QUANT WATCHLIST"
        color = 0x2ECC71 if crossing == "A" else 0xF1C40F

        factor_lines: list[str] = []
        labels = (
            ("Volatility", "volatility"),
            ("Relative strength", "relative_strength"),
            ("Volume / AVWAP", "volume"),
            ("Options gamma", "gamma"),
        )
        for label, key in labels:
            factor = breakdown.get(key, {})
            status = factor.get("status", "UNAVAILABLE")
            factor_lines.append(
                f"**{label}:** {_z(factor.get('z'))} • {status} • weight {float(factor.get('weight', 0.0)) * 100:.0f}%"
            )

        gate_text = " • ".join(
            f"{name.replace('_', ' ')}={'PASS' if passed else 'FAIL'}"
            for name, passed in gates.items()
        )
        plan_text = "Watchlist only; no paper position is armed."
        if crossing == "A" and plan:
            plan_text = (
                f"Entry zone: **{_money(plan.get('entry_low'))}–{_money(plan.get('entry_high'))}**\n"
                f"ATR: **{_money(plan.get('atr'))}**\n"
                f"Stop (1.5× ATR): **{_money(plan.get('stop'))}**\n"
                f"TP1 (1.5× ATR): **{_money(plan.get('tp1'))}**\n"
                f"TP2 (2.5× ATR): **{_money(plan.get('tp2'))}**"
            )

        return self.send_embed(
            title=f"📡 {ticker} • {tier} • {score:.1f}/100",
            description=(
                "Cross-sectional Alpha Score from volatility compression, sector-relative strength, "
                "volume/anchored-VWAP behavior, and options gamma when real gamma data is available."
            ),
            color=color,
            fields=[
                {"name": "Composite factors", "value": "\n".join(factor_lines), "inline": False},
                {"name": "Hard gates", "value": gate_text[:1024], "inline": False},
                {"name": "Paper plan", "value": plan_text, "inline": False},
            ],
            footer="Paper trading only • Yahoo factors are delayed OHLCV • Missing options gamma is never fabricated",
        )

    def entry_alert(self, position: dict[str, Any]) -> bool:
        account_value = float(position.get("account_value_at_entry", 0.0))
        invested = float(position.get("initial_notional", 0.0))
        cash_left = float(position.get("cash_unused", max(0.0, account_value - invested)))
        quantity = int(position.get("quantity", 0))
        atr_value = float(position.get("signal_atr", 0.0))
        alpha_score = float(position.get("alpha_score", position.get("score", 0.0)))
        breakdown = position.get("factor_breakdown", {})

        factors = (
            f"Volatility: **{_z(breakdown.get('volatility', {}).get('z'))}**\n"
            f"Relative strength: **{_z(breakdown.get('relative_strength', {}).get('z'))}**\n"
            f"Volume / AVWAP: **{_z(breakdown.get('volume', {}).get('z'))}**\n"
            f"Options gamma: **{_z(breakdown.get('gamma', {}).get('z'))}** "
            f"({breakdown.get('gamma', {}).get('status', 'UNAVAILABLE')})"
        )
        trade = (
            f"**Ticker:** `{position.get('ticker', 'UNKNOWN')}`\n"
            f"**Alpha Score:** `{alpha_score:.1f}/100`\n"
            f"**Shares:** `{quantity}`\n"
            f"**Reference price:** `{_money(position.get('entry_reference_price'))}`\n"
            f"**Simulated fill:** `{_money(position.get('entry_price'))}`\n"
            f"**Capital allocated:** `{_money(invested)}`\n"
            f"**Cash remaining:** `{_money(cash_left)}`"
        )
        risk = (
            f"**ATR at signal:** `{_money(atr_value)}`\n"
            f"🛑 **Initial stop (1.5× ATR):** `{_money(position.get('initial_stop'))}`\n"
            f"🎯 **TP1 (1.5× ATR):** `{_money(position.get('tp1'))}`\n"
            f"🎯 **TP2 (2.5× ATR):** `{_money(position.get('tp2'))}`\n"
            f"🏃 **Runner trail:** `Activates after TP2 at 2× 5m ATR`\n"
            f"**Planned stop risk:** `{_money(position.get('initial_risk_dollars'))}`\n"
            f"**Risk budget:** `{float(position.get('risk_budget_percent', 0.0)):.2f}% of account`"
        )
        friction = (
            f"**Entry slippage:** `{float(position.get('slippage_percent', 0.0)):.3f}%`\n"
            f"**Entry fee:** `{_money(position.get('entry_fee'))}`\n"
            f"**Modeled fees paid so far:** `{_money(position.get('fees_paid'))}`"
        )

        return self.send_embed(
            title=f"🧾 QUANT PAPER RECEIPT • {position.get('ticker', 'UNKNOWN')}",
            description=(
                "**Simulated fill confirmed.** One paper position is open using ATR risk-parity sizing. "
                "This receipt shows the measured factors and modeled execution friction."
            ),
            color=0x2ECC71,
            fields=[
                {"name": "📦 Trade", "value": trade, "inline": False},
                {"name": "🧮 Composite factors", "value": factors, "inline": False},
                {"name": "🗺️ Risk / targets", "value": risk, "inline": False},
                {"name": "🧾 Execution friction", "value": friction, "inline": False},
                {
                    "name": "💵 Account",
                    "value": (
                        f"Equity before entry: **{_money(account_value)}**\n"
                        f"Capital in position: **{_money(invested)}**\n"
                        f"Uninvested cash: **{_money(cash_left)}**"
                    ),
                    "inline": False,
                },
                {
                    "name": "🕒 Timing",
                    "value": self._timing_field(position.get("entry_time")),
                    "inline": False,
                },
            ],
            footer="Paper trading only • 0.05% slippage + configured transaction fee are modeled on fills",
        )

    def position_event_alert(
        self,
        title: str,
        position: dict[str, Any],
        message: str,
        color: int,
    ) -> bool:
        exits = position.get("exits") or []
        event_time = exits[-1].get("time") if exits else position.get("last_processed_bar")
        trailing = position.get("trailing_stop")
        stop_line = f"\nActive 2× ATR trail: **{_money(trailing)}**" if trailing is not None else ""
        return self.send_embed(
            title=f"{title} • {position['ticker']}",
            description=message,
            color=color,
            fields=[
                {
                    "name": "Position",
                    "value": (
                        f"Remaining: {position['remaining_quantity']} / {position['quantity']} shares\n"
                        f"Realized P/L: {_money(position.get('realized_pnl', 0.0))}\n"
                        f"Fees paid: {_money(position.get('fees_paid', 0.0))}\n"
                        f"Return: {position.get('last_return_percent', 0.0):+.2f}%\n"
                        f"R multiple: {position.get('last_r_multiple', 0.0):+.2f}R"
                        f"{stop_line}"
                    ),
                    "inline": False,
                },
                {
                    "name": "Timing",
                    "value": self._timing_field(event_time),
                    "inline": False,
                },
            ],
            footer="Paper trading only • Exit fills include modeled slippage and transaction fees",
        )

    def trade_closed_alert(
        self,
        position: dict[str, Any],
        outcome: str,
        account_value: float,
        performance: dict[str, Any],
    ) -> bool:
        profit_factor = performance.get("profit_factor")
        profit_factor_text = "∞" if profit_factor is None and performance.get("gross_profit", 0) > 0 else (
            f"{float(profit_factor):.2f}" if profit_factor is not None else "0.00"
        )
        last_exit = (position.get("exits") or [{}])[-1]
        return self.send_embed(
            title=f"🏁 QUANT PAPER TRADE CLOSED • {position.get('ticker', 'UNKNOWN')}",
            description=(
                f"Outcome: **{outcome}**\n"
                f"Final simulated exit: **{_money(last_exit.get('price'))}**\n"
                f"Net realized P/L: **{_money(position.get('realized_pnl', 0.0))}**\n"
                f"Total modeled fees: **{_money(position.get('fees_paid', 0.0))}**\n"
                f"Paper account equity: **{_money(account_value)}**"
            ),
            color=0x2ECC71 if float(position.get("realized_pnl", 0.0)) > 0 else 0xE74C3C,
            fields=[
                {
                    "name": "Running expectancy",
                    "value": (
                        f"Closed trades: **{int(performance.get('closed_trades', 0))}**\n"
                        f"Win rate: **{float(performance.get('win_rate_percent', 0.0)):.2f}%**\n"
                        f"Expectancy: **{_money(performance.get('expectancy_dollars', 0.0))} / trade**\n"
                        f"Profit factor: **{profit_factor_text}**\n"
                        f"Max drawdown: **{_money(performance.get('max_drawdown_dollars', 0.0))} "
                        f"({float(performance.get('max_drawdown_percent', 0.0)):.2f}%)**"
                    ),
                    "inline": False,
                }
            ],
            footer="Paper trading only • Statistics include modeled slippage and configured transaction fees",
        )
