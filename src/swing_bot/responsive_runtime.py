from __future__ import annotations

import copy
import logging
from collections import defaultdict
from datetime import datetime
from typing import Any

import pandas as pd

from .alerts import DiscordAlerter
from .config import AppConfig
from .engine import SwingBotEngine
from .paper import PaperTradingEngine
from .strategy import closed_intraday_bars

LOGGER = logging.getLogger(__name__)


class _BufferedPaperAlerter:
    """Capture paper-trade alerts so one delayed replay cannot spam Discord."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def entry_alert(self, position: dict[str, Any]) -> bool:
        self.events.append(
            {
                "kind": "entry",
                "position": copy.deepcopy(position),
            }
        )
        return True

    def position_event_alert(
        self,
        title: str,
        position: dict[str, Any],
        message: str,
        color: int,
    ) -> bool:
        self.events.append(
            {
                "kind": "position",
                "title": title,
                "position": copy.deepcopy(position),
                "message": message,
                "color": color,
            }
        )
        return True


class ResponsiveDiscordAlerter(DiscordAlerter):
    """Discord alerts with explicit event timing and delayed-replay collapse."""

    def __init__(
        self,
        alert_config: dict[str, Any],
        *,
        dry_run: bool = False,
        monitor_minutes: int = 5,
    ) -> None:
        super().__init__(alert_config, dry_run=dry_run)
        self.monitor_minutes = max(1, int(monitor_minutes))

    @staticmethod
    def _as_eastern(value: Any) -> pd.Timestamp | None:
        if value in (None, ""):
            return None
        try:
            stamp = pd.Timestamp(value)
        except (TypeError, ValueError):
            return None
        if stamp.tzinfo is None:
            return stamp.tz_localize("America/New_York")
        return stamp.tz_convert("America/New_York")

    @classmethod
    def _format_eastern(cls, value: Any) -> str:
        stamp = cls._as_eastern(value)
        if stamp is None:
            return "Unknown"
        return stamp.strftime("%b %d, %I:%M %p ET").replace(" 0", " ")

    def _timing_field(self, event_time: Any) -> str:
        event = self._as_eastern(event_time)
        now = pd.Timestamp.now(tz="America/New_York")
        sent = now.strftime("%b %d, %I:%M:%S %p ET").replace(" 0", " ")
        if event is None:
            return f"Event time: Unknown\nDiscord sent: {sent}"

        event_text = event.strftime("%b %d, %I:%M %p ET").replace(" 0", " ")
        expected_close = event + pd.Timedelta(minutes=self.monitor_minutes)
        delay_minutes = max(0.0, (now - expected_close).total_seconds() / 60.0)
        return (
            f"Event monitor bar: {event_text}\n"
            f"Discord sent: {sent}\n"
            f"Delivery lag after that bar closed: ~{delay_minutes:.1f} min"
        )

    def entry_alert(self, position: dict[str, Any]) -> bool:
        return self.send_embed(
            title=f"🟢 PAPER POSITION OPENED • {position['ticker']}",
            description=(
                "The entry zone was reached. This is a simulated position only.\n\n"
                f"Entry: **${position['entry_price']:.2f}**\n"
                f"Size: **{position['quantity']} shares** "
                f"(${position['initial_notional']:.2f} simulated)\n"
                f"Maximum planned risk: **${position['initial_risk_dollars']:.2f}**\n"
                "Current P/L at the simulated fill: **0.00%**"
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
                },
                {
                    "name": "Timing",
                    "value": self._timing_field(position.get("entry_time")),
                    "inline": False,
                },
            ],
            footer=(
                "Paper trading only • Paper fills are checked on fast monitor bars • "
                "The timing field shows any delivery delay"
            ),
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
                },
                {
                    "name": "Timing",
                    "value": self._timing_field(event_time),
                    "inline": False,
                },
            ],
            footer=(
                "Paper trading only • Paper exits are checked on fast monitor bars • "
                "The timing field shows any delivery delay"
            ),
        )

    def paper_catchup_alert(self, events: list[dict[str, Any]]) -> bool:
        if not events:
            return True

        first_position = events[0]["position"]
        last_position = events[-1]["position"]
        ticker = str(first_position.get("ticker", "UNKNOWN"))
        lines: list[str] = []

        for event in events:
            position = event["position"]
            if event["kind"] == "entry":
                event_time = position.get("entry_time")
                label = f"ENTRY @ ${float(position['entry_price']):.2f}"
            else:
                exits = position.get("exits") or []
                latest_exit = exits[-1] if exits else {}
                event_time = latest_exit.get("time") or position.get("last_processed_bar")
                clean_title = str(event.get("title", "PAPER EVENT")).replace("•", "-")
                exit_price = latest_exit.get("price")
                label = clean_title
                if exit_price is not None:
                    label += f" @ ${float(exit_price):.2f}"

            lines.append(f"• **{self._format_eastern(event_time)}** — {label}")

        return self.send_embed(
            title=f"⚠️ DELAYED PAPER CATCH-UP • {ticker}",
            description=(
                "More than one paper-trade event was recovered in the same monitor pass. "
                "Instead of sending a fake burst of separate 'live' notifications, the bot "
                "collapsed them into this single catch-up message."
            ),
            color=0xE67E22,
            fields=[
                {
                    "name": "Recovered timeline",
                    "value": "\n".join(lines)[:1024],
                    "inline": False,
                },
                {
                    "name": "Final recovered state",
                    "value": (
                        f"Remaining: {last_position.get('remaining_quantity', 0)} / "
                        f"{last_position.get('quantity', 0)} shares\n"
                        f"Realized P/L: ${float(last_position.get('realized_pnl', 0.0)):.2f}\n"
                        f"Return: {float(last_position.get('last_return_percent', 0.0)):+.2f}%\n"
                        f"R multiple: {float(last_position.get('last_r_multiple', 0.0)):+.2f}R"
                    ),
                    "inline": False,
                },
            ],
            footer=(
                "Paper trading only • Catch-up means the scheduler/data arrived late • "
                "Do not treat this message as a live entry or exit"
            ),
        )


class ResponsivePaperTradingEngine(PaperTradingEngine):
    """Run the normal paper engine but collapse multi-event replays per trade."""

    def process(
        self,
        state: dict[str, Any],
        intraday_frames: dict[str, pd.DataFrame],
    ) -> bool:
        real_alerter = self.alerter
        buffer = _BufferedPaperAlerter()
        self.alerter = buffer  # type: ignore[assignment]
        try:
            important_change = super().process(state, intraday_frames)
        finally:
            self.alerter = real_alerter

        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        order: list[str] = []
        for event in buffer.events:
            trade_id = str(event["position"].get("trade_id", "unknown"))
            if trade_id not in grouped:
                order.append(trade_id)
            grouped[trade_id].append(event)

        for trade_id in order:
            events = grouped[trade_id]
            if len(events) == 1:
                event = events[0]
                if event["kind"] == "entry":
                    real_alerter.entry_alert(event["position"])
                else:
                    real_alerter.position_event_alert(
                        event["title"],
                        event["position"],
                        event["message"],
                        int(event["color"]),
                    )
                continue

            catchup = getattr(real_alerter, "paper_catchup_alert", None)
            if callable(catchup):
                catchup(events)
            else:
                # Safe fallback: send only the final recovered event, never a burst.
                final_event = events[-1]
                if final_event["kind"] == "entry":
                    real_alerter.entry_alert(final_event["position"])
                else:
                    real_alerter.position_event_alert(
                        final_event["title"],
                        final_event["position"],
                        final_event["message"],
                        int(final_event["color"]),
                    )

        return important_change


def _parse_minutes(value: Any, default: int = 5) -> int:
    text = str(value or f"{default}m").strip().lower()
    if text.endswith("m"):
        text = text[:-1]
    try:
        return max(1, int(text))
    except ValueError:
        return default


def monitor_paper(
    engine: SwingBotEngine,
    current: datetime,
) -> dict[str, int]:
    tickers = {
        str(item["ticker"])
        for bucket in ("pending", "positions")
        for item in engine.state.get(bucket, {}).values()
        if item.get("ticker")
    }
    engine.state["last_paper_monitor_at"] = current.isoformat()

    if not tickers:
        engine.storage.save_state(engine.state, important=False)
        return {
            "monitored": 0,
            "pending": len(engine.state.get("pending", {})),
            "positions": len(engine.state.get("positions", {})),
        }

    interval_minutes = _parse_minutes(engine.paper_config.get("monitor_interval", "5m"), 5)
    period = str(engine.paper_config.get("monitor_data_period", "5d"))
    raw_frames = engine.provider.download(
        sorted(tickers),
        period=period,
        interval=f"{interval_minutes}m",
    )

    frames: dict[str, pd.DataFrame] = {}
    for ticker, frame in raw_frames.items():
        closed = closed_intraday_bars(frame, current, interval_minutes)
        if not closed.empty:
            frames[ticker] = closed

    important_change = engine.paper.process(engine.state, frames)
    engine.paper.update_mark_to_market(engine.state, frames)
    engine.storage.save_state(engine.state, important=important_change)

    LOGGER.info(
        "Fast paper monitor: %s ticker(s), %s pending, %s open positions",
        len(frames),
        len(engine.state.get("pending", {})),
        len(engine.state.get("positions", {})),
    )
    return {
        "monitored": len(frames),
        "pending": len(engine.state.get("pending", {})),
        "positions": len(engine.state.get("positions", {})),
    }


def run_responsive_auto(config: AppConfig, *, dry_run: bool = False) -> dict[str, Any]:
    engine = SwingBotEngine(config, dry_run=dry_run)
    monitor_minutes = _parse_minutes(engine.paper_config.get("monitor_interval", "5m"), 5)

    responsive_alerter = ResponsiveDiscordAlerter(
        engine.alert_config,
        dry_run=dry_run,
        monitor_minutes=monitor_minutes,
    )
    engine.alerter = responsive_alerter
    engine.paper = ResponsivePaperTradingEngine(
        engine.paper_config,
        engine.clock,
        engine.storage,
        responsive_alerter,
    )

    current = engine.clock.now()
    LOGGER.info("Responsive bot time: %s", current.isoformat())

    if engine.clock.is_open(current):
        monitor_result = monitor_paper(engine, current)
        scan_result = engine.scan(current)
        return {
            "mode": "scan",
            "paper_monitor": monitor_result,
            **scan_result,
        }

    if engine.clock.is_after_close_window(current):
        engine.after_close(current)
        return {
            "mode": "after_close",
            "positions": len(engine.state.get("positions", {})),
        }

    LOGGER.info("Outside the market scan or after-close windows; no action needed")
    return {"mode": "idle"}
