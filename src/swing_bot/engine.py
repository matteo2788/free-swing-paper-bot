from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

import pandas as pd

from .alerts import DiscordAlerter
from .config import AppConfig
from .data_provider import YahooDataProvider
from .market import MarketClock
from .paper import PaperTradingEngine
from .storage import Storage
from .strategy import closed_intraday_bars, evaluate_daily_context, score_trigger, tier_crossings
from .universe import build_universe

LOGGER = logging.getLogger(__name__)


class SwingBotEngine:
    def __init__(self, config: AppConfig, *, dry_run: bool = False) -> None:
        self.config = config
        system = config.section("system")
        storage_config = config.section("storage")
        self.context_config = config.section("context")
        self.scoring_config = config.section("scoring")
        self.paper_config = config.section("paper_trading")
        self.alert_config = config.section("alerts")
        self.universe_config = config.section("universe")

        self.clock = MarketClock(
            calendar_name=str(system.get("market_calendar", "NYSE")),
            timezone=str(system.get("timezone", "America/New_York")),
        )
        self.interval_minutes = int(str(system.get("trigger_interval", "15m")).replace("m", ""))
        self.daily_period = str(system.get("data_period_daily", "1y"))
        self.intraday_period = str(system.get("data_period_intraday", "60d"))
        self.provider = YahooDataProvider(
            batch_size=int(system.get("request_batch_size", 60)),
            pause_seconds=float(system.get("request_pause_seconds", 1.0)),
        )
        self.storage = Storage(
            runtime_path=config.path(str(storage_config["runtime_state"])),
            trades_path=config.path(str(storage_config["trades_csv"])),
            events_path=config.path(str(storage_config["events_jsonl"])),
            marker_path=config.path(str(storage_config["commit_marker"])),
        )
        self.alerter = DiscordAlerter(self.alert_config, dry_run=dry_run)
        self.paper = PaperTradingEngine(self.paper_config, self.clock, self.storage, self.alerter)
        self.state = self.storage.load_state()

    def send_test(self) -> bool:
        result = self.alerter.send_test()
        if result:
            self.storage.log_event("discord_test_sent", {"configured": self.alerter.configured})
            self.storage.save_state(self.state, important=True)
        return result

    def refresh_daily_pool(self, now: datetime | None = None) -> int:
        current = now or self.clock.now()
        LOGGER.info("Refreshing daily context pool")
        universe = build_universe(self.universe_config, self.config.root)
        if "SPY" not in universe:
            universe.append("SPY")
        frames = self.provider.download(universe, period=self.daily_period, interval="1d")
        spy = frames.get("SPY")
        if spy is None or spy.empty:
            raise RuntimeError("SPY daily data is required for relative-strength calculations")

        eligible: dict[str, Any] = {}
        evaluated = 0
        for ticker in universe:
            frame = frames.get(ticker)
            if frame is None or frame.empty:
                continue
            evaluated += 1
            result = evaluate_daily_context(ticker, frame, spy, self.context_config)
            if result.passed:
                eligible[ticker] = result.details

        self.state["daily_pool"] = eligible
        self.state["last_daily_refresh"] = current.date().isoformat()
        self.storage.log_event(
            "daily_pool_refreshed",
            {"evaluated": evaluated, "eligible": len(eligible), "date": current.date().isoformat()},
        )
        self.storage.save_state(self.state, important=True)
        LOGGER.info("Daily context: %s of %s symbols eligible", len(eligible), evaluated)
        return len(eligible)

    def ensure_daily_pool(self, now: datetime | None = None) -> None:
        current = now or self.clock.now()
        last_refresh = self.state.get("last_daily_refresh")
        pool = self.state.get("daily_pool", {})
        if not pool or last_refresh is None:
            self.refresh_daily_pool(current)
            return
        try:
            refresh_date = datetime.fromisoformat(str(last_refresh)).date()
        except ValueError:
            self.refresh_daily_pool(current)
            return
        if refresh_date < current.date() - timedelta(days=3):
            self.refresh_daily_pool(current)

    def scan(self, now: datetime | None = None) -> dict[str, int]:
        current = now or self.clock.now()
        self.ensure_daily_pool(current)
        cutoff = self.clock.latest_closed_bar_cutoff(self.interval_minutes, current)
        if cutoff is None:
            LOGGER.info("No closed trigger candle is available yet")
            return {"scanned": 0, "a_alerts": 0, "b_alerts": 0, "positions": len(self.state["positions"])}
        signal_bar_start = cutoff - timedelta(minutes=self.interval_minutes)
        scan_key = signal_bar_start.isoformat()
        if self.state.get("last_scan_bar") == scan_key:
            LOGGER.info("Closed bar %s was already processed", scan_key)
            return {"scanned": 0, "a_alerts": 0, "b_alerts": 0, "positions": len(self.state["positions"])}

        scan_tickers = set(self.state.get("daily_pool", {}).keys())
        scan_tickers.update(item["ticker"] for item in self.state.get("pending", {}).values())
        scan_tickers.update(item["ticker"] for item in self.state.get("positions", {}).values())
        if not scan_tickers:
            LOGGER.warning("Daily pool is empty")
            return {"scanned": 0, "a_alerts": 0, "b_alerts": 0, "positions": len(self.state["positions"])}

        raw_frames = self.provider.download(
            sorted(scan_tickers),
            period=self.intraday_period,
            interval=f"{self.interval_minutes}m",
        )
        frames: dict[str, pd.DataFrame] = {}
        for ticker, frame in raw_frames.items():
            closed = closed_intraday_bars(frame, current, self.interval_minutes)
            if not closed.empty:
                frames[ticker] = closed

        important_change = self.paper.process(self.state, frames)
        a_alerts = 0
        b_alerts = 0
        scanned = 0
        b_threshold = int(self.scoring_config["b_setup_threshold"])
        a_threshold = int(self.scoring_config["a_setup_threshold"])

        for ticker in sorted(self.state.get("daily_pool", {}).keys()):
            frame = frames.get(ticker)
            if frame is None or len(frame) < 220:
                continue
            scanned += 1
            score_result = score_trigger(
                frame,
                self.context_config,
                self.scoring_config,
                self.paper_config,
            )
            previous_record = self.state["scores"].get(ticker, {})
            previous_score = previous_record.get("score")
            if score_result is None:
                self.state["scores"][ticker] = {
                    "score": 0,
                    "tier": None,
                    "updated_at": scan_key,
                }
                continue

            result_dict = asdict(score_result)
            crossings = tier_crossings(previous_score, score_result.score, b_threshold, a_threshold)
            # A direct jump into A sends one strong alert, not an A and B double-message.
            if "A" in crossings:
                if bool(self.alert_config.get("send_a_setups", True)):
                    self.alerter.setup_alert(ticker, result_dict, "A")
                    self.storage.log_event("a_setup_alert", {"ticker": ticker, **result_dict})
                    a_alerts += 1
                    important_change = True
                if self.paper.create_pending(self.state, ticker, result_dict):
                    important_change = True
            elif "B" in crossings and bool(self.alert_config.get("send_b_setups", True)):
                self.alerter.setup_alert(ticker, result_dict, "B")
                self.storage.log_event("b_setup_alert", {"ticker": ticker, **result_dict})
                b_alerts += 1
                important_change = True

            self.state["scores"][ticker] = {
                "score": score_result.score,
                "tier": score_result.tier,
                "updated_at": score_result.trigger_time,
                "factors": score_result.factors,
            }

        self.paper.update_mark_to_market(self.state, frames)
        self.state["last_scan_bar"] = scan_key
        self.storage.save_state(self.state, important=important_change)
        LOGGER.info(
            "Scan complete: %s scored, %s A alerts, %s B alerts, %s open positions",
            scanned,
            a_alerts,
            b_alerts,
            len(self.state["positions"]),
        )
        return {
            "scanned": scanned,
            "a_alerts": a_alerts,
            "b_alerts": b_alerts,
            "positions": len(self.state["positions"]),
        }

    def after_close(self, now: datetime | None = None) -> None:
        current = now or self.clock.now()
        if self.state.get("last_daily_refresh") != current.date().isoformat():
            self.refresh_daily_pool(current)

        if not bool(self.alert_config.get("send_daily_open_position_summary", True)):
            return
        if self.state.get("last_summary_date") == current.date().isoformat():
            return
        positions = list(self.state.get("positions", {}).values())
        if positions:
            tickers = sorted({position["ticker"] for position in positions})
            raw = self.provider.download(tickers, period="5d", interval=f"{self.interval_minutes}m")
            frames = {
                ticker: closed_intraday_bars(frame, current, self.interval_minutes)
                for ticker, frame in raw.items()
            }
            self.paper.update_mark_to_market(self.state, frames)
            self.alerter.daily_summary(list(self.state["positions"].values()))
            self.storage.log_event(
                "daily_position_summary",
                {"date": current.date().isoformat(), "positions": list(self.state["positions"].values())},
            )
        self.state["last_summary_date"] = current.date().isoformat()
        self.storage.save_state(self.state, important=True)

    def auto(self, now: datetime | None = None) -> dict[str, Any]:
        current = now or self.clock.now()
        LOGGER.info("Bot time: %s", current.isoformat())
        if self.clock.is_open(current):
            return {"mode": "scan", **self.scan(current)}
        if self.clock.is_after_close_window(current):
            self.after_close(current)
            return {"mode": "after_close", "positions": len(self.state["positions"])}
        LOGGER.info("Outside the market scan or after-close windows; no action needed")
        return {"mode": "idle"}

    def status(self) -> dict[str, Any]:
        return {
            "last_daily_refresh": self.state.get("last_daily_refresh"),
            "eligible_tickers": len(self.state.get("daily_pool", {})),
            "pending_entries": len(self.state.get("pending", {})),
            "open_positions": len(self.state.get("positions", {})),
            "last_scan_bar": self.state.get("last_scan_bar"),
            "discord_configured": self.alerter.configured,
        }

    def print_status(self) -> None:
        print(json.dumps(self.status(), indent=2))
