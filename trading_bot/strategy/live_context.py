from __future__ import annotations

from typing import Any, Protocol

import pandas as pd


class LiveContextProvider(Protocol):
    """Read-only market context needed by the live runtime.

    The contract deliberately excludes candidate generation, entry evaluation,
    risk approval, and every broker/execution operation.
    """

    def prepare_trend_dataframe(self, frame: pd.DataFrame) -> pd.DataFrame: ...

    def prepare_setup_dataframe(self, frame: pd.DataFrame) -> pd.DataFrame: ...

    def prepare_trigger_dataframe(self, frame: pd.DataFrame) -> pd.DataFrame: ...

    def classify_session(self, now_utc: Any) -> dict[str, Any]: ...

    def analyze_market_context(
        self,
        now_utc: Any,
        trend_df: pd.DataFrame,
        setup_df: pd.DataFrame,
        trigger_df: pd.DataFrame,
        spread_points: float,
    ) -> dict[str, Any]: ...


class UnifiedStrategyContextAdapter:
    """Expose only contextual operations from an existing support engine."""

    def __init__(self, engine: Any) -> None:
        self._engine = engine

    def prepare_trend_dataframe(self, frame: pd.DataFrame) -> pd.DataFrame:
        return self._engine.prepare_trend_dataframe(frame)

    def prepare_setup_dataframe(self, frame: pd.DataFrame) -> pd.DataFrame:
        return self._engine.prepare_setup_dataframe(frame)

    def prepare_trigger_dataframe(self, frame: pd.DataFrame) -> pd.DataFrame:
        return self._engine.prepare_trigger_dataframe(frame)

    def classify_session(self, now_utc: Any) -> dict[str, Any]:
        return self._engine.classify_session(now_utc)

    def analyze_market_context(
        self,
        now_utc: Any,
        trend_df: pd.DataFrame,
        setup_df: pd.DataFrame,
        trigger_df: pd.DataFrame,
        spread_points: float,
    ) -> dict[str, Any]:
        return self._engine.analyze_market_context(now_utc, trend_df, setup_df, trigger_df, spread_points)
