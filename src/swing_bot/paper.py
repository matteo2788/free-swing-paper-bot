from __future__ import annotations

import math
import uuid
from datetime import datetime
from typing import Any

import pandas as pd

from .alerts import DiscordAlerter
from .indicators import ema
from .market import MarketClock
from .storage import Storage
from .strategy import regular_session


class PaperTradingEngine:
    def __init__(
        self,
        config: dict[str, Any],
        clock: MarketClock,
        storage: Storage,
        alerter: DiscordAlerter,
    ) -> None:
        self.config = config
        self.clock = clock
        self.storage = storage
        self.alerter = alerter

    @staticmethod
    def make_trade_id(ticker: str, signal_time: str) -> str:
        stamp = pd.Timestamp(signal_time).strftime("%Y%m%d-%H%M")
        return f"{ticker}-{stamp}-{uuid.uuid4().hex[:6]}"

    def create_pending(
        self,
        state: dict[str, Any],
        ticker: str,
        result: dict[str, Any],
    ) -> bool:
        if not bool(self.config.get("enabled", True)):
            return False
        if int(result["score"]) < int(self.config["minimum_trade_score"]):
            return False
        if not result.get("trade_plan"):
            return False
        if any(value.get("ticker") == ticker for value in state["pending"].values()):
            return False
        if any(value.get("ticker") == ticker for value in state["positions"].values()):
            return False

        trade_id = self.make_trade_id(ticker, result["trigger_time"])
        plan = result["trade_plan"]
        state["pending"][trade_id] = {
            "trade_id": trade_id,
            "ticker": ticker,
            "tier": "A",
            "score": int(result["score"]),
            "signal_time": result["trigger_time"],
            "entry_low": float(plan["entry_low"]),
            "entry_high": float(plan["entry_high"]),
            "stop": float(plan["stop"]),
            "tp1": float(plan["tp1"]),
            "tp2": float(plan["tp2"]),
            "last_processed_bar": result["trigger_time"],
        }
        self.storage.log_event("pending_created", state["pending"][trade_id])
        return True

    def process(
        self,
        state: dict[str, Any],
        intraday_frames: dict[str, pd.DataFrame],
    ) -> bool:
        important_change = False
        important_change |= self._process_pending(state, intraday_frames)
        important_change |= self._process_positions(state, intraday_frames)
        return important_change

    def _process_pending(
        self,
        state: dict[str, Any],
        intraday_frames: dict[str, pd.DataFrame],
    ) -> bool:
        changed = False
        for trade_id, pending in list(state["pending"].items()):
            ticker = pending["ticker"]
            data = regular_session(intraday_frames.get(ticker, pd.DataFrame()))
            if data.empty:
                continue
            signal_time = pd.Timestamp(pending["signal_time"])
            if signal_time.tzinfo is None and data.index.tz is not None:
                signal_time = signal_time.tz_localize(data.index.tz)
            elif signal_time.tzinfo is not None and data.index.tz is not None:
                signal_time = signal_time.tz_convert(data.index.tz)

            latest_time = data.index[-1]
            trading_days = self.clock.trading_days_between(signal_time.to_pydatetime(), latest_time.to_pydatetime())
            if trading_days > int(self.config["pending_expiry_trading_days"]):
                expired = state["pending"].pop(trade_id)
                self.storage.log_event("pending_expired", expired)
                changed = True
                continue

            last_processed = pd.Timestamp(pending["last_processed_bar"])
            if last_processed.tzinfo is None and data.index.tz is not None:
                last_processed = last_processed.tz_localize(data.index.tz)
            elif last_processed.tzinfo is not None and data.index.tz is not None:
                last_processed = last_processed.tz_convert(data.index.tz)
            unseen = data.loc[data.index > last_processed]
            for timestamp, bar in unseen.iterrows():
                pending["last_processed_bar"] = timestamp.isoformat()
                if len(state["positions"]) >= int(self.config["max_open_positions"]):
                    continue
                if not self._bar_touches_zone(bar, pending["entry_low"], pending["entry_high"]):
                    continue
                fill_price = self._fill_price(bar, pending["entry_low"], pending["entry_high"])
                position = self._open_position(pending, fill_price, timestamp)
                if position is None:
                    skipped = state["pending"].pop(trade_id)
                    skipped["reason"] = "position_size_below_one_share"
                    self.storage.log_event("pending_skipped", skipped)
                    changed = True
                    break
                state["positions"][trade_id] = position
                state["pending"].pop(trade_id, None)
                self.storage.log_event("position_opened", position)
                self.alerter.entry_alert(position)
                changed = True
                break
        return changed

    @staticmethod
    def _bar_touches_zone(bar: pd.Series, entry_low: float, entry_high: float) -> bool:
        return float(bar["High"]) >= entry_low and float(bar["Low"]) <= entry_high

    @staticmethod
    def _fill_price(bar: pd.Series, entry_low: float, entry_high: float) -> float:
        bar_open = float(bar["Open"])
        if entry_low <= bar_open <= entry_high:
            return round(bar_open, 4)
        if bar_open > entry_high:
            return round(entry_high, 4)
        return round(entry_low, 4)

    def _open_position(self, pending: dict[str, Any], fill_price: float, timestamp: pd.Timestamp) -> dict[str, Any] | None:
        risk_per_share = fill_price - float(pending["stop"])
        if risk_per_share <= 0:
            return None
        account_value = float(self.config["starting_account_value"])
        risk_budget = account_value * float(self.config["risk_per_trade_percent"]) / 100
        max_notional = account_value * float(self.config["max_position_notional_percent"]) / 100
        quantity_by_risk = math.floor(risk_budget / risk_per_share)
        quantity_by_notional = math.floor(max_notional / fill_price)
        quantity = min(quantity_by_risk, quantity_by_notional)
        if quantity < 1:
            return None

        initial_notional = fill_price * quantity
        initial_risk_dollars = risk_per_share * quantity
        target_1_quantity = max(1, math.floor(quantity * float(self.config["target_1_exit_fraction"])))
        target_1_quantity = min(target_1_quantity, quantity)
        remaining_after_tp1 = quantity - target_1_quantity
        target_2_quantity = math.floor(quantity * float(self.config["target_2_exit_fraction"]))
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
            "tp1": round(fill_price + risk_per_share * float(self.config["target_1_r"]), 4),
            "tp2": round(fill_price + risk_per_share * float(self.config["target_2_r"]), 4),
            "quantity": int(quantity),
            "remaining_quantity": int(quantity),
            "target_1_quantity": int(target_1_quantity),
            "target_2_quantity": int(target_2_quantity),
            "initial_notional": round(initial_notional, 2),
            "initial_risk_dollars": round(initial_risk_dollars, 2),
            "realized_pnl": 0.0,
            "stage": 0,
            "last_processed_bar": timestamp.isoformat(),
            "last_price": round(fill_price, 4),
            "last_return_percent": 0.0,
            "last_r_multiple": 0.0,
            "exits": [],
        }

    def _process_positions(
        self,
        state: dict[str, Any],
        intraday_frames: dict[str, pd.DataFrame],
    ) -> bool:
        changed = False
        for trade_id, position in list(state["positions"].items()):
            ticker = position["ticker"]
            data = regular_session(intraday_frames.get(ticker, pd.DataFrame()))
            if data.empty:
                continue
            last_processed = pd.Timestamp(position["last_processed_bar"])
            if last_processed.tzinfo is None and data.index.tz is not None:
                last_processed = last_processed.tz_localize(data.index.tz)
            elif last_processed.tzinfo is not None and data.index.tz is not None:
                last_processed = last_processed.tz_convert(data.index.tz)
            unseen = data.loc[data.index > last_processed]
            for timestamp, bar in unseen.iterrows():
                if trade_id not in state["positions"]:
                    break
                position = state["positions"][trade_id]
                position["last_processed_bar"] = timestamp.isoformat()
                close = float(bar["Close"])
                position["last_price"] = round(close, 4)
                self._update_metrics(position, close)

                if float(bar["Low"]) <= float(position["stop"]):
                    reason = "STOP LOSS" if int(position["stage"]) == 0 else "BREAKEVEN STOP"
                    self._close_remaining(state, trade_id, float(position["stop"]), timestamp, reason)
                    changed = True
                    break

                if int(position["stage"]) == 0 and float(bar["High"]) >= float(position["tp1"]):
                    quantity = min(int(position["target_1_quantity"]), int(position["remaining_quantity"]))
                    self._realize(position, quantity, float(position["tp1"]), timestamp, "TP1")
                    position["stop"] = float(position["entry_price"])
                    position["stage"] = 1
                    self._update_metrics(position, float(position["tp1"]))
                    self.storage.log_event("tp1_hit", position)
                    self.alerter.position_event_alert(
                        "✅ TP1 HIT",
                        position,
                        "Half of the planned paper position was taken off and the stop moved to breakeven.",
                        0x2ECC71,
                    )
                    changed = True
                    if int(position["remaining_quantity"]) == 0:
                        self._finish_trade(state, trade_id, timestamp, "TP1_FULL_EXIT")
                        break

                if trade_id not in state["positions"]:
                    break
                position = state["positions"][trade_id]
                if int(position["stage"]) == 1 and float(bar["High"]) >= float(position["tp2"]):
                    quantity = min(int(position["target_2_quantity"]), int(position["remaining_quantity"]))
                    if quantity > 0:
                        self._realize(position, quantity, float(position["tp2"]), timestamp, "TP2")
                    position["stage"] = 2
                    self._update_metrics(position, float(position["tp2"]))
                    self.storage.log_event("tp2_hit", position)
                    self.alerter.position_event_alert(
                        "🎯 TP2 HIT",
                        position,
                        "The second target was reached. Any remaining shares are now the runner.",
                        0x27AE60,
                    )
                    changed = True
                    if int(position["remaining_quantity"]) == 0:
                        self._finish_trade(state, trade_id, timestamp, "TP2_FULL_EXIT")
                        break

                if trade_id not in state["positions"]:
                    break
                position = state["positions"][trade_id]
                entry_time = pd.Timestamp(position["entry_time"])
                if entry_time.tzinfo is None and timestamp.tzinfo is not None:
                    entry_time = entry_time.tz_localize(timestamp.tzinfo)
                elif entry_time.tzinfo is not None and timestamp.tzinfo is not None:
                    entry_time = entry_time.tz_convert(timestamp.tzinfo)
                held_days = self.clock.trading_days_between(entry_time.to_pydatetime(), timestamp.to_pydatetime())

                if int(position["stage"]) >= 2 and int(position["remaining_quantity"]) > 0:
                    runner_ema = self._one_hour_ema(data.loc[:timestamp])
                    if runner_ema is not None and close < runner_ema:
                        self._close_remaining(state, trade_id, close, timestamp, "RUNNER EMA EXIT")
                        changed = True
                        break

                if held_days >= int(self.config["time_stop_trading_days"]):
                    self._close_remaining(state, trade_id, close, timestamp, "TIME STOP")
                    changed = True
                    break
        return changed

    @staticmethod
    def _realize(position: dict[str, Any], quantity: int, price: float, timestamp: pd.Timestamp, reason: str) -> None:
        if quantity <= 0:
            return
        pnl = (price - float(position["entry_price"])) * quantity
        position["realized_pnl"] = round(float(position["realized_pnl"]) + pnl, 2)
        position["remaining_quantity"] = int(position["remaining_quantity"]) - quantity
        position["exits"].append(
            {
                "time": timestamp.isoformat(),
                "reason": reason,
                "price": round(price, 4),
                "quantity": int(quantity),
                "pnl": round(pnl, 2),
            }
        )

    def _close_remaining(
        self,
        state: dict[str, Any],
        trade_id: str,
        price: float,
        timestamp: pd.Timestamp,
        reason: str,
    ) -> None:
        position = state["positions"][trade_id]
        quantity = int(position["remaining_quantity"])
        self._realize(position, quantity, price, timestamp, reason)
        self._update_metrics(position, price)
        title = {
            "STOP LOSS": "❌ STOP-LOSS HIT",
            "BREAKEVEN STOP": "🟨 BREAKEVEN STOP HIT",
            "TIME STOP": "⏰ TIME-BASED EXIT",
            "RUNNER EMA EXIT": "🏁 RUNNER EXITED",
        }.get(reason, "🏁 PAPER POSITION CLOSED")
        color = 0xE74C3C if reason == "STOP LOSS" else 0xF39C12 if reason in {"BREAKEVEN STOP", "TIME STOP"} else 0x3498DB
        self.storage.log_event("position_closed", {**position, "exit_reason": reason})
        self.alerter.position_event_alert(
            title,
            position,
            f"The remaining paper position was closed at **${price:.2f}** because of: **{reason}**.",
            color,
        )
        self._finish_trade(state, trade_id, timestamp, reason)

    def _finish_trade(self, state: dict[str, Any], trade_id: str, timestamp: pd.Timestamp, outcome: str) -> None:
        position = state["positions"].pop(trade_id)
        entry_time = pd.Timestamp(position["entry_time"])
        if entry_time.tzinfo is None and timestamp.tzinfo is not None:
            entry_time = entry_time.tz_localize(timestamp.tzinfo)
        elif entry_time.tzinfo is not None and timestamp.tzinfo is not None:
            entry_time = entry_time.tz_convert(timestamp.tzinfo)
        holding_days = self.clock.trading_days_between(entry_time.to_pydatetime(), timestamp.to_pydatetime())
        trade = {
            "trade_id": position["trade_id"],
            "ticker": position["ticker"],
            "tier": position["tier"],
            "score": position["score"],
            "signal_time": position["signal_time"],
            "entry_time": position["entry_time"],
            "exit_time": timestamp.isoformat(),
            "entry_price": position["entry_price"],
            "initial_stop": position["initial_stop"],
            "tp1": position["tp1"],
            "tp2": position["tp2"],
            "quantity": position["quantity"],
            "realized_pnl": round(float(position["realized_pnl"]), 2),
            "return_percent": round(float(position["last_return_percent"]), 3),
            "r_multiple": round(float(position["last_r_multiple"]), 3),
            "outcome": outcome,
            "holding_trading_days": holding_days,
        }
        self.storage.append_trade(trade)

    @staticmethod
    def _update_metrics(position: dict[str, Any], current_price: float) -> None:
        unrealized = (current_price - float(position["entry_price"])) * int(position["remaining_quantity"])
        total_pnl = float(position["realized_pnl"]) + unrealized
        initial_notional = max(float(position["initial_notional"]), 1e-9)
        initial_risk = max(float(position["initial_risk_dollars"]), 1e-9)
        position["last_return_percent"] = round((total_pnl / initial_notional) * 100, 3)
        position["last_r_multiple"] = round(total_pnl / initial_risk, 3)

    def update_mark_to_market(self, state: dict[str, Any], intraday_frames: dict[str, pd.DataFrame]) -> None:
        for position in state["positions"].values():
            data = regular_session(intraday_frames.get(position["ticker"], pd.DataFrame()))
            if data.empty:
                continue
            price = float(data["Close"].iloc[-1])
            position["last_price"] = round(price, 4)
            self._update_metrics(position, price)

    def _one_hour_ema(self, frame: pd.DataFrame) -> float | None:
        data = regular_session(frame)
        if data.empty:
            return None
        hourly_parts: list[pd.DataFrame] = []
        for _, session in data.groupby(data.index.date):
            session = session.sort_index()
            start = session.index[0].normalize() + pd.Timedelta(hours=9, minutes=30)
            bucket = ((session.index - start).total_seconds() // 3600).astype(int)
            grouped = session.groupby(bucket).agg(
                Open=("Open", "first"),
                High=("High", "max"),
                Low=("Low", "min"),
                Close=("Close", "last"),
                Volume=("Volume", "sum"),
            )
            labels = [start + pd.Timedelta(hours=int(value)) for value in grouped.index]
            grouped.index = pd.DatetimeIndex(labels)
            hourly_parts.append(grouped)
        hourly = pd.concat(hourly_parts).sort_index() if hourly_parts else pd.DataFrame()
        period = int(self.config["runner_ema_period_1h"])
        if len(hourly) < period:
            return None
        value = ema(hourly["Close"], period).iloc[-1]
        return None if pd.isna(value) else float(value)
