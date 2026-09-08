from __future__ import annotations

from abc import ABC, abstractmethod
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
