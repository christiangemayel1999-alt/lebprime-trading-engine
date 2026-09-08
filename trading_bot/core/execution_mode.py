from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any


V1_BASELINE = "V1_BASELINE"
V2_FULL = "V2_FULL"
V2_NO_LEBPRIM = "V2_NO_LEBPRIM"
V2_ADAPTIVE_ONLY = "V2_ADAPTIVE_ONLY"
V2_FAMILY_MANAGEMENT_ONLY = "V2_FAMILY_MANAGEMENT_ONLY"
CUSTOM = "CUSTOM"

DEFAULT_EXECUTION_MODE = V2_FULL

DASHBOARD_EXECUTION_MODES = [V1_BASELINE, V2_FULL]
ALL_EXECUTION_MODES = {
    V1_BASELINE,
    V2_FULL,
    V2_NO_LEBPRIM,
    V2_ADAPTIVE_ONLY,
    V2_FAMILY_MANAGEMENT_ONLY,
    CUSTOM,
}

MODE_LABELS = {
    V1_BASELINE: "V1 Baseline",
    V2_FULL: "V2 Full",
    V2_NO_LEBPRIM: "V2 No LEBPRIM",
    V2_ADAPTIVE_ONLY: "V2 Adaptive Only",
    V2_FAMILY_MANAGEMENT_ONLY: "V2 Family Management Only",
    CUSTOM: "Custom",
}

FAMILY_KEY_TO_NAME = {
    "breakout_retest_continuation": "BREAKOUT_RETEST_CONTINUATION",
    "compression_release": "COMPRESSION_RELEASE",
    "lebprim_scalp": "LEBPRIM_SCALP",
    "trend_pullback_reclaim": "TREND_PULLBACK_RECLAIM",
    "liquidity_sweep_reversal": "LIQUIDITY_SWEEP_REVERSAL",
}

FAMILY_KEY_TO_STRATEGY = {
    "breakout_retest_continuation": "XAU_BOT_BREAKOUT",
    "compression_release": "XAU_BOT_COMPRESS",
    "lebprim_scalp": "XAU_LEBPRIM",
    "trend_pullback_reclaim": "XAU_BOT_TREND_PU",
    "liquidity_sweep_reversal": "XAU_BOT_LIQUIDIT",
}

MODE_ENABLED_FAMILY_KEYS = {
    V1_BASELINE: ["breakout_retest_continuation", "compression_release"],
    V2_FULL: ["breakout_retest_continuation", "compression_release", "lebprim_scalp"],
    V2_NO_LEBPRIM: ["breakout_retest_continuation", "compression_release"],
    V2_ADAPTIVE_ONLY: ["breakout_retest_continuation", "compression_release", "lebprim_scalp"],
    V2_FAMILY_MANAGEMENT_ONLY: ["breakout_retest_continuation", "compression_release", "lebprim_scalp"],
}

MODE_CONFIG_PATCHES: dict[str, dict[str, Any]] = {
    V1_BASELINE: {
        "execution": {
            "enable_adaptive_execution": False,
            "enable_contextual_entry_mode": False,
            "allow_multi_position": False,
            "allow_same_direction_multi_strategy": False,
            "allow_same_family_stack": False,
            "max_concurrent_positions": 1,
            "max_positions_per_family": 1,
        },
        "exit": {
            "enable_family_specific_management": False,
        },
        "strategy": {
            "lebprim_mode": "off",
        },
    },
    V2_FULL: {
        "execution": {
            "enable_adaptive_execution": True,
            "enable_contextual_entry_mode": True,
            "allow_multi_position": True,
            "allow_same_direction_multi_strategy": True,
            "allow_same_family_stack": False,
            "max_concurrent_positions": 2,
            "max_positions_per_family": 1,
        },
        "exit": {
            "enable_family_specific_management": True,
        },
        "strategy": {
            "lebprim_mode": "restricted",
        },
    },
    V2_NO_LEBPRIM: {
        "execution": {
            "enable_adaptive_execution": True,
            "enable_contextual_entry_mode": True,
            "allow_multi_position": True,
            "allow_same_direction_multi_strategy": True,
            "allow_same_family_stack": False,
            "max_concurrent_positions": 2,
            "max_positions_per_family": 1,
        },
        "exit": {
            "enable_family_specific_management": True,
        },
        "strategy": {
            "lebprim_mode": "off",
        },
    },
    V2_ADAPTIVE_ONLY: {
        "execution": {
            "enable_adaptive_execution": True,
            "enable_contextual_entry_mode": True,
            "allow_multi_position": True,
            "allow_same_direction_multi_strategy": True,
            "allow_same_family_stack": False,
            "max_concurrent_positions": 2,
            "max_positions_per_family": 1,
        },
        "exit": {
            "enable_family_specific_management": False,
        },
        "strategy": {
            "lebprim_mode": "restricted",
        },
    },
    V2_FAMILY_MANAGEMENT_ONLY: {
        "execution": {
            "enable_adaptive_execution": False,
            "enable_contextual_entry_mode": True,
            "allow_multi_position": True,
            "allow_same_direction_multi_strategy": True,
            "allow_same_family_stack": False,
            "max_concurrent_positions": 2,
            "max_positions_per_family": 1,
        },
        "exit": {
            "enable_family_specific_management": True,
        },
        "strategy": {
            "lebprim_mode": "restricted",
        },
    },
}


def _deep_merge(target: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = deepcopy(value)
    return target


def normalize_execution_mode(value: Any, default: str = DEFAULT_EXECUTION_MODE) -> str:
    text = str(value or "").strip().upper()
    return text if text in ALL_EXECUTION_MODES else default


def dashboard_mode_options() -> list[dict[str, str]]:
    return [{"value": value, "label": MODE_LABELS[value]} for value in DASHBOARD_EXECUTION_MODES]


def mode_label(mode: str) -> str:
    normalized = normalize_execution_mode(mode)
    return MODE_LABELS.get(normalized, normalized)


@dataclass(slots=True)
class ResolvedExecutionMode:
    requested_mode: str
    selected_mode: str
    resolved_mode: str
    label: str
    engine_type: str = "unified_strategy_engine"
    adaptive_execution: bool = False
    family_specific_management: bool = False
    lebprim_mode: str = "off"
    lebprim_enabled: bool = False
    multi_position_enabled: bool = False
    enabled_family_keys: list[str] = field(default_factory=list)
    enabled_families: list[str] = field(default_factory=list)
    enabled_strategy_names: list[str] = field(default_factory=list)
    config_patch: dict[str, Any] = field(default_factory=dict)
    corrections: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_mode": self.requested_mode,
            "selected_mode": self.selected_mode,
            "resolved_mode": self.resolved_mode,
            "label": self.label,
            "engine_type": self.engine_type,
            "adaptive_execution": self.adaptive_execution,
            "family_specific_management": self.family_specific_management,
            "lebprim_mode": self.lebprim_mode,
            "lebprim_enabled": self.lebprim_enabled,
            "multi_position_enabled": self.multi_position_enabled,
            "enabled_family_keys": list(self.enabled_family_keys),
            "enabled_families": list(self.enabled_families),
            "enabled_strategy_names": list(self.enabled_strategy_names),
            "corrections": list(self.corrections),
        }


def resolve_execution_mode(
    config: dict[str, Any],
    requested_mode: str | None = None,
) -> ResolvedExecutionMode:
    cfg = config or {}
    bot_cfg = cfg.get("bot", {}) if isinstance(cfg.get("bot", {}), dict) else {}
    selected_mode = normalize_execution_mode(requested_mode or bot_cfg.get("execution_mode"))
    config_patch = deepcopy(MODE_CONFIG_PATCHES.get(selected_mode, {}))
    execution_cfg = cfg.get("execution", {}) if isinstance(cfg.get("execution", {}), dict) else {}
    exit_cfg = cfg.get("exit", {}) if isinstance(cfg.get("exit", {}), dict) else {}
    strategy_cfg = cfg.get("strategy", {}) if isinstance(cfg.get("strategy", {}), dict) else {}

    if selected_mode == CUSTOM:
        enabled_family_keys = [
            key
            for key in FAMILY_KEY_TO_NAME
            if bool(((strategy_cfg.get("setup_families", {}) or {}).get(key, {}) or {}).get("enabled", False))
            or bool(((strategy_cfg.get("setup_controls", {}) or {}).get(key, {}) or {}).get("enabled", False))
        ]
    else:
        enabled_family_keys = list(MODE_ENABLED_FAMILY_KEYS.get(selected_mode, []))

    adaptive_execution = bool(execution_cfg.get("enable_adaptive_execution", config_patch.get("execution", {}).get("enable_adaptive_execution", False)))
    family_specific_management = bool(exit_cfg.get("enable_family_specific_management", config_patch.get("exit", {}).get("enable_family_specific_management", False)))
    lebprim_mode = str(strategy_cfg.get("lebprim_mode", config_patch.get("strategy", {}).get("lebprim_mode", "off")) or "off").lower()
    lebprim_enabled = "lebprim_scalp" in enabled_family_keys and lebprim_mode != "off"
    enabled_families = [FAMILY_KEY_TO_NAME[key] for key in enabled_family_keys if key in FAMILY_KEY_TO_NAME]
    enabled_strategy_names = [FAMILY_KEY_TO_STRATEGY[key] for key in enabled_family_keys if key in FAMILY_KEY_TO_STRATEGY]

    engine_type = "legacy_strategy_engine" if selected_mode == V1_BASELINE else "unified_strategy_engine"

    return ResolvedExecutionMode(
        requested_mode=selected_mode,
        selected_mode=selected_mode,
        resolved_mode=selected_mode,
        label=mode_label(selected_mode),
        engine_type=engine_type,
        adaptive_execution=adaptive_execution,
        family_specific_management=family_specific_management,
        lebprim_mode=lebprim_mode,
        lebprim_enabled=lebprim_enabled,
        multi_position_enabled=bool(execution_cfg.get("allow_multi_position", config_patch.get("execution", {}).get("allow_multi_position", False))),
        enabled_family_keys=enabled_family_keys,
        enabled_families=enabled_families,
        enabled_strategy_names=enabled_strategy_names,
        config_patch=config_patch,
    )


def apply_execution_mode_to_config(
    config: dict[str, Any],
    requested_mode: str | None = None,
) -> tuple[dict[str, Any], ResolvedExecutionMode]:
    cfg = config or {}
    cfg.setdefault("bot", {})
    cfg.setdefault("execution", {})
    cfg.setdefault("exit", {})
    cfg.setdefault("strategy", {})
    strategy_cfg = cfg["strategy"]
    strategy_cfg.setdefault("setup_families", {})
    strategy_cfg.setdefault("setup_controls", {})
    cfg.setdefault("backtest", {})

    resolved = resolve_execution_mode(cfg, requested_mode=requested_mode)
    cfg["bot"]["execution_mode"] = resolved.selected_mode
    if resolved.config_patch:
        _deep_merge(cfg, deepcopy(resolved.config_patch))

    if resolved.selected_mode != CUSTOM:
        enabled_keys = set(resolved.enabled_family_keys)
        for key in FAMILY_KEY_TO_NAME:
            family_cfg = strategy_cfg["setup_families"].setdefault(key, {})
            control_cfg = strategy_cfg["setup_controls"].setdefault(key, {})
            enabled = key in enabled_keys
            family_cfg["enabled"] = enabled
            family_cfg["live_allowed"] = enabled
            control_cfg["enabled"] = enabled
            control_cfg["live_allowed"] = enabled
            if key == "lebprim_scalp" and not enabled:
                family_cfg["live_allowed"] = False
                control_cfg["live_allowed"] = False

        cfg["backtest"]["selected_strategies"] = list(resolved.enabled_strategy_names)

    resolved_after = resolve_execution_mode(cfg, requested_mode=resolved.selected_mode)
    cfg["bot"]["execution_mode_summary"] = resolved_after.to_dict()
    return cfg, resolved_after
