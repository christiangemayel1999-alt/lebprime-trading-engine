"""Utility helpers for the MT5 multi-timeframe scalping bot."""

from __future__ import annotations

import json
import math
import os
import shutil
import sys
import tempfile
import time as time_module
from datetime import date, datetime, time, timedelta
from pathlib import Path
from copy import deepcopy
from typing import Any

import pandas as pd
import pytz
from dotenv import load_dotenv


UTC = pytz.UTC


def utc_now() -> datetime:
    """Return the current UTC datetime as a timezone-aware value."""
    return datetime.now(tz=UTC)


def to_utc(value: datetime | str | None) -> datetime | None:
    """Convert a datetime or ISO string to a timezone-aware UTC datetime."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        parsed = value
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_hhmm(value: str) -> time:
    """Parse an HH:MM string into a time object."""
    hours, minutes = value.split(":")
    return time(hour=int(hours), minute=int(minutes), tzinfo=UTC)


def combine_utc(day: date, hhmm: str) -> datetime:
    """Combine a UTC date with an HH:MM string into a UTC datetime."""
    parsed_time = parse_hhmm(hhmm)
    return datetime.combine(day, parsed_time).astimezone(UTC)


def isoformat_or_none(value: datetime | None) -> str | None:
    """Serialize a datetime to ISO format when present."""
    return value.astimezone(UTC).isoformat() if value else None


def ensure_directory(path: str | Path) -> Path:
    """Create a directory if needed and return its Path."""
    resolved = Path(path)
    resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def mt5_runtime_config_presence(config: dict[str, Any]) -> dict[str, bool]:
    """Return a non-secret summary of MT5 runtime configuration presence."""
    mt5_config = dict(config.get("mt5", {}) or {})
    login_present = mt5_config.get("login") not in (None, "", 0)
    password_present = bool(mt5_config.get("password"))
    server_present = bool(mt5_config.get("server"))
    terminal_path_present = bool(mt5_config.get("terminal_path"))
    symbol_present = bool(mt5_config.get("symbol"))
    return {
        "login_present": bool(login_present),
        "password_present": bool(password_present),
        "server_present": bool(server_present),
        "terminal_path_present": bool(terminal_path_present),
        "symbol_present": bool(symbol_present),
        "credentials_present": bool(login_present and password_present and server_present),
    }


def resolve_python_executable(base_dir: str | Path, configured_path: str | Path | None = None) -> tuple[str, str]:
    """Resolve the safest Python executable for this project."""
    root = Path(base_dir)
    candidates: list[tuple[Path, str]] = []

    active_venv = os.environ.get("VIRTUAL_ENV")
    if active_venv:
        candidates.append((Path(active_venv) / "Scripts" / "python.exe", f"active virtual environment: {active_venv}"))

    current_executable = Path(sys.executable)
    if sys.prefix != sys.base_prefix:
        candidates.append((current_executable, "currently active virtual environment interpreter"))

    candidates.extend(
        [
            (root / ".venv" / "Scripts" / "python.exe", "repo-local .venv"),
            (root / "venv" / "Scripts" / "python.exe", "repo-local venv"),
        ]
    )

    explicit_path = configured_path or os.environ.get("PYTHON_PATH") or os.environ.get("PYTHON_EXE") or os.environ.get("BOT_PYTHON_PATH")
    if explicit_path:
        candidates.append((Path(explicit_path), "explicit configured python path"))

    for found in ("py", "python"):
        resolved = shutil.which(found)
        if resolved:
            candidates.append((Path(resolved), f"{found} on PATH"))

    candidates.append((current_executable, "current interpreter"))

    seen: set[str] = set()
    for candidate, reason in candidates:
        key = str(candidate).lower()
        if key in seen:
            continue
        seen.add(key)
        if candidate.exists():
            return str(candidate), reason

    raise FileNotFoundError(
        "No usable Python interpreter found. Checked active venv, repo-local .venv/venv, configured path, and PATH fallbacks."
    )


def load_json(path: str | Path, default: Any) -> Any:
    """Load JSON content from disk and fall back to a default object."""
    file_path = Path(path)
    if not file_path.exists():
        return default
    try:
        with file_path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (json.JSONDecodeError, OSError):
        return default


def save_json_atomic(path: str | Path, payload: Any, retries: int = 5, retry_delay: float = 0.2) -> None:
    """Persist JSON content atomically to avoid state corruption."""
    file_path = Path(path)
    ensure_directory(file_path.parent)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        delete=False,
        dir=str(file_path.parent),
        suffix=".tmp",
    ) as handle:
        json.dump(payload, handle, indent=4, sort_keys=False)
        handle.flush()
        os.fsync(handle.fileno())
        temp_name = handle.name

    last_error: OSError | None = None
    try:
        for attempt in range(retries):
            try:
                os.replace(temp_name, file_path)
                return
            except OSError as exc:
                last_error = exc
                if attempt == retries - 1:
                    raise
                # OneDrive and antivirus scanners can briefly lock the target file on Windows.
                time_module.sleep(retry_delay * (attempt + 1))
    finally:
        temp_path = Path(temp_name)
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)

    if last_error is not None:
        raise last_error


def parse_bool(value: str | None, default: bool = False) -> bool:
    """Parse a boolean environment variable."""
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def normalize_trading_mode(value: str | None, default: str = "DRY_RUN") -> str:
    """Normalize a runtime trading mode into one of the supported enum values."""
    supported = {"DRY_RUN", "LIVE", "VALIDATION_TEST", "DEMO", "BACKTEST"}
    aliases = {
        "PAPER": "DRY_RUN",
        "PAPER_TRADING": "DRY_RUN",
        "TEST": "VALIDATION_TEST",
        "VALIDATION": "VALIDATION_TEST",
        "SIM": "DEMO",
        "SIMULATION": "DEMO",
        "BT": "BACKTEST",
        "REPLAY": "BACKTEST",
    }
    raw = (value or default or "DRY_RUN").strip().upper()
    normalized = aliases.get(raw, raw)
    return normalized if normalized in supported else default


def _env_int(name: str, default: int | None = None) -> int | None:
    """Read an integer environment variable safely."""
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


def _env_str(name: str, default: str | None = None) -> str | None:
    """Read a string environment variable and normalize blanks."""
    value = os.getenv(name, default)
    if value is None:
        return None
    stripped = value.strip()
    return stripped if stripped else None


def _env_csv(name: str, default: list[str] | None = None) -> list[str]:
    """Read a comma-separated environment variable into a list."""
    value = _env_str(name)
    if value is None:
        return list(default or [])
    return [item.strip() for item in value.split(",") if item.strip()]


def _deep_merge_dicts(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge override values into a base dictionary."""
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge_dicts(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _base_config_defaults() -> dict[str, Any]:
    """Return cautious, production-safe runtime defaults."""
    return {
        "mt5": {
            "symbol": "XAUUSD",
            "terminal_path": None,
            "magic_number": 234567,
            "deviation": 10,
            "timeout": 5,
            "reconnect_retries": 3,
            "reconnect_retry_delay_seconds": 5,
            "tick_retries": 5,
            "tick_retry_delay_seconds": 0.4,
            "price_refresh_delay_seconds": 0.15,
            "max_tick_age_seconds": 5,
            "reconnect_cooldown_seconds": 8,
            "order_check_enabled": True,
            "auto_adjust_stops": True,
            "symbol_prepare_each_trade": True,
            "diagnostics_order_check": True,
            "order_send_transient_retries": 2,
        },
        "timeframes": {
            "trend": "15min",
            "setup": "3min",
            "trigger": "1min",
            "bars_to_fetch": 320,
        },
        "indicators": {
            "ema_fast": 20,
            "ema_slow": 50,
            "ema_trend": 200,
            "rsi_period": 14,
            "atr_period": 14,
            "adx_period": 14,
            "volume_period": 20,
            "regime_efficiency_lookback": 8,
            "regime_overlap_lookback": 6,
        },
        "bot": {
            "loop_sleep_seconds": 15,
            "trading_mode": "DRY_RUN",
            "demo_mode": False,
            "dry_run": True,
            "test_mode": False,
            "backtest_mode": False,
            "allow_live_execution": False,
            "close_positions_on_shutdown": False,
            "auto_recover_live_positions": False,
            "auto_restart_on_error": True,
            "rollout_phase": 1,
            "force_reduced_risk_mode": True,
            "manual_reset_id": 0,
            "enable_startup_trade_reconciliation": True,
            "analytics_auto_generate": True,
            "analytics_report_hour_utc": 22,
        },
        "strategy": {
            "setup_expiry_bars": 4,
            "setup_replace_score_delta": 8.0,
            "bias_alignment_required": True,
            "require_trend_alignment": True,
            "min_score_to_trade": 75.0,
            "value_zone_weights": {
                "ema20": 1,
                "ema50": 1,
                "breakout_level": 1,
                "impulse_origin": 1,
                "micro_zone": 1,
            },
            "live_thresholds": {
                "live_min_trend_score": 68.0,
                "live_min_setup_score": 72.0,
                "live_min_trigger_score": 70.0,
                "live_min_entry_score": 75.0,
            },
            "dry_run_thresholds": {
                "dry_run_min_trend_score": 56.0,
                "dry_run_min_setup_score": 58.0,
                "dry_run_min_trigger_score": 56.0,
                "dry_run_min_entry_score": 60.0,
            },
            "entry_modes": {
                "aggressive_enabled": True,
                "aggressive_min_entry_score": 88.0,
                "confirmed_min_entry_score": 78.0,
                "fallback_enabled": False,
                "fallback_min_entry_score": 70.0,
            },
            "setup_families": {
                "trend_pullback_reclaim": {
                    "enabled": True,
                    "live_allowed": True,
                    "sessions": ["LONDON_OPEN", "LONDON", "NY_OPEN", "LONDON_NY_OVERLAP"],
                    "regimes": ["TREND_CONTINUATION"],
                },
                "breakout_retest_continuation": {
                    "enabled": True,
                    "live_allowed": True,
                    "sessions": ["LONDON_OPEN", "LONDON", "NY_OPEN", "LONDON_NY_OVERLAP"],
                    "regimes": ["TREND_CONTINUATION"],
                },
                "liquidity_sweep_reversal": {
                    "enabled": True,
                    "live_allowed": False,
                    "sessions": ["LONDON_OPEN", "NY_OPEN", "LONDON_NY_OVERLAP"],
                    "regimes": ["RANGE_CHOP"],
                },
                "compression_release": {
                    "enabled": True,
                    "live_allowed": True,
                    "sessions": ["LONDON", "NY_OPEN", "LONDON_NY_OVERLAP"],
                    "regimes": ["TREND_CONTINUATION"],
                },
            },
        },
        "regime": {
            "ema_slope_min_atr": 0.08,
            "ema_alignment_min_atr": 0.10,
            "trend_efficiency_min": 0.42,
            "chop_efficiency_max": 0.28,
            "overlap_chop_min": 0.58,
            "compression_ratio_max": 0.78,
            "high_volatility_atr_ratio": 1.85,
            "low_volatility_atr_ratio": 0.70,
            "distance_from_value_extreme_atr": 1.40,
            "spread_instability_ratio": 1.10,
            "allow_range_regime": False,
        },
        "sessions": {
            "timezone": "UTC",
            "session_close": "21:40",
            "allow_asia_session": False,
            "buckets": [
                {"name": "ASIA", "start": "00:00", "end": "06:59", "live_allowed": False},
                {"name": "LONDON_OPEN", "start": "07:00", "end": "08:59", "live_allowed": True},
                {"name": "LONDON", "start": "09:00", "end": "12:29", "live_allowed": True},
                {"name": "NY_OPEN", "start": "12:30", "end": "14:29", "live_allowed": True},
                {"name": "LONDON_NY_OVERLAP", "start": "14:30", "end": "16:30", "live_allowed": True},
                {"name": "LATE_NY", "start": "16:31", "end": "21:39", "live_allowed": False},
            ],
            "live_allowed_buckets": ["LONDON_OPEN", "LONDON", "NY_OPEN", "LONDON_NY_OVERLAP"],
        },
        "entry": {
            "setup_entry_distance_atr_tolerance": 0.45,
            "max_trigger_candle_size_atr": 1.10,
            "value_zone_min_confluence_count": 2,
            "breakout_retest_distance_atr_tolerance": 0.35,
            "impulse_extension_limit_atr": 1.25,
            "micro_structure_lookback_bars": 5,
            "retest_lookback_bars": 4,
            "reclaim_body_ratio_min": 0.45,
            "rejection_wick_ratio_min": 0.35,
            "breakout_buffer_atr": 0.12,
            "max_setup_age_minutes": 15,
        },
        "backtest": {
            "symbol": "XAUUSD",
            "timeframe": "M1",
            "start": None,
            "end": None,
            "selected_strategies": [
                "XAU_BOT_TREND_PU",
                "XAU_BOT_LIQUIDIT",
                "XAU_BOT_COMPRESS",
                "XAU_BOT_BREAKOUT",
            ],
            "initial_balance": 10000.0,
            "risk_percent": 0.5,
            "spread_model": {"type": "fixed_points", "points": 20.0},
            "slippage_model": {"type": "fixed_points", "points": 2.0},
            "execution_model": "next_bar_open",
            "session_filter": {"enabled": False, "allowed_sessions": []},
            "same_bar_sl_tp_rule": "sl_first",
            "warmup_bars": 300,
            "notes": "",
            "history_csv_fallback_dirs": [],
        },
        "execution": {
            "max_spread_points": 25.0,
            "allow_high_volatility_reduced_risk": False,
            "allow_pyramiding": False,
            "manual_trading_enabled": True,
            "manual_trade_comment_tag_dashboard": "MANUAL_DASH",
            "manual_trade_comment_tag_telegram": "MANUAL_TG",
            "min_seconds_between_duplicate_attempts": 45,
            "hard_rejection_cooldown_seconds": 300,
            "post_send_verify_seconds": 2.0,
            "post_send_verify_retries": 4,
            "duplicate_history_minutes": 180,
            "spread": {
                "max_points": {"default": 25.0},
                "max_points_by_setup": {},
                "max_points_by_regime": {},
                "retry_window_seconds": 8,
                "recheck_interval_ms": 500,
                "enable_dynamic_limit": False,
            },
        },
        "risk": {
            "risk_percent": 0.35,
            "min_balance": 500.0,
            "live_risk_multiplier": 0.50,
            "reduced_risk_multiplier_after_drawdown": 0.50,
            "reduced_risk_trigger_drawdown_pct": 0.75,
            "live_max_lot": 1.0,
            "max_lot": 2.0,
            "min_lot": 0.01,
            "min_practical_volume": 0.01,
            "max_trades_per_day": 4,
            "max_daily_drawdown_pct": 1.50,
            "max_session_drawdown_pct": 1.00,
            "max_weekly_loss_percent": 8.0,
            "max_consecutive_losses": 2,
            "pause_after_losses_hours": 6,
            "min_margin_level": 300,
            "margin_protection_enabled": True,
            "min_free_margin_buffer": 0.0,
            "min_fitted_volume_ratio": 0.0,
        },
        "cooldowns": {
            "execution_cooldown_minutes": 20,
            "execution_cooldown_candles": 1,
            "post_loss_cooldown_minutes": 45,
            "consecutive_loss_cooldown_minutes": 90,
        },
        "exit": {
            "sl_buffer_atr": 0.35,
            "sl_buffer_pips": 5,
            "sl_min_pips": 30,
            "sl_max_pips": 90,
            "partial_tp_enabled": True,
            "partial_tp_rr": 1.0,
            "partial_close_fraction": 0.50,
            "breakeven_after_tp1": True,
            "breakeven_activation_rr": 1.0,
            "breakeven_buffer_pips": 4,
            "trailing_enabled": True,
            "trailing_mode": "structure_atr",
            "trailing_activation_rr": 1.80,
            "trailing_atr_multiplier": 1.00,
            "min_trailing_stop_pips": 18,
            "max_trade_duration_minutes": 45,
            "time_stop_min_progress_rr": 0.30,
            "momentum_failure_exit_enabled": True,
            "momentum_failure_bars": 3,
            "structure_break_exit_enabled": True,
            "trailing_structure_lookback_bars": 3,
            "trade_history_resolve_lookback_minutes": 1440,
        },
        "journaling": {
            "unresolved_trade_retry_interval_seconds": 120,
            "unresolved_trade_max_retries": 60,
            "trade_history_resolve_lookback_minutes": 1440,
        },
        "safety": {
            "max_infra_failures_before_degraded": 3,
            "degraded_mode_cooldown_minutes": 10,
            "disable_live_on_journal_failure": True,
            "max_journal_failures_before_disable": 2,
            "require_manual_reset_after_daily_lock": True,
        },
        "blackout_times": [
            {"start": "07:55", "end": "08:10", "reason": "London Open Whipsaw"},
            {"start": "12:55", "end": "13:10", "reason": "NY Open Whipsaw"},
        ],
        "telegram": {
            "timeout_seconds": 5,
            "signal_alerts": True,
            "execution_success_alerts": True,
            "execution_blocked_alerts": True,
            "error_alerts": True,
            "daily_summary_alerts": True,
            "dashboard_change_alerts": True,
            "remote_control_enabled": False,
            "manual_trade_commands_enabled": False,
            "admin_chat_ids": [],
            "admin_user_ids": [],
            "confirmation_required_commands": ["LIVE", "KILLSWITCH_OFF", "CLOSE", "CLOSEALL", "BUY", "SELL"],
            "confirmation_ttl_seconds": 120,
            "poll_interval_seconds": 3,
            "polling_timeout_seconds": 20,
        },
        "dashboard": {
            "default_port": 8501,
            "host": "0.0.0.0",
            "refresh_seconds": 5,
            "stale_heartbeat_seconds": 90,
            "readonly_mode": False,
            "theme": "dark",
        },
        "storage": {
            "database_path": "storage/bot.db",
            "state_path": "storage/state.json",
            "state_save_retry_count": 3,
            "state_save_retry_delay_seconds": 0.35,
        },
        "journaling": {
            "csv_retry_count": 3,
            "csv_retry_delay_seconds": 0.25,
            "setup_registry_retention_hours": 72,
            "signal_registry_max_entries": 1500,
            "analytics_summary_hour_utc": 21,
        },
        "validation": {
            "enabled": True,
            "daily_report_enabled": True,
            "session_report_enabled": True,
            "journal_consistency_enabled": True,
            "disable_live_on_validation_failure": True,
            "max_validation_failures_before_disable": 1,
            "report_top_block_reasons_limit": 5,
            "diagnostics_include_csv_consistency": True,
        },
        "logging": {
            "log_trades": True,
            "log_signals": True,
            "log_errors": True,
            "console_output": True,
            "daily_summary": True,
            "execution_verbosity": "detailed",
            "max_bytes": 2_000_000,
            "backup_count": 5,
        },
        "filters": {
            "max_spread_points": 25.0,
            "min_atr_3min": 3.0,
            "max_atr_3min": 12.0,
            "min_atr_15min": 1.5,
            "signal_cooldown_minutes": 10,
        },
    }


def _migrate_legacy_config(config: dict[str, Any]) -> dict[str, Any]:
    """Promote older flat keys into the new layered runtime configuration."""
    migrated = deepcopy(config)
    strategy_cfg = migrated.setdefault("strategy", {})
    risk_cfg = migrated.setdefault("risk", {})
    exit_cfg = migrated.setdefault("exit", {})
    bot_cfg = migrated.setdefault("bot", {})
    filters_cfg = migrated.setdefault("filters", {})
    execution_cfg = migrated.setdefault("execution", {})
    cooldowns_cfg = migrated.setdefault("cooldowns", {})
    safety_cfg = migrated.setdefault("safety", {})
    sessions_cfg = migrated.setdefault("sessions", {})
    regime_cfg = migrated.setdefault("regime", {})

    live_thresholds = strategy_cfg.setdefault("live_thresholds", {})
    dry_run_thresholds = strategy_cfg.setdefault("dry_run_thresholds", {})
    live_thresholds.setdefault("live_min_trend_score", strategy_cfg.get("trend_score_min", 68.0))
    live_thresholds.setdefault("live_min_setup_score", strategy_cfg.get("setup_score_min", 72.0))
    live_thresholds.setdefault("live_min_trigger_score", strategy_cfg.get("trigger_score_min", 70.0))
    live_thresholds.setdefault("live_min_entry_score", strategy_cfg.get("entry_score_min", 75.0))
    dry_run_thresholds.setdefault("dry_run_min_trend_score", strategy_cfg.get("trend_score_min", 56.0))
    dry_run_thresholds.setdefault("dry_run_min_setup_score", strategy_cfg.get("setup_score_min", 58.0))
    dry_run_thresholds.setdefault("dry_run_min_trigger_score", strategy_cfg.get("trigger_score_min", 56.0))
    dry_run_thresholds.setdefault("dry_run_min_entry_score", strategy_cfg.get("entry_score_min", 60.0))
    strategy_cfg.setdefault("require_trend_alignment", strategy_cfg.get("bias_alignment_required", True))
    strategy_cfg.setdefault("min_score_to_trade", live_thresholds.get("live_min_entry_score", 75.0))

    execution_cfg.setdefault("max_spread_points", filters_cfg.get("max_spread_points", 25.0))
    execution_cfg.setdefault("hard_rejection_cooldown_seconds", 300)
    spread_cfg = execution_cfg.setdefault("spread", {})
    spread_cfg.setdefault("max_points", {"default": float(execution_cfg.get("max_spread_points", 25.0))})
    spread_cfg.setdefault("max_points_by_setup", {})
    spread_cfg.setdefault("max_points_by_regime", {})
    spread_cfg.setdefault("retry_window_seconds", 8)
    spread_cfg.setdefault("recheck_interval_ms", 500)
    spread_cfg.setdefault("enable_dynamic_limit", False)
    cooldowns_cfg.setdefault("execution_cooldown_minutes", risk_cfg.get("cooldown_after_trade_minutes", 20))
    cooldowns_cfg.setdefault("post_loss_cooldown_minutes", risk_cfg.get("cooldown_after_loss_minutes", 45))
    cooldowns_cfg.setdefault(
        "consecutive_loss_cooldown_minutes",
        int(risk_cfg.get("pause_after_losses_hours", 2)) * 60,
    )
    risk_cfg.setdefault("max_daily_drawdown_pct", risk_cfg.get("max_daily_loss_percent", 1.5))
    risk_cfg.setdefault("live_max_lot", risk_cfg.get("max_lot", 1.0))
    risk_cfg.setdefault("min_practical_volume", risk_cfg.get("min_lot", 0.01))
    risk_cfg.setdefault("min_fitted_volume_ratio", 0.0)

    exit_cfg.setdefault("partial_tp_rr", exit_cfg.get("tp1_r_multiple", 1.0))
    exit_cfg.setdefault("partial_close_fraction", float(exit_cfg.get("tp1_close_percent", 50)) / 100.0)
    exit_cfg.setdefault("trailing_activation_rr", exit_cfg.get("trailing_start_r", 1.2))
    exit_cfg.setdefault("trailing_atr_multiplier", exit_cfg.get("trailing_distance_r", 0.8))
    exit_cfg.setdefault("breakeven_activation_rr", exit_cfg.get("breakeven_r_multiple", 1.0))
    exit_cfg.setdefault("partial_tp_enabled", True)
    exit_cfg.setdefault("breakeven_after_tp1", True)
    exit_cfg.setdefault("trailing_enabled", True)
    exit_cfg.setdefault("max_trade_duration_minutes", 45)
    exit_cfg.setdefault("momentum_failure_exit_enabled", True)
    exit_cfg.setdefault("structure_break_exit_enabled", True)

    safety_cfg.setdefault(
        "max_infra_failures_before_degraded",
        bot_cfg.get("max_infra_failures_before_degraded", 3),
    )
    safety_cfg.setdefault(
        "degraded_mode_cooldown_minutes",
        bot_cfg.get("degraded_mode_cooldown_minutes", 10),
    )
    safety_cfg.setdefault("disable_live_on_journal_failure", True)
    safety_cfg.setdefault("require_manual_reset_after_daily_lock", True)
    regime_cfg.setdefault("allow_range_regime", False)

    if "buckets" not in sessions_cfg:
        london_start = sessions_cfg.get("london_start", "07:00")
        london_end = sessions_cfg.get("london_end", "16:30")
        ny_start = sessions_cfg.get("newyork_start", "12:30")
        ny_end = sessions_cfg.get("newyork_end", "21:30")
        sessions_cfg["buckets"] = [
            {"name": "ASIA", "start": "00:00", "end": "06:59", "live_allowed": False},
            {"name": "LONDON_OPEN", "start": london_start, "end": "08:59", "live_allowed": True},
            {"name": "LONDON", "start": "09:00", "end": "12:29", "live_allowed": True},
            {"name": "NY_OPEN", "start": ny_start, "end": "14:29", "live_allowed": True},
            {"name": "LONDON_NY_OVERLAP", "start": "14:30", "end": london_end, "live_allowed": True},
            {"name": "LATE_NY", "start": "16:31", "end": ny_end, "live_allowed": False},
        ]
    sessions_cfg.setdefault("live_allowed_buckets", ["LONDON_OPEN", "LONDON", "NY_OPEN", "LONDON_NY_OVERLAP"])
    sessions_cfg.setdefault("timezone", "UTC")
    sessions_cfg.setdefault("session_close", sessions_cfg.get("session_close", "21:40"))
    sessions_cfg.setdefault("allow_asia_session", False)

    raw_mode = bot_cfg.get("trading_mode")
    if raw_mode is None:
        if bool(bot_cfg.get("demo_mode", False)):
            raw_mode = "DEMO"
        elif bool(bot_cfg.get("test_mode", False)):
            raw_mode = "VALIDATION_TEST"
        elif bool(bot_cfg.get("dry_run", False)) or not bool(bot_cfg.get("allow_live_execution", False)):
            raw_mode = "DRY_RUN"
        else:
            raw_mode = "LIVE"
    bot_cfg["trading_mode"] = normalize_trading_mode(raw_mode)
    bot_cfg["demo_mode"] = bot_cfg["trading_mode"] == "DEMO"
    bot_cfg["dry_run"] = bot_cfg["trading_mode"] == "DRY_RUN"
    bot_cfg["test_mode"] = bot_cfg["trading_mode"] == "VALIDATION_TEST"
    bot_cfg["backtest_mode"] = bot_cfg["trading_mode"] == "BACKTEST"
    bot_cfg["allow_live_execution"] = bot_cfg["trading_mode"] == "LIVE"
    bot_cfg.setdefault("auto_recover_live_positions", False)
    bot_cfg.setdefault("rollout_phase", 1 if bot_cfg["trading_mode"] in {"DRY_RUN", "DEMO"} else 2)
    bot_cfg.setdefault("force_reduced_risk_mode", int(bot_cfg.get("rollout_phase", 1)) < 3)
    bot_cfg.setdefault("manual_reset_id", 0)

    return migrated


def load_runtime_config(
    base_dir: str | Path,
    require_mt5_credentials: bool = True,
    *,
    apply_mode_env_override: bool = True,
) -> dict[str, Any]:
    """Load config.json and merge runtime environment variables."""
    root = Path(base_dir)
    env_path = root / ".env"
    load_dotenv(env_path if env_path.exists() else None)

    raw_config = load_json(root / "config.json", {})
    if not raw_config:
        raise FileNotFoundError(f"Missing or invalid config.json at {root / 'config.json'}")
    config = _deep_merge_dicts(_base_config_defaults(), _migrate_legacy_config(raw_config))

    mt5_config = config.setdefault("mt5", {})
    risk_config = config.setdefault("risk", {})
    bot_config = config.setdefault("bot", {})
    logging_config = config.setdefault("logging", {})
    telegram_config = config.setdefault("telegram", {})
    dashboard_config = config.setdefault("dashboard", {})
    storage_config = config.setdefault("storage", {})
    safety_config = config.setdefault("safety", {})
    execution_config = config.setdefault("execution", {})
    cooldowns_config = config.setdefault("cooldowns", {})
    sessions_config = config.setdefault("sessions", {})
    journaling_config = config.setdefault("journaling", {})
    validation_config = config.setdefault("validation", {})
    exit_config = config.setdefault("exit", {})
    strategy_config = config.setdefault("strategy", {})
    regime_config = config.setdefault("regime", {})

    login_value = _env_str("MT5_LOGIN")
    password_value = _env_str("MT5_PASSWORD")
    server_value = _env_str("MT5_SERVER")
    terminal_path_value = _env_str("MT5_PATH")
    trading_mode_value = (_env_str("TRADING_MODE") or _env_str("BOT_TRADING_MODE")) if apply_mode_env_override else None

    if require_mt5_credentials:
        missing = [
            name
            for name, value in {
                "MT5_LOGIN": login_value,
                "MT5_PASSWORD": password_value,
                "MT5_SERVER": server_value,
            }.items()
            if value is None
        ]
        if missing:
            joined = ", ".join(missing)
            raise RuntimeError(
                f"Missing required environment variables: {joined}. "
                "Copy .env.example to .env and populate the MT5 credentials."
            )

    mt5_config["login"] = int(login_value) if login_value is not None else None
    mt5_config["password"] = password_value
    mt5_config["server"] = server_value
    mt5_config["terminal_path"] = terminal_path_value or mt5_config.get("terminal_path")
    mt5_config.setdefault("deviation", 10)
    mt5_config.setdefault("timeout", 5)
    mt5_config.setdefault("reconnect_retries", 3)
    mt5_config.setdefault("reconnect_retry_delay_seconds", 5)
    mt5_config.setdefault("tick_retries", 5)
    mt5_config.setdefault("tick_retry_delay_seconds", 0.4)
    mt5_config.setdefault("price_refresh_delay_seconds", 0.15)
    mt5_config.setdefault("max_tick_age_seconds", 5)
    mt5_config.setdefault("reconnect_cooldown_seconds", 8)
    mt5_config.setdefault("order_check_enabled", True)
    mt5_config.setdefault("auto_adjust_stops", True)
    mt5_config.setdefault("symbol_prepare_each_trade", True)
    mt5_config.setdefault("diagnostics_order_check", True)
    mt5_config.setdefault("order_send_transient_retries", 2)

    configured_telegram_enabled = bool(telegram_config.get("enabled", True))
    telegram_config["bot_token"] = _env_str("TELEGRAM_BOT_TOKEN")
    telegram_config["chat_id"] = _env_str("TELEGRAM_CHAT_ID")
    telegram_config["enabled"] = configured_telegram_enabled and bool(telegram_config["bot_token"] and telegram_config["chat_id"])
    telegram_config.setdefault("timeout_seconds", 5)
    telegram_config.setdefault("dashboard_change_alerts", True)
    telegram_config["remote_control_enabled"] = parse_bool(
        _env_str("TELEGRAM_REMOTE_CONTROL_ENABLED"),
        bool(telegram_config.get("remote_control_enabled", False)),
    )
    telegram_config["manual_trade_commands_enabled"] = parse_bool(
        _env_str("TELEGRAM_MANUAL_TRADE_COMMANDS_ENABLED"),
        bool(telegram_config.get("manual_trade_commands_enabled", False)),
    )
    telegram_config["admin_chat_ids"] = _env_csv("TELEGRAM_ADMIN_CHAT_IDS", list(telegram_config.get("admin_chat_ids", [])))
    telegram_config["admin_user_ids"] = _env_csv("TELEGRAM_ADMIN_USER_IDS", list(telegram_config.get("admin_user_ids", [])))
    telegram_config.setdefault("confirmation_required_commands", ["LIVE", "KILLSWITCH_OFF", "CLOSE", "CLOSEALL", "BUY", "SELL"])
    telegram_config["confirmation_ttl_seconds"] = _env_int(
        "TELEGRAM_CONFIRMATION_TTL_SECONDS",
        int(telegram_config.get("confirmation_ttl_seconds", 120)),
    )
    telegram_config["poll_interval_seconds"] = _env_int(
        "TELEGRAM_POLL_INTERVAL_SECONDS",
        int(telegram_config.get("poll_interval_seconds", 3)),
    )
    telegram_config["polling_timeout_seconds"] = _env_int(
        "TELEGRAM_POLLING_TIMEOUT_SECONDS",
        int(telegram_config.get("polling_timeout_seconds", 20)),
    )

    risk_config.setdefault("risk_percent", 0.5)
    risk_config.setdefault("min_lot", 0.01)
    risk_config.setdefault("max_lot", 2.0)
    risk_config.setdefault("min_margin_level", 300)
    risk_config.setdefault("margin_protection_enabled", True)
    risk_config.setdefault("min_free_margin_buffer", 0.0)
    risk_config.setdefault("min_fitted_volume_ratio", 0.0)

    bot_config["trading_mode"] = normalize_trading_mode(trading_mode_value or bot_config.get("trading_mode"))
    bot_config["demo_mode"] = bot_config["trading_mode"] == "DEMO"
    bot_config["dry_run"] = bot_config["trading_mode"] == "DRY_RUN"
    bot_config["test_mode"] = bot_config["trading_mode"] == "VALIDATION_TEST"
    bot_config["backtest_mode"] = bot_config["trading_mode"] == "BACKTEST"
    bot_config["allow_live_execution"] = bot_config["trading_mode"] == "LIVE"
    bot_config.setdefault("auto_recover_live_positions", False)
    bot_config.setdefault("auto_restart_on_error", True)
    bot_config.setdefault("rollout_phase", 1 if bot_config["trading_mode"] in {"DRY_RUN", "DEMO"} else 2)
    bot_config.setdefault("force_reduced_risk_mode", int(bot_config["rollout_phase"]) < 3)
    bot_config.setdefault("manual_reset_id", 0)

    execution_config.setdefault("max_spread_points", 25.0)
    execution_config.setdefault("manual_trading_enabled", True)
    execution_config.setdefault("manual_trade_comment_tag_dashboard", "MANUAL_DASH")
    execution_config.setdefault("manual_trade_comment_tag_telegram", "MANUAL_TG")
    execution_config.setdefault("hard_rejection_cooldown_seconds", 300)
    spread_config = execution_config.setdefault("spread", {})
    spread_config.setdefault("max_points", {"default": float(execution_config.get("max_spread_points", 25.0))})
    spread_config.setdefault("max_points_by_setup", {})
    spread_config.setdefault("max_points_by_regime", {})
    spread_config.setdefault("retry_window_seconds", 8)
    spread_config.setdefault("recheck_interval_ms", 500)
    spread_config.setdefault("enable_dynamic_limit", False)
    cooldowns_config.setdefault("execution_cooldown_minutes", 20)
    cooldowns_config.setdefault("execution_cooldown_candles", 1)
    cooldowns_config.setdefault("post_loss_cooldown_minutes", 45)
    cooldowns_config.setdefault("consecutive_loss_cooldown_minutes", 90)

    safety_config.setdefault("max_infra_failures_before_degraded", 3)
    safety_config.setdefault("degraded_mode_cooldown_minutes", 10)
    safety_config.setdefault("disable_live_on_journal_failure", True)
    safety_config.setdefault("max_journal_failures_before_disable", 2)
    safety_config.setdefault("require_manual_reset_after_daily_lock", True)

    logging_config.setdefault("execution_verbosity", "detailed")
    logging_config.setdefault("console_output", True)

    dashboard_config.setdefault("default_port", 8501)
    dashboard_config["port"] = _env_int("DASHBOARD_PORT", int(dashboard_config["default_port"]))
    dashboard_config.setdefault("host", "0.0.0.0")
    dashboard_config.setdefault("refresh_seconds", 5)
    dashboard_config.setdefault("stale_heartbeat_seconds", 90)
    dashboard_config.setdefault("readonly_mode", False)
    dashboard_config.setdefault("theme", "dark")
    dashboard_config["username"] = _env_str("DASHBOARD_USERNAME", dashboard_config.get("username"))
    dashboard_config["password"] = _env_str("DASHBOARD_PASSWORD", dashboard_config.get("password"))

    storage_config.setdefault("database_path", "storage/bot.db")
    storage_config.setdefault("state_path", "storage/state.json")
    storage_config.setdefault("state_save_retry_count", 3)
    storage_config.setdefault("state_save_retry_delay_seconds", 0.35)
    storage_config.setdefault("history_cache_path", "storage/history_cache.db")
    storage_config.setdefault("history_csv_fallback_dirs", ["backtests", "storage", "data", "market_data"])

    sessions_config.setdefault("timezone", "UTC")
    sessions_config.setdefault("session_close", "21:40")
    sessions_config.setdefault("allow_asia_session", False)
    sessions_config.setdefault("live_allowed_buckets", ["LONDON_OPEN", "LONDON", "NY_OPEN", "LONDON_NY_OVERLAP"])
    regime_config.setdefault("allow_range_regime", False)
    backtest_config = config.setdefault("backtest", {})
    backtest_config.setdefault("symbol", mt5_config.get("symbol", "XAUUSD"))
    backtest_config.setdefault("timeframe", "M1")
    backtest_config.setdefault("start", None)
    backtest_config.setdefault("end", None)
    backtest_config.setdefault(
        "selected_strategies",
        ["XAU_BOT_TREND_PU", "XAU_BOT_LIQUIDIT", "XAU_BOT_COMPRESS", "XAU_BOT_BREAKOUT"],
    )
    backtest_config.setdefault("initial_balance", 10000.0)
    backtest_config.setdefault("risk_percent", float(risk_config.get("risk_percent", 0.5)))
    backtest_config.setdefault("spread_model", {"type": "fixed_points", "points": float(execution_config.get("max_spread_points", 25.0))})
    backtest_config.setdefault("slippage_model", {"type": "fixed_points", "points": 2.0})
    backtest_config.setdefault("execution_model", "next_bar_open")
    backtest_config.setdefault("fill_model", backtest_config.get("execution_model", "next_bar_open"))
    execution_config.setdefault("backtest_fill_model", backtest_config.get("execution_model", "next_bar_open"))
    backtest_config.setdefault("session_filter", {"enabled": False, "allowed_sessions": []})
    backtest_config.setdefault("same_bar_sl_tp_rule", "sl_first")
    backtest_config.setdefault("warmup_bars", 300)
    backtest_config.setdefault("notes", "")
    backtest_config.setdefault("history_csv_fallback_dirs", list(storage_config.get("history_csv_fallback_dirs", [])))
    journaling_config.setdefault("csv_retry_count", 3)
    journaling_config.setdefault("csv_retry_delay_seconds", 0.25)
    journaling_config.setdefault("setup_registry_retention_hours", 72)
    journaling_config.setdefault("signal_registry_max_entries", 1500)
    journaling_config.setdefault("unresolved_trade_retry_interval_seconds", 120)
    journaling_config.setdefault("unresolved_trade_max_retries", 60)
    journaling_config.setdefault("trade_history_resolve_lookback_minutes", 1440)
    validation_config.setdefault("enabled", True)
    validation_config.setdefault("daily_report_enabled", True)
    validation_config.setdefault("session_report_enabled", True)
    validation_config.setdefault("journal_consistency_enabled", True)
    validation_config.setdefault("disable_live_on_validation_failure", True)
    validation_config.setdefault("max_validation_failures_before_disable", 1)
    validation_config.setdefault("report_top_block_reasons_limit", 5)
    validation_config.setdefault("diagnostics_include_csv_consistency", True)

    entry_modes = strategy_config.setdefault("entry_modes", {})
    entry_modes.setdefault("aggressive_enabled", True)
    entry_modes.setdefault("aggressive_min_entry_score", 88.0)
    entry_modes.setdefault("confirmed_min_entry_score", 78.0)
    entry_modes.setdefault("fallback_enabled", False)
    entry_modes.setdefault("fallback_min_entry_score", 70.0)
    strategy_config.setdefault("require_trend_alignment", bool(strategy_config.get("bias_alignment_required", True)))
    strategy_config.setdefault(
        "min_score_to_trade",
        float(strategy_config.get("live_thresholds", {}).get("live_min_entry_score", 75.0)),
    )

    exit_config.setdefault("partial_tp_enabled", True)
    exit_config.setdefault("partial_tp_rr", 1.0)
    exit_config.setdefault("partial_close_fraction", 0.5)
    exit_config.setdefault("breakeven_after_tp1", True)
    exit_config.setdefault("breakeven_activation_rr", exit_config.get("partial_tp_rr", 1.0))
    exit_config.setdefault("trailing_enabled", True)
    exit_config.setdefault("trailing_mode", "structure_atr")
    exit_config.setdefault("trailing_activation_rr", 1.8)
    exit_config.setdefault("trailing_atr_multiplier", 1.0)
    exit_config.setdefault("max_trade_duration_minutes", 45)
    exit_config.setdefault("momentum_failure_exit_enabled", True)
    exit_config.setdefault("structure_break_exit_enabled", True)
    exit_config.setdefault(
        "structure_break_exit",
        {
            "enabled": bool(exit_config.get("structure_break_exit_enabled", True)),
            "min_bars_after_entry": 0,
            "require_consecutive_closes": 1,
            "atr_buffer_multiplier": 0.0,
            "spread_buffer_points": 0.0,
        },
    )
    execution_config.setdefault(
        "setup_fingerprint_guard",
        {
            "enabled": True,
            "mode": "one_execution_per_fingerprint",
            "cooldown_minutes_after_close": 30,
            "apply_to_families": ["BREAKOUT_RETEST_CONTINUATION", "COMPRESSION_RELEASE"],
        },
    )
    risk_config.setdefault(
        "backtest_account_protection",
        {
            "enabled": True,
            "stop_when_equity_below": 0.0,
            "max_total_drawdown_percent": None,
            "max_daily_drawdown_percent": None,
            "max_consecutive_losses": None,
            "enforce_margin": True,
        },
    )
    risk_config.setdefault(
        "weekend_protection",
        {
            "enabled": True,
            "friday_hard_close_time_utc": "21:00",
            "block_new_entries_after_utc": "20:30",
            "cancel_pending_orders": True,
            "close_open_positions": True,
        },
    )
    exit_config.setdefault("trade_history_resolve_lookback_minutes", journaling_config.get("trade_history_resolve_lookback_minutes", 1440))
    bot_config.setdefault("enable_startup_trade_reconciliation", True)
    bot_config.setdefault("analytics_auto_generate", True)
    bot_config.setdefault("analytics_report_hour_utc", 22)

    # Keep legacy keys aligned while the rest of the codebase migrates.
    filters_config = config.setdefault("filters", {})
    filters_config["max_spread_points"] = float(execution_config["max_spread_points"])
    risk_config["cooldown_after_trade_minutes"] = int(cooldowns_config["execution_cooldown_minutes"])
    risk_config["cooldown_after_loss_minutes"] = int(cooldowns_config["post_loss_cooldown_minutes"])
    bot_config["max_infra_failures_before_degraded"] = int(safety_config["max_infra_failures_before_degraded"])
    bot_config["degraded_mode_cooldown_minutes"] = int(safety_config["degraded_mode_cooldown_minutes"])

    from trading_bot.config.schema import ConfigSchema

    return ConfigSchema.normalize(config)


def default_state() -> dict[str, Any]:
    """Return the default persistent bot state."""
    return {
        "last_trade_time": None,
        "last_entry_time": None,
        "last_trade_result": None,
        "daily_trade_count": 0,
        "daily_pnl": 0.0,
        "weekly_pnl": 0.0,
        "consecutive_losses": 0,
        "pause_until": None,
        "cooldown_until": None,
        "total_trades": 0,
        "winning_trades": 0,
        "losing_trades": 0,
        "total_pnl": 0.0,
        "start_of_day_balance": None,
        "start_of_week_balance": None,
        "last_daily_reset": None,
        "last_weekly_reset": None,
        "last_signal": {
            "fingerprint": None,
            "timestamp": None,
        },
        "active_signal": None,
        "open_position": None,
        "demo_position": None,
        "daily_summary_date": None,
        "last_heartbeat_time": None,
        "last_heartbeat_status": None,
        "last_error": None,
        "bot_status": "INITIALIZING",
        "signal_lifecycle_status": "idle",
        "execution_status": "idle",
        "execution_reason_code": None,
        "setup_registry": {},
        "signal_registry": {},
        "journal": {
            "failure_count": 0,
            "disabled_live": False,
            "last_error": None,
            "last_failure_at": None,
        },
        "cooldowns_v2": {
            "entry_until": None,
            "post_loss_until": None,
            "consecutive_loss_until": None,
            "reason": None,
        },
        "locks": {
            "daily_loss_lock": {
                "active": False,
                "reason": None,
                "locked_at": None,
            },
            "session_loss_lock": {
                "active": False,
                "reason": None,
                "session_name": None,
                "locked_at": None,
            },
            "consecutive_loss_lock": {
                "active": False,
                "reason": None,
                "locked_at": None,
            },
            "manual_reset_required": False,
            "manual_reset_reason": None,
            "journal_failure_lock": False,
            "kill_switch": False,
            "kill_switch_reason": None,
        },
        "session_stats": {
            "session_name": None,
            "date": None,
            "start_balance": None,
            "pnl": 0.0,
            "trade_count": 0,
            "wins": 0,
            "losses": 0,
        },
        "last_manual_reset_id": 0,
        "validation": {
            "failure_count": 0,
            "last_failure": None,
            "last_failure_at": None,
            "live_disabled": False,
            "last_diagnostics_at": None,
            "last_consistency_check": None,
            "last_validation_report_day": None,
            "last_validation_report_session_key": None,
        },
        "degraded_mode": {
            "active": False,
            "reason_code": None,
            "active_since": None,
            "cooldown_until": None,
            "alert_sent_at": None,
            "failure_count": 0,
            "last_failure_at": None,
            "last_failure_class": None,
        },
    }


def sync_state_for_new_periods(
    state: dict[str, Any],
    now_utc: datetime,
    balance: float,
) -> dict[str, Any]:
    """Reset daily and weekly counters when the UTC date changes."""
    day_key = now_utc.date().isoformat()
    week_key = f"{now_utc.isocalendar().year}-W{now_utc.isocalendar().week:02d}"

    if state.get("last_daily_reset") != day_key:
        state["daily_trade_count"] = 0
        state["daily_pnl"] = 0.0
        state["start_of_day_balance"] = float(balance)
        state["last_daily_reset"] = day_key
        state["daily_summary_date"] = None

    if state.get("last_weekly_reset") != week_key:
        state["weekly_pnl"] = 0.0
        state["start_of_week_balance"] = float(balance)
        state["last_weekly_reset"] = week_key

    pause_until = to_utc(state.get("pause_until"))
    if pause_until and now_utc >= pause_until:
        state["pause_until"] = None
        state["consecutive_losses"] = 0

    cooldown_until = to_utc(state.get("cooldown_until"))
    if cooldown_until and now_utc >= cooldown_until:
        state["cooldown_until"] = None

    active_signal = state.get("active_signal")
    expires_at = to_utc(active_signal.get("expires_at")) if active_signal else None
    if active_signal and expires_at and now_utc >= expires_at:
        state["active_signal"] = None

    return state


def update_trade_statistics(
    state: dict[str, Any],
    pnl: float,
    closed_at: datetime,
) -> dict[str, Any]:
    """Update realized trade statistics inside the persistent state."""
    state["last_trade_time"] = isoformat_or_none(closed_at)
    state["daily_pnl"] = float(state.get("daily_pnl", 0.0)) + float(pnl)
    state["weekly_pnl"] = float(state.get("weekly_pnl", 0.0)) + float(pnl)
    state["total_pnl"] = float(state.get("total_pnl", 0.0)) + float(pnl)
    state["total_trades"] = int(state.get("total_trades", 0)) + 1

    if pnl >= 0:
        state["winning_trades"] = int(state.get("winning_trades", 0)) + 1
        state["consecutive_losses"] = 0
        state["last_trade_result"] = "WIN"
    else:
        state["losing_trades"] = int(state.get("losing_trades", 0)) + 1
        state["consecutive_losses"] = int(state.get("consecutive_losses", 0)) + 1
        state["last_trade_result"] = "LOSS"

    return state


def infer_pip_size(symbol: str, digits: int, point: float) -> float:
    """Infer pip size for forex and precious metals symbols."""
    upper_symbol = symbol.upper()
    if upper_symbol.startswith(("XAU", "XAG")):
        return point * 10
    if digits in (3, 5):
        return point * 10
    return point


def price_to_pips(price_distance: float, pip_size: float) -> float:
    """Convert an absolute price distance to pips."""
    if pip_size <= 0:
        return 0.0
    return abs(price_distance) / pip_size


def pips_to_price(pips: float, pip_size: float) -> float:
    """Convert pips to absolute price distance."""
    return float(pips) * pip_size


def round_volume(
    volume: float,
    min_volume: float,
    max_volume: float,
    step: float,
) -> float:
    """Round a raw lot size to the broker-supported volume step."""
    rounded = floor_volume_to_step(volume, min_volume, max_volume, step)
    if rounded <= 0:
        return 0.0
    return rounded


def volume_precision(step: float) -> int:
    """Infer the decimal precision required by a broker volume step."""
    if step <= 0:
        return 2
    return max(0, len(f"{step:.10f}".rstrip("0").split(".")[-1]))


def floor_volume_to_step(
    volume: float,
    min_volume: float,
    max_volume: float,
    step: float,
) -> float:
    """Round volume down to the nearest valid broker-supported step."""
    if max_volume <= 0 or volume <= 0:
        return 0.0

    precision = volume_precision(step)
    clipped = min(max_volume, float(volume))
    if step <= 0:
        if clipped < min_volume:
            return 0.0
        return round(clipped, precision)

    if clipped + 1e-12 < min_volume:
        return 0.0

    steps = math.floor(((clipped - min_volume) / step) + 1e-9)
    rounded = min_volume + (steps * step)
    if rounded + 1e-12 < min_volume:
        return 0.0
    return round(max(min_volume, min(max_volume, rounded)), precision)


def normalize_price(price: float, digits: int) -> float:
    """Normalize a price value to the broker's supported number of digits."""
    return round(float(price), max(0, int(digits)))


def validate_ohlc_dataframe(df: pd.DataFrame, min_rows: int = 100) -> tuple[bool, str]:
    """Validate candle dataframe integrity before running indicator logic."""
    required_columns = {
        "time",
        "open",
        "high",
        "low",
        "close",
        "tick_volume",
    }
    missing = required_columns.difference(df.columns)
    if missing:
        return False, f"Missing columns: {sorted(missing)}"
    if len(df) < min_rows:
        return False, f"Insufficient rows: {len(df)}"
    if df[["open", "high", "low", "close"]].isna().any().any():
        return False, "NaN values detected in OHLC columns"
    if ((df["high"] < df["low"]) | (df["high"] < df["open"]) | (df["high"] < df["close"])).any():
        return False, "Invalid high values detected"
    if ((df["low"] > df["open"]) | (df["low"] > df["close"]) | (df["low"] > df["high"])).any():
        return False, "Invalid low values detected"
    return True, "OK"


def is_data_recent(df: pd.DataFrame, max_age_seconds: int) -> bool:
    """Check if the most recent candle is recent enough."""
    if df.empty:
        return False
    latest_time = pd.to_datetime(df.iloc[-1]["time"], utc=True)
    return (utc_now() - latest_time.to_pydatetime()).total_seconds() <= max_age_seconds


def next_cycle_sleep_seconds(interval_seconds: int, offset_seconds: int = 2) -> float:
    """Return sleep seconds until the next aligned loop boundary."""
    now_ts = utc_now().timestamp()
    remainder = (now_ts - offset_seconds) % interval_seconds
    sleep_for = interval_seconds - remainder
    if math.isclose(sleep_for, interval_seconds, abs_tol=0.01):
        return 0.0
    return max(0.0, sleep_for)


def serialize_state(state: dict[str, Any]) -> dict[str, Any]:
    """Return a state-safe payload ready for JSON serialization."""
    serialized = dict(state)
    for key, value in serialized.items():
        if isinstance(value, datetime):
            serialized[key] = isoformat_or_none(value)
    return serialized


def parse_timeframe_minutes(label: str) -> int:
    """Convert timeframe labels like 15min to integer minutes."""
    normalized = label.lower().strip()
    if normalized.endswith("min"):
        pass
    elif normalized.endswith("m") and normalized[:-1].isdigit():
        normalized = f"{normalized[:-1]}min"
    mapping = {
        "1min": 1,
        "3min": 3,
        "5min": 5,
        "15min": 15,
        "30min": 30,
        "60min": 60,
        "1h": 60,
    }
    if normalized not in mapping:
        raise ValueError(f"Unsupported timeframe label: {label}")
    return mapping[normalized]


def timeframe_duration(label: str) -> timedelta:
    """Convert a timeframe label into a timedelta."""
    return timedelta(minutes=parse_timeframe_minutes(label))
