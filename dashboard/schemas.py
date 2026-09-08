"""Pydantic models for the FastAPI dashboard."""

from __future__ import annotations

from typing import Any
from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator, model_validator


BACKTEST_EXECUTION_MODELS = {"next_bar_open", "signal_price_touch", "current_bar_close"}


def _normalize_iso_date(value: Any, field_name: str) -> str:
    if value in (None, ""):
        raise ValueError(f"{field_name} is required")
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception as exc:
        raise ValueError("Dates must be ISO format YYYY-MM-DD") from exc
    return parsed.date().isoformat()


class ActorRequest(BaseModel):
    """Dashboard action actor metadata."""

    actor: str = Field(default="dashboard")


class ModeSwitchRequest(ActorRequest):
    """Switch the bot trading mode."""

    trading_mode: str
    confirmation_text: str | None = None

    @field_validator("trading_mode")
    @classmethod
    def validate_mode(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"DRY_RUN", "LIVE", "DEMO", "VALIDATION_TEST", "BACKTEST"}:
            raise ValueError("Unsupported trading mode")
        return normalized


class ExecutionModeRequest(ActorRequest):
    """Switch the authoritative strategy-engine mode."""

    execution_mode: str

    @field_validator("execution_mode")
    @classmethod
    def validate_execution_mode_choice(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"V1_BASELINE", "V2_FULL"}:
            raise ValueError("Unsupported execution mode")
        return normalized


class BacktestRunRequest(ActorRequest):
    """Run a historical replay/backtest job."""

    symbol: str = "XAUUSD"
    timeframe: str = "M1"
    start_date: str
    end_date: str
    enabled_strategies: list[str] = Field(default_factory=list)
    session_filter: dict[str, Any] = Field(default_factory=lambda: {"enabled": False, "allowed_sessions": []})
    initial_balance: float = 10000.0
    risk_percent: float = 0.5
    spread_model: dict[str, Any] = Field(default_factory=lambda: {"type": "fixed_points", "points": 20.0})
    slippage_model: dict[str, Any] = Field(default_factory=lambda: {"type": "fixed_points", "points": 2.0})
    execution_model: str = "next_bar_open"
    engine_mode: str | None = None
    notes: str | None = None

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("symbol is required")
        return normalized

    @field_validator("timeframe")
    @classmethod
    def validate_timeframe(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"M1", "M3", "M5", "M15", "M30", "H1"}:
            raise ValueError("Unsupported timeframe")
        return normalized

    @field_validator("execution_model")
    @classmethod
    def validate_execution_model(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in BACKTEST_EXECUTION_MODELS:
            raise ValueError("Unsupported execution_model")
        return normalized

    @field_validator("enabled_strategies")
    @classmethod
    def validate_enabled_strategies(cls, value: list[str]) -> list[str]:
        cleaned = [str(item).strip().upper() for item in value if str(item).strip()]
        if value is not None and not cleaned:
            raise ValueError("At least one strategy must be selected")
        return cleaned

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def validate_dates(cls, value: Any, info: Any) -> str:
        return _normalize_iso_date(value, str(info.field_name))

    @model_validator(mode="after")
    def validate_date_range(self) -> "BacktestRunRequest":
        if date.fromisoformat(self.start_date) > date.fromisoformat(self.end_date):
            raise ValueError("start_date must be before or equal to end_date")
        return self

    @field_validator("engine_mode")
    @classmethod
    def validate_backtest_engine_mode(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return None
        normalized = value.strip().upper()
        if normalized not in {"V1_BASELINE", "V2_FULL"}:
            raise ValueError("Unsupported engine_mode")
        return normalized


class OOSRunRequest(ActorRequest):
    """Launch an OOS evaluation matrix."""

    symbol: str = "XAUUSD"
    timeframe: str = "M1"
    start_date: str
    end_date: str
    initial_balance: float = 10000.0
    risk_percent: float = 0.5
    spread_points: float = 20.0
    slippage_points: float = 2.0
    execution_model: str = "next_bar_open"
    engine_mode: str | None = None
    enabled_strategies: list[str] = Field(default_factory=lambda: ["XAU_BOT_BREAKOUT", "XAU_BOT_COMPRESS", "XAU_LEBPRIM"])
    allowed_sessions: list[str] = Field(default_factory=list)
    baseline_run_id: int | None = None
    baseline_bundle_path: str | None = None
    baseline_database_path: str | None = None
    output_dir: str | None = None
    title: str | None = None
    notes: str | None = None

    @field_validator("symbol")
    @classmethod
    def validate_oos_symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("symbol is required")
        return normalized

    @field_validator("timeframe")
    @classmethod
    def validate_oos_timeframe(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"M1", "M3", "M5", "M15", "M30", "H1"}:
            raise ValueError("Unsupported timeframe")
        return normalized

    @field_validator("execution_model")
    @classmethod
    def validate_oos_execution_model(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in BACKTEST_EXECUTION_MODELS:
            raise ValueError("Unsupported execution_model")
        return normalized

    @field_validator("enabled_strategies")
    @classmethod
    def validate_oos_enabled_strategies(cls, value: list[str]) -> list[str]:
        cleaned = [str(item).strip().upper() for item in value if str(item).strip()]
        if value is not None and not cleaned:
            raise ValueError("At least one strategy must be selected")
        return cleaned

    @field_validator("start_date", "end_date")
    @classmethod
    def validate_oos_dates(cls, value: str) -> str:
        try:
            datetime.fromisoformat(str(value))
        except Exception as exc:
            raise ValueError("Dates must be ISO format YYYY-MM-DD") from exc
        return str(value)

    @field_validator("engine_mode")
    @classmethod
    def validate_oos_engine_mode(cls, value: str | None) -> str | None:
        if value in (None, ""):
            return None
        normalized = value.strip().upper()
        if normalized not in {"V1_BASELINE", "V2_FULL"}:
            raise ValueError("Unsupported engine_mode")
        return normalized


class OOSResumeRequest(ActorRequest):
    """Resume a persisted OOS run."""

    oos_run_id: int
    rerun_partial: bool = False


class OOSRebuildRequest(ActorRequest):
    """Rebuild report artifacts from existing completed OOS scenarios."""

    oos_run_id: int


class JobBacktestRequest(BacktestRunRequest):
    """Queue a backtest execution job."""


class JobOOSRequest(OOSRunRequest):
    """Queue an OOS matrix job."""


class JobResumeRequest(ActorRequest):
    """Resume a previous job by id."""

    job_id: int


class JobCancelRequest(ActorRequest):
    """Cancel a queued or running job by id."""

    job_id: int


class ToggleRequest(ActorRequest):
    """Toggle-style control request."""

    enabled: bool
    reason: str | None = None


class ConfigPatchRequest(ActorRequest):
    """Nested config patch request."""

    updates: dict[str, Any]
    preview_only: bool = False
    reason: str = "dashboard_patch"


class RawConfigImportRequest(ActorRequest):
    """Import full config text."""

    config_json: str


class PresetRequest(ActorRequest):
    """Apply a named dashboard preset."""

    preset_name: str

    @field_validator("preset_name")
    @classmethod
    def validate_preset_name(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"SAFE_MODE", "BALANCED_MODE", "SCALPING_MODE", "AGGRESSIVE_MODE"}:
            raise ValueError("Unsupported preset")
        return normalized


class FamilyToggleRequest(ActorRequest):
    """Toggle a strategy family and optional live permission."""

    family_name: str
    enabled: bool
    live_allowed: bool | None = None

    @field_validator("family_name")
    @classmethod
    def validate_family_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("family_name is required")
        return normalized


class StrategyToggleRequest(ActorRequest):
    """Toggle an individual strategy/setup control."""

    strategy_name: str
    enabled: bool

    @field_validator("strategy_name")
    @classmethod
    def validate_strategy_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("strategy_name is required")
        return normalized


class ManualTradeOpenRequest(ActorRequest):
    """Open a manual market trade."""

    symbol: str
    side: str
    volume: float
    sl: float | None = None
    tp: float | None = None
    comment: str | None = None

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("symbol is required")
        return normalized

    @field_validator("side")
    @classmethod
    def validate_side(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"BUY", "SELL"}:
            raise ValueError("side must be BUY or SELL")
        return normalized


class PositionTicketRequest(ActorRequest):
    """Operate on a specific open position."""

    ticket: int


class PartialCloseRequest(PositionTicketRequest):
    """Partially close an open position."""

    volume: float


class ModifyPositionRequest(PositionTicketRequest):
    """Modify SL and/or TP for an open position."""

    sl: float | None = None
    tp: float | None = None
