from __future__ import annotations

from abc import ABC, abstractmethod
from copy import deepcopy
from pathlib import Path
from typing import Any

from strategy import StrategyEngine
from trading_bot.core.execution_mode import (
    V1_BASELINE,
    apply_execution_mode_to_config,
    normalize_execution_mode,
)
from trading_bot.strategy.engine import UnifiedStrategyEngine


LEGACY_ENGINE_TYPE = "legacy_strategy_engine"
UNIFIED_ENGINE_TYPE = "unified_strategy_engine"
LIVE_SUPPORT_ENGINE_TYPE = "live_runtime_support_engine"
IGNORED_LEGACY_LIVE_CONTROL_KEYS = (
    "bot.execution_mode",
    "bot.rollout_phase",
    "strategy.lebprim_mode",
    "strategy.setup_controls.<legacy_strategy>.enabled",
    "strategy.setup_controls.<legacy_strategy>.live_allowed",
    "strategy.setup_families.<legacy_strategy>.enabled",
    "strategy.setup_families.<legacy_strategy>.live_allowed",
    "backtest.selected_strategies",
)


class StrategyEngineBase(ABC):
    """Base interface for all strategy engines with typed engine_type attribute."""
    
    engine_type: str
    
    @abstractmethod
    def generate_setup_candidates(self, *args: Any, **kwargs: Any) -> Any:
        """Generate setup candidates."""
        pass


def build_strategy_engine(config: dict[str, Any], base_dir: str | Path | None = None) -> StrategyEngineBase:
    """Build the mode-appropriate strategy engine.

    V1 must stay on the original legacy StrategyEngine path. V2 modes use the
    unified registry/adapter engine, where candidate-quality validation lives.
    
    Returns a typed StrategyEngineBase with engine_type properly set as an attribute.
    Raises ValueError if engine construction fails.
    """
    mode = _read_execution_mode(config)
    resolved_config, resolved_mode = apply_execution_mode_to_config(config, mode)
    mode = resolved_mode.selected_mode

    if mode == V1_BASELINE:
        _apply_engine_type_summary(resolved_config, LEGACY_ENGINE_TYPE)
        engine = StrategyEngine(resolved_config)
        engine.engine_type = LEGACY_ENGINE_TYPE  # Set as instance attribute instead of setattr
        return engine

    _apply_engine_type_summary(resolved_config, UNIFIED_ENGINE_TYPE)
    engine = UnifiedStrategyEngine(resolved_config, base_dir=base_dir)
    engine.engine_type = UNIFIED_ENGINE_TYPE  # Set as instance attribute instead of setattr
    return engine


def build_live_runtime_support_engine(config: dict[str, Any], base_dir: str | Path | None = None) -> StrategyEngineBase:
    """Build a non-trading compatibility engine for the LebPrime live runtime.

    The main loop still uses session classification supplied by the unified engine,
    but all legacy setup families are disabled in this private config copy. Backtest
    and historical callers continue to use ``build_strategy_engine`` unchanged.
    """
    support_config = deepcopy(config)
    strategy_config = support_config.setdefault("strategy", {})
    for section_name in ("setup_controls", "setup_families"):
        section = strategy_config.setdefault(section_name, {})
        for setup in section.values():
            if isinstance(setup, dict):
                setup["enabled"] = False
                setup["live_allowed"] = False
    engine = UnifiedStrategyEngine(support_config, base_dir=base_dir)
    engine.engine_type = LIVE_SUPPORT_ENGINE_TYPE
    return engine


def _read_execution_mode(config: dict[str, Any]) -> str:
    bot_cfg = config.get("bot", {}) if isinstance(config.get("bot"), dict) else {}
    raw_mode = bot_cfg.get("execution_mode")
    if raw_mode not in (None, ""):
        return normalize_execution_mode(raw_mode)
    if _looks_like_legacy_config(config):
        return V1_BASELINE
    return normalize_execution_mode(raw_mode)


def _looks_like_legacy_config(config: dict[str, Any]) -> bool:
    """Detect older configs that predate explicit execution modes."""
    if not isinstance(config, dict):
        return False
    if isinstance(config.get("strategies"), dict):
        return False
    strategy_cfg = config.get("strategy", {}) if isinstance(config.get("strategy"), dict) else {}
    return "setup_controls" not in strategy_cfg


def _apply_engine_type_summary(config: dict[str, Any], engine_type: str) -> None:
    summary = config.setdefault("bot", {}).setdefault("execution_mode_summary", {})
    if isinstance(summary, dict):
        summary["engine_type"] = engine_type
