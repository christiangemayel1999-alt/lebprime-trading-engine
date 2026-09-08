"""Historical replay/backtest runner for MT5 XAUUSD strategies."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import pytz

from logger import BotLogger
from mt5_connector import MT5Connector
from risk_manager import RiskManager
from services.backtest_replay import ReplayClock, ReplayContext, ReplayMarketData, ReplaySession, SimulatedAccount as _SimAccount
from services.backtest_storage import BacktestStorage
from services.database import DatabaseService
from services.historical_feed import HistoricalMT5Feed
from services.history_loader import HistoricalDataLoader
from services.simulated_executor import SimulatedExecutor
from strategy_factory import build_strategy_engine
from trading_bot.core.execution_mode import apply_execution_mode_to_config
from trading_bot.core.families import strategy_family_for_setup
from trading_bot.core.job_state import JobState
from trading_bot.core.reasons import ReasonCode
from trading_bot.execution.decision_engine import ExecutionDecisionEngine
from trading_bot.execution.exposure_policy import ExposurePolicy, ExposureSnapshot
from trading_bot.execution.pending_policy import PendingOrderPolicy
from trading_bot.execution.execution_service import ExecutionService
from trading_bot.execution.replay_broker import ReplayBroker
from trading_bot.risk.engine import RiskEngine
from utils import to_utc, utc_now


STRATEGY_REGISTRY: dict[str, str | None] = {
    "XAU_BOT_TREND_PU": "TREND_PULLBACK_RECLAIM",
    "XAU_BOT_LIQUIDIT": "LIQUIDITY_SWEEP_REVERSAL",
    "XAU_BOT_COMPRESS": "COMPRESSION_RELEASE",
    "XAU_BOT_BREAKOUT": "BREAKOUT_RETEST_CONTINUATION",
    "XAU_SCORE": None,
    "XAU_LEBPRIM": "LEBPRIM_SCALP",
}

FAMILY_CONFIG_KEYS: dict[str, str] = {
    "TREND_PULLBACK_RECLAIM": "trend_pullback_reclaim",
    "LIQUIDITY_SWEEP_REVERSAL": "liquidity_sweep_reversal",
    "COMPRESSION_RELEASE": "compression_release",
    "BREAKOUT_RETEST_CONTINUATION": "breakout_retest_continuation",
    "LEBPRIM_SCALP": "lebprim_scalp",
}

TIMEFRAME_ALIAS = {
    "M1": "1min",
    "M3": "3min",
    "M5": "5min",
    "M15": "15min",
    "M30": "30min",
    "H1": "60min",
}

EMPTY_OHLC_COLUMNS = ["time", "open", "high", "low", "close", "tick_volume", "spread"]
BACKTEST_EXECUTION_MODELS = {"next_bar_open", "signal_price_touch", "current_bar_close"}


class BacktestRunner:
    """Run replay mode using historical MT5 candles and simulated execution."""

    def __init__(self, base_dir: str | Path, config: dict[str, Any], database: DatabaseService) -> None:
        self.base_dir = Path(base_dir)
        self.config = config
        self.database = database
        self.storage = BacktestStorage(database)
        self.logger = BotLogger(self.base_dir, config)
        self.history_loader = HistoricalDataLoader(self.base_dir, config, database, self.logger)
        self.strategy = build_strategy_engine(config, self.base_dir)

    @staticmethod
    def _parse_date_range(start_date: str, end_date: str | None) -> tuple[datetime, datetime]:
        utc = pytz.UTC
        start_day = datetime.fromisoformat(str(start_date)).date()
        end_day = datetime.fromisoformat(str(end_date or start_date)).date()
        start_dt = datetime.combine(start_day, time.min, tzinfo=utc)
        end_dt = datetime.combine(end_day, time.max, tzinfo=utc)
        return start_dt, end_dt

    @staticmethod
    def _validate_backtest_request(request: dict[str, Any]) -> None:
        """Validate backtest request to prevent silent failures.
        
        Raises ValueError with clear message if any required or critical field
        is missing, invalid, or will cause the backtest to run with garbage parameters.
        """
        # Required fields
        if "start_date" not in request or not request.get("start_date"):
            raise ValueError("start_date is required")
        if "symbol" not in request or not request.get("symbol"):
            raise ValueError("symbol is required")
        
        # Validate date range
        try:
            start_dt, end_dt = BacktestRunner._parse_date_range(request["start_date"], request.get("end_date"))
        except (ValueError, TypeError) as e:
            raise ValueError(f"Invalid date format: {e}")
        
        if start_dt >= end_dt:
            raise ValueError(
                f"start_date {request['start_date']} must be before end_date {request.get('end_date') or request['start_date']}"
            )
        
        # Validate initial balance
        initial_balance = float(request.get("initial_balance", 10000.0))
        if initial_balance <= 0:
            raise ValueError(f"initial_balance must be positive, got {initial_balance}")
        
        # Validate risk parameters
        risk_percent = float(request.get("risk_percent", 0.5))
        if risk_percent <= 0 or risk_percent > 100:
            raise ValueError(f"risk_percent must be between 0 and 100, got {risk_percent}")
        
        # Validate leverage
        leverage = float(request.get("leverage", 1.0))
        if leverage <= 0:
            raise ValueError(f"leverage must be positive, got {leverage}")
        
        # Validate execution model if provided
        execution_model = str(request.get("execution_model", "next_bar_open")).lower()
        if execution_model not in BACKTEST_EXECUTION_MODELS:
            raise ValueError(f"execution_model must be one of {sorted(BACKTEST_EXECUTION_MODELS)}, got {execution_model}")

        if "enabled_strategies" in request:
            selected = [str(item).strip().upper() for item in request.get("enabled_strategies", []) if str(item).strip()]
            if not selected:
                raise ValueError("No enabled_strategies selected. Select at least one strategy or omit the field to use execution-mode defaults.")
            unknown = [item for item in selected if item not in STRATEGY_REGISTRY]
            if unknown:
                raise ValueError(f"Unknown enabled_strategies {unknown}. Available strategies: {sorted(STRATEGY_REGISTRY)}")
            if not BacktestRunner._enabled_families(selected):
                raise ValueError(f"enabled_strategies {selected} did not resolve to any valid strategy families")
        
        # Validate spread model
        spread_model = request.get("spread_model", {})
        if isinstance(spread_model, dict):
            spread_type = str(spread_model.get("type", "fixed_points")).lower()
            valid_spread_types = {"fixed_points", "bar_spread", "percent", "pips"}
            if spread_type not in valid_spread_types:
                raise ValueError(f"spread_model.type must be one of {valid_spread_types}, got {spread_type}")
            
            # Validate spread points if using fixed
            if spread_type == "fixed_points":
                spread_points = spread_model.get("points", 20.0)
                if spread_points is not None:
                    try:
                        float(spread_points)
                    except (TypeError, ValueError):
                        raise ValueError(f"spread_model.points must be numeric, got {spread_points}")

    @staticmethod
    def _enabled_families(selected: list[str]) -> set[str]:
        cleaned = [str(item).strip().upper() for item in selected if str(item).strip()]
        if not cleaned:
            cleaned = list(STRATEGY_REGISTRY.keys())
        if "XAU_SCORE" in cleaned:
            return {family for family in STRATEGY_REGISTRY.values() if family}
        out: set[str] = set()
        for item in cleaned:
            family = STRATEGY_REGISTRY.get(item)
            if family:
                out.add(family)
        return out

    @staticmethod
    def _deep_merge_config(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
        for key, value in overrides.items():
            if isinstance(value, dict) and isinstance(base.get(key), dict):
                base[key] = BacktestRunner._deep_merge_config(dict(base.get(key) or {}), value)
            else:
                base[key] = value
        return base

    @staticmethod
    def _strategy_name_from_family(family: str) -> str:
        reverse = {v: k for k, v in STRATEGY_REGISTRY.items() if v}
        return reverse.get(family, family)

    @classmethod
    def _apply_backtest_strategy_selection(cls, config: dict[str, Any], selected: list[str]) -> None:
        """Make dashboard strategy selection authoritative for this replay only.

        Live config can keep a family disabled, but if the user selects it in a
        backtest we should allow that family to generate tradable candidates for
        the replay. Unselected built-in families are disabled in this copied
        run config so engine diagnostics match the dashboard selection.
        """
        enabled_families = cls._enabled_families(selected)
        strategy_cfg = config.setdefault("strategy", {})
        setup_families = strategy_cfg.setdefault("setup_families", {})
        setup_controls = strategy_cfg.setdefault("setup_controls", {})
        for family, key in FAMILY_CONFIG_KEYS.items():
            enabled = family in enabled_families
            family_cfg = setup_families.setdefault(key, {})
            if isinstance(family_cfg, dict):
                family_cfg["enabled"] = enabled
                family_cfg["live_allowed"] = enabled
            control_cfg = setup_controls.setdefault(key, {})
            if isinstance(control_cfg, dict):
                control_cfg["enabled"] = enabled
                control_cfg["live_allowed"] = enabled

    def _refresh_strategy_config(self, config: dict[str, Any]) -> None:
        """Push replay-only config overrides into the already-built engine."""
        refresh = getattr(self.strategy, "refresh_config", None)
        if callable(refresh):
            refresh(config)
            return
        if hasattr(self.strategy, "config"):
            self.strategy.config = config

    @staticmethod
    def _management_indicator_slices(trigger_slice: pd.DataFrame, setup_slice: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Ensure trade management has derived columns expected after preparation."""
        management_trigger = trigger_slice
        management_setup = setup_slice
        if "ema_20" not in management_trigger.columns and "close" in management_trigger.columns:
            management_trigger = management_trigger.copy()
            management_trigger["ema_20"] = management_trigger["close"]
        if "atr" not in management_setup.columns and {"high", "low"}.issubset(management_setup.columns):
            management_setup = management_setup.copy()
            management_setup["atr"] = (management_setup["high"] - management_setup["low"]).abs()
        return management_trigger, management_setup

    @staticmethod
    def _spread_points(spread_model: dict[str, Any], bar: dict[str, Any]) -> float:
        model_type = str(spread_model.get("type") or "fixed_points").lower()
        if model_type == "bar_spread":
            return float(bar.get("spread", 0.0) or 0.0)
        return float(spread_model.get("points", 20.0) or 20.0)

    @staticmethod
    def _slippage_points(slippage_model: dict[str, Any]) -> float:
        return float(slippage_model.get("points", 2.0) or 2.0)

    @staticmethod
    def _clock_time_utc(raw_value: str, fallback: str) -> time:
        text = str(raw_value or fallback).strip() or fallback
        parts = text.split(":")
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
        return time(hour=hour, minute=minute)

    @staticmethod
    def _is_friday_cutoff(now_utc: datetime, cutoff: time) -> bool:
        return now_utc.weekday() == 4 and now_utc.time() >= cutoff

    @staticmethod
    def _protection_drawdown_pct(peak: float, current: float) -> float:
        if peak <= 0:
            return 0.0
        return max(0.0, (float(peak) - float(current)) / float(peak) * 100.0)

    def _refresh_backtest_account(
        self,
        account: _SimAccount,
        executor: SimulatedExecutor,
        *,
        mark_price: float,
        now_utc: datetime,
        contract_size: float,
    ) -> None:
        account.refresh(executor=executor, mark_price=mark_price, now_utc=now_utc, contract_size=contract_size)

    def _check_backtest_account_limits(self, account: _SimAccount, protection_cfg: dict[str, Any]) -> str | None:
        if not bool(protection_cfg.get("enabled", True)):
            return None
        stop_threshold = float(protection_cfg.get("stop_when_equity_below", 0.0) or 0.0)
        if float(account.equity) <= stop_threshold:
            return "account_blown"
        total_dd_limit = protection_cfg.get("max_total_drawdown_percent")
        if total_dd_limit not in (None, "") and self._protection_drawdown_pct(float(account.peak_equity), float(account.equity)) >= float(total_dd_limit):
            return "max_drawdown_reached"
        daily_dd_limit = protection_cfg.get("max_daily_drawdown_percent")
        if daily_dd_limit not in (None, "") and self._protection_drawdown_pct(float(account.daily_start_equity), float(account.equity)) >= float(daily_dd_limit):
            return "max_daily_drawdown_reached"
        max_consecutive_losses = protection_cfg.get("max_consecutive_losses")
        if max_consecutive_losses not in (None, "") and int(account.consecutive_losses) >= int(max_consecutive_losses):
            return "max_consecutive_losses_reached"
        return None

    def _reconciliation_payload(
        self,
        *,
        initial_balance: float,
        summary: dict[str, Any],
        trades: list[dict[str, Any]],
        frames: list[dict[str, Any]],
    ) -> dict[str, Any]:
        tolerance = 1e-6
        trade_total = sum(float(row.get("realized_pnl_total", row.get("pnl", 0.0)) or 0.0) for row in trades)
        summary_balance = float(summary.get("final_balance", initial_balance) or initial_balance)
        expected_balance = float(initial_balance) + float(summary.get("net_profit", 0.0) or 0.0)
        frame_balance = float(frames[-1].get("balance", initial_balance) or initial_balance) if frames else float(initial_balance)
        diffs = {
            "summary_vs_trades": float(summary.get("net_profit", 0.0) or 0.0) - trade_total,
            "balance_vs_net_profit": summary_balance - expected_balance,
            "frames_vs_summary": frame_balance - summary_balance,
        }
        max_abs_diff = max(abs(value) for value in diffs.values()) if diffs else 0.0
        return {
            "frame_final_balance": frame_balance,
            "trade_ledger_final_balance": float(initial_balance) + trade_total,
            "summary_final_balance": summary_balance,
            "reconciliation_status": "ok" if max_abs_diff <= tolerance else "failed",
            "reconciliation_diff": max_abs_diff,
            "reconciliation_details": diffs,
        }

    @staticmethod
    def _fingerprint_guard_applies(config: dict[str, Any], setup_family: str) -> bool:
        if not bool(config.get("enabled", True)):
            return False
        families = {str(item).upper() for item in config.get("apply_to_families", [])}
        return not families or str(setup_family).upper() in families

    @staticmethod
    def _mark_fingerprint_executed(registry: dict[str, dict[str, Any]], fingerprint: str, trade_id: str) -> None:
        if not fingerprint:
            return
        registry[fingerprint] = {
            "state": "active",
            "original_trade_id": trade_id,
            "cooldown_until": None,
        }

    @staticmethod
    def _mark_fingerprint_closed(
        registry: dict[str, dict[str, Any]],
        fingerprint: str,
        *,
        closed_at: datetime,
        cooldown_minutes: int,
        trade_id: str,
    ) -> None:
        if not fingerprint:
            return
        registry[fingerprint] = {
            "state": "cooldown",
            "original_trade_id": trade_id,
            "cooldown_until": (closed_at + timedelta(minutes=max(0, cooldown_minutes))).isoformat(),
        }

    @staticmethod
    def _trade_setup_family(trade: Any) -> str:
        return str(getattr(trade, "setup", "") or "")

    def _management_position_from_trade(self, trade: Any) -> dict[str, Any]:
        """Adapt a simulated trade into the shape expected by the risk manager."""
        position = dict(trade.position_state())
        position["direction"] = str(position.get("direction") or position.get("side") or "").upper()
        position["entry_price"] = float(position.get("entry_price", position.get("entry", 0.0)) or 0.0)
        position["sl"] = float(position.get("sl", 0.0) or 0.0)
        position["tp1"] = float(position.get("tp1", position.get("tp", 0.0)) or 0.0)
        position["tp2"] = float(position.get("tp", 0.0) or 0.0)
        position["volume"] = float(position.get("volume", 0.0) or 0.0)
        position["opened_at"] = str(position.get("opened_at") or position.get("open_time") or "")
        position["initial_risk_price"] = float(position.get("initial_risk_price") or abs(position["entry_price"] - position["sl"]) or 0.0)
        position["partial_closed"] = bool(position.get("partial_closed"))
        position["partial_closed_volume"] = float(position.get("partial_closed_volume", 0.0) or 0.0)
        position["partial_realized_pnl"] = float(position.get("partial_realized_pnl", 0.0) or 0.0)
        position["breakeven_moved"] = bool(position.get("breakeven_moved"))
        position["trailing_active"] = bool(position.get("trailing_active"))
        return position

    @staticmethod
    def _sync_trade_from_management_position(trade: Any, position: dict[str, Any]) -> None:
        """Persist mutable management state back onto the simulated trade."""
        for key in [
            "sl",
            "volume",
            "partial_closed",
            "partial_closed_volume",
            "partial_realized_pnl",
            "breakeven_moved",
            "trailing_active",
            "highest_price",
            "lowest_price",
            "mfe",
            "mae",
            "last_management_action",
            "close_reason",
            "last_structure_break_evaluation",
        ]:
            if key in position:
                setattr(trade, key, position[key])

    def _max_duration_minutes_for_trade(self, trade: Any) -> float:
        """Resolve a conservative config-driven max duration for one simulated trade."""
        exit_cfg = self.config.get("exit", {})
        default_minutes = float(exit_cfg.get("max_trade_duration_minutes", 45) or 45)
        setup_family = self._trade_setup_family(trade).upper()
        family_profiles = exit_cfg.get("family_profiles", {}) if isinstance(exit_cfg.get("family_profiles"), dict) else {}
        profile_key = {
            "BREAKOUT_RETEST_CONTINUATION": "breakout",
            "COMPRESSION_RELEASE": "compress",
            "LEBPRIM_SCALP": "lebprim",
        }.get(setup_family, "default")
        family_override = ((family_profiles.get(profile_key) or {}).get("max_trade_duration_minutes") if isinstance(family_profiles.get(profile_key), dict) else None)
        if family_override not in (None, ""):
            return float(family_override)
        return default_minutes

    def _current_exit_price(self, trade: Any, close_price: float, executor: SimulatedExecutor) -> float:
        """Return a conservative executable price for immediate backtest closes."""
        spread_price = float(executor.spread_points) * float(executor.point)
        slippage_price = float(executor.slippage_points) * float(executor.point)
        if str(getattr(trade, "side", "")).upper() in {"LONG", "BUY"}:
            return float(close_price) - spread_price - slippage_price
        return float(close_price) + spread_price + slippage_price

    def _open_exposures(self, executor: SimulatedExecutor, balance: float) -> list[ExposureSnapshot]:
        exposures: list[ExposureSnapshot] = []
        for trade in executor.open_trades:
            exposures.append(
                ExposureSnapshot(
                    position_id=str(getattr(trade, "open_time", "")),
                    symbol=str(getattr(trade, "symbol", "")),
                    side=str(getattr(trade, "side", "")),
                    setup_family=str(getattr(trade, "setup", "")),
                    strategy_family=strategy_family_for_setup(str(getattr(trade, "setup", ""))),
                    setup_fingerprint=str((getattr(trade, "metadata", {}) or {}).get("setup_fingerprint") or ""),
                    risk_pct=(float(getattr(trade, "risk_amount", 0.0) or 0.0) / max(float(balance), 1e-9)) * 100.0,
                    pending=False,
                    anchor_time=str((getattr(trade, "metadata", {}) or {}).get("anchor_time") or ""),
                    metadata=dict(getattr(trade, "metadata", {}) or {}),
                )
            )
        return exposures

    def _pending_exposures(self, executor: SimulatedExecutor, balance: float) -> list[ExposureSnapshot]:
        exposures: list[ExposureSnapshot] = []
        for order in executor.pending_orders:
            if getattr(order, "status", "") != "active":
                continue
            metadata = dict(getattr(order, "metadata", {}) or {})
            volume = float(metadata.get("volume", 0.0) or 0.0)
            entry_price = float(getattr(order, "pending_entry_price", 0.0) or 0.0)
            stop_loss = float(metadata.get("sl", entry_price) or entry_price)
            risk_amount = abs(entry_price - stop_loss) * volume * float(executor.contract_size)
            exposures.append(
                ExposureSnapshot(
                    position_id=str(getattr(order, "order_id", "")),
                    symbol=str(getattr(order, "symbol", "")),
                    side=str(getattr(order, "side", "")),
                    setup_family=str(getattr(order, "setup", "")),
                    strategy_family=strategy_family_for_setup(str(getattr(order, "setup", ""))),
                    setup_fingerprint=str(getattr(order, "setup_fingerprint", "")),
                    risk_pct=(risk_amount / max(float(balance), 1e-9)) * 100.0,
                    pending=True,
                    anchor_time=str((metadata.get("candidate") or {}).get("anchor_time") or ""),
                    metadata=metadata,
                )
            )
        return exposures

    def _store_pending_resolution_signal(
        self,
        *,
        run_id: int,
        ts_iso: str,
        event: dict[str, Any],
        blocked_reason: str,
        blocker_source: str,
    ) -> None:
        pending_meta = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        self.storage.store_signal(
            {
                "run_id": run_id,
                "signal_time": ts_iso,
                "strategy_name": event["strategy_name"],
                "symbol": event["symbol"],
                "side": event["side"],
                "setup": event["setup"],
                "entry": float(event.get("pending_entry_price") or 0.0),
                "sl": None,
                "tp": None,
                "score": float(((pending_meta.get("entry") or {}).get("entry_score")) or 0.0),
                "executed": False,
                "execution_reason": None,
                "blocked_reason": blocked_reason,
                "raw_signal_json": {
                    "candidate": pending_meta.get("candidate"),
                    "entry": pending_meta.get("entry"),
                    "market": pending_meta.get("market"),
                    "diagnostics": {
                        "setup_valid": bool((pending_meta.get("candidate") or {}).get("setup_valid")) if isinstance(pending_meta.get("candidate"), dict) else None,
                        "trigger_score": (pending_meta.get("entry") or {}).get("trigger_score") if isinstance(pending_meta.get("entry"), dict) else None,
                        "entry_score": (pending_meta.get("entry") or {}).get("entry_score") if isinstance(pending_meta.get("entry"), dict) else None,
                        "entry_mode": pending_meta.get("entry_mode"),
                        "pending_entry_price": pending_meta.get("pending_entry_price"),
                        "blocker_source": blocker_source,
                        "blocked_reason": blocked_reason,
                        "signal_status": "cancelled",
                    },
                    "pending_order": event,
                },
            }
        )

    def _log_trade_management_event(self, category: str, ts_iso: str, trade: Any, reason_code: str, extra: dict[str, Any] | None = None) -> None:
        payload = {
            "category": category,
            "timestamp": ts_iso,
            "side": str(getattr(trade, "side", "")),
            "entry": float(getattr(trade, "entry", 0.0) or 0.0),
            "sl": float(getattr(trade, "sl", 0.0) or 0.0),
            "tp": float(getattr(trade, "tp", 0.0) or 0.0),
            "setup_family": self._trade_setup_family(trade),
            "reason_code": reason_code,
        }
        if extra:
            payload.update(extra)
        self.logger.structured(category, payload)

    @staticmethod
    def _compact_candidate(candidate: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(candidate, dict):
            return {}
        return {
            "setup_family": str(candidate.get("setup_family") or ""),
            "side": str(candidate.get("side") or ""),
            "setup_score": float(candidate.get("setup_score") or 0.0),
            "trend_score": float(candidate.get("trend_score") or 0.0),
            "entry_score": float(candidate.get("entry_score") or 0.0),
            "entry_price": float(candidate.get("entry_price") or candidate.get("trigger_price") or 0.0),
            "setup_valid": bool(candidate.get("setup_valid")),
            "setup_fingerprint": str(candidate.get("setup_fingerprint") or ""),
            "entry_mode": str(candidate.get("entry_mode") or ""),
            "trigger_type": str(candidate.get("trigger_type") or ""),
        }

    def _compact_trade_positions(self, executor: SimulatedExecutor) -> list[dict[str, Any]]:
        return [
            {
                "position_id": str(getattr(trade, "open_time", "")),
                "strategy_name": str(getattr(trade, "strategy_name", "")),
                "side": str(getattr(trade, "side", "")),
                "setup": str(getattr(trade, "setup", "")),
                "entry": float(getattr(trade, "entry", 0.0) or 0.0),
                "sl": float(getattr(trade, "sl", 0.0) or 0.0),
                "tp": float(getattr(trade, "tp", 0.0) or 0.0),
                "volume": float(getattr(trade, "volume", 0.0) or 0.0),
            }
            for trade in executor.open_trades
            if not getattr(trade, "closed", False)
        ]

    def _compact_pending_orders(self, executor: SimulatedExecutor) -> list[dict[str, Any]]:
        return [
            {
                "order_id": str(getattr(order, "order_id", "")),
                "strategy_name": str(getattr(order, "strategy_name", "")),
                "side": str(getattr(order, "side", "")),
                "setup": str(getattr(order, "setup", "")),
                "entry_price": float(getattr(order, "pending_entry_price", 0.0) or 0.0),
                "trigger_price": float(getattr(order, "trigger_price", 0.0) or 0.0),
                "status": str(getattr(order, "status", "")),
            }
            for order in executor.pending_orders
            if str(getattr(order, "status", "")).lower() == "active"
        ]

    def _record_playback_frame(
        self,
        *,
        run_id: int,
        bar_index: int,
        bar_ts_iso: str,
        bar: dict[str, Any],
        balance: float,
        executor: SimulatedExecutor,
        candidate: dict[str, Any] | None,
        candidate_summary: dict[str, Any],
    ) -> None:
        floating = executor.floating_pnl(float(bar["close"]))
        self.storage.store_playback_frame(
            {
                "run_id": run_id,
                "bar_index": bar_index,
                "timestamp": bar_ts_iso,
                "open": float(bar.get("open", 0.0) or 0.0),
                "high": float(bar.get("high", 0.0) or 0.0),
                "low": float(bar.get("low", 0.0) or 0.0),
                "close": float(bar.get("close", 0.0) or 0.0),
                "volume": float(bar.get("real_volume", bar.get("tick_volume", 0.0)) or 0.0),
                "tick_volume": float(bar.get("tick_volume", 0.0) or 0.0),
                "spread": float(bar.get("spread", 0.0) or 0.0),
                "equity": float(balance + floating),
                "balance": float(balance),
                "floating_pnl": float(floating),
                "open_positions_json": self._compact_trade_positions(executor),
                "pending_orders_json": self._compact_pending_orders(executor),
                "selected_candidate_json": self._compact_candidate(candidate),
                "candidate_summary_json": candidate_summary,
                "state_json": {
                    "open_positions": len(executor.open_trades),
                    "pending_orders": len(executor.pending_orders),
                    "source_kind": candidate_summary.get("source_kind"),
                    "bar_index": bar_index,
                },
            }
        )
        self.logger.structured(
            "playback_frame_written",
            {
                "run_id": int(run_id),
                "bar_index": int(bar_index),
                "timestamp": bar_ts_iso,
                "source_kind": candidate_summary.get("source_kind"),
            },
        )

    def _record_playback_event(
        self,
        *,
        run_id: int,
        bar_index: int,
        timestamp: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        self.storage.store_playback_event(
            {
                "run_id": run_id,
                "bar_index": bar_index,
                "timestamp": timestamp,
                "event_type": event_type,
                "position_id": payload.get("position_id"),
                "signal_id": payload.get("signal_id"),
                "strategy_name": payload.get("strategy_name"),
                "setup_family": payload.get("setup_family"),
                "side": payload.get("side"),
                "price": payload.get("price"),
                "reason_code": payload.get("reason_code"),
                "event_payload_json": payload,
            }
        )
        self.logger.structured(
            "playback_event_written",
            {
                "run_id": int(run_id),
                "bar_index": int(bar_index),
                "timestamp": timestamp,
                "event_type": event_type,
                "reason_code": payload.get("reason_code"),
            },
        )

    def _trade_closed_event_payload(self, trade_row: dict[str, Any], *, source: str) -> dict[str, Any]:
        metadata = trade_row.get("trade_metadata_json") if isinstance(trade_row.get("trade_metadata_json"), dict) else {}
        return {
            "strategy_name": str(trade_row.get("strategy_name", "")),
            "setup_family": str(trade_row.get("setup_family") or metadata.get("setup_family") or metadata.get("setup") or ""),
            "side": str(trade_row.get("side", "")),
            "reason_code": str(trade_row.get("exit_reason_code") or trade_row.get("exit_reason") or "trade_closed"),
            "price": float(trade_row.get("exit_price") or 0.0),
            "position_id": str(trade_row.get("position_id") or trade_row.get("open_time") or ""),
            "pnl": float(trade_row.get("final_realized_pnl", trade_row.get("pnl", 0.0)) or 0.0),
            "realized_pnl_total": float(trade_row.get("realized_pnl_total", trade_row.get("pnl", 0.0)) or 0.0),
            "balance_after": trade_row.get("balance_after"),
            "close_reason": str(trade_row.get("exit_reason") or trade_row.get("exit_reason_code") or ""),
            "close_time": str(trade_row.get("close_time") or ""),
            "close_price": float(trade_row.get("exit_price") or 0.0),
            "remaining_volume_before_close": float(trade_row.get("volume_remaining_at_close", trade_row.get("volume", 0.0)) or 0.0),
            "bars_held": metadata.get("bars_held"),
            "duration_seconds": float(trade_row.get("duration_seconds", 0.0) or 0.0),
            "source": source,
        }

    def _persist_trade_close(
        self,
        *,
        run_id: int,
        bar_index: int,
        timestamp: str,
        trade_row: dict[str, Any],
        balance_before: float,
        equity_before: float,
        source: str,
        event_type: str = "trade_closed",
        extra_event_payload: dict[str, Any] | None = None,
    ) -> float:
        final_component = float(trade_row.get("final_realized_pnl", trade_row.get("pnl", 0.0)) or 0.0)
        trade_row["run_id"] = run_id
        trade_row["balance_before"] = float(balance_before)
        trade_row["balance_after"] = float(balance_before) + final_component
        trade_row["equity_before"] = float(equity_before)
        trade_row["equity_after"] = float(trade_row["balance_after"])
        payload = self._trade_closed_event_payload(trade_row, source=source)
        if extra_event_payload:
            payload.update(extra_event_payload)
        self._record_playback_event(
            run_id=run_id,
            bar_index=bar_index,
            timestamp=timestamp,
            event_type=event_type,
            payload=payload,
        )
        self.storage.store_trade(trade_row)
        return float(trade_row["balance_after"])

    def _build_run_payload(
        self,
        request: dict[str, Any],
        start_dt: datetime,
        end_dt: datetime,
        *,
        resolved_mode: dict[str, Any],
        selected_strategies: list[str],
    ) -> dict[str, Any]:
        return {
            "created_at": utc_now().isoformat(),
            "symbol": request["symbol"],
            "timeframe": request["timeframe"],
            "start_date": start_dt.isoformat(),
            "end_date": end_dt.isoformat(),
            "enabled_strategies_json": selected_strategies,
            "session_filter_json": request.get("session_filter", {}),
            "initial_balance": float(request.get("initial_balance", 10000.0)),
            "risk_percent": float(request.get("risk_percent", self.config.get("risk", {}).get("risk_percent", 0.5))),
            "spread_model": request.get("spread_model", {}),
            "slippage_model": request.get("slippage_model", {}),
            "execution_model": request.get(
                "execution_model",
                self.config.get("execution", {}).get("backtest_fill_model", self.config.get("backtest", {}).get("execution_model", "next_bar_open")),
            ),
            "notes": request.get("notes"),
            "metadata_json": {
                "requested_execution_mode": request.get("engine_mode"),
                "resolved_execution_mode": resolved_mode,
                "enabled_strategies_effective": list(selected_strategies),
            },
        }

    def ensure_historical_mode_ready(self, connector: MT5Connector) -> bool:
        """Attempt MT5 initialization, but keep the replay pipeline available if it fails."""
        if connector.initialize():
            self.logger.info("BACKTEST mode: MT5 historical bars available.")
            return True
        self.logger.warning("BACKTEST mode: MT5 initialization failed; will fall back to cache/CSV if available.")
        return False

    def _fetch_range_or_empty(
        self,
        connector: MT5Connector,
        timeframe_label: str,
        start_utc: datetime,
        end_utc: datetime,
        min_rows: int = 1,
    ) -> pd.DataFrame:
        """Fetch a historical range and degrade gracefully when the requested window has no bars."""
        try:
            return connector.fetch_rates_range(timeframe_label, start_utc, end_utc, min_rows=min_rows)
        except (RuntimeError, ValueError) as exc:
            message = str(exc)
            normalized = message.lower()
            known_no_data = (
                "bars returned in requested range" in normalized
                or "copy_rates_range failed: -2" in normalized
                or "copy_rates_range failed: -1" in normalized
                or "invalid params" in normalized
                or "empty historical window after fallback" in normalized
                or "invalid historical range" in normalized
            )
            if known_no_data:
                self.logger.warning(
                    f"BACKTEST historical range unavailable | timeframe={timeframe_label} | "
                    f"start={start_utc.isoformat()} | end={end_utc.isoformat()} | reason={message}"
                )
                return pd.DataFrame(columns=EMPTY_OHLC_COLUMNS)
            raise

    def run(
        self,
        request: dict[str, Any],
        *,
        on_run_created: Any | None = None,
        progress_callback: Any | None = None,
        should_interrupt: Any | None = None,
    ) -> dict[str, Any]:
        """Run a full historical backtest and return persisted summary bundle.
        
        Validates all request parameters before execution to prevent silent failures
        or garbage backtest results from malformed configuration.
        """
        # Validate and normalize input parameters
        self._validate_backtest_request(request)
        
        start_dt, end_dt = self._parse_date_range(request["start_date"], request.get("end_date"))
        requested_engine_mode = str(request.get("engine_mode") or self.config.get("bot", {}).get("execution_mode") or "")
        run_config = deepcopy(self.config)
        run_config, resolved_mode = apply_execution_mode_to_config(run_config, requested_engine_mode)
        config_overrides = dict(request.get("config_overrides") or {})
        if config_overrides:
            run_config = self._deep_merge_config(run_config, config_overrides)
        resolved_mode_summary = resolved_mode.to_dict()
        selected_strategies = list(request.get("enabled_strategies", []) or resolved_mode.enabled_strategy_names)
        
        # Validate that at least one family will be enabled
        if not selected_strategies:
            raise ValueError(
                "No enabled_strategies provided and execution mode has no default families. "
                "Request must explicitly enable at least one strategy family."
            )
        
        enabled_families = self._enabled_families(selected_strategies)
        if not enabled_families:
            raise ValueError(
                f"enabled_strategies {selected_strategies} did not resolve to any valid families. "
                f"Available families: {list(STRATEGY_REGISTRY.keys())}"
            )
        
        self.logger.info(
            f"BACKTEST replay started | symbol={str(request['symbol']).upper()} | timeframe={str(request.get('timeframe', 'M1')).upper()} | "
            f"start={start_dt.isoformat()} | end={end_dt.isoformat()} | strategies={selected_strategies}"
        )
        self.logger.structured(
            "backtest_mode_resolved",
            {
                "requested_mode": requested_engine_mode or resolved_mode_summary.get("selected_mode"),
                "resolved_mode": resolved_mode_summary,
                "symbol": str(request["symbol"]).upper(),
                "timeframe": str(request.get("timeframe", "M1")).upper(),
            },
        )
        run_id = self.storage.create_run(
            self._build_run_payload(
                request,
                start_dt,
                end_dt,
                resolved_mode=resolved_mode_summary,
                selected_strategies=selected_strategies,
            )
        )
        if callable(on_run_created):
            on_run_created(run_id)
        self.database.update_backtest_run_state(run_id, JobState.RUNNING.value)
        symbol = str(request["symbol"]).upper()
        run_risk_percent = float(request.get("risk_percent", self.config.get("risk", {}).get("risk_percent", 0.5)))
        self.strategy = build_strategy_engine(run_config, self.base_dir)
        self._apply_backtest_strategy_selection(run_config, selected_strategies)
        self._refresh_strategy_config(run_config)
        self.logger.structured(
            "engine_selection",
            {
                "selected_mode": run_config.get("bot", {}).get("execution_mode"),
                "engine_type": getattr(self.strategy, "engine_type", run_config.get("bot", {}).get("execution_mode_summary", {}).get("engine_type")),
                "strategy_class": self.strategy.__class__.__name__,
                "enabled_strategy_families": run_config.get("bot", {}).get("execution_mode_summary", {}).get("enabled_families", []),
                "selected_strategy_families": sorted(enabled_families),
                "source": "backtest",
            },
        )
        run_config.setdefault("risk", {})
        run_config["risk"]["risk_percent"] = run_risk_percent
        risk_manager = RiskManager(run_config, logger=self.logger)
        risk_engine = RiskEngine(run_config, risk_manager, logger=self.logger)
        self.logger.structured(
            "backtest_strategy_selection_applied",
            {
                "selected_strategies": list(selected_strategies),
                "enabled_families": sorted(enabled_families),
                "selection_overrides_live_config": True,
            },
        )
        session_filter = request.get("session_filter", {}) or {}
        session_filter_enabled = bool(session_filter.get("enabled", False))
        allowed_sessions = {str(item).upper() for item in session_filter.get("allowed_sessions", [])}
        timeframe = str(request.get("timeframe", "M1")).upper()
        trigger_tf = TIMEFRAME_ALIAS.get(timeframe, "1min")
        setup_tf = str(self.config["timeframes"]["setup"])
        trend_tf = str(self.config["timeframes"]["trend"])
        # Pre-compute bar durations used for look-ahead-free slicing inside the loop.
        # A higher-TF bar opened at T is only considered closed (and therefore visible)
        # once the current trigger bar's open time is >= T + bar_duration.
        _setup_bar_duration = pd.Timedelta(setup_tf)
        _trend_bar_duration = pd.Timedelta(trend_tf)
        warmup_bars = max(100, int(self.config.get("backtest", {}).get("warmup_bars", 300)))
        warmup_start = start_dt - timedelta(minutes=warmup_bars * 2)

        connector_cfg = dict(self.config)
        connector_cfg["mt5"] = dict(self.config["mt5"])
        connector_cfg["mt5"]["symbol"] = symbol
        connector = MT5Connector(connector_cfg, self.logger)

        connector_ready = self.ensure_historical_mode_ready(connector)
        try:
            history_resolution = self.history_loader.resolve_bar_bundle(
                connector=connector if connector_ready else None,
                symbol=symbol,
                timeframe_requests={
                    trend_tf: (warmup_start, end_dt, 1),
                    setup_tf: (warmup_start, end_dt, 1),
                    trigger_tf: (warmup_start, end_dt, 1),
                },
            )
            trend_raw = history_resolution.bar_frames[trend_tf]
            setup_raw = history_resolution.bar_frames[setup_tf]
            trigger_raw = history_resolution.bar_frames[trigger_tf]
            if trend_raw.empty or setup_raw.empty or trigger_raw.empty:
                self.storage.store_equity(
                    {"run_id": run_id, "ts": start_dt.isoformat(), "equity": float(request.get("initial_balance", 10000.0)), "balance": float(request.get("initial_balance", 10000.0)), "floating_pnl": 0.0}
                )
                self.storage.store_equity(
                    {"run_id": run_id, "ts": end_dt.isoformat(), "equity": float(request.get("initial_balance", 10000.0)), "balance": float(request.get("initial_balance", 10000.0)), "floating_pnl": 0.0}
                )
                self.logger.warning(
                    "BACKTEST completed with no historical bars in requested range; returning empty signal/trade set."
                )
                self.database.update_backtest_run_state(run_id, JobState.COMPLETED.value, progress_current=1, progress_total=1)
                return self.storage.get_run_bundle(run_id)
            trend_df = self.strategy.prepare_trend_dataframe(trend_raw)
            setup_df = self.strategy.prepare_setup_dataframe(setup_raw)
            trigger_df = self.strategy.prepare_trigger_dataframe(trigger_raw)
            replay_trigger = trigger_raw[(trigger_raw["time"] >= start_dt) & (trigger_raw["time"] <= end_dt)].copy()
            effective_first_bar = to_utc(trigger_raw["time"].min()) if not trigger_raw.empty else None
            effective_last_bar = to_utc(trigger_raw["time"].max()) if not trigger_raw.empty else None
            effective_replay_first = to_utc(replay_trigger["time"].min()) if not replay_trigger.empty else effective_first_bar
            effective_replay_last = to_utc(replay_trigger["time"].max()) if not replay_trigger.empty else effective_last_bar
            if replay_trigger.empty:
                self.storage.store_equity(
                    {"run_id": run_id, "ts": start_dt.isoformat(), "equity": float(request.get("initial_balance", 10000.0)), "balance": float(request.get("initial_balance", 10000.0)), "floating_pnl": 0.0}
                )
                self.storage.store_equity(
                    {"run_id": run_id, "ts": end_dt.isoformat(), "equity": float(request.get("initial_balance", 10000.0)), "balance": float(request.get("initial_balance", 10000.0)), "floating_pnl": 0.0}
                )
                self.logger.warning("BACKTEST completed with zero trigger candles in requested replay window.")
                self.database.update_backtest_run_state(run_id, JobState.COMPLETED.value, progress_current=1, progress_total=1)
                return self.storage.get_run_bundle(run_id)
            self.database.update_backtest_run(
                run_id,
                {
                    "metadata_json": {
                        "requested_execution_mode": requested_engine_mode or resolved_mode_summary.get("selected_mode"),
                        "resolved_execution_mode": resolved_mode_summary,
                        "enabled_strategies_effective": list(selected_strategies),
                        "history_resolution": history_resolution.source_details,
                        "requested_start": start_dt.isoformat(),
                        "requested_end": end_dt.isoformat(),
                        "effective_data_start": effective_replay_first.isoformat() if effective_replay_first else None,
                        "effective_data_end": effective_replay_last.isoformat() if effective_replay_last else None,
                        "first_bar_time": effective_first_bar.isoformat() if effective_first_bar else None,
                        "last_bar_time": effective_last_bar.isoformat() if effective_last_bar else None,
                        "data_source": history_resolution.source_kind,
                        "data_truncated_reason": (
                            "requested_end_after_available_data"
                            if effective_replay_last is not None and effective_replay_last < end_dt
                            else None
                        ),
                    }
                },
            )

            clock = ReplayClock(HistoricalMT5Feed(replay_trigger), len(replay_trigger))
            market_data = ReplayMarketData(
                trend_df=trend_df,
                setup_df=setup_df,
                trigger_df=trigger_df,
                setup_tf=setup_tf,
                trend_tf=trend_tf,
            )
            symbol_spec = connector.get_symbol_spec()
            spread_model = request.get("spread_model") or self.config.get("backtest", {}).get("spread_model") or {}
            slippage_model = request.get("slippage_model") or self.config.get("backtest", {}).get("slippage_model") or {}
            _config_exec_model = str(
                self.config.get("execution", {}).get(
                    "backtest_fill_model",
                    self.config.get("backtest", {}).get("execution_model", "next_bar_open"),
                )
            )
            execution_model = str(request.get("execution_model") or _config_exec_model).lower()
            commission_per_lot = float(
                request.get("commission_per_lot")
                if request.get("commission_per_lot") is not None
                else self.config.get("backtest", {}).get("commission_per_lot", 0.0)
            )
            execution_router = ExecutionDecisionEngine(run_config)
            exposure_policy = ExposurePolicy(run_config)
            pending_policy = PendingOrderPolicy(run_config)
            executor = SimulatedExecutor(
                spread_points=self._spread_points(spread_model, {"spread": 0}),
                slippage_points=self._slippage_points(slippage_model),
                point=float(symbol_spec["point"]),
                contract_size=float(symbol_spec["contract_size"]),
                same_bar_rule=str(self.config.get("backtest", {}).get("same_bar_sl_tp_rule", "sl_first")),
                allow_pyramiding=bool(run_config.get("execution", {}).get("allow_multi_position", run_config.get("execution", {}).get("allow_pyramiding", False))),
                commission_per_lot=commission_per_lot,
            )
            execution_service = ExecutionService(ReplayBroker(executor))
            leverage = float(request.get("leverage", self.config.get("backtest", {}).get("leverage", 100.0)) or 100.0)
            protection_cfg = dict(run_config.get("risk", {}).get("backtest_account_protection", {}) or {})
            weekend_cfg = dict(run_config.get("risk", {}).get("weekend_protection", {}) or {})
            fingerprint_guard_cfg = dict(run_config.get("execution", {}).get("setup_fingerprint_guard", {}) or {})
            friday_hard_close = self._clock_time_utc(weekend_cfg.get("friday_hard_close_time_utc", "21:00"), "21:00")
            friday_entry_cutoff = self._clock_time_utc(weekend_cfg.get("block_new_entries_after_utc", "20:30"), "20:30")
            session = ReplaySession(
                self,
                ReplayContext(
                    request=request,
                    run_config=run_config,
                    run_id=run_id,
                    symbol=symbol,
                    timeframe=timeframe,
                    start_dt=start_dt,
                    end_dt=end_dt,
                    requested_engine_mode=requested_engine_mode,
                    resolved_mode_summary=resolved_mode_summary,
                    selected_strategies=selected_strategies,
                    enabled_families=enabled_families,
                    history_resolution=history_resolution,
                    effective_first_bar=effective_first_bar,
                    effective_last_bar=effective_last_bar,
                    effective_replay_first=effective_replay_first,
                    effective_replay_last=effective_replay_last,
                    clock=clock,
                    market_data=market_data,
                    symbol_spec=symbol_spec,
                    spread_model=spread_model,
                    slippage_model=slippage_model,
                    execution_model=execution_model,
                    execution_router=execution_router,
                    exposure_policy=exposure_policy,
                    pending_policy=pending_policy,
                    executor=executor,
                    execution_service=execution_service,
                    risk_manager=risk_manager,
                    risk_engine=risk_engine,
                    protection_cfg=protection_cfg,
                    weekend_cfg=weekend_cfg,
                    fingerprint_guard_cfg=fingerprint_guard_cfg,
                    friday_hard_close=friday_hard_close,
                    friday_entry_cutoff=friday_entry_cutoff,
                    session_filter_enabled=session_filter_enabled,
                    allowed_sessions=allowed_sessions,
                ),
            )
            return session.execute(progress_callback=progress_callback, should_interrupt=should_interrupt)
        except InterruptedError as exc:
            self.database.update_backtest_run_state(run_id, JobState.INTERRUPTED.value, interrupted_reason=str(exc))
            raise
        except KeyboardInterrupt as exc:
            self.database.update_backtest_run_state(run_id, JobState.INTERRUPTED.value, interrupted_reason=str(exc))
            raise
        except Exception as exc:
            self.database.update_backtest_run_state(run_id, JobState.FAILED.value, failure_reason=str(exc))
            self.logger.structured(
                "backtest_run_failed",
                {"run_id": run_id, "symbol": symbol, "timeframe": timeframe, "error": str(exc)},
            )
            raise
        finally:
            connector.shutdown()

    def run_matrix(self, base_request: dict[str, Any], variants: list[str] | None = None) -> dict[str, Any]:
        """Run a controlled backtest matrix without editing config files."""

        all_variants: dict[str, dict[str, Any]] = {
            "A": {
                "execution_model": "current_bar_close",
                "config_overrides": {"exit": {"structure_break_exit": {"enabled": True, "min_bars_after_entry": 0, "require_consecutive_closes": 1}}},
            },
            "B": {
                "execution_model": "next_bar_open",
                "config_overrides": {"exit": {"structure_break_exit": {"enabled": True, "min_bars_after_entry": 0, "require_consecutive_closes": 1}}},
            },
            "C": {
                "execution_model": "current_bar_close",
                "config_overrides": {"exit": {"structure_break_exit": {"enabled": True, "min_bars_after_entry": 5, "require_consecutive_closes": 2}}},
            },
            "D": {
                "execution_model": "next_bar_open",
                "config_overrides": {"exit": {"structure_break_exit": {"enabled": True, "min_bars_after_entry": 5, "require_consecutive_closes": 2}}},
            },
            "E": {
                "execution_model": "next_bar_open",
                "config_overrides": {"exit": {"structure_break_exit": {"enabled": True, "min_bars_after_entry": 10, "require_consecutive_closes": 2}}},
            },
            "F": {
                "execution_model": "next_bar_open",
                "config_overrides": {"exit": {"structure_break_exit": {"enabled": False}}},
            },
        }
        selected = [item for item in (variants or ["A", "B", "C", "D", "E"]) if item in all_variants]
        results: list[dict[str, Any]] = []
        for variant_name in selected:
            request = deepcopy(base_request)
            override = deepcopy(all_variants[variant_name])
            request["execution_model"] = override["execution_model"]
            request["config_overrides"] = self._deep_merge_config(dict(request.get("config_overrides") or {}), override.get("config_overrides") or {})
            request["notes"] = f"{request.get('notes', '')} | matrix_variant={variant_name}".strip(" |")
            bundle = self.run(request)
            summary = dict(bundle.get("summary") or {})
            trades = bundle.get("trades") or []
            durations = sorted(float(row.get("duration_seconds", 0.0) or 0.0) for row in trades)
            median_duration = durations[len(durations) // 2] if durations else 0.0
            tp1_hits = sum(1 for row in trades if float(row.get("partial_realized_pnl", 0.0) or 0.0) > 0.0)
            results.append(
                {
                    "variant": variant_name,
                    "run_id": bundle.get("run", {}).get("id"),
                    "execution_model": request["execution_model"],
                    "structure_break_min_bars_after_entry": ((request.get("config_overrides") or {}).get("exit") or {}).get("structure_break_exit", {}).get("min_bars_after_entry", 0),
                    "structure_break_require_consecutive_closes": ((request.get("config_overrides") or {}).get("exit") or {}).get("structure_break_exit", {}).get("require_consecutive_closes", 1),
                    "structure_break_exit_enabled": ((request.get("config_overrides") or {}).get("exit") or {}).get("structure_break_exit", {}).get("enabled", True),
                    "total_trades": summary.get("total_trades", 0),
                    "win_rate": summary.get("win_rate", 0.0),
                    "PF": summary.get("profit_factor", 0.0),
                    "net_PnL": summary.get("net_profit", 0.0),
                    "max_drawdown": summary.get("max_drawdown", 0.0),
                    "TP1_reach_rate": (tp1_hits / max(len(trades), 1) * 100.0) if trades else 0.0,
                    "structure_break_exit_count": summary.get("structure_break_exits", 0),
                    "median_duration": median_duration,
                    "avg_R": summary.get("average_r", 0.0),
                    "account_status": summary.get("account_status", "ACTIVE"),
                    "reconciliation_status": summary.get("reconciliation_status", "unknown"),
                }
            )
        return {"variants": results}
