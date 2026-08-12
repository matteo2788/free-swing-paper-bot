from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

import pandas as pd

from .config import AppConfig
from .indicators import atr, session_vwap
from .quant_alerts import QuantDiscordAlerter
from .quant_alpha import (
    RegimeResult,
    build_raw_alpha_signal,
    evaluate_market_regime,
    load_gamma_snapshot,
    rank_alpha_signals,
    sector_relative_performance,
)
from .quant_execution import (
    apply_long_entry_slippage,
    apply_long_exit_slippage,
    risk_parity_quantity,
    transaction_fee,
    update_performance_statistics,
)
from .responsive_runtime import _parse_minutes, monitor_paper
from .single_account_runtime import SingleAccountEngine, SingleAccountPaperTradingEngine
from .strategy import closed_intraday_bars, evaluate_daily_context, regular_session, tier_crossings
from .universe import build_universe_with_sectors

LOGGER = logging.getLogger(__name__)

QUANT_MODEL_VERSION = 1


class QuantPaperTradingEngine(SingleAccountPaperTradingEngine):
    """One-position paper engine with ATR risk sizing, friction, and dynamic 5m exits."""

    def ensure_account_state(self, state: dict[str, Any]) -> bool:
        changed = bool(super().ensure_account_state(state))
        current_version = int(state.get("quant_model_version") or 0)
        if current_version == QUANT_MODEL_VERSION and isinstance(state.get("performance"), dict):
            return changed

        cleared_pending = [
            {"trade_id": item.get("trade_id"), "ticker": item.get("ticker")}
            for item in state.get("pending", {}).values()
        ]
        cleared_positions = [
            {"trade_id": item.get("trade_id"), "ticker": item.get("ticker")}
            for item in state.get("positions", {}).values()
        ]
        state["pending"] = {}
        state["positions"] = {}
        starting = float(
            state.get(
                "paper_account_starting_value",
                self.config.get("starting_account_value", 10000.0),
            )
        )
        current = float(state.get("paper_account_value", starting))
        state["quant_model_version"] = QUANT_MODEL_VERSION
        state["quant_model_migrated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        state["performance"] = {
            "closed_trades": 0,
            "wins": 0,
            "losses": 0,
            "breakeven": 0,
            "win_rate_percent": 0.0,
            "expectancy_dollars": 0.0,
            "gross_profit": 0.0,
            "gross_loss": 0.0,
            "profit_factor": 0.0,
            "total_pnl": 0.0,
            "starting_account_value": round(starting, 2),
            "current_account_value": round(current, 2),
            "peak_equity": round(max(starting, current), 2),
            "max_drawdown_dollars": 0.0,
            "max_drawdown_percent": 0.0,
        }
        self.storage.log_event(
            "quant_model_migration",
            {
                "quant_model_version": QUANT_MODEL_VERSION,
                "paper_account_value_preserved": round(current, 2),
                "cleared_pending": cleared_pending,
                "cleared_positions": cleared_positions,
                "reason": "switch_to_atr_risk_parity_and_quant_alpha",
            },
        )
        return True

    def create_pending(
        self,
        state: dict[str, Any],
        ticker: str,
        result: dict[str, Any],
    ) -> bool:
        before = set(state.get("pending", {}))
        created = super().create_pending(state, ticker, result)
        if not created:
            return False
        new_ids = [trade_id for trade_id in state.get("pending", {}) if trade_id not in before]
        if not new_ids:
            return True
        pending = state["pending"][new_ids[0]]
        plan = result.get("trade_plan") or {}
        pending.update(
            {
                "alpha_score": float(result.get("alpha_score", result.get("score", 0.0))),
                "composite_z": float(result.get("composite_z", 0.0)),
                "factor_breakdown": result.get("factor_breakdown", {}),
                "alpha_metrics": result.get("metrics", {}),
                "gates": result.get("gates", {}),
                "signal_atr": float(plan.get("atr", 0.0)),
                "entry_vwap": float(plan.get("entry_vwap", plan.get("entry_low", 0.0))),
            }
        )
        return True

    def process(
        self,
        state: dict[str, Any],
        intraday_frames: dict[str, pd.DataFrame],
    ) -> bool:
        self._prime_pending_entry_vwap(state, intraday_frames)
        return bool(super().process(state, intraday_frames))

    def _prime_pending_entry_vwap(
        self,
        state: dict[str, Any],
        intraday_frames: dict[str, pd.DataFrame],
    ) -> None:
        for pending in state.get("pending", {}).values():
            data = regular_session(intraday_frames.get(pending.get("ticker"), pd.DataFrame()))
            if data.empty:
                continue
            last_processed = pd.Timestamp(pending.get("last_processed_bar"))
            if last_processed.tzinfo is None and data.index.tz is not None:
                last_processed = last_processed.tz_localize(data.index.tz)
            elif last_processed.tzinfo is not None and data.index.tz is not None:
                last_processed = last_processed.tz_convert(data.index.tz)
            vwap_values = session_vwap(data)
            unseen = data.loc[data.index > last_processed]
            for timestamp, bar in unseen.iterrows():
                if not self._bar_touches_zone(bar, float(pending["entry_low"]), float(pending["entry_high"])):
                    continue
                candidate = vwap_values.loc[timestamp]
                if pd.notna(candidate):
                    pending["entry_vwap"] = round(float(candidate), 6)
                break

    def _open_position(
        self,
        pending: dict[str, Any],
        fill_price: float,
        timestamp: pd.Timestamp,
    ) -> dict[str, Any] | None:
        state = self._active_state or {}
        account_value = float(
            state.get("paper_account_value", self.config.get("starting_account_value", 10000.0))
        )
        if account_value <= 0:
            return None

        atr_value = float(pending.get("signal_atr", 0.0))
        if atr_value <= 0:
            return None

        slippage_percent = float(self.config.get("slippage_percent", 0.05))
        fee_percent = float(self.config.get("transaction_fee_percent", 0.01))
        execution_fill = apply_long_entry_slippage(fill_price, slippage_percent)

        stop_multiple = float(self.config.get("stop_atr_multiple", 1.5))
        tp1_multiple = float(self.config.get("target_1_atr_multiple", 1.5))
        tp2_multiple = float(self.config.get("target_2_atr_multiple", 2.5))
        stop = execution_fill - stop_multiple * atr_value
        tp1 = execution_fill + tp1_multiple * atr_value
        tp2 = execution_fill + tp2_multiple * atr_value

        quantity = risk_parity_quantity(
            account_value,
            float(self.config.get("risk_per_trade_percent", 2.0)),
            execution_fill,
            stop,
            float(self.config.get("max_position_notional_percent", 100.0)),
            fee_percent,
        )
        if quantity < 1:
            return None

        initial_notional = execution_fill * quantity
        entry_fee = transaction_fee(initial_notional, fee_percent)
        initial_risk_dollars = (execution_fill - stop) * quantity
        target_1_quantity = min(
            quantity,
            max(1, math.floor(quantity * float(self.config.get("target_1_exit_fraction", 0.50)))),
        )
        remaining_after_tp1 = quantity - target_1_quantity
        target_2_quantity = math.floor(
            quantity * float(self.config.get("target_2_exit_fraction", 0.25))
        )
        if remaining_after_tp1 > 0:
            target_2_quantity = max(1, target_2_quantity)
        target_2_quantity = min(max(0, target_2_quantity), remaining_after_tp1)

        cash_unused = max(0.0, account_value - initial_notional - entry_fee)
        entry_vwap = float(pending.get("entry_vwap", execution_fill))
        return {
            "trade_id": pending["trade_id"],
            "ticker": pending["ticker"],
            "tier": pending["tier"],
            "score": pending["score"],
            "alpha_score": float(pending.get("alpha_score", pending["score"])),
            "composite_z": float(pending.get("composite_z", 0.0)),
            "factor_breakdown": pending.get("factor_breakdown", {}),
            "alpha_metrics": pending.get("alpha_metrics", {}),
            "gates": pending.get("gates", {}),
            "signal_time": pending["signal_time"],
            "entry_time": timestamp.isoformat(),
            "entry_reference_price": round(float(fill_price), 6),
            "entry_price": round(execution_fill, 6),
            "entry_vwap": round(entry_vwap, 6),
            "signal_atr": round(atr_value, 6),
            "initial_stop": round(stop, 6),
            "stop": round(stop, 6),
            "tp1": round(tp1, 6),
            "tp2": round(tp2, 6),
            "quantity": int(quantity),
            "remaining_quantity": int(quantity),
            "target_1_quantity": int(target_1_quantity),
            "target_2_quantity": int(target_2_quantity),
            "initial_notional": round(initial_notional, 2),
            "initial_risk_dollars": round(initial_risk_dollars, 2),
            "risk_budget_percent": float(self.config.get("risk_per_trade_percent", 2.0)),
            "account_value_at_entry": round(account_value, 2),
            "cash_unused": round(cash_unused, 2),
            "allocation_percent": round((initial_notional / account_value) * 100.0, 3),
            "sizing_mode": "ATR_RISK_PARITY",
            "slippage_percent": slippage_percent,
            "transaction_fee_percent": fee_percent,
            "entry_fee": round(entry_fee, 6),
            "fees_paid": round(entry_fee, 6),
            "realized_pnl": round(-entry_fee, 6),
            "stage": 0,
            "trailing_stop": None,
            "highest_since_entry": round(execution_fill, 6),
            "last_processed_bar": timestamp.isoformat(),
            "last_price": round(execution_fill, 6),
            "last_return_percent": 0.0,
            "last_r_multiple": 0.0,
            "exits": [],
        }

    def _realize(
        self,
        position: dict[str, Any],
        quantity: int,
        price: float,
        timestamp: pd.Timestamp,
        reason: str,
    ) -> float:
        if quantity <= 0:
            return float(price)
        fill = apply_long_exit_slippage(
            float(price),
            float(position.get("slippage_percent", self.config.get("slippage_percent", 0.05))),
        )
        notional = fill * quantity
        fee = transaction_fee(
            notional,
            float(position.get("transaction_fee_percent", self.config.get("transaction_fee_percent", 0.01))),
        )
        gross_pnl = (fill - float(position["entry_price"])) * quantity
        net_pnl = gross_pnl - fee
        position["realized_pnl"] = round(float(position["realized_pnl"]) + net_pnl, 6)
        position["fees_paid"] = round(float(position.get("fees_paid", 0.0)) + fee, 6)
        position["remaining_quantity"] = int(position["remaining_quantity"]) - quantity
        position["exits"].append(
            {
                "time": timestamp.isoformat(),
                "reason": reason,
                "reference_price": round(float(price), 6),
                "price": round(fill, 6),
                "quantity": int(quantity),
                "gross_pnl": round(gross_pnl, 6),
                "fee": round(fee, 6),
                "pnl": round(net_pnl, 6),
            }
        )
        return fill

    @staticmethod
    def _declining_volume(data: pd.DataFrame, timestamp: pd.Timestamp) -> bool:
        try:
            location = int(data.index.get_loc(timestamp))
        except (KeyError, TypeError):
            return False
        if location < 1:
            return False
        current = float(data["Volume"].iloc[location])
        previous = float(data["Volume"].iloc[location - 1])
        start = max(0, location - 3)
        prior_mean = float(data["Volume"].iloc[start:location].mean())
        return current < previous and current < prior_mean

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
        fill = self._realize(position, quantity, price, timestamp, reason)
        self._update_metrics(position, fill)
        self.storage.log_event("position_closed", {**position, "exit_reason": reason})
        self._finish_trade(state, trade_id, timestamp, reason)

    def _process_positions(
        self,
        state: dict[str, Any],
        intraday_frames: dict[str, pd.DataFrame],
    ) -> bool:
        changed = False
        atr_period = int(self.config.get("atr_period", 14))
        trail_multiple = float(self.config.get("trailing_atr_multiple", 2.0))
        invalidation_bars = int(self.config.get("early_invalidation_bars", 3))

        for trade_id, position in list(state["positions"].items()):
            ticker = position["ticker"]
            data = regular_session(intraday_frames.get(ticker, pd.DataFrame()))
            if data.empty:
                continue
            atr_values = atr(data, atr_period)

            last_processed = pd.Timestamp(position["last_processed_bar"])
            if last_processed.tzinfo is None and data.index.tz is not None:
                last_processed = last_processed.tz_localize(data.index.tz)
            elif last_processed.tzinfo is not None and data.index.tz is not None:
                last_processed = last_processed.tz_convert(data.index.tz)
            unseen = data.loc[data.index > last_processed]

            entry_time = pd.Timestamp(position["entry_time"])
            if entry_time.tzinfo is None and data.index.tz is not None:
                entry_time = entry_time.tz_localize(data.index.tz)
            elif entry_time.tzinfo is not None and data.index.tz is not None:
                entry_time = entry_time.tz_convert(data.index.tz)

            for timestamp, bar in unseen.iterrows():
                if trade_id not in state["positions"]:
                    break
                position = state["positions"][trade_id]
                position["last_processed_bar"] = timestamp.isoformat()
                close = float(bar["Close"])
                low = float(bar["Low"])
                high = float(bar["High"])
                position["last_price"] = round(close, 6)
                self._update_metrics(position, close)

                active_stop = float(position["stop"])
                if low <= active_stop:
                    if int(position["stage"]) >= 2 and position.get("trailing_stop") is not None:
                        reason = "ATR TRAILING STOP"
                    elif int(position["stage"]) > 0:
                        reason = "BREAKEVEN STOP"
                    else:
                        reason = "STOP LOSS"
                    self._close_remaining(state, trade_id, active_stop, timestamp, reason)
                    changed = True
                    break

                bars_since_entry = int(((data.index > entry_time) & (data.index <= timestamp)).sum())
                if (
                    int(position["stage"]) == 0
                    and 1 <= bars_since_entry <= invalidation_bars
                    and close < float(position.get("entry_vwap", position["entry_price"]))
                    and self._declining_volume(data, timestamp)
                ):
                    self._close_remaining(state, trade_id, close, timestamp, "EARLY VWAP INVALIDATION")
                    changed = True
                    break

                if int(position["stage"]) == 0 and high >= float(position["tp1"]):
                    quantity = min(int(position["target_1_quantity"]), int(position["remaining_quantity"]))
                    fill = self._realize(position, quantity, float(position["tp1"]), timestamp, "TP1")
                    position["stop"] = max(float(position["stop"]), float(position["entry_price"]))
                    position["stage"] = 1
                    self._update_metrics(position, fill)
                    self.storage.log_event("tp1_hit", position)
                    self.alerter.position_event_alert(
                        "✅ TP1 HIT",
                        position,
                        "TP1 reached. The first partial was realized with modeled friction and the stop moved to breakeven.",
                        0x2ECC71,
                    )
                    changed = True
                    if int(position["remaining_quantity"]) == 0:
                        self._finish_trade(state, trade_id, timestamp, "TP1_FULL_EXIT")
                        break

                if trade_id not in state["positions"]:
                    break
                position = state["positions"][trade_id]
                if int(position["stage"]) == 1 and high >= float(position["tp2"]):
                    quantity = min(int(position["target_2_quantity"]), int(position["remaining_quantity"]))
                    fill = float(position["tp2"])
                    if quantity > 0:
                        fill = self._realize(position, quantity, float(position["tp2"]), timestamp, "TP2")
                    position["stage"] = 2
                    position["highest_since_entry"] = max(float(position.get("highest_since_entry", position["entry_price"])), high)
                    atr_now = atr_values.loc[timestamp] if timestamp in atr_values.index else math.nan
                    if pd.notna(atr_now) and float(atr_now) > 0:
                        trail = float(position["highest_since_entry"]) - trail_multiple * float(atr_now)
                        trail = max(float(position["entry_price"]), trail)
                        position["trailing_stop"] = round(trail, 6)
                        position["stop"] = round(max(float(position["stop"]), trail), 6)
                    self._update_metrics(position, fill)
                    self.storage.log_event("tp2_hit", position)
                    self.alerter.position_event_alert(
                        "🎯 TP2 HIT",
                        position,
                        "TP2 reached. The remaining runner is now managed by a 2× 5-minute ATR Chandelier stop.",
                        0x27AE60,
                    )
                    changed = True
                    if int(position["remaining_quantity"]) == 0:
                        self._finish_trade(state, trade_id, timestamp, "TP2_FULL_EXIT")
                        break

                if trade_id not in state["positions"]:
                    break
                position = state["positions"][trade_id]
                if int(position["stage"]) >= 2 and int(position["remaining_quantity"]) > 0:
                    position["highest_since_entry"] = max(float(position.get("highest_since_entry", position["entry_price"])), high)
                    atr_now = atr_values.loc[timestamp] if timestamp in atr_values.index else math.nan
                    if pd.notna(atr_now) and float(atr_now) > 0:
                        new_trail = float(position["highest_since_entry"]) - trail_multiple * float(atr_now)
                        previous_trail = float(position.get("trailing_stop") or position["entry_price"])
                        new_trail = max(previous_trail, new_trail, float(position["entry_price"]))
                        position["trailing_stop"] = round(new_trail, 6)
                        position["stop"] = round(max(float(position["stop"]), new_trail), 6)

                held_days = self.clock.trading_days_between(entry_time.to_pydatetime(), timestamp.to_pydatetime())
                if held_days >= int(self.config.get("time_stop_trading_days", 5)):
                    self._close_remaining(state, trade_id, close, timestamp, "TIME STOP")
                    changed = True
                    break

        return changed

    def _finish_trade(
        self,
        state: dict[str, Any],
        trade_id: str,
        timestamp: pd.Timestamp,
        outcome: str,
    ) -> None:
        position = state["positions"][trade_id]
        snapshot = dict(position)
        snapshot["exits"] = [dict(item) for item in position.get("exits", [])]
        realized_pnl = round(float(position.get("realized_pnl", 0.0)), 2)

        super()._finish_trade(state, trade_id, timestamp, outcome)

        account_value = float(state.get("paper_account_value", self.config.get("starting_account_value", 10000.0)))
        starting = float(state.get("paper_account_starting_value", self.config.get("starting_account_value", 10000.0)))
        performance = update_performance_statistics(
            state.get("performance"),
            realized_pnl=realized_pnl,
            account_value=account_value,
            starting_account_value=starting,
        )
        state["performance"] = performance
        self.storage.log_event(
            "quant_performance_updated",
            {
                "trade_id": trade_id,
                "ticker": snapshot.get("ticker"),
                "outcome": outcome,
                "realized_pnl": realized_pnl,
                "paper_account_value": round(account_value, 2),
                "performance": performance,
            },
        )
        closed_alert = getattr(self.alerter, "trade_closed_alert", None)
        if callable(closed_alert):
            closed_alert(snapshot, outcome, account_value, performance)
        else:
            profit_factor = performance.get("profit_factor")
            profit_factor_text = "∞" if profit_factor is None and performance.get("gross_profit", 0) > 0 else f"{float(profit_factor or 0.0):.2f}"
            self.alerter.position_event_alert(
                "🏁 QUANT PAPER TRADE CLOSED",
                snapshot,
                (
                    f"Outcome: **{outcome}**\n"
                    f"Net realized P/L: **${realized_pnl:,.2f}**\n"
                    f"Paper account equity: **${account_value:,.2f}**\n\n"
                    f"Running stats — Win rate **{float(performance.get('win_rate_percent', 0.0)):.2f}%**, "
                    f"Expectancy **${float(performance.get('expectancy_dollars', 0.0)):,.2f}/trade**, "
                    f"Profit factor **{profit_factor_text}**, "
                    f"Max drawdown **{float(performance.get('max_drawdown_percent', 0.0)):.2f}%**."
                ),
                0x2ECC71 if realized_pnl > 0 else 0xE74C3C,
            )


class QuantEngine(SingleAccountEngine):
    """Macro-gated cross-sectional alpha scanner feeding one ATR-risk paper position."""

    def __init__(self, config: AppConfig, *, dry_run: bool = False) -> None:
        super().__init__(config, dry_run=dry_run)
        self.regime_config = config.section("regime")
        self.alpha_config = config.section("alpha")
        self.options_gamma_config = config.section("options_gamma")
        self._cached_regime: RegimeResult | None = None

    def refresh_daily_pool(self, now: datetime | None = None) -> int:
        current = now or self.clock.now()
        LOGGER.info("Refreshing quant daily context pool")
        universe, sectors = build_universe_with_sectors(self.universe_config, self.config.root)
        sector_etfs = {
            str(value).strip().upper()
            for value in self.alpha_config.get("sector_etfs", {}).values()
            if str(value).strip()
        }
        required = set(universe) | sector_etfs | {"SPY", "QQQ"}
        frames = self.provider.download(sorted(required), period=self.daily_period, interval="1d")
        spy = frames.get("SPY")
        if spy is None or spy.empty:
            raise RuntimeError("SPY daily data is required for quant context calculations")

        eligible: dict[str, Any] = {}
        evaluated = 0
        mapping = self.alpha_config.get("sector_etfs", {})
        for ticker in universe:
            frame = frames.get(ticker)
            if frame is None or frame.empty:
                continue
            evaluated += 1
            context = evaluate_daily_context(ticker, frame, spy, self.context_config)
            if not context.passed:
                continue

            sector = sectors.get(ticker)
            sector_etf = str(mapping.get(sector, "SPY")).upper() if sector else "SPY"
            if ticker == sector_etf:
                sector_etf = "SPY"
            benchmark = frames.get(sector_etf)
            if benchmark is None or benchmark.empty:
                benchmark = spy
            relative = sector_relative_performance(frame, benchmark)
            if relative["relative_5d_percent"] is None or relative["relative_20d_percent"] is None:
                continue

            details = dict(context.details)
            details.update(
                {
                    "sector": sector or "UNKNOWN",
                    "sector_etf": sector_etf,
                    **relative,
                }
            )
            eligible[ticker] = details

        self.state["daily_pool"] = eligible
        self.state["last_daily_refresh"] = current.date().isoformat()
        self.storage.log_event(
            "quant_daily_pool_refreshed",
            {
                "evaluated": evaluated,
                "eligible": len(eligible),
                "date": current.date().isoformat(),
                "sector_mapping_coverage": sum(1 for ticker in eligible if sectors.get(ticker)),
            },
        )
        self.storage.save_state(self.state, important=True)
        LOGGER.info("Quant daily context: %s of %s symbols eligible", len(eligible), evaluated)
        return len(eligible)

    def market_regime(self, now: datetime | None = None) -> RegimeResult:
        current = now or self.clock.now()
        vix_symbol = str(self.regime_config.get("vix_symbol", "^VIX"))
        frames = self.provider.download(
            ["SPY", "QQQ", vix_symbol],
            period=str(self.regime_config.get("data_period", "6mo")),
            interval="1d",
        )
        result = evaluate_market_regime(
            frames.get("SPY", pd.DataFrame()),
            frames.get("QQQ", pd.DataFrame()),
            frames.get(vix_symbol),
            self.regime_config,
        )
        previous = self.state.get("market_regime", {})
        previous_allowed = previous.get("allow_new_longs")
        self.state["market_regime"] = {
            "checked_at": current.isoformat(),
            "allow_new_longs": result.allow_new_longs,
            "reasons": result.reasons,
            "metrics": result.metrics,
        }
        transition = previous_allowed is not None and bool(previous_allowed) != result.allow_new_longs
        if transition:
            self.storage.log_event("market_regime_transition", self.state["market_regime"])
        self.storage.save_state(self.state, important=transition)
        self._cached_regime = result
        return result

    def cancel_pending_for_regime(self, regime: RegimeResult) -> bool:
        if regime.allow_new_longs or not self.state.get("pending"):
            return False
        cancelled = list(self.state["pending"].values())
        self.state["pending"] = {}
        for pending in cancelled:
            self.storage.log_event(
                "pending_cancelled_macro_regime",
                {
                    **pending,
                    "regime_reasons": regime.reasons,
                    "regime_metrics": regime.metrics,
                },
            )
        self.storage.save_state(self.state, important=True)
        return True

    @staticmethod
    def _candidate_key(candidate: tuple[str, dict[str, Any]]) -> tuple[float, float, float, float, str]:
        ticker, result = candidate
        breakdown = result.get("factor_breakdown", {})
        return (
            float(result.get("alpha_score", 0.0)),
            float(result.get("composite_z", 0.0)),
            float(breakdown.get("relative_strength", {}).get("z") or 0.0),
            float(breakdown.get("volume", {}).get("z") or 0.0),
            ticker,
        )

    def scan(
        self,
        now: datetime | None = None,
        *,
        regime: RegimeResult | None = None,
    ) -> dict[str, Any]:
        current = now or self.clock.now()
        self.ensure_single_account_model()
        active_regime = regime or self.market_regime(current)
        if not active_regime.allow_new_longs:
            self.cancel_pending_for_regime(active_regime)
            return {
                "scanned": 0,
                "a_alerts": 0,
                "b_alerts": 0,
                "positions": len(self.state.get("positions", {})),
                "regime_on": False,
                "regime_reasons": active_regime.reasons,
            }

        self.ensure_daily_pool(current)
        cutoff = self.clock.latest_closed_bar_cutoff(self.interval_minutes, current)
        if cutoff is None:
            return {
                "scanned": 0,
                "a_alerts": 0,
                "b_alerts": 0,
                "positions": len(self.state.get("positions", {})),
                "regime_on": True,
            }

        signal_bar_start = cutoff - timedelta(minutes=self.interval_minutes)
        scan_key = signal_bar_start.isoformat()
        if self.state.get("last_scan_bar") == scan_key:
            return {
                "scanned": 0,
                "a_alerts": 0,
                "b_alerts": 0,
                "positions": len(self.state.get("positions", {})),
                "regime_on": True,
            }

        scan_tickers = set(self.state.get("daily_pool", {}).keys())
        scan_tickers.update(item["ticker"] for item in self.state.get("pending", {}).values())
        scan_tickers.update(item["ticker"] for item in self.state.get("positions", {}).values())
        if not scan_tickers:
            return {
                "scanned": 0,
                "a_alerts": 0,
                "b_alerts": 0,
                "positions": len(self.state.get("positions", {})),
                "regime_on": True,
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
        gamma_snapshot = load_gamma_snapshot(self.options_gamma_config, self.config.root)
        raw_signals = []
        for ticker, details in self.state.get("daily_pool", {}).items():
            frame = frames.get(ticker)
            if frame is None:
                continue
            raw = build_raw_alpha_signal(
                ticker,
                frame,
                details,
                self.alpha_config,
                gamma_snapshot,
            )
            if raw is not None:
                raw_signals.append(raw)

        ranked = rank_alpha_signals(raw_signals, self.alpha_config, self.paper_config)
        scanned = len(raw_signals)
        a_alerts = 0
        b_alerts = 0
        b_threshold = int(self.alpha_config.get("watchlist_score", 60))
        a_threshold = int(self.paper_config.get("minimum_trade_score", 80))
        a_candidates: list[tuple[str, dict[str, Any]]] = []

        for result in ranked:
            ticker = str(result["ticker"])
            previous = self.state["scores"].get(ticker, {})
            previous_score = previous.get("score")
            crossings = tier_crossings(previous_score, int(result["score"]), b_threshold, a_threshold)

            if bool(result.get("eligible")) and int(result.get("score", 0)) >= a_threshold and result.get("trade_plan"):
                a_candidates.append((ticker, result))
            elif "B" in crossings and bool(self.alert_config.get("send_b_setups", True)):
                self.alerter.setup_alert(ticker, result, "B")
                self.storage.log_event("quant_b_setup_alert", result)
                b_alerts += 1
                important_change = True

            self.state["scores"][ticker] = {
                "score": int(result["score"]),
                "alpha_score": float(result["alpha_score"]),
                "composite_z": float(result["composite_z"]),
                "tier": result.get("tier"),
                "updated_at": result["trigger_time"],
                "factor_breakdown": result.get("factor_breakdown", {}),
                "gates": result.get("gates", {}),
            }

        account_busy = bool(self.state.get("pending")) or bool(self.state.get("positions"))
        if a_candidates and not account_busy:
            best_ticker, best_result = max(a_candidates, key=self._candidate_key)
            if self.paper.create_pending(self.state, best_ticker, best_result):
                if bool(self.alert_config.get("send_a_setups", True)):
                    self.alerter.setup_alert(best_ticker, best_result, "A")
                    a_alerts = 1
                self.storage.log_event(
                    "quant_best_alpha_selected",
                    {
                        "ticker": best_ticker,
                        "alpha_score": best_result["alpha_score"],
                        "composite_z": best_result["composite_z"],
                        "factor_breakdown": best_result["factor_breakdown"],
                        "candidate_count": len(a_candidates),
                        "top_candidates": [
                            {
                                "ticker": ticker,
                                "alpha_score": result["alpha_score"],
                                "composite_z": result["composite_z"],
                            }
                            for ticker, result in sorted(a_candidates, key=self._candidate_key, reverse=True)[:5]
                        ],
                        "paper_account_value": float(
                            self.state.get("paper_account_value", self.paper_config.get("starting_account_value", 10000.0))
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
            "positions": len(self.state.get("positions", {})),
            "regime_on": True,
            "gamma_data_available": bool(gamma_snapshot.get("available")),
        }

    def status(self) -> dict[str, Any]:
        result = super().status()
        result.update(
            {
                "quant_model_version": self.state.get("quant_model_version"),
                "position_sizing": "ATR_RISK_PARITY",
                "risk_per_trade_percent": float(self.paper_config.get("risk_per_trade_percent", 2.0)),
                "market_regime": self.state.get("market_regime"),
                "performance": self.state.get("performance", {}),
            }
        )
        return result


def build_quant_engine(config: AppConfig, *, dry_run: bool = False) -> QuantEngine:
    engine = QuantEngine(config, dry_run=dry_run)
    monitor_minutes = _parse_minutes(engine.paper_config.get("monitor_interval", "5m"), 5)
    alerter = QuantDiscordAlerter(
        engine.alert_config,
        dry_run=dry_run,
        monitor_minutes=monitor_minutes,
    )
    engine.alerter = alerter
    engine.paper = QuantPaperTradingEngine(
        engine.paper_config,
        engine.clock,
        engine.storage,
        alerter,
    )
    engine.ensure_single_account_model()
    return engine


def run_quant_auto(config: AppConfig, *, dry_run: bool = False) -> dict[str, Any]:
    engine = build_quant_engine(config, dry_run=dry_run)
    current = engine.clock.now()
    LOGGER.info("Quant bot time: %s", current.isoformat())

    if engine.clock.is_open(current):
        regime = engine.market_regime(current)
        if not regime.allow_new_longs:
            engine.cancel_pending_for_regime(regime)
        monitor_result = monitor_paper(engine, current)
        if not regime.allow_new_longs:
            return {
                "mode": "risk_off",
                "paper_monitor": monitor_result,
                "regime_on": False,
                "regime_reasons": regime.reasons,
                "paper_account_value": round(float(engine.state.get("paper_account_value", 10000.0)), 2),
                "positions": len(engine.state.get("positions", {})),
            }

        scan_result = engine.scan(current, regime=regime)
        return {
            "mode": "scan",
            "paper_monitor": monitor_result,
            "paper_account_value": round(float(engine.state.get("paper_account_value", 10000.0)), 2),
            **scan_result,
        }

    if engine.clock.is_after_close_window(current):
        engine.after_close(current)
        return {
            "mode": "after_close",
            "positions": len(engine.state.get("positions", {})),
            "paper_account_value": round(float(engine.state.get("paper_account_value", 10000.0)), 2),
        }

    return {
        "mode": "idle",
        "paper_account_value": round(float(engine.state.get("paper_account_value", 10000.0)), 2),
    }
