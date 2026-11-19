"""Main runner for the regime-aware MT5 XAUUSD scalping bot."""

from __future__ import annotations

import copy
import csv
import json
import os
import signal
import sys
import time
import traceback
import argparse
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Callable


from logger import BotLogger
from mt5_connector import MT5Connector
from risk_manager import RiskManager
from dashboard.services import DashboardDataService
from services.audit_service import AuditService
from services.bot_control_service import BotControlService
from services.config_manager import ConfigManager
from services.database import DatabaseService
from services.control_state import ControlStateService
from services.heartbeat import HeartbeatService
from services.instance_lock import InstanceLock, pid_is_alive
from services.lebprime_live import LebPrimeLiveService
from services.manual_trade_service import ManualTradeService
from services.operator_service import OperatorService
from services.trade_journal import TradeJournalService
from services.state_manager import StateManager
from services.telegram_notifier import TelegramNotifier
from services.telegram_command_service import TelegramCommandService
from trading_bot.core.sessions import classify_session
from trading_bot.execution.execution_service import ExecutionService
from trading_bot.execution.mt5_broker import MT5Broker
from trading_bot.positions.live_lifecycle import LivePositionLifecycleManager
from trading_bot.analytics.correlation_monitor import StrategyCorrelationMonitor
from trading_bot.analytics.risk_metrics import RMultipleCalculator, TradeRiskMetadata
from trading_bot.analytics.strategy_logger import StrategyDecisionLogger
from trading_bot.risk.engine import RiskEngine
from trading_bot.risk.position_sizing import DynamicPositionSizer, PositionSizeConfig
from trading_bot.storage.repositories.journal import JournalRepository
from utils import load_json, load_runtime_config, next_cycle_sleep_seconds, normalize_trading_mode, price_to_pips, to_utc, utc_now


class TradingBot:
    """Production-grade MT5 trading bot with explicit strategy, risk, and journaling layers."""

    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        # The persisted config is the source of truth for runtime mode.
        # Launcher environment variables may still exist for bootstrap, but they
        # should not override a mode chosen from the dashboard or Telegram.
        raw_config = load_json(self.base_dir / "config.json", {})
        self.config = load_runtime_config(base_dir, require_mt5_credentials=True, apply_mode_env_override=False)
        self._boot_raw_disk_mode = (((raw_config or {}).get("bot") or {}).get("trading_mode"))
        self._boot_bot_config = dict(self.config.get("bot", {}))
        self._boot_mode_snapshot = normalize_trading_mode(self._boot_bot_config.get("trading_mode", "DRY_RUN"))
        self.instance_lock_path = self.base_dir / "storage" / "bot_instance.lock"
        self.instance_lock_acquired = False
        self._instance_lock = InstanceLock(self.instance_lock_path, self.base_dir)
        self.control_state = ControlStateService(self.base_dir)
        self._boot_control_state_runtime_mode = ((self.control_state.load().get("runtime") or {}).get("mode"))
        self._sync_runtime_mode_from_control_state(log_change=False)
        self.logger = BotLogger(self.base_dir, self.config)
        self.logger.structured(
            "startup_config_loaded",
            {
                "raw_disk_mode": self._boot_raw_disk_mode,
                "raw_mode": self._boot_bot_config.get("trading_mode"),
                "normalized_mode": self._boot_mode_snapshot,
                "allow_live_execution": bool(self._boot_bot_config.get("allow_live_execution", False)),
                "dry_run": bool(self._boot_bot_config.get("dry_run", False)),
                "env_trading_mode_present": bool(os.getenv("TRADING_MODE") or os.getenv("BOT_TRADING_MODE")),
                "apply_mode_env_override": False,
            },
            level="INFO",
        )
        self.logger.structured(
            "startup_control_state_mode_sync",
            {
                "before": self._boot_control_state_runtime_mode,
                "after": ((self.control_state.load().get("runtime") or {}).get("mode")),
                "effective_mode": self._boot_mode_snapshot,
            },
            level="INFO",
        )
        self.state_manager = StateManager(
            base_dir=self.base_dir,
            relative_path=str(self.config["storage"]["state_path"]),
            logger=self.logger,
            retry_count=int(self.config["storage"].get("state_save_retry_count", 3)),
            retry_delay_seconds=float(self.config["storage"].get("state_save_retry_delay_seconds", 0.35)),
        )
        self.state = self.state_manager.load_state()
        self.database = DatabaseService(self.base_dir / self.config["storage"]["database_path"])
        self.heartbeat_service = HeartbeatService(self.database)
        self.trade_journal = TradeJournalService(self.base_dir, self.database, self.logger, self.config)
        self.state_manager.logger = self.logger
        self.config_manager = ConfigManager(self.base_dir)
        self.audit_service = AuditService(self.database, self.base_dir)
        self.dashboard_data = DashboardDataService(self.base_dir, self.database)
        self.bot_control_service = BotControlService(self.base_dir, self.config_manager, self.control_state, self.audit_service)
        self.operator_service = OperatorService(self.base_dir, self.config_manager, self.audit_service, self.bot_control_service)
        self.manual_trade_service = ManualTradeService(self.base_dir, self.config_manager, self.control_state, self.audit_service)
        self.telegram_command_service = TelegramCommandService(
            self.base_dir,
            self.config_manager,
            self.control_state,
            self.dashboard_data,
            self.audit_service,
            self.operator_service,
            self.manual_trade_service,
        )
        if self.config.get("bot", {}) != self._boot_bot_config:
            self.config["bot"] = dict(self._boot_bot_config)
            self.logger.structured(
                "startup_config_restored",
                {
                    "restored_mode": self._boot_mode_snapshot,
                    "config_mode_after_restore": self.config["bot"].get("trading_mode"),
                },
                level="INFO",
            )
        self.notifier = TelegramNotifier(
            token=self.config["telegram"].get("bot_token"),
            chat_id=self.config["telegram"].get("chat_id"),
            timeout_seconds=int(self.config["telegram"].get("timeout_seconds", 5)),
            enabled=bool(self.config["telegram"].get("enabled", False)),
        )
        self.connector = MT5Connector(self.config, self.logger)
        self.execution_service = ExecutionService(MT5Broker(self.connector))
        analytics_cfg = self.config.get("analytics", {})
        decision_cfg = analytics_cfg.get("decision_logging", {})
        corr_cfg = analytics_cfg.get("correlation_monitoring", {})
        self.decision_logger = (
            StrategyDecisionLogger(log_dir=str(self.base_dir / str(decision_cfg.get("log_dir", "logs/strategy_decisions"))))
            if bool(decision_cfg.get("enabled", False))
            else None
        )
        self.r_calculator = RMultipleCalculator()
        self.correlation_monitor = (
            StrategyCorrelationMonitor(
                correlation_threshold=float(corr_cfg.get("correlation_threshold", 0.7)),
                concentration_threshold=float(corr_cfg.get("concentration_threshold", 0.8)),
            )
            if bool(corr_cfg.get("enabled", False))
            else None
        )
        sizing_cfg = self.config.get("position_sizing", {})
        self.dynamic_position_sizer = (
            DynamicPositionSizer(
                PositionSizeConfig(
                    base_risk_percent=float(sizing_cfg.get("base_risk_percent", self.config.get("risk", {}).get("risk_percent", 1.0))),
                    max_risk_percent=float(sizing_cfg.get("max_risk_percent", 2.0)),
                    min_risk_percent=float(sizing_cfg.get("min_risk_percent", 0.25)),
                    regime_multipliers=dict(sizing_cfg.get("regime_multipliers", {})) or PositionSizeConfig().regime_multipliers,
                )
            )
            if bool(sizing_cfg.get("dynamic_enabled", False))
            else None
        )
        self.risk_manager = RiskManager(self.config, logger=self.logger)
        self.risk_engine = RiskEngine(self.config, self.risk_manager, logger=self.logger)
        self.lebprime_v3_4_0 = LebPrimeLiveService(
            self.config,
            self.database.db_path,
            logger=self.logger,
            connector_factory=self._build_lebprime_connector,
            risk_manager=self.risk_manager,
            risk_state=self.state,
        )
        self.journal_repository = JournalRepository(self.logger, self.trade_journal)
        self.live_lifecycle = LivePositionLifecycleManager(
            config=self.config,
            state=self.state,
            connector=self.connector,
            execution_service=self.execution_service,
            risk_manager=self.risk_manager,
            trade_journal=self.trade_journal,
            logger=self.logger,
        )
        self.running = True
        self.execution_paused = False
        self.auto_execution_enabled = True
        self.signal_generation_enabled = True
        self.trading_mode = "DRY_RUN"
        self.demo_mode = False
        self.dry_run = True
        self.test_mode = False
        self.backtest_mode = False
        self._refresh_mode_flags()
        self.logger.structured(
            "startup_effective_mode",
            {
                "effective_mode": self.trading_mode,
                "allow_live_execution": bool(self.config["bot"].get("allow_live_execution")),
                "dry_run": bool(self.config["bot"].get("dry_run")),
                "config_mode": self.config["bot"].get("trading_mode"),
                "execution_mode": self.config.get("bot", {}).get("execution_mode_summary", {}),
                "resolved_runtime": self.config_manager.resolved_runtime_snapshot(self.config),
            },
            level="INFO",
        )
        self.logger.structured(
            "startup_source_of_truth",
            self.config_manager.source_of_truth_snapshot(
                runtime_state=self.control_state.summarize(),
                process_runtime={
                    "pid": os.getpid(),
                    "mode": self.trading_mode,
                    "database_path": str(self.config["storage"].get("database_path", "")),
                },
            ),
            level="INFO",
        )
        self.logger.structured(
            "runtime_policy_resolved",
            {
                "source": "startup",
                "execution_mode": self.config.get("bot", {}).get("execution_mode_summary", {}),
                "resolved_runtime": self.config_manager.resolved_runtime_snapshot(self.config),
            },
            level="INFO",
        )
        self._acquire_instance_lock()

    def _build_lebprime_connector(self, mapping: Any) -> MT5Connector:
        """Build a symbol-bound connector while sharing the existing XAU connection."""
        if str(self.connector.symbol).upper() == str(mapping.mt5_symbol).upper():
            return self.connector
        symbol_config = copy.deepcopy(self.config)
        symbol_config.setdefault("mt5", {})["symbol"] = mapping.mt5_symbol
        return MT5Connector(symbol_config, self.logger)

    def _acquire_instance_lock(self) -> None:
        """Ensure only one bot process runs at a time."""
        self._instance_lock.acquire()
        self.instance_lock_acquired = True

    def _sync_runtime_mode_from_control_state(self, log_change: bool = True) -> str:
        """Keep the cached runtime mode aligned with the shared control state."""
        control = self.control_state.summarize()
        runtime = control.get("runtime", {}) if isinstance(control.get("runtime", {}), dict) else {}
        control_is_active = bool(control.get("process_alive")) or bool(control.get("bot_running"))
        config_mode = normalize_trading_mode(self.config["bot"].get("trading_mode", "DRY_RUN"))
        runtime_mode = normalize_trading_mode(runtime.get("mode")) if runtime.get("mode") else None
        normalized_mode = config_mode
        previous_mode = getattr(self, "trading_mode", None)
        if runtime_mode and runtime_mode != normalized_mode and hasattr(self, "logger"):
            self.logger.structured(
                "runtime_mode_control_state_mismatch",
                {
                    "config_mode": config_mode,
                    "control_state_mode": runtime_mode,
                    "control_state_active": control_is_active,
                    "boot_mode_snapshot": getattr(self, "_boot_mode_snapshot", None),
                },
                level="INFO",
            )
        self.config["bot"]["allow_live_execution"] = normalized_mode == "LIVE"
        self.config["bot"]["dry_run"] = normalized_mode == "DRY_RUN"
        self.config["bot"]["demo_mode"] = normalized_mode == "DEMO"
        self.config["bot"]["test_mode"] = normalized_mode == "VALIDATION_TEST"
        self.config["bot"]["backtest_mode"] = normalized_mode == "BACKTEST"
        self.trading_mode = normalized_mode
        self.demo_mode = normalized_mode == "DEMO"
        self.dry_run = normalized_mode == "DRY_RUN"
        self.test_mode = normalized_mode == "VALIDATION_TEST"
        self.backtest_mode = normalized_mode == "BACKTEST"
        if control.get("runtime", {}).get("mode") != normalized_mode:
            self.control_state.update({"runtime": {"mode": normalized_mode}})
            if hasattr(self, "logger"):
                self.logger.structured(
                    "startup_control_state_runtime_mode_applied",
                    {
                        "applied_mode": normalized_mode,
                        "previous_control_state_mode": runtime.get("mode"),
                        "control_state_active": control_is_active,
                    },
                    level="INFO",
                )
        if log_change and previous_mode not in {None, normalized_mode} and hasattr(self, "logger"):
            self.logger.structured(
                "runtime_mode_sync",
                {
                    "previous_mode": previous_mode,
                    "applied_mode": normalized_mode,
                    "control_state_mode": runtime.get("mode"),
                    "control_state_active": control_is_active,
                    "config_mode": self.config["bot"].get("trading_mode"),
                },
                level="INFO",
            )
        return normalized_mode

    @staticmethod
    def _pid_is_alive(pid: int) -> bool:
        """Return whether a pid still exists."""
        return pid_is_alive(pid)

    def _release_instance_lock(self) -> None:
        """Release the owned single-instance lock on shutdown."""
        self._instance_lock.release()
        self.instance_lock_acquired = False

    def _apply_runtime_updates(self) -> None:
        """Reload config and control flags so dashboard changes can take effect without restart."""
        control = self.control_state.load()
        previous_mode = self.trading_mode
        self.execution_paused = bool(control.get("execution_paused", False))
        self.auto_execution_enabled = bool(control.get("auto_execution_enabled", True))
        self.signal_generation_enabled = bool(control.get("signal_generation_enabled", True))
        if bool(control.get("kill_switch", False)):
            self.state.setdefault("locks", {})["kill_switch"] = True
            self.state["locks"]["kill_switch_reason"] = str(control.get("kill_switch_reason") or "dashboard_kill_switch")
        elif self.state.get("locks", {}).get("kill_switch_reason") == "dashboard_kill_switch":
            self.state["locks"]["kill_switch"] = False
            self.state["locks"]["kill_switch_reason"] = None
        if bool(control.get("request_shutdown", False)):
            self.running = False
            self.state["shutdown_reason"] = "dashboard_stop_requested"

        if bool(control.get("request_reload_config", False)):
            self.logger.structured(
                "config_reload_requested",
                {
                    "request_reload_config": True,
                    "control_state_mode": (control.get("runtime", {}) or {}).get("mode"),
                    "config_mode": self.config.get("bot", {}).get("trading_mode"),
                },
                level="INFO",
            )
            try:
                refreshed = load_runtime_config(self.base_dir, require_mt5_credentials=False, apply_mode_env_override=False)
                # Only apply changes if loading succeeded
                self.config = refreshed
                self.logger.config = refreshed
                self.connector.config = refreshed
                self.connector.mt5_config = refreshed["mt5"]
                self.execution_service = ExecutionService(MT5Broker(self.connector))
                lebprime_live = getattr(self, "lebprime_v3_4_0", None)
                if lebprime_live is not None:
                    lebprime_live.refresh_config(refreshed)
            except Exception as exc:
                # Config reload failed - keep previous working config and strategy
                self.logger.structured(
                    "config_reload_failed_using_fallback",
                    {
                        "error": str(exc),
                        "error_type": exc.__class__.__name__,
                        "fallback_to_previous": True,
                        "previous_mode": self.config.get("bot", {}).get("execution_mode"),
                    },
                    level="ERROR",
                )
                # Mark control state to clear reload request since we handled (even with fallback)
                control["request_reload_config"] = False
                self.control_state.update({"request_reload_config": False})

            self.risk_manager.config = refreshed
            self.risk_engine.config = refreshed
            self.live_lifecycle.refresh(
                config=self.config,
                state=self.state,
                connector=self.connector,
                execution_service=self.execution_service,
                risk_manager=self.risk_manager,
                trade_journal=self.trade_journal,
                logger=self.logger,
            )
            self.notifier.token = str(refreshed["telegram"].get("bot_token", "") or "")
            self.notifier.chat_id = str(refreshed["telegram"].get("chat_id", "") or "")
            self.notifier.base_url = f"https://api.telegram.org/bot{self.notifier.token}/sendMessage" if self.notifier.token else ""
            self.notifier.enabled = bool(refreshed["telegram"].get("enabled", False)) and bool(self.notifier.token and self.notifier.chat_id)
            self.notifier.timeout_seconds = int(refreshed["telegram"].get("timeout_seconds", 5))
            self.control_state.update({"request_reload_config": False})
            self._sync_runtime_mode_from_control_state(log_change=False)
            self._refresh_mode_flags()
            self.logger.structured(
                "config_reload_applied",
                {
                    "applied_mode": self.trading_mode,
                    "config_mode": self.config["bot"].get("trading_mode"),
                    "allow_live_execution": bool(self.config["bot"].get("allow_live_execution")),
                    "dry_run": bool(self.config["bot"].get("dry_run")),
                    "execution_paused": self.execution_paused,
                    "auto_execution_enabled": self.auto_execution_enabled,
                    "signal_generation_enabled": self.signal_generation_enabled,
                    "execution_mode": self.config.get("bot", {}).get("execution_mode_summary", {}),
                },
                level="INFO",
            )
        else:
            self._sync_runtime_mode_from_control_state(log_change=False)
            if previous_mode != self.trading_mode and hasattr(self, "logger"):
                self.logger.structured(
                    "mode_switch_applied",
                    {
                        "previous_mode": previous_mode,
                        "applied_mode": self.trading_mode,
                        "execution_paused": self.execution_paused,
                        "auto_execution_enabled": self.auto_execution_enabled,
                        "signal_generation_enabled": self.signal_generation_enabled,
                    },
                    level="INFO",
                )

    def _refresh_mode_flags(self) -> None:
        """Refresh execution-mode compatibility flags from the canonical trading mode."""
        trading_mode = normalize_trading_mode(self.config["bot"].get("trading_mode", "DRY_RUN"))
        self.config["bot"]["trading_mode"] = trading_mode
        self.trading_mode = trading_mode
        self.demo_mode = trading_mode == "DEMO"
        self.dry_run = trading_mode == "DRY_RUN"
        self.test_mode = trading_mode == "VALIDATION_TEST"
        self.backtest_mode = trading_mode == "BACKTEST"

    def _execution_mode_summary(self) -> dict[str, Any]:
        """Return the current operating mode and rollout details."""
        self._refresh_mode_flags()
        rollout_phase = int(self.config["bot"].get("rollout_phase", 1))
        mode = self.trading_mode
        allow_live_execution = mode == "LIVE"
        force_reduced_risk = bool(self.config["bot"].get("force_reduced_risk_mode", mode != "LIVE"))
        return {
            "mode": mode,
            "configured_trading_mode": mode,
            "rollout_phase": rollout_phase,
            "demo_mode": self.demo_mode,
            "dry_run": self.dry_run,
            "test_mode": self.test_mode,
            "backtest_mode": self.backtest_mode,
            "allow_live_execution": allow_live_execution,
            "force_reduced_risk_mode": force_reduced_risk,
            "real_broker_orders_enabled": mode == "LIVE",
            "paper_broker_validation_enabled": mode in {"DRY_RUN", "VALIDATION_TEST"},
            "symbol": self.config["mt5"]["symbol"],
            "magic_number": int(self.config["mt5"]["magic_number"]),
        }

    def requires_live_tick_validation(self) -> bool:
        """Return whether startup should hard-require fresh live tick prices."""
        mode = self._execution_mode_summary()["mode"]
        if mode == "BACKTEST":
            return False
        # LIVE/DRY_RUN/VALIDATION_TEST should start gracefully on closed sessions and
        # enforce freshness later through execution gating in `process_cycle`.
        return False

    def _log_exception(
        self,
        event_type: str,
        message: str,
        exc: BaseException,
        details: dict[str, Any] | None = None,
        notify: bool = False,
    ) -> None:
        """Emit structured exception telemetry without swallowing the stack trace."""
        payload = {
            "event_type": event_type,
            "message": message,
            "exception_type": type(exc).__name__,
            "error": str(exc),
            "stack_trace": traceback.format_exc(),
            "details": details or {},
        }
        self.logger.structured("exception", payload, level="ERROR")
        self._record_event(
            "CRITICAL" if notify else "ERROR",
            event_type,
            f"{message}: {exc}",
            reason_code=event_type,
            details=payload,
        )
        if notify:
            self._safe_notify_call(lambda: self.notifier.send_error(f"{event_type} | {message}: {exc}"))

    def _stage_result(
        self,
        stage: str,
        ok: bool,
        reason_code: str,
        human_reason: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return a normalized execution-stage object."""
        return {
            "ok": bool(ok),
            "stage": stage,
            "reason_code": reason_code,
            "human_reason": human_reason,
            "payload": payload or {},
        }

    def _safe_database_call(self, action: str, callback: Callable[[], None]) -> bool:
        """Execute a database callback safely."""
        try:
            callback()
            return True
        except Exception as exc:
            self.logger.warning(f"Database write failed during {action}: {exc}")
            return False

    def _safe_notify_call(self, callback: Callable[[], bool]) -> None:
        """Execute a Telegram notification safely."""
        try:
            callback()
        except Exception as exc:
            self.logger.warning(f"Telegram notification failed: {exc}")

    def _safe_journal_call(self, action: str, callback: Callable[[], None]) -> bool:
        """Execute a journaling callback and escalate repeated failures safely."""
        try:
            callback()
            journal_state = self.state.setdefault("journal", {})
            journal_state["failure_count"] = 0
            journal_state["last_error"] = None
            return True
        except Exception as exc:
            self.logger.error(f"Journal failure during {action}: {exc}")
            self.state = self.risk_manager.register_journal_failure(self.state, f"{action}: {exc}")
            self._record_event(
                "WARNING",
                "journaling_failure",
                f"Journal failure during {action}",
                reason_code="journaling_failure",
                details={"action": action, "error": str(exc)},
            )
            self.logger.structured(
                "journaling_failure",
                {"action": action, "error": str(exc), "failure_mode": "warning_only"},
                level="WARNING",
            )
            return False

    def _validation_state(self) -> dict[str, Any]:
        """Return the validation subsystem state payload."""
        validation_state = self.state.get("validation")
        if not isinstance(validation_state, dict):
            validation_state = {}
            self.state["validation"] = validation_state
        validation_state.setdefault("failure_count", 0)
        validation_state.setdefault("last_failure", None)
        validation_state.setdefault("last_failure_at", None)
        validation_state.setdefault("live_disabled", False)
        validation_state.setdefault("last_diagnostics_at", None)
        validation_state.setdefault("last_consistency_check", None)
        validation_state.setdefault("last_validation_report_day", None)
        validation_state.setdefault("last_validation_report_session_key", None)
        return validation_state

    def _register_validation_failure(
        self,
        reason_code: str,
        message: str,
        details: dict[str, Any] | None = None,
        disable_live: bool | None = None,
    ) -> None:
        """Track validation-layer failures and optionally disable live execution."""
        validation_cfg = self.config.get("validation", {})
        validation_state = self._validation_state()
        validation_state["failure_count"] = int(validation_state.get("failure_count", 0)) + 1
        validation_state["last_failure"] = reason_code
        validation_state["last_failure_at"] = utc_now().isoformat()
        self.logger.structured(
            "validation_failure",
            {
                "reason_code": reason_code,
                "message": message,
                "details": details or {},
                "failure_count": validation_state["failure_count"],
            },
            level="ERROR",
        )
        self._record_event("ERROR", "validation_failure", message, reason_code=reason_code, details=details)

        should_disable_live = bool(
            validation_cfg.get("disable_live_on_validation_failure", True)
            if disable_live is None
            else disable_live
        )
        # Default threshold changed from 1 to 5 to prevent live trading from being disabled
        # on single transient validation failures (e.g., single tick data refresh delay).
        # This allows the bot to recover from temporary issues without manual intervention.
        threshold = int(validation_cfg.get("max_validation_failures_before_disable", 5))
        if should_disable_live and validation_state["failure_count"] >= threshold:
            validation_state["live_disabled"] = True
            locks = self.state.setdefault("locks", {})
            locks["kill_switch"] = True
            locks["kill_switch_reason"] = reason_code

    def _normalize_block_reason(self, reason_code: str | None, human_reason: str | None = None) -> str | None:
        """Map internal reasons to explicit validation-layer reason codes."""
        if reason_code is None:
            return None
        normalized = str(reason_code)
        mapping = {
            "session_live_blocked": "blocked_bad_session",
            "family_live_blocked": "blocked_bad_session",
            "regime_live_blocked": "blocked_bad_regime",
            "fitted_volume_ratio_below_min": "blocked_low_fitted_volume_ratio",
            "execution_cooldown": "cooldown_active",
            "post_loss_cooldown": "cooldown_active",
            "consecutive_loss_cooldown": "cooldown_active",
            "max_trades_per_day": "risk_lock_active",
            "balance_below_minimum": "risk_lock_active",
            "consecutive_loss_lock": "risk_lock_active",
            "session_drawdown_lock": "disabled_due_to_drawdown",
        }
        normalized = mapping.get(normalized, normalized)
        if normalized == "daily_drawdown_lock":
            return "disabled_due_to_drawdown"
        return normalized

    def _load_csv_rows(self, path: Path) -> list[dict[str, Any]]:
        """Read CSV rows safely for validation checks."""
        if not path.exists() or path.stat().st_size == 0:
            return []
        try:
            with path.open("r", newline="", encoding="utf-8") as handle:
                return list(csv.DictReader(handle))
        except Exception as exc:
            self._register_validation_failure("csv_read_failure", f"Failed reading {path.name}", {"path": str(path), "error": str(exc)})
            return []

    def _bool_from_csv(self, value: Any) -> bool:
        """Parse CSV boolean-ish values."""
        return str(value).strip().lower() in {"1", "true", "yes"}

    def _check_journaling_consistency(
        self,
        day_key: str | None = None,
        session_name: str | None = None,
        require_analytics_row: bool = True,
    ) -> dict[str, Any]:
        """Verify journaling integrity across signals, trades, and analytics outputs."""
        signals = self._load_csv_rows(self.logger.signals_path)
        trades = self._load_csv_rows(self.logger.trades_path)
        analytics = self._load_csv_rows(self.logger.analytics_path)
        scope_signals = [row for row in signals if not day_key or str(row.get("timestamp", "")).startswith(day_key)]
        scope_trades = [row for row in trades if not day_key or str(row.get("timestamp", "")).startswith(day_key)]
        scope_analytics = [row for row in analytics if not day_key or str(row.get("day", "")) == day_key]
        if session_name:
            scope_signals = [row for row in scope_signals if str(row.get("session_name", "")) == session_name]
            scope_trades = [row for row in scope_trades if str(row.get("session_at_entry", "")) == session_name]

        executed_signals = [row for row in scope_signals if self._bool_from_csv(row.get("executed"))]
        open_trade_rows = [row for row in scope_trades if str(row.get("event_type", "")).upper() in {"TRADE_OPENED", "TRADE_MANAGED", "OPENED"}]
        close_trade_rows = [row for row in scope_trades if str(row.get("event_type", "")).upper() in {"TRADE_CLOSED", "TRADE_FINALIZED", "CLOSED", "FINALIZED", "CLOSED_UNVERIFIED", "PENDING_CLOSE_RESOLUTION"}]
        trade_fingerprints = {str(row.get("setup_fingerprint", "")) for row in scope_trades if row.get("setup_fingerprint")}
        signal_fingerprints = {str(row.get("setup_fingerprint", "")) for row in scope_signals if row.get("setup_fingerprint")}

        executed_without_trade = []
        for signal_row in executed_signals:
            fingerprint = str(signal_row.get("setup_fingerprint", ""))
            if fingerprint and fingerprint not in trade_fingerprints:
                executed_without_trade.append(fingerprint)

        closes_missing_pnl = [
            row.get("ticket")
            for row in close_trade_rows
            if row.get("pnl") in {"", None} and str(row.get("status", "")).upper() != "CLOSED_UNVERIFIED"
        ]
        inconsistent_fingerprints = [
            fingerprint
            for fingerprint in trade_fingerprints
            if fingerprint and not fingerprint.startswith("RECOVERED") and fingerprint not in signal_fingerprints
        ]

        signal_duplicate_counter = Counter(
            (
                str(row.get("event_type", "")),
                str(row.get("setup_fingerprint", "")),
                str(row.get("reason_code", "")),
                str(row.get("timestamp", "")),
            )
            for row in scope_signals
        )
        trade_duplicate_counter = Counter(
            (
                str(row.get("event_type", "")),
                str(row.get("ticket", "")),
                str(row.get("entry_time", "")),
                str(row.get("exit_time", "")),
            )
            for row in scope_trades
        )
        duplicate_signal_events = [key for key, count in signal_duplicate_counter.items() if count > 1 and any(key)]
        duplicate_trade_events = [key for key, count in trade_duplicate_counter.items() if count > 1 and any(key)]
        analytics_row_present = bool(scope_analytics) if day_key else bool(analytics)
        analytics_ok = analytics_row_present if require_analytics_row else True

        ok = not any([executed_without_trade, closes_missing_pnl, inconsistent_fingerprints, duplicate_signal_events, duplicate_trade_events]) and analytics_ok
        result = {
            "ok": ok,
            "day": day_key,
            "session_name": session_name,
            "executed_trade_without_trade_row": len(executed_without_trade),
            "close_missing_pnl": len(closes_missing_pnl),
            "inconsistent_fingerprints": len(inconsistent_fingerprints),
            "duplicate_signal_events": len(duplicate_signal_events),
            "duplicate_trade_events": len(duplicate_trade_events),
            "analytics_row_present": analytics_row_present,
            "analytics_required": require_analytics_row,
            "signal_rows": len(scope_signals),
            "trade_rows": len(scope_trades),
        }
        self._validation_state()["last_consistency_check"] = result
        if not ok:
            self._register_validation_failure("journaling_consistency_failed", "Journaling consistency check failed", result)
        return result

    def _sync_risk_state(self, now_utc: datetime, balance: float, session_name: str) -> None:
        """Synchronize daily/session risk state and surface cooldown-state failures explicitly."""
        try:
            self.state = self.risk_manager.sync_state(
                self.state,
                now_utc,
                float(balance),
                session_name,
                int(self.config["bot"].get("manual_reset_id", 0)),
            )
        except Exception as exc:
            self._register_validation_failure(
                "cooldown_state_update_failed",
                "Risk governor state sync failed",
                {"error": str(exc), "session_name": session_name},
            )
            raise

    def _apply_trade_close_state(self, total_pnl: float, closed_at: datetime, session_name: str) -> None:
        """Apply close-side risk and cooldown state updates with validation-layer safeguards."""
        try:
            self.state = self.risk_manager.apply_trade_close_to_state(
                self.state,
                float(total_pnl),
                closed_at,
                session_name,
            )
        except Exception as exc:
            self._register_validation_failure(
                "cooldown_state_update_failed",
                "Risk governor close-state update failed",
                {"error": str(exc), "session_name": session_name, "pnl": total_pnl},
            )
            raise
        if self.dynamic_position_sizer is not None:
            self.dynamic_position_sizer.update_after_trade(bool(total_pnl > 0))

    def calculate_trade_r_multiple(self, trade_record: dict[str, Any]) -> float | None:
        """Calculate and validate R-multiple for completed trades."""
        r_calculator = getattr(self, "r_calculator", None)
        if r_calculator is None:
            return None
        connector = getattr(self, "connector", None)
        account_info_getter = getattr(connector, "get_account_info", None)
        account_info = account_info_getter() if callable(account_info_getter) else None
        config = getattr(self, "config", {}) or {}
        direction = str(trade_record.get("direction") or trade_record.get("side") or "LONG").upper()
        metadata = TradeRiskMetadata(
            entry_price=float(trade_record.get("entry_price") or trade_record.get("entry") or 0.0),
            stop_loss_price=float(trade_record.get("stop_loss") or trade_record.get("sl") or 0.0),
            exit_price=float(trade_record.get("exit_price") or trade_record.get("exit") or 0.0),
            position_size=float(trade_record.get("position_size") or trade_record.get("volume") or trade_record.get("lot_size") or 0.0),
            direction=direction,
            setup_family=str(trade_record.get("setup_family") or "MANUAL"),
            risk_percent=float(config.get("risk", {}).get("risk_percent", 1.0) or 1.0),
            account_balance=float(getattr(account_info, "balance", 0.0) or 0.0),
        )
        r_multiple = r_calculator.calculate_r_multiple(metadata)
        if r_multiple is not None:
            self.logger.info(f"Trade R-multiple: {r_multiple:.2f}R")
        else:
            self.logger.warning("Could not calculate valid R-multiple for trade")
        return r_multiple

    def _record_event(
        self,
        level: str,
        event_type: str,
        message: str,
        reason_code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Write an event to the logger and SQLite database."""
        if level.upper() == "WARNING":
            self.logger.warning(message)
        elif level.upper() in {"ERROR", "CRITICAL"}:
            self.logger.error(message)
        else:
            self.logger.info(message)
        self._safe_database_call(
            "event insert",
            lambda: self.database.insert_event(
                {
                    "timestamp": utc_now().isoformat(),
                    "level": level.upper(),
                    "event_type": event_type,
                    "message": message,
                    "reason_code": reason_code,
                    "details_json": json.dumps(details or {}, ensure_ascii=True, default=str, sort_keys=True),
                }
            ),
        )
        if level.upper() in {"ERROR", "CRITICAL"}:
            self.state["last_error"] = message

    def _write_heartbeat(
        self,
        status: str,
        spread: float | None,
        market_context: dict[str, Any] | None,
        account_info: Any | None,
        open_positions: int,
        reason_code: str | None = None,
    ) -> None:
        """Persist a heartbeat snapshot."""
        regime_name = (market_context or {}).get("regime", {}).get("regime_name", "UNKNOWN")
        session_name = (market_context or {}).get("session", {}).get("session_name", "UNKNOWN")
        bias_name = (market_context or {}).get("bias", {}).get("direction", "UNKNOWN")
        setup_name = self.state.get("last_cycle_setup_family") or "WAIT"
        payload = {
            "timestamp": utc_now().isoformat(),
            "status": status,
            "symbol": self.config["mt5"]["symbol"],
            "spread": spread,
            "trend": bias_name,
            "setup": setup_name,
            "balance": float(account_info.balance) if account_info is not None else None,
            "equity": float(account_info.equity) if account_info is not None else None,
            "open_positions": int(open_positions),
            "regime": regime_name,
            "session": session_name,
            "reason_code": reason_code,
            "mode": self._execution_mode_summary()["mode"],
        }
        self.state["last_heartbeat_time"] = payload["timestamp"]
        self.state["last_heartbeat_status"] = status
        self.state["bot_status"] = status
        self._safe_database_call("heartbeat insert", lambda: self.heartbeat_service.record(payload))
        self.control_state.record_runtime_snapshot(
            {
                "bot_pid": os.getpid(),
                "mode": self.trading_mode,
                "heartbeat_at": payload["timestamp"],
                "heartbeat_status": status,
                "bot_running": status != "STOPPED",
                "desired_state": "RUNNING" if status != "STOPPED" else "STOPPED",
                "execution_paused": self.execution_paused,
                "auto_execution_enabled": self.auto_execution_enabled,
                "signal_generation_enabled": bool(self.control_state.load().get("signal_generation_enabled", True)),
                "kill_switch": bool(self.state.get("locks", {}).get("kill_switch")),
                "kill_switch_reason": self.state.get("locks", {}).get("kill_switch_reason"),
                "request_shutdown": bool(self.control_state.load().get("request_shutdown", False)),
                "last_execution_result": self.state.get("execution_reason_code"),
                "last_signal": self.state.get("last_signal", {}).get("fingerprint"),
                "session": session_name,
                "regime": regime_name,
                "spread_points": spread,
                "mt5_connected": bool(getattr(self.connector.terminal_info, "connected", False)) if self.connector.terminal_info is not None else False,
                "trade_allowed": bool(getattr(self.connector.account_info, "trade_allowed", False)) if self.connector.account_info is not None else False,
                "account_login": getattr(self.connector.account_info, "login", None) if self.connector.account_info is not None else None,
                "resolved_runtime": self.config_manager.resolved_runtime_snapshot(self.config),
            }
        )

    def _degraded_state(self) -> dict[str, Any]:
        """Return the mutable degraded-mode state payload."""
        degraded = self.state.get("degraded_mode")
        if not isinstance(degraded, dict):
            degraded = {}
            self.state["degraded_mode"] = degraded
        degraded.setdefault("active", False)
        degraded.setdefault("reason_code", None)
        degraded.setdefault("active_since", None)
        degraded.setdefault("cooldown_until", None)
        degraded.setdefault("alert_sent_at", None)
        degraded.setdefault("failure_count", 0)
        degraded.setdefault("last_failure_at", None)
        degraded.setdefault("last_failure_class", None)
        return degraded

    def save_state(self) -> bool:
        """Persist the current runtime state without killing the trading loop on transient IO issues."""
        result = self.state_manager.save_state(self.state)
        if result.get("ok"):
            return True
        self.state["last_error"] = f"State save failed: {result.get('error')}"
        self.logger.structured("state_save_failed", result, level="WARNING")
        return False

    def _handle_shutdown(self, *_: Any) -> None:
        """Stop the main loop on SIGINT or SIGTERM."""
        self.state["shutdown_reason"] = "shutdown_requested"
        self._record_event("INFO", "shutdown_requested", "Shutdown initiated...")
        self.running = False

    def register_signal_handlers(self) -> None:
        """Attach graceful shutdown signal handlers."""
        signal.signal(signal.SIGINT, self._handle_shutdown)
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, self._handle_shutdown)

    def startup(self) -> None:
        """Run the startup sequence."""
        self.state["shutdown_reason"] = None
        self._record_event("INFO", "startup", "Bot startup sequence started")
        if not self.connector.initialize():
            raise RuntimeError(
                f"Unable to initialize MT5 after {int(self.config['mt5'].get('reconnect_retries', 3))} attempts"
            )
        mode_ctx = self._execution_mode_summary()
        self.logger.structured(
            "startup_effective_mode_before_banner",
            {
                "mode": mode_ctx["mode"],
                "allow_live_execution": bool(mode_ctx.get("allow_live_execution")),
                "dry_run": bool(mode_ctx.get("dry_run")),
                "config_mode": self.config["bot"].get("trading_mode"),
            },
            level="INFO",
        )
        startup_bars = int(self.config["timeframes"].get("bars_to_fetch", 320))
        require_live_tick = self.requires_live_tick_validation()
        startup_ok, startup_reason = self.connector.validate_startup(
            bars=startup_bars,
            require_live_tick=require_live_tick,
        )
        if not startup_ok:
            self._record_event("CRITICAL", "startup_validation_failed", startup_reason)
            raise RuntimeError(startup_reason)
        if "tick deferred" in startup_reason.lower():
            if mode_ctx["mode"] == "BACKTEST":
                self._record_event(
                    "INFO",
                    "startup_backtest_skip_live_tick",
                    "BACKTEST mode: skipping live tick validation and using historical replay path",
                )
                self.logger.info("BACKTEST mode startup: live tick validation skipped; historical replay is allowed on closed market.")
            elif mode_ctx["mode"] == "LIVE":
                self._record_event(
                    "WARNING",
                    "startup_live_stale_tick_wait",
                    "LIVE mode started without fresh tick; bot will idle safely until market data is fresh",
                    reason_code="stale_tick",
                )
                self.logger.warning("LIVE mode startup: stale/no tick detected, entering safe idle/wait state.")
            else:
                self._record_event(
                    "WARNING",
                    "startup_dry_run_stale_tick_wait",
                    "DRY_RUN mode started without fresh tick; execution will wait for fresh market data",
                    reason_code="stale_tick",
                )
                self.logger.warning("DRY_RUN mode startup: stale/no tick detected, execution paused until fresh data.")
        health_report = self.connector.health_report()
        account_info = self.connector.get_account_info()
        self._sync_risk_state(utc_now(), float(account_info.balance), classify_session(self.config, utc_now())["session_name"])
        startup_payload = {
            "config_path": str((self.base_dir / "config.json").resolve()),
            "mode": mode_ctx["mode"],
            "allow_live_execution": mode_ctx["allow_live_execution"],
            "auto_recover_live_positions": bool(self.config["bot"].get("auto_recover_live_positions", False)),
            "symbol": self.config["mt5"]["symbol"],
            "session_bucket_live_allowed_flags": {
                str(bucket.get("name", "")): bool(bucket.get("live_allowed", False))
                for bucket in self.config.get("sessions", {}).get("buckets", [])
            },
            "session_live_allowed_buckets": list(self.config.get("sessions", {}).get("live_allowed_buckets", [])),
            "allow_asia_session": bool(self.config.get("sessions", {}).get("allow_asia_session", False)),
            "regime_allow_range_regime": bool(self.config.get("regime", {}).get("allow_range_regime", False)),
            "strategy_require_trend_alignment": bool(self.config.get("strategy", {}).get("require_trend_alignment", False)),
            "strategy_live_thresholds": self.config.get("strategy", {}).get("live_thresholds", {}),
            "liquidity_sweep_reversal_family": self.config.get("strategy", {}).get("setup_families", {}).get("liquidity_sweep_reversal", {}),
            "liquidity_sweep_reversal_control": self.config.get("strategy", {}).get("setup_controls", {}).get("liquidity_sweep_reversal", {}),
            "compression_release_control": self.config.get("strategy", {}).get("setup_controls", {}).get("compression_release", {}),
            "breakout_retest_continuation_control": self.config.get("strategy", {}).get("setup_controls", {}).get("breakout_retest_continuation", {}),
            "trend_pullback_reclaim_control": self.config.get("strategy", {}).get("setup_controls", {}).get("trend_pullback_reclaim", {}),
            "execution_path": self.connector.execution_path_summary(),
            "connection_ok": bool(health_report.get("connection_ok")),
            "symbol_ok": bool(health_report.get("symbol_ok")),
            "tick_ok": bool(health_report.get("tick_ok")),
            "connection": health_report.get("connection", {}),
            "symbol_snapshot": health_report.get("symbol_snapshot", {}),
            "account": {
                "login": getattr(account_info, "login", None),
                "server": getattr(account_info, "server", None),
                "trade_allowed": getattr(account_info, "trade_allowed", None),
                "balance": float(getattr(account_info, "balance", 0.0) or 0.0),
                "equity": float(getattr(account_info, "equity", 0.0) or 0.0),
                "margin_free": float(getattr(account_info, "margin_free", 0.0) or 0.0),
            },
            "real_broker_orders_enabled": mode_ctx["real_broker_orders_enabled"],
            "paper_broker_validation_enabled": mode_ctx["paper_broker_validation_enabled"],
        }
        self.logger.structured("startup_status", startup_payload)
        self._record_event(
            "INFO",
            "startup_ready",
            (
                f"Startup ready | mode={mode_ctx['mode']} | symbol={self.config['mt5']['symbol']} | "
                f"connection_ok={health_report.get('connection_ok')} | symbol_ok={health_report.get('symbol_ok')} | "
                f"trade_allowed={startup_payload['connection'].get('trade_allowed')} | "
                f"tradeapi_disabled={startup_payload['connection'].get('tradeapi_disabled')}"
            ),
            details=startup_payload,
        )
        self._safe_notify_call(
            lambda: self.notifier.send_info(
                (
                    f"Bot started | mode={mode_ctx['mode']} | symbol={self.config['mt5']['symbol']} | "
                    f"connection_ok={health_report.get('connection_ok')} | symbol_ok={health_report.get('symbol_ok')} | "
                    f"trade_allowed={startup_payload['connection'].get('trade_allowed')} | "
                    f"tradeapi_disabled={startup_payload['connection'].get('tradeapi_disabled')}"
                )
            )
        )
        for label, callback in [
            ("startup_position_reconciliation", self._reconcile_startup_position_state),
            ("startup_unresolved_trade_reconciliation", lambda: self._resolve_unresolved_trades(source="startup")),
            ("startup_mt5_db_reconciliation", self._reconcile_mt5_positions_with_database),
        ]:
            try:
                callback()
            except Exception as exc:
                self._log_exception(label, f"{label} failed during startup", exc, notify=False)
        try:
            self.telegram_command_service.start()
        except Exception as exc:
            self._log_exception("telegram_command_service_start_failed", "Telegram command service failed to start", exc, notify=False)
        lebprime_live = getattr(self, "lebprime_v3_4_0", None)
        if lebprime_live is not None and lebprime_live.enabled:
            lebprime_live.start()
        self.save_state()

    def run(self) -> None:
        """Execute the main trading loop."""
        self.register_signal_handlers()
        try:
            self.startup()
            loop_sleep = int(self.config["bot"]["loop_sleep_seconds"])
            while self.running:
                cycle_started = time.time()
                try:
                    self.process_cycle()
                except Exception as exc:
                    auto_restart = bool(self.config["bot"].get("auto_restart_on_error", False))
                    self._log_exception(
                        "main_loop_exception",
                        "Main loop exception",
                        exc,
                        details={"mode": self._execution_mode_summary()["mode"], "auto_restart_on_error": auto_restart},
                        notify=not auto_restart,
                    )
                    self.save_state()
                    if not auto_restart:
                        self.state["shutdown_reason"] = "fatal_error"
                        self.running = False
                elapsed = time.time() - cycle_started
                sleep_for = max(0.0, next_cycle_sleep_seconds(loop_sleep, offset_seconds=2) - min(elapsed, loop_sleep))
                if self.running and sleep_for > 0:
                    time.sleep(sleep_for)
        except Exception as exc:
            self.running = False
            self.state["shutdown_reason"] = "fatal_error"
            self._log_exception("fatal_error", "Fatal bot error", exc, notify=True)
        finally:
            self.shutdown()

    def _setup_registry(self) -> dict[str, Any]:
        """Return the setup registry state."""
        registry = self.state.get("setup_registry")
        if not isinstance(registry, dict):
            registry = {}
            self.state["setup_registry"] = registry
        return registry

    def _journal_trade_close(self, row: dict[str, Any]) -> bool:
        """Write a trade-close lifecycle event."""
        return self._safe_journal_call("trade close journal", lambda: self.trade_journal.record_trade_close(row))

    def process_cycle(self) -> None:
        """Process one complete trading cycle."""
        self._apply_runtime_updates()
        self.trade_journal.flush_pending()
        now_utc = utc_now()
        mode_ctx = self._execution_mode_summary()
        if mode_ctx["mode"] == "BACKTEST":
            self.state["execution_status"] = "paused"
            self.state["execution_reason_code"] = "backtest_mode_requires_dashboard_runner"
            self._record_event(
                "INFO",
                "backtest_mode_idle",
                "Bot loop is idle in BACKTEST mode. Run backtests from dashboard Backtest page.",
                reason_code="backtest_mode_requires_dashboard_runner",
            )
            self._write_heartbeat(
                "BACKTEST_IDLE",
                None,
                {"session": {"session_name": "BACKTEST"}, "regime": {"regime_name": "BACKTEST"}, "bias": {"direction": "NONE"}},
                self.connector.account_info,
                0,
                "backtest_mode_requires_dashboard_runner",
            )
            self.save_state()
            return
        lebprime_status: dict[str, Any] = {}
        try:
            lebprime_live = getattr(self, "lebprime_v3_4_0", None)
            if lebprime_live is not None:
                execution_block_reason = None
                if self.state.get("locks", {}).get("kill_switch", False):
                    execution_block_reason = "KILL_SWITCH_ACTIVE"
                elif self.execution_paused:
                    execution_block_reason = "EXECUTION_PAUSED"
                elif not self.auto_execution_enabled:
                    execution_block_reason = "AUTO_EXECUTION_DISABLED"
                elif not self.signal_generation_enabled:
                    execution_block_reason = "SIGNAL_GENERATION_DISABLED"
                lebprime_status = lebprime_live.scan_once(
                    now_utc,
                    execution_enabled=execution_block_reason is None,
                    execution_block_reason=execution_block_reason,
                )
        except Exception as exc:
            self._log_exception(
                "lebprime_v3_4_0_cycle_failed",
                "LebPrime v3.4.0 cycle failed",
                exc,
                notify=False,
            )
            lebprime_status = {"started": False, "startup_block_reason": str(exc), "active_positions": 0}
        lebprime_cfg = self.config.get("lebprime_v3_4_0") if isinstance(self.config.get("lebprime_v3_4_0"), dict) else {}
        enabled = bool(lebprime_cfg.get("enabled", False))
        started = bool(lebprime_status.get("started", False))
        status = "LEBPRIME_ACTIVE" if enabled and started else "LEBPRIME_IDLE"
        reason = lebprime_status.get("startup_block_reason") or (None if enabled else "lebprime_v3_4_0_disabled")
        self.state["execution_status"] = "active" if status == "LEBPRIME_ACTIVE" else "idle"
        self.state["execution_reason_code"] = reason or "lebprime_v3_4_0_cycle_complete"
        self._write_heartbeat(
            status,
            None,
            {"session": {"session_name": "LEBPRIME"}, "regime": {"regime_name": "LEBPRIME"}, "bias": {"direction": "NONE"}},
            self.connector.account_info,
            int(lebprime_status.get("active_positions", 0) or 0),
            reason,
        )
        self.save_state()

    def _reconcile_startup_position_state(self) -> None:
        """Reconcile in-memory and persistent trade state against MT5 on startup."""
        if self.demo_mode:
            return
        try:
            live_positions = self.connector.get_bot_positions()
        except Exception:
            live_positions = []
        live_tickets = {str(getattr(position, "ticket", "")) for position in live_positions}
        unresolved = list(self.database.get_pending_resolution_trades())
        open_rows = list(self.database.get_open_trades())

        for row in unresolved:
            row = dict(row)
            self._finalize_trade_from_history_row(row, source="startup_reconcile")

        for row in open_rows:
            row = dict(row)
            ticket = str(row["mt5_ticket"] or row["position_id"] or row["trade_id"] or "")
            if not ticket:
                continue
            if ticket in live_tickets:
                continue
            self._finalize_trade_from_history_row(row, source="startup_reconcile")

        if self.state.get("open_position"):
            previous_ticket = self.state.get("open_position", {}).get("ticket")
            if str(previous_ticket) not in live_tickets:
                self.state["open_position"] = None
                self._record_event(
                    "WARNING",
                    "startup_open_position_state_cleared",
                    "Cleared stale open_position state on startup after reconciliation",
                    details={
                        "previous_ticket": previous_ticket,
                        "live_position_count": len(live_positions),
                        "reconciled": True,
                    },
                )
        if live_positions:
            self._record_event(
                "INFO",
                "startup_live_positions_detected",
                "Live MT5 positions reconciled at startup",
                details={
                    "tickets": [str(getattr(position, "ticket", "")) for position in live_positions],
                    "count": len(live_positions),
                    "auto_recover_live_positions": bool(self.config.get("bot", {}).get("auto_recover_live_positions", False)),
                },
            )

    def _resolve_unresolved_trades(self, max_rows: int | None = None, source: str = "cycle") -> dict[str, Any]:
        """Retry unresolved trade finalization from history."""
        resolved = 0
        failed = 0
        pending = list(self.database.get_pending_resolution_trades())
        if max_rows is not None:
            pending = pending[: max(0, int(max_rows))]
        retry_policy = self._close_resolution_policy()
        retry_interval = retry_policy["retry_interval_seconds"]
        now_utc = utc_now()
        for row in pending:
            row = dict(row)
            ticket = str(row.get("mt5_ticket") or row.get("position_id") or row.get("trade_id") or row.get("ticket") or "")
            if not ticket:
                continue
            attempts = int(row.get("resolution_attempts", 0) or 0)
            if attempts >= retry_policy["max_retries"] and self._should_fail_close_resolution(row, attempts, now_utc, retry_policy):
                self._mark_trade_resolution_failed(row, source=source, reason="max_resolution_retries_exceeded")
                failed += 1
                continue
            last_attempt = to_utc(row.get("last_resolution_attempt_at"))
            if last_attempt is not None and (now_utc - last_attempt).total_seconds() < retry_interval:
                continue
            resolution = self._finalize_trade_from_history_row(row, source=source)
            if bool(resolution.get("ok")):
                resolved += 1
                continue
            next_attempt = int(resolution.get("attempts", attempts + 1) or 0)
            resolution_row = dict(resolution.get("payload") or row)
            if next_attempt >= retry_policy["max_retries"] and self._should_fail_close_resolution(resolution_row, next_attempt, now_utc, retry_policy):
                self._mark_trade_resolution_failed(resolution_row, source=source, reason="max_resolution_retries_exceeded")
                failed += 1
        return {"pending": len(pending), "resolved": resolved, "failed": failed, "source": source}

    def _reconcile_mt5_positions_with_database(self) -> dict[str, Any]:
        """Compare MT5 open positions against persisted trade rows."""
        if self.demo_mode:
            return {"recovered_missing_open_journal": 0, "pending_resolution": 0}
        try:
            live_positions = self.connector.get_bot_positions()
        except Exception:
            live_positions = []
        live_tickets = {str(getattr(position, "ticket", "")) for position in live_positions}
        open_rows = list(self.database.get_open_trades())
        recovered_missing_open_journal = 0
        pending_resolution = 0

        for position in live_positions:
            ticket = str(getattr(position, "ticket", ""))
            if not ticket:
                continue
            db_row = self.database.get_trade_by_identity(trade_id=ticket, mt5_ticket=ticket, position_id=ticket)
            if db_row is None:
                position_state = {
                    "trade_id": ticket,
                    "mt5_ticket": ticket,
                    "position_id": ticket,
                    "symbol": getattr(position, "symbol", self.config["mt5"]["symbol"]),
                    "side": "LONG" if int(getattr(position, "type", 0)) == 0 else "SHORT",
                    "status": "OPENED",
                    "event_type": "TRADE_OPENED",
                    "entry_price": float(getattr(position, "price_open", 0.0)),
                    "stop_loss": float(getattr(position, "sl", 0.0)),
                    "take_profit": float(getattr(position, "tp", 0.0)),
                    "volume": float(getattr(position, "volume", 0.0)),
                    "bot_mode": self._execution_mode_summary()["mode"],
                    "magic_number": int(self.config["mt5"]["magic_number"]),
                    "created_at": utc_now().isoformat(),
                    "updated_at": utc_now().isoformat(),
                    "comment": "recovered_missing_open_journal",
                    "metadata": {
                        "recovered_missing_open_journal": True,
                        "position": self.connector._serialize_log_value(position) if hasattr(self.connector, "_serialize_log_value") else str(position),
                    },
                }
                self.trade_journal.record_trade_open(position_state)
                recovered_missing_open_journal += 1

        for row in open_rows:
            row = dict(row)
            ticket = str(row["mt5_ticket"] or row["position_id"] or row["trade_id"] or "")
            if not ticket:
                continue
            if ticket in live_tickets:
                continue
            resolution = self._finalize_trade_from_history_row(row, source="reconcile_positions")
            if bool(resolution.get("ok")):
                pending_resolution += 1
            else:
                retry_policy = self._close_resolution_policy()
                next_attempt = int(resolution.get("attempts", int(row.get("resolution_attempts", 0) or 0) + 1) or 0)
                resolution_row = dict(resolution.get("payload") or row)
                if next_attempt >= retry_policy["max_retries"] and self._should_fail_close_resolution(resolution_row, next_attempt, utc_now(), retry_policy):
                    self._mark_trade_resolution_failed(resolution_row, source="reconcile_positions", reason="max_resolution_retries_exceeded")
                pending_resolution += 1

        return {
            "recovered_missing_open_journal": recovered_missing_open_journal,
            "pending_resolution": pending_resolution,
        }

    def _trade_identity(self, position_state: dict[str, Any]) -> str:
        """Return a stable trade identity."""
        return str(
            position_state.get("trade_id")
            or position_state.get("mt5_ticket")
            or position_state.get("ticket")
            or position_state.get("position_id")
            or position_state.get("setup_fingerprint")
            or ""
        )

    def _close_resolution_policy(self) -> dict[str, int]:
        """Return bounded retry policy for live close reconciliation."""
        journaling_cfg = self.config.get("journaling", {}) if isinstance(self.config.get("journaling"), dict) else {}
        retry_interval = int(journaling_cfg.get("unresolved_trade_retry_interval_seconds", 120) or 120)
        max_retries = int(journaling_cfg.get("unresolved_trade_max_retries", 60) or 60)
        min_age_before_fail = int(journaling_cfg.get("unresolved_trade_min_age_before_fail_seconds", max(retry_interval * 10, 1800)) or max(retry_interval * 10, 1800))
        max_window = int(journaling_cfg.get("unresolved_trade_max_retry_window_seconds", max(max_retries * retry_interval * 2, 7200)) or max(max_retries * retry_interval * 2, 7200))
        return {
            "retry_interval_seconds": retry_interval,
            "max_retries": max_retries,
            "min_age_before_fail_seconds": min_age_before_fail,
            "max_retry_window_seconds": max_window,
        }

    def _should_fail_close_resolution(
        self,
        row: dict[str, Any],
        attempts: int,
        now_utc: datetime,
        retry_policy: dict[str, int] | None = None,
    ) -> bool:
        """Return whether an unresolved close should now be treated as terminal."""
        policy = retry_policy or self._close_resolution_policy()
        started_at = to_utc(
            row.get("resolution_started_at")
            or row.get("closed_at")
            or row.get("detected_close_at")
            or row.get("updated_at")
            or row.get("timestamp")
        )
        age_seconds = max(0.0, (now_utc - started_at).total_seconds()) if started_at is not None else 0.0
        if age_seconds >= float(policy["max_retry_window_seconds"]):
            return True
        return attempts >= int(policy["max_retries"]) and age_seconds >= float(policy["min_age_before_fail_seconds"])

    def _record_trade_resolution_stage(
        self,
        row: dict[str, Any],
        *,
        status: str,
        source: str,
        reason: str,
        evidence: dict[str, Any] | None,
        attempt_count: int,
    ) -> dict[str, Any]:
        """Persist one unresolved reconciliation stage with evidence and idempotent transitions."""
        ticket = str(row.get("mt5_ticket") or row.get("position_id") or row.get("trade_id") or row.get("ticket") or "")
        now_iso = utc_now().isoformat()
        evidence_payload = dict(evidence or {})
        stage_payload = {
            **row,
            "trade_id": row.get("trade_id") or ticket,
            "mt5_ticket": row.get("mt5_ticket") or ticket,
            "position_id": row.get("position_id") or ticket,
            "status": status,
            "event_type": "TRADE_CLOSED",
            "close_reason": row.get("close_reason") or reason,
            "unresolved_reason": reason,
            "updated_at": now_iso,
            "last_resolution_attempt_at": now_iso,
            "resolution_stage": str(evidence_payload.get("stage") or status).lower(),
            "resolution_attempts": attempt_count,
            "resolution_started_at": row.get("resolution_started_at") or row.get("closed_at") or row.get("detected_close_at") or now_iso,
            "resolution_last_evidence_at": now_iso,
            "matched_order_ids": ",".join(str(item) for item in (evidence_payload.get("matched_order_ids") or []) if str(item).strip()),
            "matched_deal_ids": ",".join(str(item) for item in (evidence_payload.get("matched_deal_ids") or []) if str(item).strip()),
            "matched_volume": evidence_payload.get("matched_volume"),
            "reconciliation_confidence": evidence_payload.get("confidence"),
            "resolution_metadata": evidence_payload,
            "raw_mt5_json": evidence_payload,
        }
        self.database.update_trade_resolution_attempt(
            ticket,
            status,
            attempt_count,
            reason=reason,
            raw_mt5_json=evidence_payload,
            resolution_stage=stage_payload["resolution_stage"],
            resolution_metadata=evidence_payload,
            matched_order_ids=evidence_payload.get("matched_order_ids") or [],
            matched_deal_ids=evidence_payload.get("matched_deal_ids") or [],
            matched_volume=evidence_payload.get("matched_volume"),
            reconciliation_confidence=evidence_payload.get("confidence"),
        )
        if str(row.get("status") or "").upper() != str(status).upper():
            self._journal_trade_close(stage_payload)
        self._record_event(
            "WARNING" if status != "FINALIZED" else "INFO",
            "trade_resolution_stage_changed",
            f"Trade {ticket} reconciliation stage is {status}",
            details={
                "ticket": ticket,
                "source": source,
                "status": status,
                "reason": reason,
                "attempt_count": attempt_count,
                "confidence": evidence_payload.get("confidence"),
                "matched_order_ids": evidence_payload.get("matched_order_ids") or [],
                "matched_deal_ids": evidence_payload.get("matched_deal_ids") or [],
            },
        )
        return stage_payload

    def _mark_trade_resolution_failed(self, row: dict[str, Any], source: str, reason: str) -> None:
        """Persist a terminal failure when history resolution exhausts retries."""
        ticket = str(row.get("mt5_ticket") or row.get("position_id") or row.get("trade_id") or row.get("ticket") or "")
        if not ticket:
            return
        if str(row.get("status") or "").upper() == "FAILED_CLOSE_RESOLUTION":
            return
        now_iso = utc_now().isoformat()
        failure_payload = {
            **row,
            "trade_id": ticket,
            "mt5_ticket": ticket,
            "position_id": row.get("position_id") or ticket,
            "status": "FAILED_CLOSE_RESOLUTION",
            "event_type": "TRADE_CLOSED",
            "close_reason": "history_resolution_failed",
            "unresolved_reason": reason,
            "resolution_stage": "failed_close_resolution",
            "outcome_label": "UNKNOWN",
            "win_loss": "UNKNOWN",
            "updated_at": now_iso,
            "last_resolution_attempt_at": now_iso,
        }
        self._journal_trade_close(failure_payload)
        self._record_event(
            "WARNING",
            "trade_resolution_failed",
            f"Trade {ticket} failed resolution after retries",
            details={"ticket": ticket, "source": source, "reason": reason},
        )

    def _derive_close_reason_from_history(
        self,
        position_state: dict[str, Any],
        history_bundle: dict[str, list[Any]],
        market_position: Any | None = None,
        fallback_reason: str | None = None,
    ) -> str:
        """Infer a close reason from MT5 history or fallback context."""
        orders = history_bundle.get("orders", [])
        deals = history_bundle.get("deals", [])
        all_text = " ".join(
            str(getattr(item, "comment", "") or getattr(item, "reason", "") or getattr(item, "external_id", "") or "")
            for item in [*orders, *deals]
        ).lower()
        if market_position is not None:
            return str(fallback_reason or "manual_close")
        if any(term in all_text for term in ["takeprofit", "tp"]):
            return "take_profit"
        if any(term in all_text for term in ["stoploss", "sl", "stop loss"]):
            return "stop_loss"
        if any(term in all_text for term in ["trailing"]):
            return "trailing_stop"
        if any(term in all_text for term in ["closeall", "session_close", "session close"]):
            return "session_close"
        if any(term in all_text for term in ["manual", "close"]):
            return "manual_close"
        if fallback_reason:
            return str(fallback_reason)
        return "resolved_from_history_unknown_exit_trigger"

    def _finalize_trade_from_history_row(self, trade_row: Any, source: str = "resolver") -> dict[str, Any]:
        """Finalize a trade row using MT5 history and persist the result."""
        if trade_row is None:
            return {"ok": False, "status": "missing_trade_row"}
        row = dict(trade_row) if hasattr(trade_row, "keys") else dict(trade_row)
        ticket = str(row.get("mt5_ticket") or row.get("position_id") or row.get("trade_id") or row.get("ticket") or "")
        if not ticket:
            return {"ok": False, "status": "missing_ticket"}
        detected_close_at = str(row.get("detected_close_at") or row.get("updated_at") or row.get("closed_at") or row.get("timestamp") or utc_now().isoformat())
        attempts = int(row.get("resolution_attempts", 0) or 0) + 1
        expected = {
            "order_id": row.get("order_id"),
            "symbol": row.get("symbol") or self.config["mt5"]["symbol"],
            "expected_volume": float(row.get("final_volume") or row.get("volume") or row.get("lot_size") or 0.0),
            "expected_close_at": detected_close_at,
            "entry_price": float(row.get("entry_price") or row.get("entry") or 0.0),
            "entry_price_tolerance_points": float(self.config.get("execution", {}).get("pending_fill_entry_tolerance_points", 25.0) or 25.0),
        }
        try:
            resolution = self.connector.resolve_trade_close_history(
                ticket,
                row.get("created_at") or row.get("opened_at") or row.get("timestamp"),
                expected=expected,
                lookback_minutes=int(self.config.get("exit", {}).get("trade_history_resolve_lookback_minutes", 1440)),
            )
        except Exception as exc:
            self._record_event(
                "WARNING",
                "trade_history_resolver_failed",
                f"Failed to fetch history bundle for {ticket}",
                details={"ticket": ticket, "source": source, "error": str(exc)},
            )
            payload = self._record_trade_resolution_stage(
                row,
                status="CLOSE_HISTORY_PENDING",
                source=source,
                reason=str(exc),
                evidence={"stage": "close_history_pending", "error": str(exc)},
                attempt_count=attempts,
            )
            return {"ok": False, "status": "CLOSE_HISTORY_PENDING", "reason": str(exc), "attempts": attempts, "payload": payload}

        if not bool(resolution.get("resolved")):
            status = str(resolution.get("status") or "CLOSE_HISTORY_PENDING")
            reason = str(resolution.get("reason") or "history_unavailable")
            payload = self._record_trade_resolution_stage(
                row,
                status=status,
                source=source,
                reason=reason,
                evidence=dict(resolution.get("evidence") or {}),
                attempt_count=attempts,
            )
            return {"ok": False, "status": status, "reason": reason, "attempts": attempts, "payload": payload}

        deals = list(resolution.get("deals") or [])
        orders = list(resolution.get("orders") or [])
        close_deal = resolution.get("close_deal") or (deals[-1] if deals else None)
        close_price = float(getattr(close_deal, "price", row.get("exit_price") or row.get("entry_price") or 0.0) or 0.0)
        entry_price = float(row.get("entry_price") or row.get("entry") or 0.0)
        volume = float(row.get("volume") or row.get("lot_size") or 0.0)
        pip_size = float(self.connector.get_symbol_spec()["pip_size"])
        pnl = 0.0
        fees = 0.0
        swap = 0.0
        commissions = 0.0
        if close_deal is not None:
            pnl = float(getattr(close_deal, "profit", 0.0) or 0.0)
            swap = float(getattr(close_deal, "swap", 0.0) or 0.0)
            commissions = float(getattr(close_deal, "commission", 0.0) or 0.0)
            fees = swap + commissions
        if abs(pnl) <= 1e-8 and entry_price and close_price:
            direction = str(row.get("side") or row.get("direction") or "LONG")
            pnl = self._calculate_pnl(direction, entry_price, close_price, volume)
        risk_amount = float(row.get("risk_amount") or row.get("initial_risk_price") or 0.0)
        if not risk_amount and row.get("initial_risk_price"):
            risk_amount = float(row.get("initial_risk_price")) * max(volume, 1.0)
        realized_r = pnl / max(risk_amount, 1e-9)
        corrected_r = self.calculate_trade_r_multiple(
            {
                **row,
                "entry_price": entry_price,
                "stop_loss": row.get("stop_loss") or row.get("sl"),
                "exit_price": close_price,
                "volume": volume,
                "direction": str(row.get("side") or row.get("direction") or "LONG"),
                "setup_family": row.get("setup_family") or "MANUAL",
            }
        )
        if corrected_r is not None:
            realized_r = corrected_r
        logger = getattr(self, "logger", None)
        log_warning = getattr(logger, "warning", None)
        if abs(realized_r) > 5 and callable(log_warning):
            log_warning("Realized R outside expected range: %.2f (ticket=%s)", realized_r, ticket)
        hold_seconds = None
        hold_minutes = None
        opened_at = to_utc(row.get("created_at") or row.get("opened_at") or row.get("timestamp"))
        history_close_at = to_utc(getattr(close_deal, "time", None)) if close_deal is not None else None
        if history_close_at is None:
            history_close_at = to_utc(getattr(close_deal, "time_msc", None) / 1000.0) if close_deal is not None and getattr(close_deal, "time_msc", None) else None
        detected_close_dt = to_utc(detected_close_at)
        closed_at = history_close_at or detected_close_dt or utc_now()
        if opened_at and closed_at:
            hold_seconds = max(0.0, (closed_at - opened_at).total_seconds())
            hold_minutes = hold_seconds / 60.0
        close_source = "history" if history_close_at is not None else "fallback_detection"
        history_bundle = {"orders": orders, "deals": deals}
        close_reason = self._derive_close_reason_from_history(row, history_bundle, market_position=None, fallback_reason=str(row.get("close_reason") or row.get("unresolved_reason") or "unknown_history_resolved"))
        outcome_label = "BREAKEVEN" if abs(pnl) <= 1e-8 else ("WIN" if pnl > 0 else "LOSS")
        evidence_payload = dict(resolution.get("evidence") or {})
        finalized = {
            **row,
            "trade_id": ticket,
            "mt5_ticket": ticket,
            "position_id": row.get("position_id") or ticket,
            "status": "FINALIZED",
            "event_type": "TRADE_FINALIZED",
            "exit_price": close_price,
            "pnl": pnl,
            "fees": fees,
            "swap": swap,
            "commissions": commissions,
            "realized_r": realized_r,
            "hold_minutes": hold_minutes,
            "hold_seconds": hold_seconds,
            "win_loss": outcome_label,
            "outcome_label": outcome_label,
            "closed_at": closed_at.isoformat() if closed_at else utc_now().isoformat(),
            "updated_at": utc_now().isoformat(),
            "unresolved_reason": None,
            "resolution_attempts": attempts,
            "last_resolution_attempt_at": utc_now().isoformat(),
            "resolution_stage": "finalized",
            "resolution_started_at": row.get("resolution_started_at") or row.get("closed_at") or detected_close_at,
            "resolution_last_evidence_at": utc_now().isoformat(),
            "matched_order_ids": ",".join(str(item) for item in (evidence_payload.get("matched_order_ids") or []) if str(item).strip()),
            "matched_deal_ids": ",".join(str(item) for item in (evidence_payload.get("matched_deal_ids") or []) if str(item).strip()),
            "matched_volume": evidence_payload.get("matched_volume"),
            "reconciliation_confidence": evidence_payload.get("confidence"),
            "close_price_source": "history_deal" if close_deal is not None else close_source,
            "raw_mt5_json": json.dumps(
                {
                    "orders": [getattr(order, "_asdict", lambda: {"repr": str(order)})() if hasattr(order, "_asdict") else {"repr": str(order)} for order in orders],
                    "deals": [getattr(deal, "_asdict", lambda: {"repr": str(deal)})() if hasattr(deal, "_asdict") else {"repr": str(deal)} for deal in deals],
                    "evidence": evidence_payload,
                },
                ensure_ascii=True,
                default=str,
                sort_keys=True,
            ),
            "close_reason": close_reason,
            "note": row.get("note") or close_reason,
            "timestamp": row.get("timestamp") or closed_at.isoformat(),
        }
        existing_metadata = row.get("metadata_json") if isinstance(row.get("metadata_json"), dict) else {}
        trade_payload = {
            **finalized,
            "entry_price": entry_price,
            "stop_loss": row.get("stop_loss") or row.get("sl"),
            "take_profit": row.get("take_profit") or row.get("tp"),
            "symbol": row.get("symbol") or self.config["mt5"]["symbol"],
            "side": row.get("side") or row.get("direction"),
            "bot_mode": row.get("bot_mode") or row.get("mode"),
            "magic_number": row.get("magic_number") or self.config["mt5"]["magic_number"],
            "comment": row.get("comment"),
            "metadata": {
                **(row.get("metadata") if isinstance(row.get("metadata"), dict) else {}),
                **existing_metadata,
                "close_source": close_source,
                "history_available": True,
                "detected_close_at": detected_close_at,
                "chosen_timestamp": closed_at.isoformat(),
                "pending_resolution": False,
                "resolution_source": source,
            },
            "raw_mt5_json": {
                "orders": [getattr(order, "_asdict", lambda: str(order))() if hasattr(order, "_asdict") else str(order) for order in orders],
                "deals": [getattr(deal, "_asdict", lambda: str(deal))() if hasattr(deal, "_asdict") else str(deal) for deal in deals],
                "evidence": evidence_payload,
            },
            "resolution_metadata": evidence_payload,
        }
        self.trade_journal.record_trade_close(trade_payload)
        correlation_monitor = getattr(self, "correlation_monitor", None)
        if correlation_monitor is not None and realized_r is not None:
            correlation_monitor.add_trade_result(str(trade_payload.get("setup_family") or "UNKNOWN"), float(realized_r), utc_now())
        self._record_event(
            "INFO",
            "trade_finalized",
            f"Trade {ticket} finalized from history",
            details={
                "ticket": ticket,
                "close_reason": close_reason,
                "outcome_label": outcome_label,
                "source": source,
                "close_source": close_source,
                "history_available": True,
                "detected_close_at": detected_close_at,
                "chosen_timestamp": closed_at.isoformat(),
            },
        )
        tracked_open_position = self.state.get("open_position") or {}
        if str(tracked_open_position.get("ticket") or "") == ticket:
            self.state["open_position"] = None
        return {"ok": True, "status": "FINALIZED", "payload": trade_payload, "attempts": attempts}

    def _calculate_pnl(self, direction: str, entry_price: float, exit_price: float, volume: float) -> float:
        """Calculate realized PnL using pip value per lot."""
        spec = self.connector.get_symbol_spec()
        pip_move = price_to_pips(exit_price - entry_price, spec["pip_size"]) if direction == "LONG" else price_to_pips(entry_price - exit_price, spec["pip_size"])
        sign = 1 if (exit_price >= entry_price and direction == "LONG") or (exit_price <= entry_price and direction == "SHORT") else -1
        return pip_move * float(spec["pip_value_per_lot"]) * volume * sign

    def _finalize_demo_trade(self, position: dict[str, Any], exit_price: float, close_reason: str) -> None:
        """Close a demo trade and record final statistics."""
        remaining_pnl = self._calculate_pnl(str(position["direction"]), float(position["entry_price"]), float(exit_price), float(position["remaining_volume"]))
        total_pnl = float(position.get("partial_realized_pnl", 0.0)) + remaining_pnl
        closed_at = utc_now()
        hold_minutes = (closed_at - to_utc(position.get("opened_at"))).total_seconds() / 60.0 if to_utc(position.get("opened_at")) else 0.0
        self._apply_trade_close_state(total_pnl, closed_at, str(position.get("session_at_entry", "UNKNOWN")))
        trade_payload = {
            "timestamp": closed_at.isoformat(),
            "event_type": "CLOSE",
            "mode": position["mode"],
            "symbol": position["symbol"],
            "side": position["direction"],
            "type": position["direction"],
            "ticket": position["ticket"],
            "position_id": position["position_id"],
            "setup_fingerprint": position["setup_fingerprint"],
            "setup_family": position["setup_family"],
            "setup_type": position["setup_type"],
            "regime_at_entry": position["regime_at_entry"],
            "session_at_entry": position["session_at_entry"],
            "trigger_type": position["trigger_type"],
            "entry_time": position["opened_at"],
            "exit_time": closed_at.isoformat(),
            "entry": position["entry_price"],
            "entry_price": position["entry_price"],
            "exit_price": float(exit_price),
            "sl": position["sl"],
            "tp": position["tp2"],
            "volume": position["volume"],
            "lot_size": position["volume"],
            "final_volume": position["remaining_volume"],
            "pnl": total_pnl,
            "pnl_pips": price_to_pips(float(exit_price) - float(position["entry_price"]), self.connector.get_symbol_spec()["pip_size"]) if str(position["direction"]) == "LONG" else price_to_pips(float(position["entry_price"]) - float(exit_price), self.connector.get_symbol_spec()["pip_size"]),
            "realized_r": total_pnl / max(float(position["initial_risk_price"]), 1e-9),
            "mae": position.get("mae"),
            "mfe": position.get("mfe"),
            "hold_minutes": hold_minutes,
            "status": "CLOSED",
            "win_loss": "WIN" if total_pnl >= 0 else "LOSS",
            "close_reason": close_reason,
            "note": close_reason,
            "metadata": position,
        }
        corrected_r = self.calculate_trade_r_multiple(
            {
                **trade_payload,
                "stop_loss": position.get("sl"),
                "direction": position.get("direction"),
                "volume": position.get("volume"),
            }
        )
        if corrected_r is not None:
            trade_payload["realized_r"] = corrected_r
        if self.correlation_monitor is not None and trade_payload.get("realized_r") is not None:
            self.correlation_monitor.add_trade_result(str(position.get("setup_family") or "UNKNOWN"), float(trade_payload["realized_r"]), utc_now())
        self._journal_trade_close(trade_payload)
        self._safe_notify_call(lambda: self.notifier.send_trade_close({**trade_payload, "setup_family": position["setup_family"], "close_reason": close_reason}))
        self.state["demo_position"] = None

    def _finalize_live_trade_from_history(self, position_state: dict[str, Any], close_reason: str) -> None:
        """Use account history to finalize a closed live position."""
        now_iso = utc_now().isoformat()
        detected_close_at = str(position_state.get("detected_close_at") or now_iso)
        trade_row = {
            **position_state,
            "trade_id": self._trade_identity(position_state),
            "mt5_ticket": position_state.get("ticket"),
            "position_id": position_state.get("position_id") or position_state.get("ticket"),
            "close_reason": close_reason,
            "unresolved_reason": close_reason,
            "status": "PENDING_CLOSE_RESOLUTION",
            "event_type": "TRADE_CLOSED",
            "note": close_reason,
            "created_at": position_state.get("opened_at"),
            "updated_at": now_iso,
            "resolution_stage": "close_detected",
            "resolution_started_at": detected_close_at,
            "timestamp": str(position_state.get("timestamp") or position_state.get("opened_at") or detected_close_at),
            "closed_at": str(position_state.get("closed_at") or detected_close_at),
            "detected_close_at": detected_close_at,
            "metadata": {
                **(position_state.get("metadata") if isinstance(position_state.get("metadata"), dict) else {}),
                "close_source": "pending_resolution",
                "history_available": False,
                "detected_close_at": detected_close_at,
                "pending_resolution": True,
                "close_reason": close_reason,
            },
        }
        self._journal_trade_close(trade_row)
        self._finalize_trade_from_history_row(trade_row, source="live_close")
        self.state["open_position"] = None

    def _filter_validation_rows(
        self,
        rows: list[dict[str, Any]],
        day_key: str,
        scope_type: str,
        scope_name: str,
        session_field: str,
    ) -> list[dict[str, Any]]:
        """Filter CSV-backed journal rows to a day or session validation scope."""
        scoped = [row for row in rows if str(row.get("timestamp", "")).startswith(day_key)]
        if scope_type == "session":
            scoped = [row for row in scoped if str(row.get(session_field, "")) == scope_name]
        return scoped

    def _validation_row_identity(self, row: dict[str, Any]) -> str:
        """Return a stable identity for validation-summary deduplication."""
        fingerprint = str(row.get("setup_fingerprint", "") or "").strip()
        if fingerprint:
            return fingerprint
        return "|".join(
            [
                str(row.get("event_type", "")),
                str(row.get("timestamp", "")),
                str(row.get("setup_family", "")),
                str(row.get("reason_code", "")),
            ]
        )

    def _build_validation_report(self, day_key: str, scope_type: str, scope_name: str, mode_ctx: dict[str, Any]) -> dict[str, Any]:
        """Aggregate a daily or session validation summary from CSV journals."""
        validation_cfg = self.config.get("validation", {})
        signals = self._load_csv_rows(self.logger.signals_path)
        signal_rows = self._filter_validation_rows(signals, day_key, scope_type, scope_name, "session_name")
        observed_rows = [row for row in signal_rows if str(row.get("event_type", "")).upper() in {"SIGNAL_OBSERVED", "SIGNAL_CREATED", "CANDIDATE_CREATED"}]
        validation_rows = [
            row
            for row in signal_rows
            if str(row.get("event_type", "")).upper() in {
                "EXECUTION_ATTEMPT",
                "VALIDATION_GATE",
                "SIGNAL_OBSERVED",
                "SIGNAL_CREATED",
                "CANDIDATE_CREATED",
                "CANDIDATE_REJECTED_STRATEGY",
                "CANDIDATE_REJECTED_RISK",
                "CANDIDATE_REJECTED_SYSTEM",
                "ORDER_MISSED_DRIFT",
                "ORDER_MISSED_STATE_BLOCK",
                "ORDER_MISSED_RISK_BLOCK",
            }
        ]
        setups_by_family = Counter(str(row.get("setup_family") or "UNKNOWN") for row in observed_rows)
        anti_chase_reasons = {"blocked_chasing_entry", "blocked_far_from_value_zone", "blocked_expanded_trigger"}

        normalized_rows: list[tuple[str | None, dict[str, Any]]] = []
        for row in validation_rows:
            normalized_rows.append((self._normalize_block_reason(str(row.get("reason_code") or ""), str(row.get("reason") or "")), row))

        def unique_count(reason_codes: set[str]) -> int:
            return len(
                {
                    self._validation_row_identity(row)
                    for reason_code, row in normalized_rows
                    if reason_code in reason_codes
                }
            )

        neutral_reasons = {
            None,
            "",
            "entry_valid",
            "live_validation_passed",
            "live_eligible_setup",
            "dry_run_validated",
            "paper_validated_test_mode",
            "executed",
            "executed_live",
        }
        top_block_reasons = Counter(
            reason_code
            for reason_code, _ in normalized_rows
            if reason_code not in neutral_reasons
        )
        consistency = self._check_journaling_consistency(
            day_key=day_key,
            session_name=scope_name if scope_type == "session" else None,
            require_analytics_row=scope_type == "day",
        )
        journaling_errors = self.database.count_events(
            day=day_key,
            reason_codes=[
                "journaling_failure",
                "journaling_consistency_failed",
                "analytics_summary_write_failed",
                "validation_summary_write_failed",
                "csv_read_failure",
            ],
        )
        if not consistency["ok"]:
            journaling_errors += 1

        return {
            "timestamp": utc_now().isoformat(),
            "day": day_key,
            "scope_type": scope_type,
            "scope_name": scope_name,
            "mode": mode_ctx["mode"],
            "rollout_phase": mode_ctx["rollout_phase"],
            "setups_by_family_json": json.dumps(dict(setups_by_family), ensure_ascii=True, sort_keys=True),
            "blocked_by_regime": unique_count({"blocked_bad_regime"}),
            "blocked_by_session": unique_count({"blocked_bad_session"}),
            "blocked_by_anti_chase": unique_count(anti_chase_reasons),
            "duplicate_entries_prevented": unique_count({"duplicate_entry_blocked", "setup_fingerprint_already_traded"}),
            "live_eligible_setups": unique_count({"live_eligible_setup"}),
            "paper_validated_setups": unique_count({"dry_run_validated", "paper_validated_test_mode"}),
            "journaling_errors": journaling_errors,
            "top_block_reasons_json": json.dumps(
                dict(top_block_reasons.most_common(int(validation_cfg.get("report_top_block_reasons_limit", 5)))),
                ensure_ascii=True,
                sort_keys=True,
            ),
            "consistency_ok": consistency["ok"],
            "consistency_summary_json": json.dumps(consistency, ensure_ascii=True, sort_keys=True),
        }

    def _emit_validation_report(self, day_key: str, scope_type: str, scope_name: str, mode_ctx: dict[str, Any]) -> None:
        """Persist a validation summary once per day/session scope."""
        validation_cfg = self.config.get("validation", {})
        if scope_type == "day" and not bool(validation_cfg.get("daily_report_enabled", True)):
            return
        if scope_type == "session" and not bool(validation_cfg.get("session_report_enabled", True)):
            return

        validation_state = self._validation_state()
        session_key = f"{day_key}|{scope_name}"
        if scope_type == "day" and validation_state.get("last_validation_report_day") == day_key:
            return
        if scope_type == "session" and validation_state.get("last_validation_report_session_key") == session_key:
            return

        payload = self._build_validation_report(day_key, scope_type, scope_name, mode_ctx)
        ok = self._safe_journal_call(
            f"{scope_type} validation report journal",
            lambda: (
                self.logger.log_validation_summary(payload),
                self.database.insert_validation_report(payload),
            ),
        )
        if not ok:
            self._register_validation_failure(
                "validation_summary_write_failed",
                f"Failed writing {scope_type} validation report",
                {"day": day_key, "scope_name": scope_name},
            )
            return
        self.logger.structured("validation_report", payload)
        if scope_type == "day":
            validation_state["last_validation_report_day"] = day_key
        else:
            validation_state["last_validation_report_session_key"] = session_key

    def _emit_daily_analytics(self, day_key: str, mode: str) -> None:
        """Build and persist a daily analytics summary once per day."""
        if self.state.get("daily_summary_date") == day_key:
            return
        summary = self.database.build_daily_analytics(day_key)
        payload = {
            "timestamp": utc_now().isoformat(),
            "day": day_key,
            "mode": mode,
            "setups": summary["setups"],
            "live_trades": summary["live_trades"],
            "win_rate": summary["win_rate"],
            "average_r": summary["average_r"],
            "average_hold_minutes": summary["average_hold_minutes"],
            "long_trades": summary["long_trades"],
            "short_trades": summary["short_trades"],
            "by_setup_family_json": summary["by_setup_family"],
            "by_regime_json": summary["by_regime"],
            "by_session_json": summary["by_session"],
        }
        ok = self._safe_journal_call(
            "daily analytics journal",
            lambda: (
                self.logger.log_analytics_summary(payload),
                self.database.insert_daily_analytics(
                    {
                        **payload,
                        "by_setup_family_json": json.dumps(payload["by_setup_family_json"], ensure_ascii=True, sort_keys=True),
                        "by_regime_json": json.dumps(payload["by_regime_json"], ensure_ascii=True, sort_keys=True),
                        "by_session_json": json.dumps(payload["by_session_json"], ensure_ascii=True, sort_keys=True),
                    }
                ),
            ),
        )
        if not ok:
            self._register_validation_failure(
                "analytics_summary_write_failed",
                "Failed writing daily analytics summary",
                {"day": day_key, "mode": mode},
            )
            return
        self._safe_notify_call(
            lambda: self.notifier.send_daily_summary(
                {
                    "trades": summary["live_trades"],
                    "wins": round(summary["win_rate"] * summary["live_trades"]) if summary["live_trades"] else 0,
                    "losses": summary["live_trades"] - (round(summary["win_rate"] * summary["live_trades"]) if summary["live_trades"] else 0),
                    "pnl": float(self.state.get("daily_pnl", 0.0)),
                    "win_rate": summary["win_rate"] * 100.0,
                }
            )
        )
        self.state["daily_summary_date"] = day_key
        self._emit_validation_report(day_key, "day", day_key, self._execution_mode_summary())

    def _generate_daily_reports(self, day_key: str, source: str = "manual") -> dict[str, Any]:
        """Generate structured daily reports and export CSV/markdown outputs."""
        summary = self.database.build_daily_analytics(day_key)
        performance = self.database.build_trade_performance_report(day_key)
        paths = self.trade_journal.write_daily_reports(day_key, summary, performance)
        payload = {
            "day": day_key,
            "source": source,
            "summary": summary,
            "performance_rows": len(performance.get("rows", [])) if isinstance(performance, dict) else 0,
            "paths": {key: str(value) for key, value in paths.items()},
        }
        self.logger.structured("daily_report_generated", payload)
        self._safe_notify_call(
            lambda: self.notifier.send_info(
                f"Daily report generated for {day_key} | trades={summary.get('live_trades', 0)} | pnl={float(summary.get('total_pnl', 0.0)):.2f} | r={float(summary.get('total_r', 0.0)):.2f}"
            )
        )
        self.state["daily_report_generated_date"] = day_key
        return payload

    def _cooldown_snapshot(self, now_utc: datetime) -> dict[str, Any]:
        """Return current cooldown state with remaining time."""
        cooldowns = self.state.setdefault("cooldowns_v2", {})
        snapshot: dict[str, Any] = {"reason": cooldowns.get("reason")}
        for key in ["entry_until", "post_loss_until", "consecutive_loss_until"]:
            expiry = to_utc(cooldowns.get(key))
            active = bool(expiry and now_utc < expiry)
            remaining = max(0, int((expiry - now_utc).total_seconds())) if active and expiry else 0
            snapshot[key] = {
                "active": active,
                "until": expiry.isoformat() if expiry else None,
                "remaining_seconds": remaining,
            }
        return snapshot

    def _recent_setup_fingerprints(self, limit: int = 8) -> list[dict[str, Any]]:
        """Return the most recent traded/attempted setup fingerprints from state."""
        registry = self._setup_registry()
        ordered = sorted(
            registry.items(),
            key=lambda item: str(item[1].get("last_updated_at") or item[1].get("first_seen_at") or ""),
            reverse=True,
        )
        return [
            {
                "setup_fingerprint": fingerprint,
                "status": payload.get("status"),
                "ticket": payload.get("ticket"),
                "last_updated_at": payload.get("last_updated_at"),
            }
            for fingerprint, payload in ordered[:limit]
        ]

    def shutdown(self) -> None:
        """Run the shutdown sequence."""
        try:
            shutdown_reason = str(self.state.get("shutdown_reason") or "shutdown")
            if self.config["bot"].get("close_positions_on_shutdown", False):
                if self.demo_mode and self.state.get("demo_position"):
                    self._finalize_demo_trade(self.state["demo_position"], float(self.state["demo_position"]["entry_price"]), "shutdown")
                elif not self.demo_mode and self.state.get("open_position"):
                    self.connector.close_all_bot_positions()
                    self._finalize_live_trade_from_history(self.state["open_position"], "shutdown")
            self._emit_daily_analytics(utc_now().date().isoformat(), self._execution_mode_summary()["mode"])
            if self.decision_logger is not None:
                self.decision_logger.flush_daily_logs()
            self.trade_journal.flush_pending()
            self._write_heartbeat("STOPPED", None, None, self.connector.account_info, 0, shutdown_reason)
            self.save_state()
            if shutdown_reason == "fatal_error":
                self._record_event("ERROR", "shutdown", "Bot stopped after fatal error", reason_code=shutdown_reason)
            else:
                self._record_event("INFO", "shutdown", "Bot stopped cleanly")
                self._safe_notify_call(lambda: self.notifier.send_info("Bot stopped cleanly"))
        finally:
            try:
                self.telegram_command_service.stop()
            except Exception:
                pass
            try:
                lebprime_live = getattr(self, "lebprime_v3_4_0", None)
                if lebprime_live is not None:
                    lebprime_live.shutdown()
            except Exception:
                pass
            self.connector.shutdown()
            self._release_instance_lock()
            self.control_state.mark_process_stopped(str(self.state.get("shutdown_reason") or "shutdown"))

    def run_diagnostics(self) -> int:
        """Print and log a rollout-aware diagnostics snapshot without running the bot loop."""
        try:
            now_utc = utc_now()
            mode_ctx = self._execution_mode_summary()
            report = self.connector.health_report()
            session_info = classify_session(self.config, now_utc)
            try:
                account_info = self.connector.get_account_info() if report.get("connection_ok") and report.get("symbol_ok") else None
            except Exception:
                account_info = None
            risk_snapshot = (
                self.risk_manager.evaluate_risk_gate(
                    self.state,
                    account_info,
                    now_utc,
                    session_info["session_name"],
                    live_requested=bool(mode_ctx["mode"] == "LIVE"),
                    force_reduced_risk=bool(mode_ctx["force_reduced_risk_mode"]),
                )
                if account_info is not None
                else None
            )
            try:
                open_positions = [
                    {
                        "ticket": str(position.ticket),
                        "volume": float(position.volume),
                        "price_open": float(position.price_open),
                    }
                    for position in (self.connector.get_bot_positions() if report.get("connection_ok") else [])
                ]
            except Exception:
                open_positions = []
            try:
                pending_orders = [
                    {
                        "ticket": str(order.ticket),
                        "volume": float(order.volume_current),
                        "price_open": float(order.price_open),
                    }
                    for order in (self.connector.get_pending_orders() if report.get("connection_ok") else [])
                ]
            except Exception:
                pending_orders = []

            active_locks: dict[str, Any] = {}
            for key, value in self.state.get("locks", {}).items():
                if isinstance(value, dict):
                    if value.get("active"):
                        active_locks[key] = value
                elif bool(value):
                    active_locks[key] = value

            consistency = None
            if bool(self.config.get("validation", {}).get("diagnostics_include_csv_consistency", True)):
                consistency = self._check_journaling_consistency(day_key=now_utc.date().isoformat(), require_analytics_row=False)

            journal_state = self.state.setdefault("journal", {})
            validation_state = self._validation_state()
            diagnostics = {
                "timestamp": now_utc.isoformat(),
                "rollout_phase": mode_ctx["rollout_phase"],
                "mode": mode_ctx["mode"],
                "live_execution_enabled": bool(mode_ctx["mode"] == "LIVE"),
                "live_execution_disabled": bool(validation_state.get("live_disabled") or journal_state.get("disabled_live") or self.state.get("locks", {}).get("kill_switch")),
                "allow_live_execution_flag": bool(mode_ctx["allow_live_execution"]),
                "force_reduced_risk_mode": bool(mode_ctx["force_reduced_risk_mode"]),
                "active_risk_locks": active_locks,
                "risk_snapshot": risk_snapshot,
                "current_cooldowns": self._cooldown_snapshot(now_utc),
                "degraded_mode": self._degraded_state(),
                "open_positions": open_positions,
                "pending_orders": pending_orders,
                "last_traded_setup_fingerprints": self._recent_setup_fingerprints(),
                "journaling_health": {
                    "journal_failure_count": int(journal_state.get("failure_count", 0)),
                    "journal_disabled_live": bool(journal_state.get("disabled_live", False)),
                    "last_error": journal_state.get("last_error"),
                    "consistency": consistency,
                },
                "mt5_health": report,
            }
            validation_state["last_diagnostics_at"] = now_utc.isoformat()
            self.state["last_diagnostics_snapshot"] = diagnostics
            self.logger.structured("diagnostics_snapshot", diagnostics)

            print("Bot Diagnostics")
            print(f"Rollout Phase: {diagnostics['rollout_phase']}")
            print(f"Mode: {diagnostics['mode']}")
            print(f"Live Execution Enabled: {diagnostics['live_execution_enabled'] and not diagnostics['live_execution_disabled']}")
            print(f"Force Reduced Risk Mode: {diagnostics['force_reduced_risk_mode']}")
            print(f"Session: {session_info['session_name']}")
            print(f"Terminal Connected: {report.get('connection', {}).get('terminal_connected')}")
            print(f"Terminal Trade Allowed: {report.get('connection', {}).get('trade_allowed')}")
            print(f"Trade API Disabled: {report.get('connection', {}).get('tradeapi_disabled')}")
            print(f"Symbol Visible: {report.get('symbol_snapshot', {}).get('visible')}")
            print(f"Symbol Trade Mode: {report.get('symbol_snapshot', {}).get('trade_mode')}")
            print(f"Active Risk Locks: {json.dumps(active_locks, ensure_ascii=True, default=str)}")
            print(f"Current Cooldowns: {json.dumps(diagnostics['current_cooldowns'], ensure_ascii=True, default=str)}")
            print(f"Degraded Mode: {json.dumps(diagnostics['degraded_mode'], ensure_ascii=True, default=str)}")
            print(f"Open Positions: {json.dumps(open_positions, ensure_ascii=True, default=str)}")
            print(f"Pending Orders: {json.dumps(pending_orders, ensure_ascii=True, default=str)}")
            print(f"Last Traded Setup Fingerprints: {json.dumps(diagnostics['last_traded_setup_fingerprints'], ensure_ascii=True, default=str)}")
            print(f"Journaling Health: {json.dumps(diagnostics['journaling_health'], ensure_ascii=True, default=str)}")
            print(f"MT5 Health: {json.dumps(report, ensure_ascii=True, default=str)}")
            diagnostics_ok = bool(report.get("connection_ok") and report.get("symbol_ok") and report.get("tick_ok"))
            if consistency is not None:
                diagnostics_ok = diagnostics_ok and bool(consistency.get("ok"))
            return 0 if diagnostics_ok else 1
        finally:
            self.connector.shutdown()
            self._release_instance_lock()


def main() -> int:
    """Entrypoint for the trading bot."""
    base_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--diagnostics", action="store_true")
    parser.add_argument("--reconcile-trades", action="store_true")
    parser.add_argument("--finalize-unresolved", action="store_true")
    parser.add_argument("--generate-daily-report", action="store_true")
    parser.add_argument("--report-day", type=str, default=None)
    args = parser.parse_args()
    bot = TradingBot(base_dir)
    if args.diagnostics:
        return bot.run_diagnostics()
    if args.reconcile_trades:
        summary = bot._reconcile_mt5_positions_with_database()
        unresolved = bot._resolve_unresolved_trades(source="cli_reconcile")
        bot.trade_journal.flush_pending()
        print(json.dumps({"reconcile": summary, "unresolved": unresolved}, ensure_ascii=True, default=str))
        return 0
    if args.finalize_unresolved:
        unresolved = bot._resolve_unresolved_trades(source="cli_finalize")
        bot.trade_journal.flush_pending()
        print(json.dumps(unresolved, ensure_ascii=True, default=str))
        return 0
    if args.generate_daily_report:
        report_day = args.report_day or utc_now().date().isoformat()
        report = bot._generate_daily_reports(report_day, source="cli")
        print(json.dumps(report, ensure_ascii=True, default=str))
        return 0
    bot.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
