from __future__ import annotations

from pathlib import Path
from typing import Any

from trading_bot.config.schema import ConfigSchema, ConfigValidationResult
from utils import load_runtime_config


class UnifiedConfigLoader:
    """Single config loader facade used by new runtime components."""

    def __init__(self, base_dir: str | Path) -> None:
        self.base_dir = Path(base_dir)

    def load(
        self,
        *,
        require_mt5_credentials: bool = True,
        apply_mode_env_override: bool = True,
    ) -> dict[str, Any]:
        config = load_runtime_config(
            self.base_dir,
            require_mt5_credentials=require_mt5_credentials,
            apply_mode_env_override=apply_mode_env_override,
        )
        return ConfigSchema.normalize(config)

    @staticmethod
    def validate(config: dict[str, Any]) -> ConfigValidationResult:
        return ConfigSchema.validate(config)

