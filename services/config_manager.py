"""Safe config persistence and dashboard-facing config helpers."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from trading_bot.core.execution_mode import apply_execution_mode_to_config, dashboard_mode_options
from utils import ensure_directory, load_json, load_runtime_config, normalize_trading_mode, save_json_atomic, utc_now


class ConfigManager:
    """Load, validate, diff, and persist bot configuration safely."""

    RESTART_REQUIRED_PATHS = {
        "mt5.terminal_path",
        "mt5.login",
        "mt5.password",
        "mt5.server",
        "storage.database_path",
        "storage.state_path",
        "dashboard.host",
        "dashboard.default_port",
        "dashboard.port",
    }
    HOT_RELOADABLE_PREFIXES = {
        "bot.",
        "strategy.",
        "risk.",
        "execution.",
        "exit.",
        "entry.",
        "validation.",
        "shorts.",
        "sessions.",
        "symbols.",
        "cooldowns.",
        "backtest.",
        "mt5.deviation",
        "telegram.",
        "dashboard.readonly_mode",
        "dashboard.refresh_seconds",
        "dashboard.stale_heartbeat_seconds",
    }

    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)
        self.config_path = self.base_dir / "config.json"
        self.shadow_config_path = self.base_dir / "storage" / "config.json"
        self.backup_dir = ensure_directory(self.base_dir / "storage" / "config_backups")

    def load_raw(self) -> dict[str, Any]:
        """Load the raw JSON config file."""
        return load_json(self.config_path, {})

    def load_runtime(self) -> dict[str, Any]:
        """Load the normalized runtime config."""
        return load_runtime_config(self.base_dir, require_mt5_credentials=False, apply_mode_env_override=False)

    def resolve_runtime_mode(self, control_state: dict[str, Any] | None = None) -> tuple[str, str]:
        """Resolve the authoritative runtime mode with clear precedence.
        
        Precedence (highest to lowest):
        1. Control state runtime mode (if provided and fresh) - dashboard/bot live selection
        2. Config file mode - persisted last-known mode
        3. Default fallback - "DRY_RUN"
        
        Returns:
            Tuple of (resolved_mode, source) where source is one of:
            - "control_state" (dashboard/live override)
            - "config_file" (persisted configuration)
            - "default" (fallback)
        """
        # Priority 1: Check control_state if provided (represents live operator intent)
        if control_state and isinstance(control_state, dict):
            runtime = control_state.get("runtime", {})
            mode_from_control = runtime.get("mode")
            if mode_from_control:
                normalized = normalize_trading_mode(mode_from_control)
                if normalized:
                    return normalized, "control_state"
        
        # Priority 2: Check config.json (last persisted/default configuration)
        try:
            config = self.load_runtime()
            mode_from_config = config.get("bot", {}).get("trading_mode")
            if mode_from_config:
                normalized = normalize_trading_mode(mode_from_config)
                if normalized:
                    return normalized, "config_file"
        except Exception:
            pass
        
        # Priority 3: Default fallback
        return normalize_trading_mode("DRY_RUN"), "default"

    def load_shadow(self) -> dict[str, Any]:
        """Load the non-authoritative storage shadow config if present."""
        return load_json(self.shadow_config_path, {})

    def save(self, config: dict[str, Any], actor: str = "dashboard", reason: str = "config_update") -> dict[str, Any]:
        """Persist config with backup and rollback protection."""
        backup_path = self.create_backup()
        previous = self.load_raw()
        save_json_atomic(self.config_path, config)
        try:
            validated = load_runtime_config(self.base_dir, require_mt5_credentials=False, apply_mode_env_override=False)
        except Exception:
            save_json_atomic(self.config_path, previous)
            raise
        save_json_atomic(self.shadow_config_path, validated)
        return {
            "ok": True,
            "backup_path": str(backup_path),
            "saved_at": utc_now().isoformat(),
            "actor": actor,
            "reason": reason,
            "config": validated,
        }

    def create_backup(self) -> Path:
        """Create a timestamped backup before overwriting config."""
        timestamp = utc_now().strftime("%Y%m%d-%H%M%S")
        backup_path = self.backup_dir / f"config-{timestamp}.json"
        save_json_atomic(backup_path, self.load_raw())
        return backup_path

    def apply_updates(self, updates: dict[str, Any]) -> dict[str, Any]:
        """Apply nested updates onto the current raw config."""
        merged = copy.deepcopy(self.load_raw())
        self._deep_merge(merged, updates)
        self._normalize(merged)
        return merged

    def diff(self, before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
        """Return a flattened config diff."""
        changes: list[dict[str, Any]] = []
        self._walk_diff("", before, after, changes)
        return changes

    def classify_changes(self, changes: list[dict[str, Any]]) -> dict[str, Any]:
        """Classify config changes by runtime apply behavior."""
        hot: list[dict[str, Any]] = []
        restart: list[dict[str, Any]] = []
        for change in changes:
            path = str(change.get("path") or "")
            if self._requires_restart(path):
                restart.append(change)
            else:
                hot.append(change)
        return {
            "hot_reloadable": hot,
            "restart_required": restart,
            "applied_immediately": bool(hot),
            "pending_restart": bool(restart),
            "hot_reloadable_count": len(hot),
            "restart_required_count": len(restart),
        }

    def control_plane_view(self) -> dict[str, Any]:
        """Return effective config plus operator trust metadata."""
        config = self.dashboard_view()
        return {
            "config": config,
            "resolved_runtime": self.resolved_runtime_snapshot(config),
            "source_of_truth": self.source_of_truth_snapshot(),
            "metadata": {
                "source": str(self.config_path),
                "model": "single_json_with_validated_dashboard_overrides",
                "hot_reloadable_prefixes": sorted(self.HOT_RELOADABLE_PREFIXES),
                "restart_required_paths": sorted(self.RESTART_REQUIRED_PATHS),
                "execution_mode_options": dashboard_mode_options(),
            },
        }

    def export(self) -> str:
        """Return the current raw config as JSON text."""
        return json.dumps(self.load_raw(), indent=4)

    def import_text(self, payload: str) -> dict[str, Any]:
        """Parse imported JSON config text."""
        parsed = json.loads(payload)
        if not isinstance(parsed, dict):
            raise ValueError("Imported config must be a JSON object")
        self._normalize(parsed)
        return parsed

    def dashboard_view(self) -> dict[str, Any]:
        """Return a dashboard-friendly config payload."""
        config = self.load_runtime()
        config, resolved_mode = apply_execution_mode_to_config(config)
        strategy_cfg = config.setdefault("strategy", {})
        setup_families = strategy_cfg.setdefault("setup_families", {})
        setup_controls = strategy_cfg.setdefault("setup_controls", {})
        enabled_symbol = str(config.get("mt5", {}).get("symbol", "XAUUSD"))
        symbols_cfg = config.setdefault("symbols", {})
        symbols_cfg.setdefault(
            enabled_symbol,
            {
                "enabled": True,
                "display_name": enabled_symbol,
                "tradeable": True,
            },
        )
        for family_key, family_cfg in setup_families.items():
            setup_controls.setdefault(
                family_key,
                {
                    "enabled": bool(family_cfg.get("enabled", True)),
                    "min_trend_score": None,
                    "min_setup_score": None,
                    "min_trigger_score": None,
                    "min_entry_score": float(strategy_cfg.get("min_score_to_trade", 47.0)),
                    "confirmation_required": False,
                    "require_trend_alignment": bool(strategy_cfg.get("require_trend_alignment", True)),
                    "allowed_sessions": list(family_cfg.get("sessions", [])),
                    "max_spread_points": float(config.get("execution", {}).get("max_spread_points", 25.0)),
                    "sl_multiplier": 1.0,
                    "tp_multiplier": 1.0,
                    "cooldown_minutes": int(config.get("cooldowns", {}).get("execution_cooldown_minutes", 8)),
                    "max_trades_per_session": int(config.get("risk", {}).get("max_trades_per_symbol", 3)),
                    "allowed_regimes": list(family_cfg.get("regimes", [])),
                    "allow_long": True,
                    "allow_short": True,
                },
            )
        config.setdefault("dashboard", {})
        config["dashboard"].setdefault("readonly_mode", False)
        config["dashboard"].setdefault("host", "0.0.0.0")
        config["dashboard"].setdefault("default_port", 8501)
        config["dashboard"].setdefault("theme", "dark")
        config.setdefault("execution", {})
        config.setdefault("backtest", {})
        config["execution"].setdefault("backtest_fill_model", config["backtest"].get("execution_model", "next_bar_open"))
        config["execution"].setdefault("manual_trading_enabled", True)
        config["execution"].setdefault("manual_trade_comment_tag_dashboard", "MANUAL_DASH")
        config["execution"].setdefault("manual_trade_comment_tag_telegram", "MANUAL_TG")
        config.setdefault("telegram", {})
        config["telegram"].setdefault("remote_control_enabled", False)
        config["telegram"].setdefault("manual_trade_commands_enabled", False)
        config["telegram"].setdefault("admin_chat_ids", [])
        config["telegram"].setdefault("admin_user_ids", [])
        config["telegram"].setdefault("confirmation_required_commands", ["LIVE", "KILLSWITCH_OFF", "CLOSE", "CLOSEALL", "BUY", "SELL"])
        config["telegram"].setdefault("confirmation_ttl_seconds", 120)
        config["telegram"].setdefault("poll_interval_seconds", 3)
        config["telegram"].setdefault("polling_timeout_seconds", 20)
        config["backtest"].setdefault("symbol", config.get("mt5", {}).get("symbol", "XAUUSD"))
        config["backtest"].setdefault("timeframe", "M1")
        config["backtest"].setdefault("start", None)
        config["backtest"].setdefault("end", None)
        config["backtest"].setdefault(
            "selected_strategies",
            ["XAU_BOT_TREND_PU", "XAU_BOT_LIQUIDIT", "XAU_BOT_COMPRESS", "XAU_BOT_BREAKOUT", "XAU_LEBPRIM"],
        )
        config["backtest"].setdefault("initial_balance", 10000.0)
        config["backtest"].setdefault("risk_percent", float(config.get("risk", {}).get("risk_percent", 0.5)))
        config["backtest"].setdefault("spread_model", {"type": "fixed_points", "points": float(config.get("execution", {}).get("max_spread_points", 25.0))})
        config["backtest"].setdefault("slippage_model", {"type": "fixed_points", "points": 2.0})
        config["backtest"].setdefault("execution_model", "next_bar_open")
        config["backtest"].setdefault("fill_model", config["backtest"].get("execution_model", "next_bar_open"))
        config["backtest"].setdefault("session_filter", {"enabled": False, "allowed_sessions": []})
        config["backtest"].setdefault("same_bar_sl_tp_rule", "sl_first")
        config["backtest"].setdefault("warmup_bars", 300)
        config["backtest"].setdefault("notes", "")
        config["backtest"]["selected_strategies"] = list(resolved_mode.enabled_strategy_names)
        config["mode"] = resolved_mode.to_dict()
        config["resolved_runtime"] = self.resolved_runtime_snapshot(config)
        if "password" in config["dashboard"]:
            config["dashboard"]["password"] = None
        return config

    def resolved_runtime_snapshot(self, config: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return the authoritative resolved runtime policy snapshot."""
        cfg = copy.deepcopy(config or self.load_runtime())
        cfg, resolved_mode = apply_execution_mode_to_config(cfg)
        strategy_cfg = cfg.get("strategy", {}) if isinstance(cfg.get("strategy"), dict) else {}
        execution_cfg = cfg.get("execution", {}) if isinstance(cfg.get("execution"), dict) else {}
        risk_cfg = cfg.get("risk", {}) if isinstance(cfg.get("risk"), dict) else {}
        exit_cfg = cfg.get("exit", {}) if isinstance(cfg.get("exit"), dict) else {}
        sessions_cfg = cfg.get("sessions", {}) if isinstance(cfg.get("sessions"), dict) else {}
        setup_controls = strategy_cfg.get("setup_controls", {}) if isinstance(strategy_cfg.get("setup_controls"), dict) else {}
        setup_families = strategy_cfg.get("setup_families", {}) if isinstance(strategy_cfg.get("setup_families"), dict) else {}
        enabled_families = sorted([name for name, payload in setup_families.items() if isinstance(payload, dict) and bool(payload.get("enabled", True))])
        disabled_families = sorted([name for name, payload in setup_families.items() if isinstance(payload, dict) and not bool(payload.get("enabled", True))])
        enabled_strategies = sorted([name for name, payload in setup_controls.items() if isinstance(payload, dict) and bool(payload.get("enabled", True))])
        disabled_strategies = sorted([name for name, payload in setup_controls.items() if isinstance(payload, dict) and not bool(payload.get("enabled", True))])
        skipped_reasons = {
            name: "disabled_in_setup_controls"
            for name, payload in setup_controls.items()
            if isinstance(payload, dict) and not bool(payload.get("enabled", True))
        }
        skipped_reasons.update(
            {
                name: "disabled_in_setup_families"
                for name, payload in setup_families.items()
                if isinstance(payload, dict) and not bool(payload.get("enabled", True))
            }
        )
        return {
            "mode": str(cfg.get("bot", {}).get("trading_mode", "DRY_RUN")),
            "allow_live_execution": bool(cfg.get("bot", {}).get("allow_live_execution", False)),
            "dry_run": bool(cfg.get("bot", {}).get("dry_run", False)),
            "database_path": str(cfg.get("storage", {}).get("database_path", "")),
            "execution_mode": resolved_mode.to_dict(),
            "enabled_strategies": enabled_strategies,
            "disabled_strategies": disabled_strategies,
            "enabled_families": enabled_families,
            "disabled_families": disabled_families,
            "skipped_entities": skipped_reasons,
            "active_risk_profile": {
                "risk_percent": float(risk_cfg.get("risk_percent", 0.0) or 0.0),
                "short_risk_multiplier": float(risk_cfg.get("short_risk_multiplier", 1.0) or 1.0),
                "quality_buckets": dict(risk_cfg.get("quality_buckets", {})) if isinstance(risk_cfg.get("quality_buckets"), dict) else {},
            },
            "active_execution_policy": {
                "adaptive_execution": bool(execution_cfg.get("enable_adaptive_execution", True)),
                "contextual_entry_mode": bool(execution_cfg.get("enable_contextual_entry_mode", True)),
                "aggressive_entry_allowed": bool(execution_cfg.get("aggressive_entry_allowed", True)),
                "pending_policy": dict(execution_cfg.get("pending_policy", {})) if isinstance(execution_cfg.get("pending_policy"), dict) else {},
            },
            "active_session_filters": {
                "timezone": str(sessions_cfg.get("timezone", "UTC")),
                "live_allowed_buckets": list(sessions_cfg.get("live_allowed_buckets", [])),
            },
            "family_management_activation": bool(exit_cfg.get("enable_family_specific_management", True)),
            "adaptive_execution_activation": bool(execution_cfg.get("enable_adaptive_execution", True)),
        }

    def source_of_truth_snapshot(
        self,
        runtime_state: dict[str, Any] | None = None,
        process_runtime: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return authoritative/raw values plus source precedence for key operator fields."""
        root_config = self.load_raw()
        shadow_config = self.load_shadow()
        effective_runtime = self.resolved_runtime_snapshot()
        runtime_payload = dict(runtime_state or {})
        runtime_section = runtime_payload.get("runtime", {}) if isinstance(runtime_payload.get("runtime"), dict) else {}
        process_payload = dict(process_runtime or {})

        root_mode = str(root_config.get("bot", {}).get("trading_mode", "") or "").strip().upper()
        shadow_mode = str(shadow_config.get("bot", {}).get("trading_mode", "") or "").strip().upper()
        control_mode = str(runtime_section.get("mode", "") or "").strip().upper()
        process_mode = str(process_payload.get("mode", "") or "").strip().upper()

        root_db_path = str(root_config.get("storage", {}).get("database_path", "") or "")
        shadow_db_path = str(shadow_config.get("storage", {}).get("database_path", "") or "")
        process_db_path = str(process_payload.get("database_path", "") or "")

        control_execution_enabled = runtime_payload.get("auto_execution_enabled")
        root_execution_enabled = bool(root_config.get("bot", {}).get("allow_live_execution", False))
        shadow_execution_enabled = bool(shadow_config.get("bot", {}).get("allow_live_execution", False)) if shadow_config else None

        def _resolve(value_order: list[tuple[str, Any]]) -> tuple[Any, str]:
            for source_name, source_value in value_order:
                if source_value not in (None, ""):
                    return source_value, source_name
            return None, "unknown"

        mode_value, mode_source = _resolve([
            ("process_runtime", process_mode),
            ("persisted_control_state", control_mode),
            ("root_config", root_mode),
            ("storage_config", shadow_mode),
        ])
        db_value, db_source = _resolve([
            ("process_runtime", process_db_path),
            ("root_config", root_db_path),
            ("storage_config", shadow_db_path),
        ])
        execution_enabled_value, execution_enabled_source = _resolve([
            ("persisted_control_state", control_execution_enabled),
            ("root_config", root_execution_enabled),
            ("storage_config", shadow_execution_enabled),
        ])
        drift = {
            "detected": bool(shadow_config) and (
                root_mode != shadow_mode
                or root_db_path != shadow_db_path
                or root_execution_enabled != shadow_execution_enabled
            ),
            "mode_mismatch": bool(shadow_config) and root_mode != shadow_mode,
            "database_path_mismatch": bool(shadow_config) and root_db_path != shadow_db_path,
            "execution_enabled_mismatch": bool(shadow_config) and root_execution_enabled != shadow_execution_enabled,
        }
        return {
            "precedence": ["process_runtime", "persisted_control_state", "root_config", "storage_config"],
            "effective": {
                "mode": mode_value,
                "database_path": db_value,
                "execution_enabled": bool(execution_enabled_value) if execution_enabled_value is not None else None,
                "resolved_runtime": effective_runtime,
            },
            "sources": {
                "mode": mode_source,
                "database_path": db_source,
                "execution_enabled": execution_enabled_source,
            },
            "raw": {
                "root_config": {
                    "mode": root_mode,
                    "database_path": root_db_path,
                    "execution_enabled": root_execution_enabled,
                },
                "storage_config": {
                    "mode": shadow_mode,
                    "database_path": shadow_db_path,
                    "execution_enabled": shadow_execution_enabled,
                },
                "persisted_control_state": {
                    "mode": control_mode,
                    "execution_enabled": control_execution_enabled,
                    "bot_pid": runtime_payload.get("bot_pid"),
                },
                "process_runtime": process_payload,
            },
            "drift": drift,
        }

    def _normalize(self, config: dict[str, Any]) -> None:
        """Normalize critical compatibility values before save."""
        bot_cfg = config.setdefault("bot", {})
        trading_mode = normalize_trading_mode(bot_cfg.get("trading_mode", "DRY_RUN"))
        bot_cfg["trading_mode"] = trading_mode
        bot_cfg["demo_mode"] = trading_mode == "DEMO"
        bot_cfg["dry_run"] = trading_mode == "DRY_RUN"
        bot_cfg["test_mode"] = trading_mode == "VALIDATION_TEST"
        bot_cfg["backtest_mode"] = trading_mode == "BACKTEST"
        bot_cfg["allow_live_execution"] = trading_mode == "LIVE"
        config.setdefault("strategy", {}).setdefault("setup_controls", {})
        config.setdefault("symbols", {})
        config.setdefault("telegram", {})
        config.setdefault("sessions", {})
        config.setdefault("execution", {})
        config.setdefault("risk", {})
        config.setdefault("dashboard", {})
        apply_execution_mode_to_config(config)
        config.setdefault("bot", {}).pop("execution_mode_summary", None)
        self._validate(config)

    def _validate(self, config: dict[str, Any]) -> None:
        """Validate dashboard-editable config ranges before persistence."""
        strategy_cfg = config.setdefault("strategy", {})
        execution_cfg = config.setdefault("execution", {})
        validation_cfg = config.setdefault("validation", {})
        shorts_cfg = config.setdefault("shorts", {})
        risk_cfg = config.setdefault("risk", {})
        mt5_cfg = config.setdefault("mt5", {})
        sessions_cfg = config.setdefault("sessions", {})
        telegram_cfg = config.setdefault("telegram", {})

        self._assert_between(float(strategy_cfg.get("min_score_to_trade", 0.0)), 0.0, 100.0, "strategy.min_score_to_trade")
        for group_name in ["live_thresholds", "dry_run_thresholds"]:
            group = strategy_cfg.get(group_name, {})
            if isinstance(group, dict):
                for key, value in group.items():
                    self._assert_between(float(value), 0.0, 100.0, f"strategy.{group_name}.{key}")

        for name, setup in strategy_cfg.get("setup_controls", {}).items():
            if not isinstance(setup, dict):
                raise ValueError(f"strategy.setup_controls.{name} must be an object")
            for score_key in ["min_entry_score", "min_trend_score", "min_setup_score", "min_trigger_score"]:
                if setup.get(score_key) not in (None, ""):
                    self._assert_between(float(setup[score_key]), 0.0, 100.0, f"strategy.setup_controls.{name}.{score_key}")
            if setup.get("max_spread_points") not in (None, ""):
                self._assert_positive(float(setup["max_spread_points"]), f"strategy.setup_controls.{name}.max_spread_points")
            if setup.get("cooldown_minutes") not in (None, ""):
                self._assert_non_negative(float(setup["cooldown_minutes"]), f"strategy.setup_controls.{name}.cooldown_minutes")

        self._assert_positive(float(execution_cfg.get("max_spread_points", 0.0)), "execution.max_spread_points")
        spread_cfg = execution_cfg.get("spread", {}) if isinstance(execution_cfg.get("spread", {}), dict) else {}
        max_points_cfg = spread_cfg.get("max_points", {}) if isinstance(spread_cfg.get("max_points", {}), dict) else {}
        if "default" in max_points_cfg:
            self._assert_positive(float(max_points_cfg.get("default", 0.0)), "execution.spread.max_points.default")
        if spread_cfg.get("retry_window_seconds") not in (None, ""):
            self._assert_non_negative(float(spread_cfg.get("retry_window_seconds")), "execution.spread.retry_window_seconds")
        if spread_cfg.get("recheck_interval_ms") not in (None, ""):
            self._assert_non_negative(float(spread_cfg.get("recheck_interval_ms")), "execution.spread.recheck_interval_ms")
        if execution_cfg.get("manual_trade_comment_tag_dashboard") in (None, ""):
            raise ValueError("execution.manual_trade_comment_tag_dashboard is required")
        if execution_cfg.get("manual_trade_comment_tag_telegram") in (None, ""):
            raise ValueError("execution.manual_trade_comment_tag_telegram is required")
        for key in ["market_entry_max_drift_atr", "aggressive_limit_max_drift_atr", "limit_entry_max_drift_atr", "limit_trigger_max_atr"]:
            if execution_cfg.get(key) not in (None, ""):
                self._assert_non_negative(float(execution_cfg.get(key)), f"execution.{key}")
        for key in [
            "max_stop_points_long",
            "max_stop_points_short",
            "max_stop_atr_long",
            "max_stop_atr_short",
            "max_trigger_range_atr",
            "max_trigger_range_atr_short",
            "max_trigger_body_atr",
            "max_trigger_body_atr_short",
            "max_entry_drift_points",
            "max_entry_drift_points_short",
            "max_entry_drift_atr",
            "max_entry_drift_atr_short",
        ]:
            if validation_cfg.get(key) not in (None, ""):
                self._assert_positive(float(validation_cfg.get(key)), f"validation.{key}")
        for key in ["min_bearish_score", "min_bias_delta", "min_bearish_slope_atr", "min_structure_score", "min_reward_headroom_atr", "max_extension_from_origin_atr"]:
            if shorts_cfg.get(key) not in (None, ""):
                self._assert_non_negative(float(shorts_cfg.get(key)), f"shorts.{key}")
        self._assert_non_negative(float(mt5_cfg.get("deviation", 0.0)), "mt5.deviation")
        self._assert_between(float(risk_cfg.get("risk_percent", 0.0)), 0.0, 5.0, "risk.risk_percent")
        self._assert_positive(float(risk_cfg.get("max_lot", 0.0)), "risk.max_lot")
        self._assert_positive(float(risk_cfg.get("min_lot", 0.0)), "risk.min_lot")
        if float(risk_cfg.get("min_lot", 0.0)) > float(risk_cfg.get("max_lot", 0.0)):
            raise ValueError("risk.min_lot cannot exceed risk.max_lot")
        if risk_cfg.get("short_risk_multiplier") not in (None, ""):
            self._assert_between(float(risk_cfg.get("short_risk_multiplier", 0.0)), 0.0, 1.5, "risk.short_risk_multiplier")
        self._assert_non_negative(float(risk_cfg.get("max_daily_drawdown_pct", 0.0)), "risk.max_daily_drawdown_pct")
        self._assert_non_negative(float(risk_cfg.get("max_trades_per_day", 0.0)), "risk.max_trades_per_day")
        self._assert_non_negative(float(risk_cfg.get("max_trades_per_symbol", 0.0)), "risk.max_trades_per_symbol")
        self._assert_non_negative(float(risk_cfg.get("min_margin_level", 0.0)), "risk.min_margin_level")

        buckets = sessions_cfg.get("buckets", [])
        if not isinstance(buckets, list):
            raise ValueError("sessions.buckets must be a list")
        for index, bucket in enumerate(buckets):
            if not isinstance(bucket, dict):
                raise ValueError(f"sessions.buckets[{index}] must be an object")
            for field in ["start", "end"]:
                value = str(bucket.get(field, ""))
                if len(value.split(":")) != 2:
                    raise ValueError(f"sessions.buckets[{index}].{field} must be HH:MM")
        for key in ["admin_chat_ids", "admin_user_ids", "confirmation_required_commands"]:
            if key in telegram_cfg and not isinstance(telegram_cfg.get(key), list):
                raise ValueError(f"telegram.{key} must be a list")
        self._assert_positive(float(telegram_cfg.get("confirmation_ttl_seconds", 120)), "telegram.confirmation_ttl_seconds")
        self._assert_positive(float(telegram_cfg.get("poll_interval_seconds", 3)), "telegram.poll_interval_seconds")
        self._assert_positive(float(telegram_cfg.get("polling_timeout_seconds", 20)), "telegram.polling_timeout_seconds")

    def _requires_restart(self, path: str) -> bool:
        if path in self.RESTART_REQUIRED_PATHS:
            return True
        return not any(path == prefix.rstrip(".") or path.startswith(prefix) for prefix in self.HOT_RELOADABLE_PREFIXES)

    @staticmethod
    def _assert_positive(value: float, path: str) -> None:
        if value <= 0:
            raise ValueError(f"{path} must be greater than 0")

    @staticmethod
    def _assert_non_negative(value: float, path: str) -> None:
        if value < 0:
            raise ValueError(f"{path} must be 0 or greater")

    @staticmethod
    def _assert_between(value: float, minimum: float, maximum: float, path: str) -> None:
        if value < minimum or value > maximum:
            raise ValueError(f"{path} must be between {minimum} and {maximum}")

    def _deep_merge(self, target: dict[str, Any], updates: dict[str, Any]) -> None:
        """Merge nested dicts recursively."""
        for key, value in updates.items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                self._deep_merge(target[key], value)
            else:
                target[key] = value

    def _walk_diff(self, path: str, before: Any, after: Any, changes: list[dict[str, Any]]) -> None:
        """Flatten diff entries recursively."""
        if isinstance(before, dict) and isinstance(after, dict):
            keys = sorted(set(before.keys()) | set(after.keys()))
            for key in keys:
                next_path = f"{path}.{key}" if path else key
                self._walk_diff(next_path, before.get(key), after.get(key), changes)
            return
        if before != after:
            changes.append({"path": path, "before": before, "after": after})
