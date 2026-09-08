from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from trading_bot.core.execution_mode import apply_execution_mode_to_config


REQUIRED_SECTIONS = {
    "bot",
    "mt5",
    "symbols",
    "strategy",
    "regime",
    "execution",
    "risk",
    "sessions",
    "dashboard",
    "backtest",
}


@dataclass(slots=True)
class ConfigValidationResult:
    valid: bool
    missing_sections: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class ConfigSchema:
    """Single schema boundary for all runtime modes.

    The app still works with dictionaries today. This class normalizes the
    shared contract so LIVE, DEMO, DRY_RUN, and BACKTEST all see the same
    top-level sections while legacy callers continue to read existing keys.
    """

    @staticmethod
    def normalize(config: dict[str, Any]) -> dict[str, Any]:
        cfg = config
        cfg.setdefault("bot", {})
        cfg.setdefault("runtime", {})
        cfg.setdefault("broker", {})
        cfg.setdefault("integrations", {})
        cfg.setdefault("symbols", {})
        cfg.setdefault("strategy", {})
        cfg.setdefault("strategies", cfg["strategy"].setdefault("setup_controls", {}))
        cfg.setdefault("execution", {})
        cfg.setdefault("validation", {})
        cfg.setdefault("shorts", {})
        cfg.setdefault("risk", {})
        cfg.setdefault("exit", {})
        cfg.setdefault("sessions", {})
        cfg.setdefault("regime", {})
        cfg.setdefault("dashboard", {})
        cfg.setdefault("backtest", {})
        cfg.setdefault("mt5", {})
        cfg["runtime"]["mode"] = cfg.get("bot", {}).get("trading_mode", cfg["runtime"].get("mode", "DRY_RUN"))
        cfg["broker"]["mt5"] = cfg.get("mt5", {})
        cfg["integrations"]["telegram"] = cfg.get("telegram", {})
        cfg["strategy"].setdefault("lebprim_mode", "restricted")
        cfg["execution"].setdefault("enable_adaptive_execution", True)
        cfg["execution"].setdefault("allow_multi_position", bool(cfg["execution"].get("allow_pyramiding", False)))
        cfg["execution"].setdefault("max_concurrent_positions", 2)
        cfg["execution"].setdefault("max_positions_per_family", 1)
        cfg["execution"].setdefault("max_total_open_risk_pct", 1.25)
        cfg["execution"].setdefault("max_directional_risk_pct", 1.0)
        cfg["execution"].setdefault("allow_same_direction_multi_strategy", False)
        cfg["execution"].setdefault("allow_same_family_stack", False)
        cfg["execution"].setdefault("enable_contextual_entry_mode", True)
        cfg["execution"].setdefault("aggressive_entry_allowed", True)
        cfg["execution"].setdefault("market_entry_max_drift_atr", 0.22)
        cfg["execution"].setdefault("aggressive_limit_min_quality_score", 66.0)
        cfg["execution"].setdefault("aggressive_limit_max_drift_atr", 0.55)
        cfg["execution"].setdefault("limit_entry_max_drift_atr", 1.10)
        cfg["execution"].setdefault("limit_trigger_max_atr", 1.55)
        cfg["execution"].setdefault("quality_tiers", {"high_min_score": 64.0, "medium_min_score": 54.0})
        cfg["execution"].setdefault("pending_policy", {"default_expiry_minutes": 5, "default_expiry_bars": 3})
        cfg["execution"].setdefault("backtest_fill_model", cfg.get("backtest", {}).get("execution_model", "next_bar_open"))
        cfg["execution"].setdefault(
            "setup_fingerprint_guard",
            {
                "enabled": True,
                "mode": "one_execution_per_fingerprint",
                "cooldown_minutes_after_close": 30,
                "apply_to_families": ["BREAKOUT_RETEST_CONTINUATION", "COMPRESSION_RELEASE"],
            },
        )
        cfg["validation"].setdefault("enable_stop_width_filter", True)
        cfg["validation"].setdefault("max_stop_points_long", 650.0)
        cfg["validation"].setdefault("max_stop_points_short", 520.0)
        cfg["validation"].setdefault("max_stop_atr_long", 1.80)
        cfg["validation"].setdefault("max_stop_atr_short", 1.45)
        cfg["validation"].setdefault("max_trigger_range_atr", 1.15)
        cfg["validation"].setdefault("max_trigger_range_atr_short", 0.95)
        cfg["validation"].setdefault("max_trigger_body_atr", 0.85)
        cfg["validation"].setdefault("max_trigger_body_atr_short", 0.72)
        cfg["validation"].setdefault("max_trigger_adverse_wick_ratio", 1.15)
        cfg["validation"].setdefault("max_entry_drift_points", 180.0)
        cfg["validation"].setdefault("max_entry_drift_points_short", 140.0)
        cfg["validation"].setdefault("max_entry_drift_atr", 0.85)
        cfg["validation"].setdefault("max_entry_drift_atr_short", 0.65)
        cfg["validation"].setdefault("quality_aplus_min_score", 82.0)
        cfg["validation"].setdefault("quality_a_min_score", 70.0)
        cfg["validation"].setdefault("quality_b_min_score", 58.0)
        cfg["validation"].setdefault("log_candidate_quality_metrics", True)
        cfg["shorts"].setdefault("require_strong_bearish_bias", True)
        cfg["shorts"].setdefault("min_bearish_score", 64.0)
        cfg["shorts"].setdefault("min_bias_delta", 24.0)
        cfg["shorts"].setdefault("min_bearish_slope_atr", 0.10)
        cfg["shorts"].setdefault("min_structure_score", 58.0)
        cfg["shorts"].setdefault("min_reward_headroom_atr", 0.90)
        cfg["shorts"].setdefault("max_extension_from_origin_atr", 1.45)
        cfg["risk"].setdefault("short_risk_multiplier", 0.80)
        cfg["risk"].setdefault(
            "quality_buckets",
            {
                "aplus_min_score": 82.0,
                "aplus_risk_multiplier": 1.0,
                "a_min_score": 70.0,
                "a_risk_multiplier": 0.9,
                "b_min_score": 58.0,
                "b_risk_multiplier": 0.65,
                "c_risk_multiplier": 0.0,
            },
        )
        cfg["risk"].setdefault("family_risk_multipliers", {})
        cfg["risk"].setdefault(
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
        cfg["risk"].setdefault(
            "weekend_protection",
            {
                "enabled": True,
                "friday_hard_close_time_utc": "21:00",
                "block_new_entries_after_utc": "20:30",
                "cancel_pending_orders": True,
                "close_open_positions": True,
            },
        )
        cfg["exit"].setdefault("enable_family_specific_management", True)
        cfg["exit"].setdefault("partial_min_rr", 0.95)
        cfg["exit"].setdefault("breakeven_min_bars", 2)
        cfg["exit"].setdefault("time_stop_early_minutes", 12.0)
        cfg["exit"].setdefault("time_stop_mid_minutes", 24.0)
        cfg["exit"].setdefault("time_stop_early_min_rr", 0.10)
        cfg["exit"].setdefault("time_stop_mid_min_rr", 0.25)
        cfg["exit"].setdefault("momentum_failure_min_bars", 3)
        cfg["exit"].setdefault("momentum_failure_min_progress_rr", 0.12)
        cfg["exit"].setdefault("momentum_failure_max_adverse_close_fraction", 0.67)
        cfg["exit"].setdefault("family_profiles", {})
        cfg["exit"].setdefault(
            "structure_break_exit",
            {
                "enabled": bool(cfg["exit"].get("structure_break_exit_enabled", True)),
                "min_bars_after_entry": 0,
                "require_consecutive_closes": 1,
                "atr_buffer_multiplier": 0.0,
                "spread_buffer_points": 0.0,
            },
        )
        cfg["backtest"].setdefault("fill_model", cfg["backtest"].get("execution_model", "next_bar_open"))
        cfg, _ = apply_execution_mode_to_config(cfg)
        return cfg

    @staticmethod
    def validate(config: dict[str, Any]) -> ConfigValidationResult:
        normalized = ConfigSchema.normalize(config)
        missing = [section for section in sorted(REQUIRED_SECTIONS) if section not in normalized]
        warnings: list[str] = []
        if normalized.get("bot", {}).get("trading_mode") != normalized.get("runtime", {}).get("mode"):
            warnings.append("runtime.mode differs from bot.trading_mode")
        return ConfigValidationResult(valid=not missing, missing_sections=missing, warnings=warnings)
