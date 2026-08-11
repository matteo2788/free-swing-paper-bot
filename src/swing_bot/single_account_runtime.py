from __future__ import annotations

import math
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from .config import AppConfig
from .engine import SwingBotEngine
from .responsive_runtime import (
    ResponsiveDiscordAlerter,
    ResponsivePaperTradingEngine,
    _parse_minutes,
    monitor_paper,
)
from .strategy import closed_intraday_bars, score_trigger, tier_crossings

MODEL_VERSION = 2


class SingleAccountPaperTradingEngine(ResponsivePaperTradingEngine):
    """Paper engine for one rolling all-in simulated account."""

    def __init__(self, config: dict[str, Any], clock: Any, storage: Any, alerter: Any) -> None:
        super().__init__(config, clock, storage, alerter)
        self._active_state: dict[str, Any] | None = None

    def ensure_account_state(self, state: dict[str, Any]) -> bool:
        starting_value = round(float(self.config.get("starting_account_value", 10000.0)), 2)
        current_version = int(state.get("paper_account_model_version") or 0)
        current_value = state.get("paper_account_value")

        if current_version == MODEL_VERSION and isinstance(current_value, (int, float)):
            return False

        old_pending = [
            {"trade_id": item.get("trade_id"), "ticker": item.get("ticker"), "score": item.get("score")}
            for item in state.get("pending", {}).values()
        ]
        old_positions = [
            {
                "trade_id": item.get("trade_id"),
                "ticker": item.get("ticker"),
                "score": item.get("score"),
                "last_return_percent": item.get("last_return_percent"),
            }
            for item in state.get("positions", {}).values()
        ]

        state["pending"] = {}
        state["positions"] = {}
        state["paper_account_model_version"] = MODEL_VERSION
        state["paper_account_starting_value"] = starting_value
        state["paper_account_value"] = starting_value
        state["paper_account_reset_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.storage.log_event(
            "paper_account_reset",
            {
                "model_version": MODEL_VERSION,
                "starting_account_value": starting_value,
                "cleared_pending": old_pending,
                "cleared_positions": old_positions,
                "reason": "migrate_to_single_all_in_account",
            },
        )
        return True

    def create_pending(
        self,
        state: dict[str, Any],
        ticker: str,
        result: dict[str, Any],
    ) -> bool:
        self.ensure_account_state(state)
        if state.get("pending") or state.get("positions"):
            return False
        return super().create_pending(state, ticker, result)

    def process(
        self,
        state: dict[str, Any],
        intraday_frames: dict[str, pd.DataFrame],
    ) -> bool:
        migrated = self.ensure_account_state(state)
        self._active_state = state
        try:
            changed = super().process(state, intraday_frames)
        finally:
            self._active_state = None
        return migrated or changed

    def _open_position(
        self,
        pending: dict[str, Any],
        fill_price: float,
        timestamp: pd.Timestamp,
    ) -> dict[str, Any] | None:
        risk_per_share = fill_price - float(pending["stop"])
        if risk_per_share <= 0:
            return None

        state = self._active_state or {}
        account_value = float(
            state.get("paper_account_value", self.config.get("starting_account_value", 10000.0))
        )
        if account_value <= 0:
            return None

        allocation_percent = float(self.config.get("max_position_notional_percent", 100.0))
        max_notional = account_value * allocation_percent / 100.0
        quantity = math.floor(max_notional / fill_price)
        if quantity < 1:
            return None

        initial_notional = fill_price * quantity
        initial_risk_dollars = risk_per_share * quantity
        target_1_quantity = max(
            1,
            math.floor(quantity * float(self.config["target_1_exit_fraction"])),
        )
        target_1_quantity = min(target_1_quantity, quantity)
        remaining_after_tp1 = quantity - target_1_quantity
        target_2_quantity = math.floor(
            quantity * float(self.config["target_2_exit_fraction"])
        )
        if remaining_after_tp1 > 0:
            target_2_quantity = max(1, target_2_quantity)
        target_2_quantity = min(max(0, target_2_quantity), remaining_after_tp1)

        return {
            "trade_id": pending["trade_id"],
            "ticker": pending["ticker"],
            "tier": pending["tier"],
            "score": pending["score"],
            "signal_time": pending["signal_time"],
            "entry_time": timestamp.isoformat(),
            "entry_price": round(fill_price, 4),
            "initial_stop": round(float(pending["stop"]), 4),
            "stop": round(float(pending["stop"]), 4),
            "tp1": round(
                fill_price + risk_per_share * float(self.config["target_1_r"]),
                4,
            ),
            "tp2": round(
                fill_price + risk_per_share * float(self.config["target_2_r"]),
                4,
            ),
            "quantity": int(quantity),
            "remaining_quantity": int(quantity),
            "target_1_quantity": int(target_1_quantity),
            "target_2_quantity": int(target_2_quantity),
            "initial_notional": round(initial_notional, 2),
            "initial_risk_dollars": round(initial_risk_dollars, 2),
            "account_value_at_entry": round(account_value, 2),
            "cash_unused": round(max(0.0, account_value - initial_notional), 2),
            "allocation_percent": round((initial_notional / account_value) * 100.0, 3),
            "sizing_mode": "ALL_IN",
            "realized_pnl": 0.0,
            "stage": 0,
            "last_processed_bar": timestamp.isoformat(),
            "last_price": round(fill_price, 4),
            "last_return_percent": 0.0,
            "last_r_multiple": 0.0,
            "exits": [],
        }

    def _finish_trade(
        self,
        state: dict[str, Any],
        trade_id: str,
        timestamp: pd.Timestamp,
        outcome: str,
    ) -> None:
        position = state["positions"][trade_id]
        realized_pnl = round(float(position["realized_pnl"]), 2)
        account_before = round(
            float(
                state.get(
                    "paper_account_value",
                    self.config.get("starting_account_value", 10000.0),
                )
            ),
            2,
        )

        super()._finish_trade(state, trade_id, timestamp, outcome)

        account_after = round(account_before + realized_pnl, 2)
        state["paper_account_value"] = account_after
        self.storage.log_event(
            "paper_account_updated",
            {
                "trade_id": trade_id,
                "ticker": position["ticker"],
                "account_before": account_before,
                "realized_pnl": realized_pnl,
                "account_after": account_after,
                "outcome": outcome,
            },
        )


class SingleAccountEngine(SwingBotEngine):
    """Scanner that ranks all current A setups and chooses only the best one."""

    def ensure_single_account_model(self) -> bool:
        ensure = getattr(self.paper, "ensure_account_state", None)
        if not callable(ensure):
            return False
        changed = bool(ensure(self.state))
        if changed:
            self.storage.save_state(self.state, important=True)
        return changed

    @staticmethod
    def _candidate_key(candidate: tuple[str, dict[str, Any]]) -> tuple[int, int, int, int, int, int, str]:
        ticker, result = candidate
        factors = result.get("factors", {})
        return (
            int(result.get("score", 0)),
            int(factors.get("price_structure", 0)),
            int(factors.get("relative_volume", 0)),
            int(factors.get("clean_structure", 0)),
            int(factors.get("momentum", 0)),
            int(factors.get("vwap", 0)),
            ticker,
        )

    def scan(self, now: datetime | None = None) -> dict[str, int]:
        current = now or self.clock.now()
        self.ensure_single_account_model()
        self.ensure_daily_pool(current)

        cutoff = self.clock.latest_closed_bar_cutoff(self.interval_minutes, current)
        if cutoff is None:
            return {
                "scanned": 0,
                "a_alerts": 0,
                "b_alerts": 0,
                "positions": len(self.state["positions"]),
            }

        signal_bar_start = cutoff - timedelta(minutes=self.interval_minutes)
        scan_key = signal_bar_start.isoformat()
        if self.state.get("last_scan_bar") == scan_key:
            return {
                "scanned": 0,
                "a_alerts": 0,
                "b_alerts": 0,
                "positions": len(self.state["positions"]),
            }

        scan_tickers = set(self.state.get("daily_pool", {}).keys())
        scan_tickers.update(item["ticker"] for item in self.state.get("pending", {}).values())
        scan_tickers.update(item["ticker"] for item in self.state.get("positions", {}).values())
        if not scan_tickers:
            return {
                "scanned": 0,
                "a_alerts": 0,
                "b_alerts": 0,
                "positions": len(self.state["positions"]),
            }

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
        a_candidates: list[tuple[str, dict[str, Any]]] = []

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
            crossings = tier_crossings(
                previous_score,
                score_result.score,
                b_threshold,
                a_threshold,
            )

            if (
                int(score_result.score) >= a_threshold
                and result_dict.get("trade_plan")
            ):
                a_candidates.append((ticker, result_dict))
            elif "B" in crossings and bool(
                self.alert_config.get("send_b_setups", True)
            ):
                self.alerter.setup_alert(ticker, result_dict, "B")
                self.storage.log_event(
                    "b_setup_alert",
                    {"ticker": ticker, **result_dict},
                )
                b_alerts += 1
                important_change = True

            self.state["scores"][ticker] = {
                "score": score_result.score,
                "tier": score_result.tier,
                "updated_at": score_result.trigger_time,
                "factors": score_result.factors,
            }

        account_busy = bool(self.state.get("pending")) or bool(self.state.get("positions"))
        if a_candidates and not account_busy:
            best_ticker, best_result = max(a_candidates, key=self._candidate_key)
            created = self.paper.create_pending(
                self.state,
                best_ticker,
                best_result,
            )
            if created:
                ranked = sorted(
                    a_candidates,
                    key=self._candidate_key,
                    reverse=True,
                )
                if bool(self.alert_config.get("send_a_setups", True)):
                    self.alerter.setup_alert(best_ticker, best_result, "A")
                    a_alerts = 1
                self.storage.log_event(
                    "best_a_selected",
                    {
                        "ticker": best_ticker,
                        "score": int(best_result["score"]),
                        "candidate_count": len(a_candidates),
                        "top_candidates": [
                            {
                                "ticker": ticker,
                                "score": int(result["score"]),
                                "factors": result.get("factors", {}),
                            }
                            for ticker, result in ranked[:5]
                        ],
                        "paper_account_value": float(
                            self.state.get(
                                "paper_account_value",
                                self.paper_config.get(
                                    "starting_account_value",
                                    10000.0,
                                ),
                            )
                        ),
                    },
                )
                important_change = True

        self.paper.update_mark_to_market(self.state, frames)
        self.state["last_scan_bar"] = scan_key
        self.storage.save_state(self.state, important=important_change)

        return {
            "scanned": scanned,
            "a_alerts": a_alerts,
            "b_alerts": b_alerts,
            "positions": len(self.state["positions"]),
        }

    def status(self) -> dict[str, Any]:
        result = super().status()
        result["paper_account_value"] = round(
            float(
                self.state.get(
                    "paper_account_value",
                    self.paper_config.get("starting_account_value", 10000.0),
                )
            ),
            2,
        )
        result["single_position_mode"] = True
        result["position_sizing"] = "ALL_IN"
        return result


def run_single_account_auto(
    config: AppConfig,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    engine = SingleAccountEngine(config, dry_run=dry_run)
    monitor_minutes = _parse_minutes(
        engine.paper_config.get("monitor_interval", "5m"),
        5,
    )

    responsive_alerter = ResponsiveDiscordAlerter(
        engine.alert_config,
        dry_run=dry_run,
        monitor_minutes=monitor_minutes,
    )
    engine.alerter = responsive_alerter
    engine.paper = SingleAccountPaperTradingEngine(
        engine.paper_config,
        engine.clock,
        engine.storage,
        responsive_alerter,
    )
    engine.ensure_single_account_model()

    current = engine.clock.now()

    if engine.clock.is_open(current):
        monitor_result = monitor_paper(engine, current)
        scan_result = engine.scan(current)
        return {
            "mode": "scan",
            "paper_monitor": monitor_result,
            "paper_account_value": round(
                float(engine.state.get("paper_account_value", 10000.0)),
                2,
            ),
            **scan_result,
        }

    if engine.clock.is_after_close_window(current):
        engine.after_close(current)
        return {
            "mode": "after_close",
            "positions": len(engine.state.get("positions", {})),
            "paper_account_value": round(
                float(engine.state.get("paper_account_value", 10000.0)),
                2,
            ),
        }

    return {
        "mode": "idle",
        "paper_account_value": round(
            float(engine.state.get("paper_account_value", 10000.0)),
            2,
        ),
    }
