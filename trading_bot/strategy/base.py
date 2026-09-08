from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import pandas as pd

from trading_bot.core.models import StrategyCandidate


class BaseStrategy(ABC):
    """Base contract for all concrete strategies in the unified engine."""

    strategy_name: str
    setup_families: set[str]

    @abstractmethod
    def prepare_trend_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        raise NotImplementedError

    @abstractmethod
    def prepare_setup_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        raise NotImplementedError

    @abstractmethod
    def prepare_trigger_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        raise NotImplementedError

    @abstractmethod
    def analyze_market_context(
        self,
        now_utc: Any,
        trend_df: pd.DataFrame,
        setup_df: pd.DataFrame,
        trigger_df: pd.DataFrame,
        spread_points: float,
    ) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def generate_setup_candidates(
        self,
        market_context: dict[str, Any],
        trend_df: pd.DataFrame,
        setup_df: pd.DataFrame,
        trigger_df: pd.DataFrame,
        symbol_spec: dict[str, Any],
    ) -> list[StrategyCandidate]:
        raise NotImplementedError

    @abstractmethod
    def evaluate_entry(
        self,
        candidate: dict[str, Any],
        trigger_df: pd.DataFrame,
        symbol_spec: dict[str, Any],
        now_utc: Any,
        live_profile: bool,
    ) -> dict[str, Any]:
        raise NotImplementedError

