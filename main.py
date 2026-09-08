"""Main runner for the regime-aware MT5 XAUUSD scalping bot."""

from __future__ import annotations

import csv
import json
import os
import signal
import sys
import time
import traceback
import argparse
import subprocess
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import pandas as pd
import pytz

from logger import BotLogger
from mt5_connector import MT5Connector
from risk_manager import RiskManager
from dashboard.services import DashboardDataService
from services.audit_service import AuditService
from services.bot_control_service import BotControlService
from services.config_manager import ConfigManager
from services.database import DatabaseService
from services.execution_flow import normalize_final_outcome, resolve_spread_limit, should_mark_duplicate_or_cooldown
from services.control_state import ControlStateService
from services.heartbeat import HeartbeatService
from services.manual_trade_service import ManualTradeService
from services.operator_service import OperatorService
from services.trade_journal import TradeJournalService
from services.state_manager import StateManager
from services.telegram_notifier import TelegramNotifier
from services.telegram_command_service import TelegramCommandService
from strategy_factory import build_strategy_engine
from trading_bot.execution.decision_engine import BLOCK, ExecutionDecisionEngine
from trading_bot.execution.canonical import CanonicalExecutionPlan, build_execution_request_from_plan
from trading_bot.execution.exposure_policy import ExposurePolicy, ExposureSnapshot
from trading_bot.core.models import ExecutionRequest
from trading_bot.execution.execution_service import ExecutionService
from trading_bot.execution.mt5_broker import MT5Broker
from trading_bot.core.reasons import JournalEventType, ReasonCode
from trading_bot.positions.live_lifecycle import LivePositionLifecycleManager
from trading_bot.analytics.correlation_monitor import StrategyCorrelationMonitor
from trading_bot.analytics.risk_metrics import RMultipleCalculator, TradeRiskMetadata
from trading_bot.analytics.strategy_logger import StrategyDecisionLogger
from trading_bot.risk.engine import RiskEngine
from trading_bot.risk.position_sizing import DynamicPositionSizer, PositionSizeConfig
from trading_bot.strategy.shadow_tester import ShadowStrategyRunner
from trading_bot.strategy.diagnostics import build_strategy_candidate_diagnostics
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
        self.strategy = build_strategy_engine(self.config, self.base_dir)
        analytics_cfg = self.config.get("analytics", {})
        decision_cfg = analytics_cfg.get("decision_logging", {})
        shadow_cfg = analytics_cfg.get("shadow_testing", {})
        corr_cfg = analytics_cfg.get("correlation_monitoring", {})
        self.decision_logger = (
            StrategyDecisionLogger(log_dir=str(self.base_dir / str(decision_cfg.get("log_dir", "logs/strategy_decisions"))))
            if bool(decision_cfg.get("enabled", False))
            else None
        )
        self.r_calculator = RMultipleCalculator()
        self.shadow_runner = (
            ShadowStrategyRunner(
                strategy_engine=self.strategy,
                min_shadow_samples=int(shadow_cfg.get("min_samples_before_recommendation", 50)),
                required_win_rate=float(shadow_cfg.get("required_win_rate", 0.45)),
                required_expectancy=float(shadow_cfg.get("required_expectancy", 0.2)),
                shadow_families=list(shadow_cfg.get("families", ["TREND_PULLBACK_RECLAIM", "LIQUIDITY_SWEEP_REVERSAL"])),
            )
            if bool(shadow_cfg.get("enabled", False))
            else None
        )
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
        self.logger.structured(
            "engine_selection",
            {
                "selected_mode": self.config.get("bot", {}).get("execution_mode"),
                "engine_type": getattr(self.strategy, "engine_type", self.config.get("bot", {}).get("execution_mode_summary", {}).get("engine_type")),
                "strategy_class": self.strategy.__class__.__name__,
                "enabled_strategy_families": self.config.get("bot", {}).get("execution_mode_summary", {}).get("enabled_families", []),
            },
            level="INFO",
        )
        self.risk_manager = RiskManager(self.config, logger=self.logger)
        self.risk_engine = RiskEngine(self.config, self.risk_manager, logger=self.logger)
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

    def _acquire_instance_lock(self) -> None:
        """Ensure only one bot process runs at a time."""
        self.instance_lock_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"pid": os.getpid(), "started_at": utc_now().isoformat()}
        while True:
            try:
                fd = os.open(str(self.instance_lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle)
                self.instance_lock_acquired = True
                return
            except FileExistsError:
                existing: dict[str, Any] = {}
                try:
                    existing = json.loads(self.instance_lock_path.read_text(encoding="utf-8"))
                except Exception:
                    existing = {}
                existing_pid = int(existing.get("pid") or 0)
                if existing_pid and self._pid_is_alive(existing_pid):
                    raise RuntimeError(f"Another bot instance is already running with PID {existing_pid}")
                try:
                    self.instance_lock_path.unlink(missing_ok=True)
                except Exception as exc:
                    raise RuntimeError("Unable to clear stale bot instance lock") from exc

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
        if pid <= 0:
            return False
        if os.name == "nt":
            try:
                result = subprocess.run(
                    [
                        "powershell",
                        "-NoProfile",
                        "-Command",
                        f"Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue | Select-Object -First 1 | Format-Table -HideTableHeaders",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                return bool((result.stdout or "").strip())
            except Exception:
                return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def _release_instance_lock(self) -> None:
        """Release the owned single-instance lock on shutdown."""
        if not self.instance_lock_acquired:
            return
        try:
            existing = json.loads(self.instance_lock_path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
        if int(existing.get("pid") or 0) == os.getpid():
            try:
                self.instance_lock_path.unlink(missing_ok=True)
            except Exception:
                pass
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
                # Save current state in case reload fails
                previous_config = dict(self.config)
                previous_strategy = self.strategy
                
                # Load new config
                refreshed = load_runtime_config(self.base_dir, require_mt5_credentials=False, apply_mode_env_override=False)
                
                # Try to build new strategy - this is where failures commonly occur
                new_strategy = build_strategy_engine(refreshed, self.base_dir)
                if new_strategy is None:
                    raise ValueError("Strategy engine build returned None (invalid configuration)")
                
                # Only apply changes if all critical components succeeded
                self.config = refreshed
                self.logger.config = refreshed
                self.connector.config = refreshed
                self.connector.mt5_config = refreshed["mt5"]
                self.execution_service = ExecutionService(MT5Broker(self.connector))
                self.strategy = new_strategy
                if self.shadow_runner is not None:
                    self.shadow_runner.engine = self.strategy
                
                self.logger.structured(
                    "engine_selection",
                    {
                        "selected_mode": refreshed.get("bot", {}).get("execution_mode"),
                        "engine_type": getattr(self.strategy, "engine_type", refreshed.get("bot", {}).get("execution_mode_summary", {}).get("engine_type")),
                        "strategy_class": self.strategy.__class__.__name__,
                        "enabled_strategy_families": refreshed.get("bot", {}).get("execution_mode_summary", {}).get("enabled_families", []),
                        "source": "config_reload",
                    },
                    level="INFO",
                )
            except Exception as exc:
                # Config reload failed - keep previous working config and strategy
                self.logger.structured(
                    "config_reload_failed_using_fallback",
                    {
                        "error": str(exc),
                        "error_type": exc.__class__.__name__,
                        "fallback_to_previous": True,
                        "previous_mode": self.config.get("bot", {}).get("execution_mode"),
                        "previous_strategy": self.strategy.__class__.__name__,
                    },
                    level="ERROR",
                )
                # Explicitly restore previous strategy if needed
                self.strategy = previous_strategy
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

    def is_backtest_mode(self) -> bool:
        """Return whether runtime mode is BACKTEST."""
        return self._execution_mode_summary()["mode"] == "BACKTEST"

    def requires_live_tick_validation(self) -> bool:
        """Return whether startup should hard-require fresh live tick prices."""
        mode = self._execution_mode_summary()["mode"]
        if mode == "BACKTEST":
            return False
        # LIVE/DRY_RUN/VALIDATION_TEST should start gracefully on closed sessions and
        # enforce freshness later through execution gating in `process_cycle`.
        return False

    def validate_live_market_readiness(self) -> dict[str, Any]:
        """Validate current live market-data readiness for runtime execution gating."""
        connection_stage = self.connector.connection_health_check(reconnect_if_needed=True)
        symbol_stage = (
            self.connector.symbol_prepare_stage(log_details=True)
            if connection_stage["ok"]
            else self._stage_result(
                "symbol_prepare",
                False,
                "symbol_prepare_skipped",
                "Symbol preparation skipped because MT5 connection is unavailable",
            )
        )
        tick_stage = (
            self.connector.tick_fetch_stage(refresh=False)
            if connection_stage["ok"] and symbol_stage["ok"]
            else self._stage_result(
                "fresh_tick_check",
                False,
                "fresh_tick_skipped",
                "Tick fetch skipped because MT5 connection is unavailable",
            )
        )
        return {
            "connection_stage": connection_stage,
            "symbol_stage": symbol_stage,
            "tick_stage": tick_stage,
            "ready": bool(connection_stage["ok"] and symbol_stage["ok"] and tick_stage["ok"]),
        }

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

    def _send_setup_notification_once(
        self,
        fingerprint: str,
        notification_key: str,
        callback: Callable[[], bool],
        status: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """Send a deduplicated Telegram notification per setup fingerprint and event key."""
        normalized_fingerprint = str(fingerprint or "").strip()
        if not normalized_fingerprint:
            return
        registry = self._setup_registry()
        registry_row = registry.get(normalized_fingerprint, {})
        existing_keys = registry_row.get("notification_keys", [])
        notification_keys = list(existing_keys) if isinstance(existing_keys, list) else []
        if notification_key in notification_keys:
            return
        self._safe_notify_call(callback)
        notification_keys.append(notification_key)
        update_payload = {"notification_keys": notification_keys[-24:]}
        if extra:
            update_payload.update(extra)
        self._update_setup_registry(
            normalized_fingerprint,
            status or str(registry_row.get("status") or "observed"),
            utc_now(),
            update_payload,
        )

    def _mark_hard_rejection(
        self,
        fingerprint: str,
        reason_code: str,
        reason: str,
        execution_result: dict[str, Any],
    ) -> None:
        """Track a broker/runtime hard rejection so repeated retries can cool down safely."""
        now_utc = utc_now()
        self._update_setup_registry(
            fingerprint,
            "hard_rejected",
            now_utc,
            {
                "last_attempt_at": now_utc.isoformat(),
                "last_hard_rejection_at": now_utc.isoformat(),
                "last_hard_rejection_reason": reason_code,
                "last_hard_rejection_message": reason,
                "last_hard_rejection_failure_class": execution_result.get("failure_class"),
            },
        )

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

    @staticmethod
    def _decision_status(reason_code: str | None, executed: bool = False) -> str:
        """Map internal reasons to a compact final decision label."""
        if executed:
            return "EXECUTED"
        normalized = str(reason_code or "").strip()
        if normalized in {"executed", "executed_live"}:
            return "EXECUTED"
        if normalized in {"", "entry_valid", "live_validation_passed", "live_eligible_setup", "dry_run_validated", "paper_validated_test_mode"}:
            return "ACCEPTED"
        if normalized in {"blocked_bad_regime", "regime_filter_blocked"}:
            return "BLOCKED_BY_REGIME"
        if normalized in {"blocked_bad_session", "session_filter_blocked", "session_bucket_live_disabled", "asia_session_disabled", "setup_family_live_disabled"}:
            return "BLOCKED_BY_SESSION"
        if normalized in {"blocked_chasing_entry", "blocked_far_from_value_zone", "blocked_expanded_trigger", "trigger_not_confirmed", "trigger_score_below_threshold", "entry_score_below_min_score_to_trade", "setup_score_below_threshold", "trend_score_below_threshold", "trend_alignment_required"}:
            return "BLOCKED_BY_ENTRY"
        if normalized in {"cooldown_active", "risk_lock_active", "disabled_due_to_drawdown"}:
            return "BLOCKED_BY_RISK"
        if normalized in {"journal_failure_lock", "journaling_failure"}:
            return "BLOCKED_BY_RISK"
        if normalized in {"order_send_failed", "order_send_runtime_failure", "order_check_api_error", "order_check_none", "blocked_invalid_volume", "blocked_invalid_stops", "blocked_invalid_fill_mode", "blocked_margin_constraint", "blocked_insufficient_margin"}:
            return "SEND_FAILED"
        return "BLOCKED_OTHER"

    def _final_live_validation_decision(
        self,
        candidate: dict[str, Any],
        live_entry_assessment: dict[str, Any],
        market_context: dict[str, Any],
        execution_action: str | None = None,
    ) -> dict[str, Any]:
        """Return the single source-of-truth live validation decision."""
        current_regime = str(market_context["regime"].get("regime_name", "UNKNOWN"))
        family_enabled = bool(candidate.get("family_enabled", False))
        family_live_allowed = bool(candidate.get("family_live_allowed", False))
        family_allowed_regimes = list(candidate.get("effective_allowed_regimes") or candidate.get("family_allowed_regimes") or [])
        regime_allow_range = bool(self.config.get("regime", {}).get("allow_range_regime", False))
        session_live_allowed = bool(market_context["session"].get("session_live_allowed", False))
        session_allowed = bool(candidate.get("session_allowed", session_live_allowed))
        regime_matches_family = current_regime in set(family_allowed_regimes) if family_allowed_regimes else True
        regime_live_allowed = regime_matches_family
        if current_regime == "RANGE_MEAN_REVERSION":
            regime_live_allowed = regime_matches_family and regime_allow_range

        final_block_reason = None
        if not family_enabled or not family_live_allowed:
            final_block_reason = "setup_family_live_disabled"
        elif not session_live_allowed or not session_allowed:
            final_block_reason = "blocked_bad_session"
        elif not regime_live_allowed:
            final_block_reason = "blocked_bad_regime"
        elif not bool(live_entry_assessment.get("live_ready", False)) and str(execution_action or BLOCK) == BLOCK:
            final_block_reason = self._normalize_block_reason(
                str(live_entry_assessment.get("reason_code")),
                str(live_entry_assessment.get("reason")),
            ) or str(live_entry_assessment.get("reason_code") or "live_validation_failed")

        return {
            "family_enabled": family_enabled,
            "family_live_allowed": family_live_allowed,
            "family_allowed_regimes": family_allowed_regimes,
            "regime_allow_range_regime": regime_allow_range,
            "regime_live_allowed": bool(regime_live_allowed),
            "session_live_allowed": session_live_allowed,
            "session_allowed": session_allowed,
            "final_live_decision": final_block_reason is None,
            "final_block_reason": final_block_reason,
        }

    def _log_execution_diagnostics(
        self,
        candidate: dict[str, Any],
        entry_assessment: dict[str, Any],
        market_context: dict[str, Any],
        live_validation: dict[str, Any],
    ) -> None:
        """Log the final execution validation decision before send or block."""
        blocked_reasons = set(entry_assessment.get("blocked_reasons", []) or [])
        diagnostics = {
            "timestamp": utc_now().isoformat(),
            "symbol": self.config["mt5"]["symbol"],
            "setup": candidate.get("setup_family"),
            "side": candidate.get("side"),
            "regime": market_context["regime"].get("regime_name"),
            "session": market_context["session"].get("session_name"),
            "session_live_allowed": live_validation.get("session_live_allowed"),
            "session_quality_score": market_context["session"].get("session_quality_score"),
            "family_enabled": live_validation.get("family_enabled"),
            "family_live_allowed": live_validation.get("family_live_allowed"),
            "family_allowed_regimes": live_validation.get("family_allowed_regimes"),
            "regime_allow_range_regime": live_validation.get("regime_allow_range_regime"),
            "regime_live_allowed": live_validation.get("regime_live_allowed"),
            "regime_confidence": market_context["regime"].get("regime_confidence"),
            "trend_score": candidate.get("trend_score"),
            "setup_score": candidate.get("setup_score"),
            "trigger_score": entry_assessment.get("trigger_score"),
            "entry_score": entry_assessment.get("entry_score"),
            "trigger_confirmed": "trigger_not_confirmed" not in blocked_reasons and bool(entry_assessment.get("trigger_filter_pass", False)),
            "value_zone_ok": "blocked_far_from_value_zone" not in blocked_reasons,
            "entry_mode": entry_assessment.get("entry_mode"),
            "final_live_decision": live_validation.get("final_live_decision"),
            "final_block_reason": live_validation.get("final_block_reason"),
            "decision_status": "ACCEPTED" if bool(live_validation.get("final_live_decision", False)) else f"BLOCKED_{str(live_validation.get('final_block_reason') or 'UNKNOWN').upper()}",
        }
        self.logger.structured("execution_validation_diagnostics", diagnostics)

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

    def _is_degraded_mode_active(self, now_utc: datetime | None = None) -> bool:
        """Return whether infrastructure degraded mode is still active."""
        degraded = self._degraded_state()
        if not degraded.get("active"):
            return False
        current_time = now_utc or utc_now()
        cooldown_until = to_utc(degraded.get("cooldown_until"))
        return bool(cooldown_until and current_time < cooldown_until)

    def _register_infra_failure(
        self,
        failure_class: str,
        reason_code: str,
        human_reason: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """Track repeated infrastructure failures and activate degraded mode when needed."""
        degraded = self._degraded_state()
        now_iso = utc_now().isoformat()
        same_class = degraded.get("last_failure_class") == failure_class
        degraded["failure_count"] = int(degraded.get("failure_count", 0)) + 1 if same_class else 1
        degraded["last_failure_class"] = failure_class
        degraded["last_failure_at"] = now_iso
        threshold = int(self.config["safety"].get("max_infra_failures_before_degraded", 3))
        cooldown_minutes = int(self.config["safety"].get("degraded_mode_cooldown_minutes", 10))
        if degraded["failure_count"] < threshold:
            self.logger.structured(
                "infra_failure",
                {
                    "failure_class": failure_class,
                    "reason_code": reason_code,
                    "failure_count": degraded["failure_count"],
                    "threshold": threshold,
                    "payload": payload or {},
                },
                level="WARNING",
            )
            return
        degraded["active"] = True
        degraded["reason_code"] = reason_code
        degraded["active_since"] = degraded.get("active_since") or now_iso
        degraded["cooldown_until"] = (utc_now() + timedelta(minutes=cooldown_minutes)).isoformat()
        degraded["alert_sent_at"] = now_iso
        self.logger.structured(
            "degraded_mode_entered",
            {
                "failure_class": failure_class,
                "reason_code": reason_code,
                "human_reason": human_reason,
                "cooldown_until": degraded["cooldown_until"],
                "payload": payload or {},
            },
            level="ERROR",
        )
        self._safe_notify_call(
            lambda: self.notifier.send_warning(
                f"disabled_due_to_infra | reason={reason_code} | cooldown_until={degraded['cooldown_until']}"
            )
        )

    def _clear_degraded_mode_if_recovered(self, now_utc: datetime) -> None:
        """Clear degraded mode once the infrastructure has recovered."""
        degraded = self._degraded_state()
        if not degraded.get("active"):
            return
        cooldown_until = to_utc(degraded.get("cooldown_until"))
        if cooldown_until and now_utc < cooldown_until:
            return
        degraded.update(
            {
                "active": False,
                "reason_code": None,
                "active_since": None,
                "cooldown_until": None,
                "alert_sent_at": None,
                "failure_count": 0,
                "last_failure_class": None,
                "last_failure_at": now_utc.isoformat(),
            }
        )
        self.logger.structured("degraded_mode_recovered", {"recovered_at": now_utc.isoformat()})

    def _degraded_gate(self, now_utc: datetime) -> dict[str, Any]:
        """Return whether live trading should be blocked due to degraded mode."""
        if not self._is_degraded_mode_active(now_utc):
            return {"blocked": False, "reason_code": None}
        degraded = self._degraded_state()
        return {"blocked": True, "reason_code": degraded.get("reason_code") or "disabled_due_to_infra"}

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
        self._sync_risk_state(utc_now(), float(account_info.balance), self.strategy.classify_session(utc_now())["session_name"])
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

    def _prune_setup_registry(self, now_utc: datetime) -> None:
        """Prune stale setup fingerprints from persistent state."""
        registry = self._setup_registry()
        retention_hours = int(self.config["journaling"].get("setup_registry_retention_hours", 72))
        max_entries = int(self.config["journaling"].get("signal_registry_max_entries", 1500))
        stale_keys: list[str] = []
        for fingerprint, payload in registry.items():
            seen_at = to_utc(payload.get("first_seen_at"))
            if seen_at and now_utc - seen_at > timedelta(hours=retention_hours):
                stale_keys.append(fingerprint)
        for key in stale_keys:
            registry.pop(key, None)
        if len(registry) > max_entries:
            ordered = sorted(registry.items(), key=lambda item: str(item[1].get("first_seen_at") or ""))
            for key, _ in ordered[: len(registry) - max_entries]:
                registry.pop(key, None)

    def _update_setup_registry(
        self,
        fingerprint: str,
        status: str,
        now_utc: datetime,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create or update a setup registry row."""
        normalized_fingerprint = str(fingerprint or "").strip()
        if not normalized_fingerprint:
            self._register_validation_failure(
                "setup_fingerprint_generation_failed",
                "Attempted to update setup registry with an empty fingerprint",
                {"status": status, "extra": extra or {}},
            )
            return {}
        registry = self._setup_registry()
        payload = registry.setdefault(normalized_fingerprint, {"first_seen_at": now_utc.isoformat()})
        payload["status"] = status
        payload["last_updated_at"] = now_utc.isoformat()
        if extra:
            payload.update(extra)
        return payload

    def _blocked_setup_replay_stage(
        self,
        candidate: dict[str, Any],
        entry_assessment: dict[str, Any],
        market_context: dict[str, Any],
        final_reason: str,
        now_utc: datetime,
    ) -> dict[str, Any]:
        """Return whether a blocked setup replay should be suppressed for this fingerprint."""
        fingerprint = str(candidate.get("setup_fingerprint", "") or "").strip()
        if not fingerprint:
            return {"suppress": False}
        registry_row = self._setup_registry().get(fingerprint, {})
        if str(registry_row.get("last_decision") or "") != "blocked":
            return {"suppress": False}
        last_blocked_at = to_utc(registry_row.get("last_blocked_at"))
        if last_blocked_at is None:
            return {"suppress": False}
        suppression_window = int(self.config.get("execution", {}).get("blocked_setup_replay_suppression_seconds", 45) or 45)
        if suppression_window <= 0 or (now_utc - last_blocked_at).total_seconds() >= suppression_window:
            return {"suppress": False}

        current_anchor = str(candidate.get("anchor_time") or candidate.get("detected_at") or "").strip()
        previous_anchor = str(registry_row.get("last_anchor_time") or "").strip()
        if current_anchor and previous_anchor and current_anchor != previous_anchor:
            return {"suppress": False, "material_change": "new_anchor_time"}

        current_entry = float(entry_assessment.get("entry_price", 0.0) or 0.0)
        previous_entry = float(registry_row.get("last_entry_price", 0.0) or 0.0)
        price_delta_points = abs(current_entry - previous_entry) / max(float(self.connector.get_symbol_spec()["point"]), 1e-9) if current_entry and previous_entry else 0.0
        required_price_delta = float(self.config.get("execution", {}).get("blocked_setup_replay_price_delta_points", 15.0) or 15.0)
        if price_delta_points >= required_price_delta:
            return {"suppress": False, "material_change": "entry_price_delta"}

        current_entry_score = float(entry_assessment.get("entry_score", 0.0) or 0.0)
        previous_entry_score = float(registry_row.get("last_entry_score", 0.0) or 0.0)
        if abs(current_entry_score - previous_entry_score) >= float(self.config.get("execution", {}).get("blocked_setup_replay_score_delta", 4.0) or 4.0):
            return {"suppress": False, "material_change": "entry_score_delta"}

        current_spread = float(market_context.get("spread_points", 0.0) or 0.0)
        previous_spread = float(registry_row.get("last_spread_points", 0.0) or 0.0)
        if previous_spread > 0 and (previous_spread - current_spread) >= float(self.config.get("execution", {}).get("blocked_setup_replay_spread_improvement_points", 2.0) or 2.0):
            return {"suppress": False, "material_change": "spread_improved"}

        last_reason = str(registry_row.get("last_block_reason") or "")
        last_suppressed_at = to_utc(registry_row.get("last_replay_suppressed_at"))
        should_log = last_suppressed_at is None or (now_utc - last_suppressed_at).total_seconds() >= max(10, min(30, suppression_window))
        return {
            "suppress": True,
            "should_log": should_log,
            "human_reason": f"Replay suppressed for blocked setup {fingerprint} ({last_reason or final_reason}) within {suppression_window}s window",
            "last_reason": last_reason or final_reason,
        }

    def _journal_signal_event(self, row: dict[str, Any]) -> bool:
        """Write a signal/attempt row to CSV and SQLite once."""
        return self._safe_journal_call("signal journal", lambda: self.trade_journal.record_signal_event(row))

    def _journal_trade_open(self, row: dict[str, Any]) -> bool:
        """Write a trade-open lifecycle event."""
        return self._safe_journal_call("trade open journal", lambda: self.trade_journal.record_trade_open(row))

    def _journal_trade_close(self, row: dict[str, Any]) -> bool:
        """Write a trade-close lifecycle event."""
        return self._safe_journal_call("trade close journal", lambda: self.trade_journal.record_trade_close(row))

    def _fetch_market_data(self) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        """Fetch and prepare all required timeframes."""
        bars = int(self.config["timeframes"]["bars_to_fetch"])
        trend_df = self.strategy.prepare_trend_dataframe(self.connector.fetch_rates(self.config["timeframes"]["trend"], bars))
        setup_df = self.strategy.prepare_setup_dataframe(self.connector.fetch_rates(self.config["timeframes"]["setup"], bars))
        trigger_df = self.strategy.prepare_trigger_dataframe(self.connector.fetch_rates(self.config["timeframes"]["trigger"], bars))
        return trend_df, setup_df, trigger_df

    def _has_open_position(self) -> bool:
        """Return whether a demo or live position is open."""
        if self.demo_mode:
            return self.state.get("demo_position") is not None
        return bool(self.connector.get_bot_positions() or self.state.get("open_position"))

    def _duplicate_exposure_stage(self, fingerprint: str, now_utc: datetime) -> dict[str, Any]:
        """Block duplicate entries by setup fingerprint and existing exposure."""
        registry = self._setup_registry()
        registry_row = registry.get(fingerprint, {})
        allow_multi_position = bool(self.config["execution"].get("allow_multi_position", self.config["execution"].get("allow_pyramiding", False)))
        if bool(self._has_open_position()) and not allow_multi_position:
            return self._stage_result(
                "duplicate_exposure_check",
                False,
                "exposure_already_open",
                "A bot-managed position is already open",
                {"setup_fingerprint": fingerprint},
            )
        if self.connector.get_pending_orders() and not allow_multi_position:
            return self._stage_result(
                "duplicate_exposure_check",
                False,
                "pending_order_already_open",
                "A bot-managed pending order already exists",
                {"setup_fingerprint": fingerprint},
            )
        if registry_row.get("status") == "executed":
            return self._stage_result(
                "duplicate_exposure_check",
                False,
                "setup_fingerprint_already_traded",
                "This setup fingerprint has already produced a live order",
                {"setup_fingerprint": fingerprint},
            )
        last_attempt_at = to_utc(registry_row.get("last_attempt_at"))
        min_gap = int(self.config["execution"].get("min_seconds_between_duplicate_attempts", 45))
        if last_attempt_at and (now_utc - last_attempt_at).total_seconds() < min_gap:
            return self._stage_result(
                "duplicate_exposure_check",
                False,
                "duplicate_entry_blocked",
                "Duplicate execution attempt blocked by cooldown",
                {"setup_fingerprint": fingerprint},
            )
        hard_rejection_at = to_utc(registry_row.get("last_hard_rejection_at"))
        hard_rejection_cooldown = int(self.config["execution"].get("hard_rejection_cooldown_seconds", 300))
        if hard_rejection_at and hard_rejection_cooldown > 0:
            remaining = hard_rejection_cooldown - int((now_utc - hard_rejection_at).total_seconds())
            if remaining > 0:
                last_reason = str(registry_row.get("last_hard_rejection_reason") or "broker_rejection")
                return self._stage_result(
                    "duplicate_exposure_check",
                    False,
                    "hard_rejection_cooldown_active",
                    f"Recent hard rejection cooldown active for another {remaining}s after {last_reason}",
                    {
                        "setup_fingerprint": fingerprint,
                        "remaining_seconds": remaining,
                        "last_hard_rejection_reason": last_reason,
                        "last_hard_rejection_message": registry_row.get("last_hard_rejection_message"),
                    },
                )
        return self._stage_result(
            "duplicate_exposure_check",
            True,
            "duplicate_check_ok",
            "No duplicate setup exposure detected",
            {"setup_fingerprint": fingerprint},
        )

    def _effective_live_block_reason(
        self,
        candidate: dict[str, Any] | None,
        live_entry_assessment: dict[str, Any],
        market_context: dict[str, Any],
        risk_gate: dict[str, Any],
        degraded_gate: dict[str, Any],
        now_utc: datetime,
    ) -> str | None:
        """Return the normalized reason live execution would currently be blocked."""
        if candidate is None:
            return None
        fingerprint = str(candidate.get("setup_fingerprint", "") or "").strip()
        if not fingerprint:
            return "setup_fingerprint_generation_failed"
        if degraded_gate.get("blocked"):
            return "disabled_due_to_infra"
        execution_decision = ExecutionDecisionEngine(self.config).decide(
            candidate=candidate,
            entry=live_entry_assessment,
            market_context=market_context,
        )
        live_validation = self._final_live_validation_decision(
            candidate,
            live_entry_assessment,
            market_context,
            execution_decision.action,
        )
        if not bool(live_validation.get("final_live_decision", False)):
            return str(live_validation.get("final_block_reason") or "live_validation_failed")
        if bool(risk_gate.get("cooldown_active")):
            return "cooldown_active"
        if bool(risk_gate.get("risk_lock_active")):
            return self._normalize_block_reason(str(risk_gate.get("risk_lock_reason")))

        duplicate_stage = self._duplicate_exposure_stage(fingerprint, now_utc)
        if not duplicate_stage["ok"]:
            return self._normalize_block_reason(str(duplicate_stage["reason_code"]), str(duplicate_stage["human_reason"]))
        if bool(self.config.get("execution", {}).get("allow_multi_position", False)) and (self.connector.get_bot_positions() or self.connector.get_pending_orders()):
            return "blocked_live_multi_position_unsupported"
        spread_limit, _ = self._spread_policy_limit(candidate, market_context)
        if float(market_context.get("spread_points", 0.0) or 0.0) > float(spread_limit):
            return "spread_too_wide"
        return None

    def _log_validation_cycle(
        self,
        mode_ctx: dict[str, Any],
        market_context: dict[str, Any],
        candidate: dict[str, Any] | None,
        active_entry_assessment: dict[str, Any],
        live_entry_assessment: dict[str, Any],
        live_block_reason: str | None,
    ) -> None:
        """Emit a compact validation-layer cycle snapshot."""
        payload = {
            "timestamp": utc_now().isoformat(),
            "regime_name": market_context["regime"]["regime_name"],
            "session_name": market_context["session"]["session_name"],
            "setup_family": candidate["setup_family"] if candidate else "WAIT",
            "setup_fingerprint": str(candidate.get("setup_fingerprint", "")) if candidate else "",
            "setup_status": (
                "eligible" if candidate and candidate.get("setup_valid")
                else (str(candidate.get("reason_code")) if candidate else "wait")
            ),
            "trigger_status": "confirmed" if active_entry_assessment.get("valid") else str(active_entry_assessment.get("trigger_type") or "blocked"),
            "entry_validation_status": "live_ready" if live_entry_assessment.get("live_ready") else str(live_entry_assessment.get("reason_code") or "blocked"),
            "live_block_reason": live_block_reason,
            "dry_run_or_live": mode_ctx["mode"],
            "rollout_phase": mode_ctx["rollout_phase"],
            "execution_status": self.state.get("execution_status"),
            "execution_reason_code": self._normalize_block_reason(str(self.state.get("execution_reason_code"))) if self.state.get("execution_reason_code") else None,
            "decision_status": self._decision_status(
                self.state.get("execution_reason_code"),
                executed=str(self.state.get("execution_reason_code")) in {"executed", "executed_live"},
            ),
        }
        self.state["last_validation_cycle"] = payload
        self.logger.structured("validation_cycle", payload)

    def _record_setup_observation(
        self,
        candidate: dict[str, Any],
        market_context: dict[str, Any],
        entry_assessment: dict[str, Any],
        mode_ctx: dict[str, Any],
        now_utc: datetime,
    ) -> None:
        """Journal a setup candidate once by fingerprint."""
        registry = self._setup_registry()
        fingerprint = str(candidate.get("setup_fingerprint", "") or "").strip()
        if not fingerprint:
            self._register_validation_failure(
                "setup_fingerprint_generation_failed",
                "Setup observation skipped because fingerprint generation failed",
                {"setup_family": candidate.get("setup_family"), "anchor_time": candidate.get("anchor_time")},
            )
            return
        registry_row = registry.get(fingerprint, {})
        if registry_row.get("observed"):
            return
        self._journal_signal_event(
            {
                "timestamp": now_utc.isoformat(),
                "event_type": JournalEventType.CANDIDATE_CREATED.value,
                "mode": mode_ctx["mode"],
                "live_or_dry": mode_ctx["mode"],
                "symbol": self.config["mt5"]["symbol"],
                "side": candidate["side"],
                "timeframe": self.config["timeframes"]["setup"],
                "signal_type": candidate["setup_family"],
                "setup_fingerprint": fingerprint,
                "setup_family": candidate["setup_family"],
                "setup_anchor_time": candidate["anchor_time"],
                "trigger_type": candidate["trigger_type"],
                "price": candidate["setup_price"],
                "setup_price": candidate["setup_price"],
                "value_price": candidate["value_price"],
                "entry_price": entry_assessment.get("entry_price"),
                "trend_ok": True,
                "setup_ok": bool(candidate["setup_valid"]),
                "entry_ok": False,
                "executed": False,
                "entry_mode": entry_assessment.get("entry_mode", "observation_only"),
                "regime_name": market_context["regime"]["regime_name"],
                "regime_confidence": market_context["regime"]["regime_confidence"],
                "session_name": market_context["session"]["session_name"],
                "session_live_allowed": market_context["session"]["session_live_allowed"],
                "reason_code": candidate["reason_code"],
                "reason": candidate["reason"],
                "trend_score": candidate.get("trend_score"),
                "setup_score": candidate.get("setup_score"),
                "trigger_score": entry_assessment.get("trigger_score"),
                "entry_score": entry_assessment.get("entry_score"),
                "metadata": candidate,
            }
        )
        self._update_setup_registry(fingerprint, "observed", now_utc, {"observed": True})
        signal_payload = {
            "timestamp": now_utc.isoformat(),
            "mode": mode_ctx["mode"],
            "symbol": self.config["mt5"]["symbol"],
            "setup_fingerprint": fingerprint,
            "setup_family": candidate["setup_family"],
            "side": candidate["side"],
            "session": market_context["session"]["session_name"],
            "regime": market_context["regime"]["regime_name"],
            "spread_points": market_context.get("spread_points"),
            "final_score": entry_assessment.get("entry_score"),
            "trend_score": candidate.get("trend_score"),
            "setup_score": candidate.get("setup_score"),
            "trigger_score": entry_assessment.get("trigger_score"),
            "entry_mode": entry_assessment.get("entry_mode", "observation_only"),
            "trend_filter_result": bool(entry_assessment.get("trend_filter_pass", candidate.get("trend_alignment", True))),
            "setup_filter_result": bool(entry_assessment.get("setup_filter_pass", candidate.get("setup_valid", False))),
            "trigger_filter_result": bool(entry_assessment.get("trigger_filter_pass", entry_assessment.get("valid", False))),
            "session_allowed": bool(candidate.get("session_allowed", market_context["session"].get("session_live_allowed", False))),
            "regime_allowed": bool(candidate.get("regime_allowed", market_context["regime"].get("live_allowed", False))),
            "family_enabled": bool(candidate.get("family_enabled", True)),
            "family_live_allowed": bool(candidate.get("family_live_allowed", False)),
            "trend_alignment": bool(candidate.get("trend_alignment", True)),
            "reason_code": candidate["reason_code"],
            "reason": candidate["reason"],
        }
        self.logger.structured("signal_detected", signal_payload)
        self.logger.signal_console(
            f"Signal detected | {candidate['setup_family']} | session={market_context['session']['session_name']} | "
            f"regime={market_context['regime']['regime_name']} | score={float(entry_assessment.get('entry_score', 0.0) or 0.0):.1f}"
        )
        self._send_setup_notification_once(
            fingerprint,
            "signal_detected",
            lambda: self.notifier.send_signal(
                {
                    "symbol": self.config["mt5"]["symbol"],
                    "side": candidate["side"],
                    "reason": candidate["reason"],
                    "executed": False,
                    "setup_family": candidate["setup_family"],
                    "regime_name": market_context["regime"]["regime_name"],
                    "session_name": market_context["session"]["session_name"],
                    "entry_mode": entry_assessment.get("entry_mode", "observation_only"),
                    "score": entry_assessment.get("entry_score"),
                    "execution_reason": entry_assessment.get("reason_code") or candidate["reason_code"],
                    "execution_blocked_reason": (
                        entry_assessment.get("reason_code")
                        if entry_assessment.get("reason_code") not in {None, "", "entry_valid"}
                        else None
                    ),
                    "entry": entry_assessment.get("entry_price"),
                }
            ),
            status="observed",
        )

    def _attempt_id(self, fingerprint: str, now_utc: datetime | None = None) -> str:
        """Generate a stable attempt id for observability across logs/journal/dashboard."""
        ts = (now_utc or utc_now()).strftime("%Y%m%d%H%M%S%f")
        compact = str(fingerprint or "unknown").replace("|", "_")[:48]
        return f"{compact}:{ts}"

    @staticmethod
    def _signal_lifecycle_event(
        final_reason: str,
        execution_state: str,
        *,
        requested_volume: float | None = None,
        final_volume: float | None = None,
    ) -> str:
        if final_reason == "executed_live":
            if requested_volume is not None and final_volume is not None and abs(float(final_volume) - float(requested_volume)) > 1e-9:
                return JournalEventType.ORDER_RESIZED_AND_SUBMITTED.value
            return JournalEventType.ORDER_SUBMITTED.value
        if final_reason in {"spread_too_wide", "entry_drift_too_far", "chasing_entry", "order_missed_drift"}:
            return JournalEventType.ORDER_MISSED_DRIFT.value
        if final_reason in {"cooldown_active", "position_exists", "pending_order_exists", "blocked_live_multi_position_unsupported"}:
            return JournalEventType.ORDER_MISSED_STATE_BLOCK.value
        if final_reason in {"risk_rejected", "margin_insufficient", "risk_resized_to_min_volume"}:
            return JournalEventType.ORDER_MISSED_RISK_BLOCK.value
        if execution_state == "precheck_failed":
            if final_reason.startswith("blocked_") or final_reason in {
                "stop_too_wide_points",
                "stop_too_wide_atr",
                "noisy_trigger_candle",
                "oversized_trigger_candle",
                "exhaustion_trigger_candle",
                "weak_short_bias",
                "weak_short_slope",
                "short_reward_constrained",
            }:
                return JournalEventType.CANDIDATE_REJECTED_STRATEGY.value
            return JournalEventType.CANDIDATE_REJECTED_SYSTEM.value
        return JournalEventType.EXECUTION_FAILURE.value

    def _spread_policy_limit(self, candidate: dict[str, Any], market_context: dict[str, Any]) -> tuple[float, str]:
        """Resolve spread limit from setup/regime policy with conservative defaults."""
        setup_fallback = float(
            candidate.get("strategy_control", {}).get(
                "max_spread_points",
                self.config["execution"]["max_spread_points"],
            )
        )
        return resolve_spread_limit(
            execution_cfg=self.config.get("execution", {}),
            setup_name=str(candidate.get("setup_family") or ""),
            regime_name=str(market_context.get("regime", {}).get("regime_name") or ""),
            fallback_limit=setup_fallback,
        )

    def _wait_for_spread_improvement(
        self,
        spread_limit: float,
    ) -> tuple[bool, dict[str, Any]]:
        """Optionally wait for spread normalization within a short retry window."""
        execution_cfg = self.config.get("execution", {})
        spread_cfg = execution_cfg.get("spread", {}) if isinstance(execution_cfg.get("spread"), dict) else {}
        retry_window_seconds = max(0.0, float(spread_cfg.get("retry_window_seconds", 0.0)))
        recheck_interval_ms = max(50, int(spread_cfg.get("recheck_interval_ms", 500)))
        deadline = time.time() + retry_window_seconds
        last_stage = self.connector.spread_check_stage(spread_limit)
        checks = 1
        while not last_stage["ok"] and retry_window_seconds > 0 and time.time() < deadline:
            time.sleep(recheck_interval_ms / 1000.0)
            last_stage = self.connector.spread_check_stage(spread_limit)
            checks += 1
        payload = dict(last_stage.get("payload") or {})
        payload.update({"checks": checks, "retry_window_seconds": retry_window_seconds})
        return bool(last_stage["ok"]), payload

    def _record_execution_attempt(
        self,
        candidate: dict[str, Any],
        entry_assessment: dict[str, Any],
        mode_ctx: dict[str, Any],
        market_context: dict[str, Any],
        reason_code: str,
        reason: str,
        trade_plan: dict[str, Any] | None = None,
        requested_volume: float | None = None,
        final_volume: float | None = None,
        order_check_status: str = "not_reached",
        order_send_status: str = "not_reached",
        event_type: str = "execution_attempt",
        update_registry: bool = True,
        extra_metadata: dict[str, Any] | None = None,
        attempt_id: str | None = None,
        detected_at: str | None = None,
        validated_at: str | None = None,
        final_outcome_at: str | None = None,
        order_send_attempted: bool = False,
        order_send_retcode: int | None = None,
        order_send_retcode_text: str | None = None,
        spread_at_validation: float | None = None,
        spread_limit_used: float | None = None,
        spread_limit_source: str | None = None,
        cooldown_applied: bool = False,
        duplicate_guard_applied: bool = False,
        failure_stage: str = "validation",
        execution_state: str = "precheck_failed",
    ) -> None:
        """Journal a single execution attempt row per setup fingerprint and cycle."""
        now_utc = utc_now()
        fingerprint = str(candidate.get("setup_fingerprint", "") or "").strip()
        if not fingerprint:
            self._register_validation_failure(
                "setup_fingerprint_generation_failed",
                "Execution attempt journaling skipped because fingerprint generation failed",
                {"setup_family": candidate.get("setup_family"), "reason_code": reason_code},
            )
            return
        normalized_reason_code = self._normalize_block_reason(reason_code, reason) or reason_code
        final_reason = normalize_final_outcome(
            normalized_reason_code,
            str((extra_metadata or {}).get("failure_class") or ""),
            execution_state=execution_state,
            order_send_attempted=bool(order_send_attempted),
        )
        final_time = final_outcome_at or now_utc.isoformat()
        local_attempt_id = attempt_id or self._attempt_id(fingerprint, now_utc)
        replay_stage = self._blocked_setup_replay_stage(candidate, entry_assessment, market_context, final_reason, now_utc)
        if final_reason != "executed_live" and bool(replay_stage.get("suppress")):
            self._update_setup_registry(
                fingerprint,
                "blocked",
                now_utc,
                {
                    "last_replay_suppressed_at": now_utc.isoformat(),
                    "last_replay_suppressed_reason": replay_stage.get("last_reason") or final_reason,
                },
            )
            if not bool(replay_stage.get("should_log")):
                self.state["execution_status"] = "blocked"
                self.state["execution_reason_code"] = "blocked_setup_replay_suppressed"
                return
            reason = str(replay_stage.get("human_reason") or reason)
            final_reason = "blocked_setup_replay_suppressed"
        metadata = {
            "attempt_id": local_attempt_id,
            "correlation_id": local_attempt_id,
            "detected_at": detected_at,
            "validated_at": validated_at,
            "final_outcome_at": final_time,
            "final_outcome_reason": final_reason,
            "execution_state": execution_state,
            "failure_stage": failure_stage,
            "order_send_attempted": bool(order_send_attempted),
            "order_send_retcode": order_send_retcode,
            "order_send_retcode_text": order_send_retcode_text,
            "spread_at_validation": spread_at_validation,
            "spread_limit_used": spread_limit_used,
            "spread_limit_source": spread_limit_source,
            "cooldown_applied": bool(cooldown_applied),
            "duplicate_guard_applied": bool(duplicate_guard_applied or final_reason == "blocked_setup_replay_suppressed"),
            "order_check_status": order_check_status,
            "order_send_status": order_send_status,
            "raw_reason_code": reason_code,
            "raw_reason": reason,
        }
        if extra_metadata:
            metadata.update(extra_metadata)
        trend_filter_result = bool(entry_assessment.get("trend_filter_pass", candidate.get("trend_alignment", True)))
        setup_filter_result = bool(entry_assessment.get("setup_filter_pass", candidate.get("setup_valid", False)))
        trigger_filter_result = bool(entry_assessment.get("trigger_filter_pass", entry_assessment.get("valid", False)))
        lifecycle_event = self._signal_lifecycle_event(
            final_reason,
            execution_state,
            requested_volume=requested_volume,
            final_volume=final_volume,
        )
        self._journal_signal_event(
            {
                "timestamp": now_utc.isoformat(),
                "event_type": lifecycle_event,
                "mode": mode_ctx["mode"],
                "live_or_dry": mode_ctx["mode"],
                "symbol": self.config["mt5"]["symbol"],
                "side": candidate["side"],
                "timeframe": self.config["timeframes"]["trigger"],
                "signal_type": candidate["setup_family"],
                "trade_id": fingerprint,
                "mt5_ticket": fingerprint,
                "position_id": fingerprint,
                "status": "EXECUTED" if final_reason == "executed_live" else "BLOCKED",
                "setup_fingerprint": fingerprint,
                "setup_family": candidate["setup_family"],
                "setup_anchor_time": candidate["anchor_time"],
                "trigger_type": entry_assessment.get("trigger_type"),
                "price": entry_assessment.get("entry_price"),
                "setup_price": candidate["setup_price"],
                "value_price": candidate["value_price"],
                "entry_price": trade_plan["entry_price"] if trade_plan else entry_assessment.get("entry_price"),
                "sl": trade_plan["stop_loss"] if trade_plan else None,
                "tp": trade_plan["tp2"] if trade_plan else None,
                "spread_points": market_context["spread_points"],
                "requested_volume": requested_volume,
                "final_volume": final_volume,
                "trend_ok": trend_filter_result,
                "setup_ok": setup_filter_result,
                "entry_ok": trigger_filter_result,
                "executed": final_reason == "executed_live",
                "entry_mode": entry_assessment.get("entry_mode", "observation_only"),
                "regime_name": market_context["regime"]["regime_name"],
                "regime_confidence": market_context["regime"]["regime_confidence"],
                "session_name": market_context["session"]["session_name"],
                "session_live_allowed": market_context["session"]["session_live_allowed"],
                "reason_code": final_reason,
                "reason": reason,
                "trend_score": entry_assessment.get("trend_score"),
                "setup_score": entry_assessment.get("setup_score"),
                "trigger_score": entry_assessment.get("trigger_score"),
                "entry_score": entry_assessment.get("entry_score"),
                "metadata": metadata,
            }
        )
        if update_registry:
            existing_registry_row = self._setup_registry().get(fingerprint, {})
            registry_extra = {
                "last_attempt_at": now_utc.isoformat(),
                "last_decision": "executed" if final_reason == "executed_live" else "blocked",
                "last_block_reason": final_reason if final_reason != "executed_live" else existing_registry_row.get("last_block_reason", ""),
                "last_blocked_at": now_utc.isoformat() if final_reason != "executed_live" else existing_registry_row.get("last_blocked_at"),
                "last_anchor_time": candidate.get("anchor_time"),
                "last_entry_price": entry_assessment.get("entry_price"),
                "last_entry_score": entry_assessment.get("entry_score"),
                "last_spread_points": market_context.get("spread_points"),
                "symbol": self.config["mt5"]["symbol"],
                "side": candidate.get("side"),
                "setup_family": candidate.get("setup_family"),
            }
            if final_reason == "executed_live":
                registry_extra["last_decision"] = "executed"
                registry_extra["last_executed_at"] = now_utc.isoformat()
                self._update_setup_registry(fingerprint, "executed", now_utc, registry_extra)
            elif should_mark_duplicate_or_cooldown(execution_state):
                registry_extra["last_decision"] = "attempted"
                self._update_setup_registry(fingerprint, "attempted", now_utc, registry_extra)
            else:
                self._update_setup_registry(fingerprint, "blocked", now_utc, registry_extra)
        structured_payload = {
            "timestamp": now_utc.isoformat(),
            "event_type": lifecycle_event,
            "mode": mode_ctx["mode"],
            "symbol": self.config["mt5"]["symbol"],
            "setup_fingerprint": fingerprint,
            "setup_family": candidate["setup_family"],
            "side": candidate["side"],
            "session": market_context["session"]["session_name"],
            "regime": market_context["regime"]["regime_name"],
            "spread_points": market_context.get("spread_points"),
            "final_score": entry_assessment.get("entry_score"),
            "trend_score": entry_assessment.get("trend_score"),
            "setup_score": entry_assessment.get("setup_score"),
            "trigger_score": entry_assessment.get("trigger_score"),
            "trend_filter_result": trend_filter_result,
            "setup_filter_result": setup_filter_result,
            "trigger_filter_result": trigger_filter_result,
            "session_allowed": bool(candidate.get("session_allowed", market_context["session"].get("session_live_allowed", False))),
            "regime_allowed": bool(candidate.get("regime_allowed", market_context["regime"].get("live_allowed", False))),
            "family_enabled": bool(candidate.get("family_enabled", True)),
            "family_live_allowed": bool(candidate.get("family_live_allowed", False)),
            "trend_alignment": bool(candidate.get("trend_alignment", True)),
            "requested_volume": requested_volume,
            "final_volume": final_volume,
            "entry_price": trade_plan["entry_price"] if trade_plan else entry_assessment.get("entry_price"),
            "sl": trade_plan["stop_loss"] if trade_plan else None,
            "tp": trade_plan["tp2"] if trade_plan else None,
            "reason_code": final_reason,
            "reason": reason,
            "order_check_status": order_check_status,
            "order_send_status": order_send_status,
            "decision_status": self._decision_status(final_reason, final_reason == "executed_live"),
            "metadata": metadata,
        }
        if final_reason == "executed_live":
            self.logger.structured("order_send_result", structured_payload)
        elif lifecycle_event not in {JournalEventType.ORDER_SUBMITTED.value, JournalEventType.ORDER_RESIZED_AND_SUBMITTED.value}:
            self.logger.structured("signal_rejected", structured_payload, level="WARNING")
            self.logger.signal_console(
                f"Signal rejected | {candidate['setup_family']} | reason={final_reason} | "
                f"session={market_context['session']['session_name']} | regime={market_context['regime']['regime_name']} | "
                f"score={float(entry_assessment.get('entry_score', 0.0) or 0.0):.1f}"
            )
            self._send_setup_notification_once(
                fingerprint,
                f"rejection:{final_reason}",
                lambda: self.notifier.send_signal(
                    {
                        "symbol": self.config["mt5"]["symbol"],
                        "side": candidate["side"],
                        "reason": reason,
                        "executed": False,
                        "setup_family": candidate["setup_family"],
                        "regime_name": market_context["regime"]["regime_name"],
                        "session_name": market_context["session"]["session_name"],
                        "entry_mode": entry_assessment.get("entry_mode", "observation_only"),
                        "score": entry_assessment.get("entry_score"),
                        "execution_reason": final_reason,
                        "execution_blocked_reason": metadata.get("raw_reason_code"),
                        "volume": final_volume or requested_volume,
                        "entry": trade_plan["entry_price"] if trade_plan else entry_assessment.get("entry_price"),
                        "sl": trade_plan["stop_loss"] if trade_plan else None,
                        "tp": trade_plan["tp2"] if trade_plan else None,
                    }
                ),
                extra={"last_rejection_notification_reason": final_reason},
            )

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
        validation_cfg = self.config.get("validation", {})
        if bool(self.config.get("bot", {}).get("enable_startup_trade_reconciliation", True)):
            try:
                self._resolve_unresolved_trades(source="cycle")
                self._reconcile_mt5_positions_with_database()
            except Exception as exc:
                self._log_exception("cycle_trade_reconciliation_failed", "Trade reconciliation failed during cycle", exc, notify=False)
        self._maybe_auto_generate_daily_reports(now_utc)
        strict_live_profile = mode_ctx["mode"] in {"LIVE", "VALIDATION_TEST"}
        previous_session_state = dict(self.state.get("session_stats", {})) if isinstance(self.state.get("session_stats"), dict) else {}
        self.state["execution_status"] = "idle"
        self.state["execution_reason_code"] = "cycle_idle"
        session_info = self.strategy.classify_session(now_utc)
        self.logger.structured(
            "cycle_start",
            {
                "timestamp": now_utc.isoformat(),
                "mode": mode_ctx["mode"],
                "symbol": self.config["mt5"]["symbol"],
                "session": session_info["session_name"],
                "tracked_position": bool(self.state.get("demo_position") or self.state.get("open_position")),
                "degraded_mode_active": self._is_degraded_mode_active(now_utc),
                "last_execution_status": self.state.get("execution_status"),
                "last_execution_reason": self.state.get("execution_reason_code"),
            },
        )
        market_readiness = self.validate_live_market_readiness()
        connection_stage = market_readiness["connection_stage"]
        symbol_stage = market_readiness["symbol_stage"]
        tick_stage = market_readiness["tick_stage"]

        if not connection_stage["ok"] or not symbol_stage["ok"] or not tick_stage["ok"]:
            failing_stage = next(stage for stage in [connection_stage, symbol_stage, tick_stage] if not stage["ok"])
            self._register_infra_failure("connection_failure", str(failing_stage["reason_code"]), str(failing_stage["human_reason"]), failing_stage.get("payload"))
            degraded_market_context = {
                "session": session_info,
                "regime": {"regime_name": "UNKNOWN", "regime_confidence": 0.0},
                "bias": {"direction": "UNKNOWN"},
                "spread_points": None,
            }
            self.state["execution_status"] = "blocked"
            self.state["execution_reason_code"] = "disabled_due_to_infra"
            self._log_validation_cycle(
                mode_ctx,
                degraded_market_context,
                None,
                {"valid": False, "live_ready": False, "reason_code": "disabled_due_to_infra", "trigger_type": "blocked"},
                {"valid": False, "live_ready": False, "reason_code": "disabled_due_to_infra", "trigger_type": "blocked"},
                "disabled_due_to_infra",
            )
            self._write_heartbeat("DEGRADED", None, degraded_market_context, None, 0, str(failing_stage["reason_code"]))
            self.save_state()
            return

        self._clear_degraded_mode_if_recovered(now_utc)
        account_info = self.connector.get_account_info()
        self._sync_risk_state(now_utc, float(account_info.balance), session_info["session_name"])
        previous_session_name = str(previous_session_state.get("session_name") or "")
        previous_session_day = str(previous_session_state.get("date") or "")
        current_day = now_utc.date().isoformat()
        if (
            bool(validation_cfg.get("session_report_enabled", True))
            and previous_session_name
            and previous_session_day
            and (previous_session_name != session_info["session_name"] or previous_session_day != current_day)
        ):
            self._emit_validation_report(previous_session_day, "session", previous_session_name, mode_ctx)
        self._prune_setup_registry(now_utc)

        trend_df, setup_df, trigger_df = self._fetch_market_data()
        if self.connector.market_data_degraded:
            degraded_market_context = {
                "session": session_info,
                "regime": {"regime_name": "UNKNOWN", "regime_confidence": 0.0},
                "bias": {"direction": "UNKNOWN"},
                "spread_points": None,
                "market_data_source": self.connector.market_data_source,
            }
            self._register_infra_failure(
                "market_data_failure",
                "market_data_degraded",
                "MT5 historical bars were unavailable; synthetic fallback data was used",
                {
                    "market_data_source": self.connector.market_data_source,
                    "symbol": self.config["mt5"]["symbol"],
                },
            )
            self.state["execution_status"] = "blocked"
            self.state["execution_reason_code"] = "market_data_degraded"
            self._record_event(
                "WARNING",
                "market_data_degraded",
                "Market data fallback engaged; skipping strategy evaluation and trade execution",
                reason_code="market_data_degraded",
            )
            self._write_heartbeat("DEGRADED", None, degraded_market_context, account_info, 0, "market_data_degraded")
            self.save_state()
            return
        symbol_spec = self.connector.get_symbol_spec()
        tick_snapshot = tick_stage["payload"].get("tick", {}) or {}
        point = float(symbol_spec["point"])
        spread_points = ((float(tick_snapshot.get("ask", 0.0)) - float(tick_snapshot.get("bid", 0.0))) / point) if point > 0 else 0.0
        market_context = self.strategy.analyze_market_context(now_utc, trend_df, setup_df, trigger_df, spread_points)
        cycle_id = f"{now_utc.strftime('%Y%m%dT%H%M%S.%f')}-{int(time.time() * 1000) % 1000000}"
        if self.decision_logger is not None:
            self.decision_logger.log_market_context(market_context, cycle_id)
        if not trigger_df.empty:
            latest_trigger = trigger_df.iloc[-1]
            market_context["latest_bar"] = {
                "time": latest_trigger.get("time"),
                "open": float(latest_trigger.get("open", 0.0) or 0.0),
                "high": float(latest_trigger.get("high", 0.0) or 0.0),
                "low": float(latest_trigger.get("low", 0.0) or 0.0),
                "close": float(latest_trigger.get("close", 0.0) or 0.0),
            }
        self._manage_existing_positions(trigger_df, setup_df, market_context, mode_ctx)

        if self._session_close_due(now_utc):
            self._handle_session_close(now_utc, market_context)
            self.save_state()
            return

        if not self.signal_generation_enabled:
            self.state["execution_status"] = "paused"
            self.state["execution_reason_code"] = "signal_generation_disabled"
            self._record_event(
                "INFO",
                "signal_generation_disabled",
                "New entries are disabled by dashboard control; existing positions remain managed.",
                reason_code="signal_generation_disabled",
            )
            self._log_validation_cycle(
                mode_ctx,
                market_context,
                None,
                {"valid": False, "live_ready": False, "reason_code": "signal_generation_disabled", "trigger_type": "runtime_control"},
                {"valid": False, "live_ready": False, "reason_code": "signal_generation_disabled", "trigger_type": "runtime_control"},
                "signal_generation_disabled",
            )
            self._write_heartbeat(
                "RUNNING",
                spread_points,
                market_context,
                account_info,
                1 if self._has_open_position() else 0,
                "signal_generation_disabled",
            )
            self.save_state()
            return

        candidates = self.strategy.generate_setup_candidates(market_context, trend_df, setup_df, trigger_df, symbol_spec)
        if self.decision_logger is not None:
            self.decision_logger.log_candidate_generation(candidates, market_context, cycle_id)
        if self.shadow_runner is not None:
            self.shadow_runner.process_cycle(market_context, trend_df, setup_df, trigger_df, symbol_spec)
        best_candidate = self.strategy.choose_best_setup(candidates)
        self.logger.structured(
            "strategy_candidate_diagnostics",
            build_strategy_candidate_diagnostics(
                config=self.config,
                engine=self.strategy,
                market_context=market_context,
                candidates=candidates,
                selected_candidate=best_candidate,
                runtime_mode=mode_ctx["mode"],
            ),
        )
        live_entry_assessment = self.strategy.evaluate_entry(best_candidate, trigger_df, symbol_spec, now_utc, live_profile=True)
        active_entry_assessment = live_entry_assessment if strict_live_profile else self.strategy.evaluate_entry(best_candidate, trigger_df, symbol_spec, now_utc, live_profile=False)
        self.state["last_cycle_setup_family"] = best_candidate["setup_family"] if best_candidate else "WAIT"

        if best_candidate is not None and not str(best_candidate.get("setup_fingerprint", "") or "").strip():
            self._register_validation_failure(
                "setup_fingerprint_generation_failed",
                "Setup candidate generated without a stable fingerprint",
                {"setup_family": best_candidate.get("setup_family"), "anchor_time": best_candidate.get("anchor_time")},
            )
            self.state["execution_status"] = "blocked"
            self.state["execution_reason_code"] = "setup_fingerprint_generation_failed"
            self._log_validation_cycle(mode_ctx, market_context, best_candidate, active_entry_assessment, live_entry_assessment, "setup_fingerprint_generation_failed")
            self._write_heartbeat(
                "RUNNING",
                spread_points,
                market_context,
                account_info,
                1 if self._has_open_position() else 0,
                self.state.get("execution_reason_code"),
            )
            self.save_state()
            return

        if best_candidate is not None:
            self._record_setup_observation(best_candidate, market_context, active_entry_assessment, mode_ctx, now_utc)

        risk_gate = self.risk_manager.evaluate_risk_gate(
            self.state,
            account_info,
            now_utc,
            market_context["session"]["session_name"],
            live_requested=bool(mode_ctx["mode"] == "LIVE"),
            force_reduced_risk=bool(mode_ctx["force_reduced_risk_mode"]),
        )
        degraded_gate = self._degraded_gate(now_utc)
        live_block_reason = self._effective_live_block_reason(best_candidate, live_entry_assessment, market_context, risk_gate, degraded_gate, now_utc)
        if self.decision_logger is not None:
            all_gates_passed = bool(
                best_candidate
                and active_entry_assessment.get("valid")
                and live_entry_assessment.get("live_ready")
                and not live_block_reason
            )
            self.decision_logger.log_execution_decision(
                candidate=best_candidate,
                entry_evaluation=active_entry_assessment,
                risk_gates={"all_passed": all_gates_passed, "gate_results": risk_gate},
                cycle_id=cycle_id,
                executed=all_gates_passed,
            )

        if best_candidate is not None:
            self._record_execution_attempt(
                best_candidate,
                live_entry_assessment,
                mode_ctx,
                market_context,
                live_block_reason or "live_validation_passed",
                "Strict live validation passed" if live_block_reason is None else "Strict live validation blocked",
                event_type="validation_gate",
                update_registry=False,
                extra_metadata={"rollout_phase": mode_ctx["rollout_phase"]},
            )
            self._attempt_entry(best_candidate, active_entry_assessment, live_entry_assessment, market_context, account_info, symbol_spec, mode_ctx, risk_gate, degraded_gate)

        self._log_validation_cycle(mode_ctx, market_context, best_candidate, active_entry_assessment, live_entry_assessment, live_block_reason)
        self.logger.structured(
            "cycle_summary",
            {
                "mode": mode_ctx["mode"],
                "regime": market_context["regime"]["regime_name"],
                "session": market_context["session"]["session_name"],
                "bias": market_context["bias"]["direction"],
                "setup_family": best_candidate["setup_family"] if best_candidate else "WAIT",
                "setup_status": "valid" if best_candidate and best_candidate.get("setup_valid") else "wait",
                "trigger_status": "valid" if active_entry_assessment.get("valid") else "blocked",
                "execution_status": self.state.get("execution_status", "idle"),
                "reason_code": self.state.get("execution_reason_code", active_entry_assessment.get("reason_code")),
            },
        )
        self._write_heartbeat(
            "RUNNING",
            spread_points,
            market_context,
            account_info,
            1 if self._has_open_position() else 0,
            self.state.get("execution_reason_code"),
        )
        self.save_state()

    def _attempt_entry(
        self,
        candidate: dict[str, Any],
        entry_assessment: dict[str, Any],
        live_entry_assessment: dict[str, Any],
        market_context: dict[str, Any],
        account_info: Any,
        symbol_spec: dict[str, Any],
        mode_ctx: dict[str, Any],
        risk_gate: dict[str, Any],
        degraded_gate: dict[str, Any],
    ) -> None:
        """Run the strict staged execution pipeline for a setup candidate."""
        now_utc = utc_now()
        self.state["execution_status"] = "blocked"
        self.state["execution_reason_code"] = entry_assessment.get("reason_code")
        fingerprint = str(candidate.get("setup_fingerprint") or "")
        attempt_id = self._attempt_id(fingerprint, now_utc)
        detected_at = str(candidate.get("detected_at") or candidate.get("anchor_time") or now_utc.isoformat())
        validated_at = now_utc.isoformat()
        spread_limit_used: float | None = None
        spread_limit_source: str | None = None
        spread_at_validation: float | None = float(market_context.get("spread_points", 0.0) or 0.0)
        order_send_attempted = False
        send_retcode: int | None = None
        send_retcode_text: str | None = None
        strict_live_mode = mode_ctx["mode"] in {"LIVE", "VALIDATION_TEST"}
        execution_router = ExecutionDecisionEngine(self.config)
        routed_source_assessment = live_entry_assessment if strict_live_mode else entry_assessment
        execution_decision = execution_router.decide(
            candidate=candidate,
            entry=routed_source_assessment,
            market_context=market_context,
        )
        self._journal_signal_event(
            {
                "timestamp": now_utc.isoformat(),
                "event_type": JournalEventType.EXECUTION_PLAN_CREATED.value,
                "mode": mode_ctx["mode"],
                "symbol": self.config["mt5"]["symbol"],
                "side": candidate["side"],
                "setup_fingerprint": fingerprint,
                "setup_family": candidate["setup_family"],
                "entry_mode": execution_decision.entry_mode,
                "entry_price": execution_decision.entry_price,
                "reason_code": execution_decision.reason_code,
                "reason": execution_decision.reason,
                "executed": False,
                "metadata": execution_decision.metadata,
            }
        )
        routed_entry_assessment = {
            **entry_assessment,
            "entry_mode": execution_decision.entry_mode,
            "entry_price": float(execution_decision.entry_price or entry_assessment.get("entry_price") or 0.0),
            "pending_entry_price": float(execution_decision.entry_price or entry_assessment.get("pending_entry_price") or entry_assessment.get("entry_price") or 0.0),
            "execution_decision": execution_decision.action,
            "execution_reason_code": execution_decision.reason_code,
            "quality_tier": execution_decision.quality_tier,
            "strategy_family": execution_decision.strategy_family,
            "management_profile": execution_decision.management_profile,
            "management_profile_key": execution_decision.strategy_family.lower(),
            "volatility_state": execution_decision.metadata.get("volatility_state"),
            "pending_expiry_minutes": execution_decision.pending_expiry_minutes or entry_assessment.get("pending_expiry_minutes"),
            "pending_expiry_bars": execution_decision.pending_expiry_bars or entry_assessment.get("pending_expiry_bars"),
        }
        routed_live_entry_assessment = {
            **live_entry_assessment,
            "entry_mode": execution_decision.entry_mode,
            "entry_price": float(execution_decision.entry_price or live_entry_assessment.get("entry_price") or 0.0),
            "pending_entry_price": float(execution_decision.entry_price or live_entry_assessment.get("pending_entry_price") or live_entry_assessment.get("entry_price") or 0.0),
            "execution_decision": execution_decision.action,
            "execution_reason_code": execution_decision.reason_code,
            "quality_tier": execution_decision.quality_tier,
            "strategy_family": execution_decision.strategy_family,
            "management_profile": execution_decision.management_profile,
            "management_profile_key": execution_decision.strategy_family.lower(),
            "volatility_state": execution_decision.metadata.get("volatility_state"),
            "pending_expiry_minutes": execution_decision.pending_expiry_minutes or live_entry_assessment.get("pending_expiry_minutes"),
            "pending_expiry_bars": execution_decision.pending_expiry_bars or live_entry_assessment.get("pending_expiry_bars"),
        }
        entry_assessment = routed_entry_assessment
        live_entry_assessment = routed_live_entry_assessment

        if self.execution_paused:
            self._record_execution_attempt(
                candidate,
                live_entry_assessment if strict_live_mode else entry_assessment,
                mode_ctx,
                market_context,
                "execution_paused",
                "Execution is paused by dashboard control",
                attempt_id=attempt_id,
                detected_at=detected_at,
                validated_at=validated_at,
                final_outcome_at=utc_now().isoformat(),
                spread_at_validation=spread_at_validation,
                failure_stage="validation",
                execution_state="precheck_failed",
            )
            self.state["execution_reason_code"] = "execution_paused"
            return
        if strict_live_mode and not self.auto_execution_enabled:
            self._record_execution_attempt(
                candidate,
                live_entry_assessment,
                mode_ctx,
                market_context,
                "auto_execution_disabled",
                "Auto execution disabled by dashboard control while signal generation remains active",
                attempt_id=attempt_id,
                detected_at=detected_at,
                validated_at=validated_at,
                final_outcome_at=utc_now().isoformat(),
                spread_at_validation=spread_at_validation,
                failure_stage="validation",
                execution_state="precheck_failed",
            )
            self.state["execution_reason_code"] = "auto_execution_disabled"
            return
        if strict_live_mode and degraded_gate["blocked"]:
            self._record_execution_attempt(
                candidate,
                live_entry_assessment,
                mode_ctx,
                market_context,
                "disabled_due_to_infra",
                f"Live execution blocked by degraded infrastructure mode: {degraded_gate.get('reason_code') or 'disabled_due_to_infra'}",
                attempt_id=attempt_id,
                detected_at=detected_at,
                validated_at=validated_at,
                final_outcome_at=utc_now().isoformat(),
                spread_at_validation=spread_at_validation,
                failure_stage="validation",
                execution_state="precheck_failed",
            )
            self.state["execution_reason_code"] = "disabled_due_to_infra"
            return
        live_validation = self._final_live_validation_decision(
            candidate,
            live_entry_assessment,
            market_context,
            execution_decision.action,
        )
        if strict_live_mode:
            self._log_execution_diagnostics(candidate, live_entry_assessment, market_context, live_validation)
        if strict_live_mode and not bool(live_validation.get("final_live_decision", False)):
            final_block_reason = str(live_validation.get("final_block_reason") or "live_validation_failed")
            if final_block_reason == "setup_family_live_disabled":
                human_reason = (
                    f"Live execution blocked for setup family {candidate['setup_family']}: "
                    f"enabled={live_validation.get('family_enabled')} | live_allowed={live_validation.get('family_live_allowed')}"
                )
            elif final_block_reason == "blocked_bad_regime":
                human_reason = (
                    f"Live execution blocked by regime filter: regime={market_context['regime']['regime_name']} | "
                    f"family_allowed_regimes={live_validation.get('family_allowed_regimes')} | "
                    f"allow_range_regime={live_validation.get('regime_allow_range_regime')}"
                )
            elif final_block_reason == "blocked_bad_session":
                human_reason = (
                    f"Live execution blocked by session filter: session={market_context['session']['session_name']} | "
                    f"reason={market_context['session'].get('session_block_reason') or candidate.get('session_block_reason') or 'session_filter_blocked'}"
                )
            else:
                human_reason = str(live_entry_assessment.get("reason") or final_block_reason)
            self._record_execution_attempt(
                candidate,
                live_entry_assessment,
                mode_ctx,
                market_context,
                final_block_reason,
                human_reason,
                attempt_id=attempt_id,
                detected_at=detected_at,
                validated_at=validated_at,
                final_outcome_at=utc_now().isoformat(),
                spread_at_validation=spread_at_validation,
                failure_stage="validation",
                execution_state="precheck_failed",
            )
            self.state["execution_reason_code"] = final_block_reason
            return
        if strict_live_mode and risk_gate["cooldown_active"]:
            self._record_execution_attempt(
                candidate,
                live_entry_assessment,
                mode_ctx,
                market_context,
                "cooldown_active",
                f"Cooldown active: {risk_gate.get('cooldown_reason')} ({risk_gate.get('cooldown_remaining_seconds')}s remaining)",
                attempt_id=attempt_id,
                detected_at=detected_at,
                validated_at=validated_at,
                final_outcome_at=utc_now().isoformat(),
                spread_at_validation=spread_at_validation,
                failure_stage="precheck",
                execution_state="precheck_failed",
            )
            self.state["execution_reason_code"] = "cooldown_active"
            return
        if strict_live_mode and risk_gate["risk_lock_active"]:
            reason_code = self._normalize_block_reason(str(risk_gate.get("risk_lock_reason"))) or "risk_lock_active"
            self._record_execution_attempt(
                candidate,
                live_entry_assessment,
                mode_ctx,
                market_context,
                reason_code,
                "Live execution blocked by risk governor",
                attempt_id=attempt_id,
                detected_at=detected_at,
                validated_at=validated_at,
                final_outcome_at=utc_now().isoformat(),
                spread_at_validation=spread_at_validation,
                failure_stage="precheck",
                execution_state="precheck_failed",
            )
            self.state["execution_reason_code"] = reason_code
            return

        readiness_assessment = live_entry_assessment if strict_live_mode else entry_assessment
        ready = readiness_assessment["live_ready"] if strict_live_mode else readiness_assessment["dry_run_ready"]
        if not ready and execution_decision.action == BLOCK:
            self._record_execution_attempt(
                candidate,
                readiness_assessment,
                mode_ctx,
                market_context,
                str(readiness_assessment["reason_code"]),
                str(readiness_assessment["reason"]),
                attempt_id=attempt_id,
                detected_at=detected_at,
                validated_at=validated_at,
                final_outcome_at=utc_now().isoformat(),
                spread_at_validation=spread_at_validation,
                failure_stage="validation",
                execution_state="precheck_failed",
            )
            self.state["execution_reason_code"] = self._normalize_block_reason(str(readiness_assessment["reason_code"])) or str(readiness_assessment["reason_code"])
            return

        duplicate_stage = self._duplicate_exposure_stage(str(candidate["setup_fingerprint"]), now_utc)
        if not duplicate_stage["ok"]:
            self._record_execution_attempt(
                candidate,
                readiness_assessment,
                mode_ctx,
                market_context,
                str(duplicate_stage["reason_code"]),
                str(duplicate_stage["human_reason"]),
                attempt_id=attempt_id,
                detected_at=detected_at,
                validated_at=validated_at,
                final_outcome_at=utc_now().isoformat(),
                spread_at_validation=spread_at_validation,
                failure_stage="precheck",
                execution_state="precheck_failed",
            )
            self.state["execution_reason_code"] = self._normalize_block_reason(str(duplicate_stage["reason_code"])) or str(duplicate_stage["reason_code"])
            return

        try:
            trade_plan = self.risk_manager.calculate_trade_levels(candidate, float(entry_assessment["entry_price"]), symbol_spec)
            spread_limit_used, spread_limit_source = self._spread_policy_limit(candidate, market_context)
            spread_ok, spread_payload = self._wait_for_spread_improvement(spread_limit_used)
            spread_at_validation = float(spread_payload.get("spread_points", spread_at_validation or 0.0) or 0.0)
            if not spread_ok:
                self._record_execution_attempt(
                    candidate,
                    entry_assessment,
                    mode_ctx,
                    market_context,
                    "spread_too_wide",
                    f"Spread {spread_at_validation:.2f} exceeds {float(spread_limit_used):.2f}",
                    trade_plan=trade_plan,
                    attempt_id=attempt_id,
                    detected_at=detected_at,
                    validated_at=validated_at,
                    final_outcome_at=utc_now().isoformat(),
                    spread_at_validation=spread_at_validation,
                    spread_limit_used=spread_limit_used,
                    spread_limit_source=spread_limit_source,
                    failure_stage="precheck",
                    execution_state="precheck_failed",
                )
                self.state["execution_reason_code"] = "spread_too_wide"
                return

            if execution_decision.action == BLOCK:
                self._record_execution_attempt(
                    candidate,
                    entry_assessment,
                    mode_ctx,
                    market_context,
                    str(execution_decision.reason_code or entry_assessment.get("reason_code") or "entry_not_ready"),
                    str(execution_decision.reason or entry_assessment.get("reason") or "entry_not_ready"),
                    trade_plan=trade_plan,
                    attempt_id=attempt_id,
                    detected_at=detected_at,
                    validated_at=validated_at,
                    final_outcome_at=utc_now().isoformat(),
                    spread_at_validation=spread_at_validation,
                    spread_limit_used=spread_limit_used,
                    spread_limit_source=spread_limit_source,
                    failure_stage="precheck",
                    execution_state="precheck_failed",
                )
                self.state["execution_reason_code"] = self._normalize_block_reason(str(execution_decision.reason_code)) or str(execution_decision.reason_code)
                return

            risk_decision = self.risk_engine.approve_order(
                candidate=candidate,
                entry=entry_assessment,
                market_context=market_context,
                account_info=account_info,
                symbol_spec=symbol_spec,
                spread_points=spread_at_validation,
                live_requested=bool(strict_live_mode or live_entry_assessment.get("live_ready") or execution_decision.action != BLOCK),
                reduced_risk=bool(risk_gate["reduced_risk"]),
            )
            size_result = risk_decision.metadata.get("sizing", {}) if isinstance(risk_decision.metadata, dict) else {}
            if risk_decision.approved and isinstance(risk_decision.metadata, dict) and risk_decision.metadata.get("trade_plan"):
                trade_plan = risk_decision.metadata["trade_plan"]
            if not size_result.get("valid"):
                reason_code = str(risk_decision.reason_code or size_result.get("reason_code") or "risk_rejected")
                self._record_execution_attempt(
                    candidate,
                    entry_assessment,
                    mode_ctx,
                    market_context,
                    reason_code,
                    str(size_result.get("reason") or reason_code),
                    trade_plan=trade_plan,
                    requested_volume=float(size_result.get("requested_volume", 0.0)),
                    attempt_id=attempt_id,
                    detected_at=detected_at,
                    validated_at=validated_at,
                    final_outcome_at=utc_now().isoformat(),
                    spread_at_validation=spread_at_validation,
                    spread_limit_used=spread_limit_used,
                    spread_limit_source=spread_limit_source,
                    failure_stage="precheck",
                    execution_state="precheck_failed",
                )
                self.state["execution_reason_code"] = self._normalize_block_reason(reason_code) or reason_code
                return

            order_type = 0 if trade_plan["direction"] == "LONG" else 1
            margin_result = self.risk_manager.fit_volume_to_margin(
                float(size_result["volume"]),
                account_info,
                symbol_spec,
                lambda volume: self.connector.calculate_margin(order_type=order_type, volume=float(volume), price=float(trade_plan["entry_price"])),
            )
            if not margin_result.get("valid"):
                self._record_execution_attempt(
                    candidate,
                    entry_assessment,
                    mode_ctx,
                    market_context,
                    str(margin_result["reason_code"]),
                    str(margin_result["reason"]),
                    trade_plan=trade_plan,
                    requested_volume=float(size_result["requested_volume"]),
                    final_volume=float(margin_result.get("volume", 0.0)),
                    attempt_id=attempt_id,
                    detected_at=detected_at,
                    validated_at=validated_at,
                    final_outcome_at=utc_now().isoformat(),
                    spread_at_validation=spread_at_validation,
                    spread_limit_used=spread_limit_used,
                    spread_limit_source=spread_limit_source,
                    failure_stage="precheck",
                    execution_state="precheck_failed",
                )
                self.state["execution_reason_code"] = self._normalize_block_reason(str(margin_result["reason_code"])) or str(margin_result["reason_code"])
                return

            requested_volume = float(size_result["requested_volume"])
            final_volume = float(margin_result["volume"])
            current_risk_pct = float(self.config.get("risk", {}).get("risk_percent", 0.0) or 0.0)
            if self.dynamic_position_sizer is not None:
                dynamic_risk_pct = self.dynamic_position_sizer.calculate_position_risk(
                    regime=str(market_context.get("regime", {}).get("regime_name", "NO_TRADE")),
                    entry_score=float(entry_assessment.get("entry_score", 0.0) or 0.0),
                    bias_confidence=float(market_context.get("bias", {}).get("confidence", 0.0) or 0.0),
                    account_balance=float(account_info.balance),
                    current_drawdown_pct=float(self.state.get("drawdown_pct", self.state.get("daily_drawdown_pct", 0.0)) or 0.0),
                )
                base_risk = max(current_risk_pct, 1e-9)
                scale = max(0.0, dynamic_risk_pct / base_risk)
                requested_volume *= scale
                final_volume *= scale
                size_result["requested_volume"] = requested_volume
                size_result["volume"] = final_volume
                current_risk_pct = dynamic_risk_pct
            exposure_policy = ExposurePolicy(self.config)
            open_exposures = [
                ExposureSnapshot(
                    position_id=str(getattr(position, "ticket", getattr(position, "position_id", ""))),
                    symbol=str(getattr(position, "symbol", self.config["mt5"]["symbol"])),
                    side=str((self.state.get("open_position") or {}).get("direction") or getattr(position, "side", "")),
                    setup_family=str((self.state.get("open_position") or {}).get("setup_family") or ""),
                    strategy_family=str((self.state.get("open_position") or {}).get("strategy_family") or ""),
                    setup_fingerprint=str((self.state.get("open_position") or {}).get("setup_fingerprint") or ""),
                    risk_pct=current_risk_pct,
                    pending=False,
                    anchor_time=str((self.state.get("open_position") or {}).get("opened_at") or ""),
                )
                for position in self.connector.get_bot_positions()
            ]
            pending_exposures = [
                ExposureSnapshot(
                    position_id=str(getattr(order, "ticket", getattr(order, "order", ""))),
                    symbol=str(getattr(order, "symbol", self.config["mt5"]["symbol"])),
                    side=str((self.state.get("pending_order") or {}).get("direction") or ""),
                    setup_family=str((self.state.get("pending_order") or {}).get("setup_family") or ""),
                    strategy_family=str((self.state.get("pending_order") or {}).get("strategy_family") or ""),
                    setup_fingerprint=str((self.state.get("pending_order") or {}).get("setup_fingerprint") or ""),
                    risk_pct=current_risk_pct,
                    pending=True,
                    anchor_time=str((self.state.get("pending_order") or {}).get("submitted_at") or ""),
                )
                for order in self.connector.get_pending_orders()
            ]
            if strict_live_mode and bool(self.config.get("execution", {}).get("allow_multi_position", False)) and (open_exposures or pending_exposures):
                reason_code = "blocked_live_multi_position_unsupported"
                self._record_execution_attempt(
                    candidate,
                    entry_assessment,
                    mode_ctx,
                    market_context,
                    reason_code,
                    "Live runtime still tracks one managed position/pending order; multi-position remains backtest-only until lifecycle state is upgraded",
                    trade_plan=trade_plan,
                    requested_volume=requested_volume,
                    final_volume=final_volume,
                    attempt_id=attempt_id,
                    detected_at=detected_at,
                    validated_at=validated_at,
                    final_outcome_at=utc_now().isoformat(),
                    spread_at_validation=spread_at_validation,
                    spread_limit_used=spread_limit_used,
                    spread_limit_source=spread_limit_source,
                    failure_stage="precheck",
                    execution_state="precheck_failed",
                )
                self.state["execution_reason_code"] = reason_code
                return
            exposure_decision = exposure_policy.evaluate(
                candidate=candidate,
                projected_risk_pct=(float(size_result.get("risk_amount", 0.0) or 0.0) / max(float(account_info.balance), 1e-9)) * 100.0,
                open_exposures=open_exposures,
                pending_exposures=pending_exposures,
            )
            if not exposure_decision.allowed:
                reason_code = str(exposure_decision.reason_code or "blocked_portfolio_risk_cap")
                self._record_execution_attempt(
                    candidate,
                    entry_assessment,
                    mode_ctx,
                    market_context,
                    reason_code,
                    str(exposure_decision.reason or reason_code),
                    trade_plan=trade_plan,
                    requested_volume=requested_volume,
                    final_volume=final_volume,
                    attempt_id=attempt_id,
                    detected_at=detected_at,
                    validated_at=validated_at,
                    final_outcome_at=utc_now().isoformat(),
                    spread_at_validation=spread_at_validation,
                    spread_limit_used=spread_limit_used,
                    spread_limit_source=spread_limit_source,
                    failure_stage="precheck",
                    execution_state="precheck_failed",
                )
                self.state["execution_reason_code"] = self._normalize_block_reason(reason_code) or reason_code
                return
        except Exception as exc:
            self._log_exception("exception_before_send", "Exception before order_send", exc, details={"attempt_id": attempt_id}, notify=False)
            self._record_execution_attempt(
                candidate,
                entry_assessment,
                mode_ctx,
                market_context,
                "exception_before_send",
                str(exc),
                attempt_id=attempt_id,
                detected_at=detected_at,
                validated_at=validated_at,
                final_outcome_at=utc_now().isoformat(),
                spread_at_validation=spread_at_validation,
                spread_limit_used=spread_limit_used,
                spread_limit_source=spread_limit_source,
                failure_stage="exception",
                execution_state="precheck_failed",
            )
            self.state["execution_reason_code"] = "exception_before_send"
            return

        self.logger.structured(
            "risk_margin_fit",
            {
                "timestamp": now_utc.isoformat(),
                "mode": mode_ctx["mode"],
                "symbol": self.config["mt5"]["symbol"],
                "setup_fingerprint": candidate["setup_fingerprint"],
                "setup_family": candidate["setup_family"],
                "entry_price": size_result.get("entry_price", trade_plan["entry_price"]),
                "sl_distance_pips": size_result.get("sl_distance_pips"),
                "raw_lot": size_result.get("raw_lot"),
                "normalized_lot": size_result.get("normalized_volume", size_result.get("volume")),
                "adjusted_lot": final_volume,
                "requested_volume": requested_volume,
                "free_margin_before": margin_result.get("free_margin_before"),
                "margin_before": margin_result.get("margin_before"),
                "margin_level_before": margin_result.get("margin_level_before"),
                "expected_margin_usage": margin_result.get("required_margin"),
                "projected_margin_level": margin_result.get("projected_margin_level"),
                "requested_projected_margin_level": margin_result.get("requested_projected_margin_level"),
                "volume_adjusted": bool(margin_result.get("adjusted", False)),
                "fit_reason": margin_result.get("reason_code"),
            },
        )
        strict_live_candidate = (
            bool(live_validation.get("final_live_decision", False))
            and not degraded_gate.get("blocked")
            and not bool(risk_gate.get("risk_lock_active"))
        )
        if strict_live_candidate:
            self._record_execution_attempt(
                candidate,
                live_entry_assessment,
                mode_ctx,
                market_context,
                "live_eligible_setup",
                "Setup passed full live validation pipeline",
                trade_plan=trade_plan,
                requested_volume=requested_volume,
                final_volume=final_volume,
                event_type="validation_gate",
                update_registry=False,
                extra_metadata={"rollout_phase": mode_ctx["rollout_phase"], "strict_live_candidate": True},
                attempt_id=attempt_id,
                detected_at=detected_at,
                validated_at=validated_at,
                spread_at_validation=spread_at_validation,
                spread_limit_used=spread_limit_used,
                spread_limit_source=spread_limit_source,
                failure_stage="validation",
                execution_state="precheck_failed",
            )

        if self.demo_mode:
            self._open_demo_trade(candidate, entry_assessment, trade_plan, requested_volume, final_volume, market_context, mode_ctx)
            return

        entry_mode = str(entry_assessment.get("entry_mode") or "")
        uses_limit_entry = entry_mode in {"lebprim_limit", "limit_value", "wait_retest"}
        request_entry_price = float(
            entry_assessment.get("pending_entry_price")
            or entry_assessment.get("entry_price")
            or trade_plan["entry_price"]
        ) if uses_limit_entry else float(trade_plan["entry_price"])
        pending_expires_at = None
        if uses_limit_entry:
            pending_expiry_minutes = int(
                entry_assessment.get("pending_expiry_minutes")
                or self.config.get("strategy", {}).get("lebprim_pending_expiry_minutes", 5)
                or 5
            )
            pending_expires_at = (now_utc + timedelta(minutes=pending_expiry_minutes)).isoformat()

        self.trade_journal.record_trade_event(
            {
                "timestamp": now_utc.isoformat(),
                "trade_id": candidate["setup_fingerprint"],
                "mt5_ticket": candidate["setup_fingerprint"],
                "position_id": candidate["setup_fingerprint"],
                "event_type": JournalEventType.ORDER_SUBMITTED.value,
                "status": "SUBMITTED",
                "symbol": self.config["mt5"]["symbol"],
                "side": trade_plan["direction"],
                "setup": candidate["setup_fingerprint"],
                "setup_family": candidate["setup_family"],
                "regime": market_context["regime"]["regime_name"],
                "session": market_context["session"]["session_name"],
                "entry_price": request_entry_price,
                "stop_loss": trade_plan["stop_loss"],
                "take_profit": trade_plan["tp2"],
                "volume": final_volume,
                "execution_reason": "order_submitted",
                "comment": "order submitted to MT5",
                "metadata": {"candidate": candidate, "trade_plan": trade_plan},
            }
        )
        try:
            execution_plan = CanonicalExecutionPlan(
                strategy_name=str(candidate.get("strategy_name") or candidate.get("setup_family") or "XAU_BOT"),
                symbol=str(self.config["mt5"]["symbol"]),
                side=str(trade_plan["direction"]),
                order_type="limit" if uses_limit_entry else "market",
                entry_price=request_entry_price,
                volume=final_volume,
                stop_loss=float(trade_plan["stop_loss"]),
                take_profit=float(trade_plan["tp2"]),
                setup_family=str(candidate.get("setup_family") or ""),
                setup_fingerprint=str(candidate.get("setup_fingerprint") or ""),
                entry_mode=entry_mode,
                execution_model="live",
                signal_time=now_utc.isoformat(),
                execution_time=now_utc.isoformat(),
                data_available_through_time=now_utc.isoformat(),
                tp1=float(trade_plan.get("tp1", trade_plan["tp2"])),
                trigger_price=float(entry_assessment.get("trigger_price") or entry_assessment.get("entry_price") or request_entry_price),
                expires_at=pending_expires_at,
                metadata={
                    "comment": f"{str(self.config.get('execution', {}).get('order_comment_tag', 'XAU_BOT'))[:10]}_{candidate['setup_family'][:8]}",
                    "dry_run": mode_ctx["mode"] != "LIVE",
                    "execution_decision": entry_assessment.get("execution_decision"),
                    "execution_reason_code": entry_assessment.get("execution_reason_code"),
                    "quality_tier": entry_assessment.get("quality_tier"),
                    "strategy_family": entry_assessment.get("strategy_family"),
                    "management_profile": entry_assessment.get("management_profile"),
                },
            )
            execution_result = self.execution_service.submit_order(build_execution_request_from_plan(execution_plan))
            order_send_attempted = True
        except Exception as exc:
            self._log_exception("exception_after_send_unknown_state", "Exception during/after order_send", exc, details={"attempt_id": attempt_id}, notify=False)
            self._record_execution_attempt(
                candidate,
                entry_assessment,
                mode_ctx,
                market_context,
                "exception_after_send_unknown_state",
                str(exc),
                trade_plan=trade_plan,
                requested_volume=requested_volume,
                final_volume=final_volume,
                attempt_id=attempt_id,
                detected_at=detected_at,
                validated_at=validated_at,
                final_outcome_at=utc_now().isoformat(),
                order_send_attempted=True,
                spread_at_validation=spread_at_validation,
                spread_limit_used=spread_limit_used,
                spread_limit_source=spread_limit_source,
                failure_stage="exception",
                execution_state="send_attempted",
            )
            self.state["execution_reason_code"] = "exception_after_send_unknown_state"
            return
        pipeline = execution_result.get("pipeline_stages", [])
        order_check_stage = next((stage for stage in pipeline if stage.get("stage") == "order_check"), None)
        order_send_stage = next((stage for stage in pipeline if stage.get("stage") == "order_send_or_paper_accept"), None)
        if order_send_stage:
            send_payload = order_send_stage.get("payload") or {}
            send_retcode = send_payload.get("retcode")
            send_retcode_text = str(send_payload.get("comment") or "")

        if execution_result.get("ok") and str(execution_result.get("status")) == "pending":
            order_id = str(execution_result.get("order_id") or candidate["setup_fingerprint"])
            pending_state = {
                "order_id": order_id,
                "symbol": self.config["mt5"]["symbol"],
                "direction": trade_plan["direction"],
                "entry_price": request_entry_price,
                "sl": float(trade_plan["stop_loss"]),
                "tp1": float(trade_plan.get("tp1", trade_plan["tp2"])),
                "tp2": float(trade_plan["tp2"]),
                "volume": final_volume,
                "requested_volume": requested_volume,
                "setup_fingerprint": candidate["setup_fingerprint"],
                "setup_family": candidate["setup_family"],
                "strategy_family": entry_assessment.get("strategy_family"),
                "entry_mode": entry_mode,
                "execution_decision": entry_assessment.get("execution_decision"),
                "quality_tier": entry_assessment.get("quality_tier"),
                "management_profile": entry_assessment.get("management_profile"),
                "submitted_at": now_utc.isoformat(),
                "expires_at": pending_expires_at,
                "candidate": candidate,
                "entry_assessment": entry_assessment,
                "trade_plan": trade_plan,
                "market_context": market_context,
            }
            self.state["pending_order"] = pending_state
            self.state["execution_status"] = "pending"
            self.state["execution_reason_code"] = ReasonCode.ORDER_PENDING.value
            self._update_setup_registry(candidate["setup_fingerprint"], "pending", now_utc, {"order_id": order_id, "last_attempt_at": now_utc.isoformat()})
            self.trade_journal.record_trade_event(
                {
                    "timestamp": now_utc.isoformat(),
                    "trade_id": order_id,
                    "mt5_ticket": order_id,
                    "position_id": order_id,
                    "order_id": order_id,
                    "event_type": JournalEventType.ORDER_PENDING.value,
                    "status": "PENDING",
                    "symbol": pending_state["symbol"],
                    "side": pending_state["direction"],
                    "setup": pending_state["setup_fingerprint"],
                    "setup_family": pending_state["setup_family"],
                    "regime": market_context["regime"]["regime_name"],
                    "session": market_context["session"]["session_name"],
                    "entry_mode": entry_mode,
                    "execution_reason": ReasonCode.ORDER_PENDING.value,
                    "entry_price": request_entry_price,
                    "stop_loss": pending_state["sl"],
                    "take_profit": pending_state["tp2"],
                    "volume": final_volume,
                    "magic_number": self.config["mt5"]["magic_number"],
                    "metadata": pending_state,
                }
            )
            self._record_execution_attempt(
                candidate,
                entry_assessment,
                mode_ctx,
                market_context,
                ReasonCode.ORDER_PENDING.value,
                "Limit order submitted and waiting for fill",
                trade_plan=trade_plan,
                requested_volume=requested_volume,
                final_volume=final_volume,
                order_check_status=str(order_check_stage.get("reason_code") if order_check_stage else "not_reached"),
                order_send_status=str(order_send_stage.get("reason_code") if order_send_stage else "not_reached"),
                attempt_id=attempt_id,
                detected_at=detected_at,
                validated_at=validated_at,
                final_outcome_at=utc_now().isoformat(),
                order_send_attempted=True,
                order_send_retcode=send_retcode,
                order_send_retcode_text=send_retcode_text,
                spread_at_validation=spread_at_validation,
                spread_limit_used=spread_limit_used,
                spread_limit_source=spread_limit_source,
                failure_stage="broker",
                execution_state="send_accepted",
            )
            return

        if not execution_result.get("executed"):
            reason_code = str(execution_result.get("reason") or "order_send_failed")
            journal_reason_code = "paper_validated_test_mode" if mode_ctx["mode"] == "VALIDATION_TEST" and reason_code == "dry_run_validated" else reason_code
            journal_reason = (
                "Paper validation passed in validation-test mode"
                if journal_reason_code == "paper_validated_test_mode"
                else str(execution_result.get("execution_blocked_reason") or reason_code)
            )
            self._record_execution_attempt(
                candidate,
                entry_assessment,
                mode_ctx,
                market_context,
                journal_reason_code,
                journal_reason,
                trade_plan=trade_plan,
                requested_volume=requested_volume,
                final_volume=final_volume,
                order_check_status=str(order_check_stage.get("reason_code") if order_check_stage else "not_reached"),
                order_send_status=str(order_send_stage.get("reason_code") if order_send_stage else "not_reached"),
                extra_metadata={
                    "failure_class": execution_result.get("failure_class"),
                    "execution_blocked_reason": execution_result.get("execution_blocked_reason"),
                    "mt5_request": execution_result.get("request"),
                    "mt5_result": execution_result.get("result"),
                },
                attempt_id=attempt_id,
                detected_at=detected_at,
                validated_at=validated_at,
                final_outcome_at=utc_now().isoformat(),
                order_send_attempted=order_send_attempted,
                order_send_retcode=send_retcode,
                order_send_retcode_text=send_retcode_text,
                spread_at_validation=spread_at_validation,
                spread_limit_used=spread_limit_used,
                spread_limit_source=spread_limit_source,
                duplicate_guard_applied=True,
                failure_stage="send" if order_send_attempted else "precheck",
                execution_state="send_rejected" if order_send_attempted else "precheck_failed",
            )
            self.state["execution_reason_code"] = normalize_final_outcome(
                journal_reason_code,
                str(execution_result.get("failure_class") or ""),
                execution_state="send_rejected" if order_send_attempted else "precheck_failed",
                order_send_attempted=bool(order_send_attempted),
            )
            if str(execution_result.get("failure_class")) == "broker_rejection" and journal_reason_code not in {"dry_run_validated", "paper_validated_test_mode"}:
                self._mark_hard_rejection(str(candidate["setup_fingerprint"]), journal_reason_code, journal_reason, execution_result)
            if mode_ctx["mode"] in {"DRY_RUN", "VALIDATION_TEST"} and reason_code == "dry_run_validated":
                self.state["execution_status"] = "paper_validated"
            elif str(execution_result.get("failure_class")) in {"runtime_api_failure", "connection_failure"}:
                self._register_infra_failure("execution_failure", reason_code, str(execution_result.get("execution_blocked_reason") or reason_code), execution_result)
            return

        actual_entry = float(execution_result.get("entry") or trade_plan["entry_price"])
        slippage = actual_entry - float(trade_plan["entry_price"]) if trade_plan["direction"] == "LONG" else float(trade_plan["entry_price"]) - actual_entry
        actual_trade_plan = self.risk_manager.rebase_trade_levels(trade_plan, actual_entry, symbol_spec)
        actual_trade_plan["stop_loss"] = float(execution_result.get("sl") or actual_trade_plan["stop_loss"])
        actual_trade_plan["tp2"] = float(execution_result.get("tp") or actual_trade_plan["tp2"])
        ticket = str((execution_result.get("result") or {}).get("order") or (execution_result.get("result") or {}).get("deal") or int(now_utc.timestamp()))
        position_state = self.risk_manager.build_position_state(
            actual_trade_plan,
            ticket,
            self.config["mt5"]["symbol"],
            final_volume,
            candidate,
            entry_assessment,
            now_utc,
            mode_ctx["mode"],
            market_context["spread_points"],
            requested_volume,
            slippage,
        )
        self.state["open_position"] = position_state
        self.state["execution_status"] = "executed"
        self.state["execution_reason_code"] = "executed_live"
        self.state["daily_trade_count"] = int(self.state.get("daily_trade_count", 0)) + 1
        self._update_setup_registry(candidate["setup_fingerprint"], "executed", now_utc, {"ticket": ticket, "last_attempt_at": now_utc.isoformat()})

        trade_payload = self._build_trade_open_payload(position_state)
        trade_payload["order_id"] = str((execution_result.get("result") or {}).get("order") or ticket)
        trade_payload["execution_reason"] = "executed"
        trade_payload["risk_amount"] = position_state["initial_risk_price"] * max(position_state["volume"], 1.0)
        trade_payload["risk_percent"] = self.config["risk"].get("risk_percent")
        trade_payload["magic_number"] = self.config["mt5"]["magic_number"]
        trade_payload["timeframe_bias"] = self.config["timeframes"]["trend"]
        trade_payload["timeframe_setup"] = self.config["timeframes"]["setup"]
        trade_payload["timeframe_entry"] = self.config["timeframes"]["trigger"]
        trade_payload["comment"] = position_state["setup_fingerprint"]
        self._journal_trade_open(trade_payload)
        self._record_execution_attempt(
            candidate,
            entry_assessment,
            mode_ctx,
            market_context,
            "executed",
            "Live order executed",
            trade_plan=actual_trade_plan,
            requested_volume=requested_volume,
            final_volume=final_volume,
            order_check_status=str(order_check_stage.get("reason_code") if order_check_stage else "not_reached"),
            order_send_status=str(order_send_stage.get("reason_code") if order_send_stage else "not_reached"),
            attempt_id=attempt_id,
            detected_at=detected_at,
            validated_at=validated_at,
            final_outcome_at=utc_now().isoformat(),
            order_send_attempted=True,
            order_send_retcode=send_retcode,
            order_send_retcode_text=send_retcode_text,
            spread_at_validation=spread_at_validation,
            spread_limit_used=spread_limit_used,
            spread_limit_source=spread_limit_source,
            cooldown_applied=True,
            duplicate_guard_applied=True,
            failure_stage="broker",
            execution_state="send_accepted",
        )
        self._safe_notify_call(lambda: self.notifier.send_trade_open(trade_payload))

    def _build_trade_open_payload(self, position_state: dict[str, Any]) -> dict[str, Any]:
        """Create a trade-open payload for journals and notifications."""
        ticket = str(position_state["ticket"])
        return {
            "timestamp": utc_now().isoformat(),
            "trade_id": ticket,
            "mt5_ticket": ticket,
            "order_id": position_state.get("order_id") or ticket,
            "event_type": "TRADE_OPENED",
            "mode": position_state["mode"],
            "symbol": position_state["symbol"],
            "side": position_state["direction"],
            "type": position_state["direction"],
            "ticket": ticket,
            "position_id": position_state["position_id"],
            "setup_fingerprint": position_state["setup_fingerprint"],
            "setup": position_state["setup_fingerprint"],
            "setup_family": position_state["setup_family"],
            "setup_type": position_state["setup_type"],
            "regime": position_state["regime_at_entry"],
            "regime_at_entry": position_state["regime_at_entry"],
            "session": position_state["session_at_entry"],
            "session_at_entry": position_state["session_at_entry"],
            "trigger_type": position_state["trigger_type"],
            "entry_time": position_state["opened_at"],
            "entry": position_state["entry_price"],
            "entry_price": position_state["entry_price"],
            "sl": position_state["sl"],
            "tp": position_state["tp2"],
            "volume": position_state["volume"],
            "lot_size": position_state["volume"],
            "requested_volume": position_state["requested_volume"],
            "final_volume": position_state["volume"],
            "executed_volume": position_state.get("executed_volume", position_state["volume"]),
            "volume_adjusted": position_state.get("volume_adjusted", False),
            "spread_at_entry": position_state["spread_at_entry"],
            "slippage": position_state["slippage"],
            "initial_risk": position_state["initial_risk_price"],
            "initial_risk_pips": position_state["initial_risk_pips"],
            "status": "OPENED",
            "note": f"{position_state['entry_mode']} | fingerprint={position_state['setup_fingerprint']}",
            "score": position_state["entry_score"],
            "entry_mode": position_state["entry_mode"],
            "metadata": position_state,
        }

    def _open_demo_trade(
        self,
        candidate: dict[str, Any],
        entry_assessment: dict[str, Any],
        trade_plan: dict[str, Any],
        requested_volume: float,
        final_volume: float,
        market_context: dict[str, Any],
        mode_ctx: dict[str, Any],
    ) -> None:
        """Open a simulated local trade in demo mode."""
        now_utc = utc_now()
        ticket = f"DEMO-{int(now_utc.timestamp())}"
        position_state = self.risk_manager.build_position_state(
            trade_plan,
            ticket,
            self.config["mt5"]["symbol"],
            final_volume,
            candidate,
            entry_assessment,
            now_utc,
            mode_ctx["mode"],
            market_context["spread_points"],
            requested_volume,
            0.0,
        )
        self.state["demo_position"] = position_state
        self.state["daily_trade_count"] = int(self.state.get("daily_trade_count", 0)) + 1
        self.state["execution_status"] = "executed"
        self.state["execution_reason_code"] = "executed"
        self._update_setup_registry(candidate["setup_fingerprint"], "executed", now_utc, {"ticket": ticket})
        trade_payload = self._build_trade_open_payload(position_state)
        trade_payload["execution_reason"] = "executed"
        trade_payload["risk_amount"] = position_state["initial_risk_price"] * max(position_state["volume"], 1.0)
        trade_payload["risk_percent"] = self.config["risk"].get("risk_percent")
        trade_payload["magic_number"] = self.config["mt5"]["magic_number"]
        trade_payload["comment"] = position_state["setup_fingerprint"]
        self._journal_trade_open(trade_payload)
        self._record_execution_attempt(candidate, entry_assessment, mode_ctx, market_context, "executed", "Demo trade accepted", trade_plan=trade_plan, requested_volume=requested_volume, final_volume=final_volume)

    def _manage_existing_positions(
        self,
        trigger_df: pd.DataFrame,
        setup_df: pd.DataFrame,
        market_context: dict[str, Any],
        mode_ctx: dict[str, Any],
    ) -> None:
        """Manage open demo or live positions."""
        if self.demo_mode:
            position = self.state.get("demo_position")
            if not position or trigger_df.empty:
                return
            current_price = float(trigger_df.iloc[-1]["close"])
            actions = self.risk_manager.evaluate_management_actions(position, trigger_df, setup_df, current_price, utc_now(), self.connector.get_symbol_spec())
            self.logger.structured(
                "position_update",
                {
                    "mode": mode_ctx["mode"],
                    "ticket": position["ticket"],
                    "symbol": position["symbol"],
                    "direction": position["direction"],
                    "entry_price": position["entry_price"],
                    "current_price": current_price,
                    "remaining_volume": position["remaining_volume"],
                    "sl": position["sl"],
                    "tp": position["tp2"],
                    "session": market_context["session"]["session_name"],
                    "regime": market_context["regime"]["regime_name"],
                    "pending_actions": [action["action"] for action in actions],
                },
            )
            for action in actions:
                self.logger.structured("trade_management_action", {"ticket": position["ticket"], **action})
                if action["action"] == "move_stop":
                    position["sl"] = float(action["sl"])
                    self.trade_journal.record_trade_managed({**position, "event_type": "TRADE_MANAGED", "status": "OPENED", "note": action.get("reason_code"), "comment": action.get("human_reason")})
                elif action["action"] == "close_full":
                    self._finalize_demo_trade(position, current_price, action["reason_code"])
                    return
        else:
            self.live_lifecycle.sync_pending_order(market_context, mode_ctx)
            self._recover_live_position_if_needed()
            position = self.state.get("open_position")
            if not position:
                return
            live_position = self.connector.get_position_by_ticket(position["ticket"])
            if live_position is None:
                self._finalize_live_trade_from_history(position, "position_closed")
                return
            tick = self.connector.get_tick()
            current_price = float(tick.bid) if str(position["direction"]) == "LONG" else float(tick.ask)
            actions = self.risk_manager.evaluate_management_actions(position, trigger_df, setup_df, current_price, utc_now(), self.connector.get_symbol_spec())
            self.logger.structured(
                "position_update",
                {
                    "mode": mode_ctx["mode"],
                    "ticket": position["ticket"],
                    "symbol": position["symbol"],
                    "direction": position["direction"],
                    "entry_price": position["entry_price"],
                    "current_price": current_price,
                    "remaining_volume": position["remaining_volume"],
                    "sl": position["sl"],
                    "tp": position["tp2"],
                    "session": market_context["session"]["session_name"],
                    "regime": market_context["regime"]["regime_name"],
                    "pending_actions": [action["action"] for action in actions],
                },
            )
            closed = self.live_lifecycle.apply_management_actions(
                position,
                actions,
                self._finalize_live_trade_from_history,
            )
            if closed:
                return

    def _recover_live_position_if_needed(self) -> None:
        """Rebuild minimal state if a live bot position exists after restart."""
        if self.demo_mode or self.state.get("open_position"):
            return
        if not bool(self.config.get("bot", {}).get("auto_recover_live_positions", False)):
            return
        positions = self.connector.get_bot_positions()
        if not positions:
            return
        position = positions[0]
        direction = "LONG" if int(position.type) == 0 else "SHORT"
        entry_price = float(position.price_open)
        stop_loss = float(position.sl or entry_price)
        risk_price = abs(entry_price - stop_loss)
        recovered = {
            "ticket": str(position.ticket),
            "position_id": str(position.ticket),
            "symbol": position.symbol,
            "mode": "LIVE",
            "direction": direction,
            "entry_price": entry_price,
            "sl": stop_loss,
            "tp1": entry_price + risk_price if direction == "LONG" else entry_price - risk_price,
            "tp2": float(position.tp or entry_price),
            "volume": float(position.volume),
            "requested_volume": float(position.volume),
            "remaining_volume": float(position.volume),
            "setup_fingerprint": f"RECOVERED|{position.ticket}",
            "setup_family": "RECOVERED",
            "setup_type": "RECOVERED",
            "regime_at_entry": "UNKNOWN",
            "session_at_entry": "UNKNOWN",
            "trigger_type": "RECOVERED",
            "entry_mode": "recovered",
            "atr_at_entry": 0.0,
            "initial_risk_price": risk_price,
            "initial_risk_pips": price_to_pips(risk_price, self.connector.get_symbol_spec()["pip_size"]),
            "trend_score": 0.0,
            "setup_score": 0.0,
            "trigger_score": 0.0,
            "entry_score": 0.0,
            "spread_at_entry": 0.0,
            "slippage": 0.0,
            "partial_closed": False,
            "partial_closed_volume": 0.0,
            "breakeven_moved": False,
            "trailing_active": False,
            "opened_at": utc_now().isoformat(),
            "highest_price": entry_price,
            "lowest_price": entry_price,
            "mfe": 0.0,
            "mae": 0.0,
            "partial_realized_pnl": 0.0,
            "close_reason": None,
        }
        self.state["open_position"] = recovered
        self._record_event("WARNING", "position_recovered", f"Recovered live position state for ticket {position.ticket}")

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

    def _session_close_due(self, now_utc: datetime) -> bool:
        """Return whether the configured session close time has been reached."""
        timezone_name = str(self.config["sessions"].get("timezone", "UTC"))
        local_now = now_utc.astimezone(__import__("pytz").timezone(timezone_name))
        close_hhmm = str(self.config["sessions"].get("session_close", "21:40"))
        close_minutes = int(close_hhmm.split(":")[0]) * 60 + int(close_hhmm.split(":")[1])
        return (local_now.hour * 60 + local_now.minute) >= close_minutes

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

    def _maybe_auto_generate_daily_reports(self, now_utc: datetime) -> None:
        """Generate reports once per day after the configured UTC hour."""
        if not bool(self.config.get("bot", {}).get("analytics_auto_generate", True)):
            return
        report_hour = int(self.config.get("bot", {}).get("analytics_report_hour_utc", 22))
        if now_utc.hour < report_hour:
            return
        day_key = now_utc.date().isoformat()
        if self.state.get("daily_report_generated_date") == day_key:
            return
        self._generate_daily_reports(day_key, source="auto")
        self.state["daily_report_generated_date"] = day_key

    def _handle_session_close(self, now_utc: datetime, market_context: dict[str, Any]) -> None:
        """Enforce session-end shutdown behavior and emit analytics."""
        if self.demo_mode and self.state.get("demo_position"):
            self._finalize_demo_trade(self.state["demo_position"], float(self.state["demo_position"]["entry_price"]), "session_close")
        elif not self.demo_mode and self.state.get("open_position"):
            self.connector.close_all_bot_positions()
            self._finalize_live_trade_from_history(self.state["open_position"], "session_close")
        self._emit_validation_report(now_utc.date().isoformat(), "session", market_context["session"]["session_name"], self._execution_mode_summary())
        self._emit_daily_analytics(now_utc.date().isoformat(), self._execution_mode_summary()["mode"])
        self._write_heartbeat("SESSION_CLOSED", None, market_context, self.connector.account_info, 0, "session_close")

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
            self.connector.shutdown()
            self._release_instance_lock()
            self.control_state.mark_process_stopped(str(self.state.get("shutdown_reason") or "shutdown"))

    def run_diagnostics(self) -> int:
        """Print and log a rollout-aware diagnostics snapshot without running the bot loop."""
        try:
            now_utc = utc_now()
            mode_ctx = self._execution_mode_summary()
            report = self.connector.health_report()
            session_info = self.strategy.classify_session(now_utc)
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
