from __future__ import annotations

import logging
import os
from typing import Any

import requests

LOGGER = logging.getLogger(__name__)


class DiscordAlerter:
    def __init__(self, alert_config: dict[str, Any], dry_run: bool = False) -> None:
        self.webhook_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
        self.username = str(alert_config.get("username", "Free Swing Paper Bot"))
        self.mention_everyone = bool(alert_config.get("mention_everyone", False))
        self.dry_run = dry_run

    @property
    def configured(self) -> bool:
        return bool(self.webhook_url)

    def send_embed(
        self,
        *,
        title: str,
        description: str,
        color: int,
        fields: list[dict[str, Any]] | None = None,
        footer: str = "Paper trading only • Free data may be delayed",
    ) -> bool:
        payload = {
            "username": self.username,
            "allowed_mentions": {"parse": ["everyone"] if self.mention_everyone else []},
            "embeds": [
                {
                    "title": title[:256],
                    "description": description[:4096],
                    "color": color,
                    "fields": (fields or [])[:25],
                    "footer": {"text": footer[:2048]},
                }
            ],
        }
        if self.dry_run or not self.webhook_url:
            LOGGER.info("Discord alert (%s): %s", title, description.replace("\n", " | "))
            return True
        try:
            response = requests.post(self.webhook_url, json=payload, timeout=20)
            response.raise_for_status()
            return True
        except requests.RequestException as exc:
            LOGGER.error("Discord webhook failed: %s", exc)
            return False

    def send_test(self) -> bool:
        return self.send_embed(
            title="✅ Free Swing Paper Bot Connected",
            description="Discord is connected. Future messages will be paper setup, entry, target, stop, and position-summary alerts.",
            color=0x2ECC71,
        )

    def setup_alert(self, ticker: str, result: dict[str, Any], crossing: str) -> bool:
        plan = result.get("trade_plan") or {}
        factors = result.get("factors", {})
        score = int(result["score"])
        tier_label = "A-SETUP" if crossing == "A" else "B-SETUP WATCHLIST"
        color = 0x2ECC71 if crossing == "A" else 0xF1C40F
        plan_text = "Watchlist only; no paper position will be opened yet."
        if crossing == "A" and plan:
            plan_text = (
                f"Entry zone: **${plan['entry_low']:.2f}–${plan['entry_high']:.2f}**\n"
                f"Stop: **${plan['stop']:.2f}**\n"
                f"TP1: **${plan['tp1']:.2f}** | TP2: **${plan['tp2']:.2f}**"
            )
        fields = [
            {
                "name": "Score breakdown",
                "value": (
                    f"Volume {factors.get('relative_volume', 0)}/20 • "
                    f"Break {factors.get('price_structure', 0)}/20 • "
                    f"VWAP {factors.get('vwap', 0)}/20 • "
                    f"Momentum {factors.get('momentum', 0)}/20 • "
                    f"Room {factors.get('clean_structure', 0)}/20"
                ),
                "inline": False,
            },
            {"name": "Paper plan", "value": plan_text, "inline": False},
        ]
        return self.send_embed(
            title=f"📡 {ticker} • {tier_label} • {score}/100",
            description="The score freshly crossed into this tier on a closed 15-minute candle.",
            color=color,
            fields=fields,
        )

    def entry_alert(self, position: dict[str, Any]) -> bool:
        return self.send_embed(
            title=f"🟢 PAPER POSITION OPENED • {position['ticker']}",
            description=(
                f"The entry zone was reached. This is a simulated position only.\n\n"
                f"Entry: **${position['entry_price']:.2f}**\n"
                f"Size: **{position['quantity']} shares** (${position['initial_notional']:.2f} simulated)\n"
                f"Maximum planned risk: **${position['initial_risk_dollars']:.2f}**\n"
                f"Current P/L: **0.00%**"
            ),
            color=0x2ECC71,
            fields=[
                {
                    "name": "Levels",
                    "value": (
                        f"Stop ${position['stop']:.2f} • TP1 ${position['tp1']:.2f} • "
                        f"TP2 ${position['tp2']:.2f}"
                    ),
                    "inline": False,
                }
            ],
        )

    def position_event_alert(self, title: str, position: dict[str, Any], message: str, color: int) -> bool:
        return self.send_embed(
            title=f"{title} • {position['ticker']}",
            description=message,
            color=color,
            fields=[
                {
                    "name": "Position",
                    "value": (
                        f"Remaining: {position['remaining_quantity']} / {position['quantity']} shares\n"
                        f"Realized P/L: ${position['realized_pnl']:.2f}\n"
                        f"Total return at this event: {position.get('last_return_percent', 0.0):+.2f}%\n"
                        f"Total R at this event: {position.get('last_r_multiple', 0.0):+.2f}R"
                    ),
                    "inline": False,
                }
            ],
        )

    def daily_summary(self, positions: list[dict[str, Any]]) -> bool:
        lines: list[str] = []
        for position in positions:
            lines.append(
                f"**{position['ticker']}** — {position.get('last_return_percent', 0.0):+.2f}% "
                f"({position.get('last_r_multiple', 0.0):+.2f}R), "
                f"{position['remaining_quantity']} shares left"
            )
        return self.send_embed(
            title="📋 End-of-Day Paper Position Summary",
            description="\n".join(lines) if lines else "No open paper positions.",
            color=0x3498DB,
        )
