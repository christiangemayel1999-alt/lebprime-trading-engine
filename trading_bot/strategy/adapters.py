from __future__ import annotations

from typing import Any

import pandas as pd

from lebprim_strategy import LebprimStrategy
from strategy import StrategyEngine
from trading_bot.core.models import StrategyCandidate
from trading_bot.strategy.base import BaseStrategy


class LegacyStrategyAdapter(BaseStrategy):
    """Adapter for the original multi-family strategy engine."""

    strategy_name = "XAU_LEGACY_MULTI"
    setup_families = {
        "TREND_PULLBACK_RECLAIM",
        "LIQUIDITY_SWEEP_REVERSAL",
        "COMPRESSION_RELEASE",
        "BREAKOUT_RETEST_CONTINUATION",
    }

    def __init__(self, config: dict[str, Any]) -> None:
        self.engine = StrategyEngine(config)

    @property
    def config(self) -> dict[str, Any]:
        return self.engine.config

    @config.setter
    def config(self, value: dict[str, Any]) -> None:
        self.engine.config = value

    def prepare_trend_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        return self.engine.prepare_trend_dataframe(df)

    def prepare_setup_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        return self.engine.prepare_setup_dataframe(df)

    def prepare_trigger_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        return self.engine.prepare_trigger_dataframe(df)

    def analyze_market_context(self, now_utc: Any, trend_df: pd.DataFrame, setup_df: pd.DataFrame, trigger_df: pd.DataFrame, spread_points: float) -> dict[str, Any]:
        return self.engine.analyze_market_context(now_utc, trend_df, setup_df, trigger_df, spread_points)

    def generate_setup_candidates(self, market_context: dict[str, Any], trend_df: pd.DataFrame, setup_df: pd.DataFrame, trigger_df: pd.DataFrame, symbol_spec: dict[str, Any]) -> list[StrategyCandidate]:
        raw = self.engine.generate_setup_candidates(market_context, trend_df, setup_df, trigger_df, symbol_spec)
        symbol = str(symbol_spec.get("name") or symbol_spec.get("symbol") or "XAUUSD")
        return [
            StrategyCandidate.from_mapping(item, strategy_name=_strategy_name_for_family(str(item.get("setup_family") or "")), symbol=symbol)
            for item in raw
            if str(item.get("setup_family") or "") in self.setup_families
        ]

    def evaluate_entry(self, candidate: dict[str, Any], trigger_df: pd.DataFrame, symbol_spec: dict[str, Any], now_utc: Any, live_profile: bool) -> dict[str, Any]:
        return self.engine.evaluate_entry(candidate, trigger_df, symbol_spec, now_utc, live_profile)

    def __getattr__(self, item: str) -> Any:
        return getattr(self.engine, item)


class LebprimStrategyAdapter(BaseStrategy):
    """Adapter for LEBPRIM using the same engine contract as all other setups."""

    strategy_name = "XAU_LEBPRIM"
    setup_families = {"LEBPRIM_SCALP"}

    def __init__(self, config: dict[str, Any]) -> None:
        self.engine = LebprimStrategy(config)

    @property
    def config(self) -> dict[str, Any]:
        return self.engine.config

    @config.setter
    def config(self, value: dict[str, Any]) -> None:
        self.engine.config = value

    def prepare_trend_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        return self.engine.prepare_trend_dataframe(df)

    def prepare_setup_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        return self.engine.prepare_setup_dataframe(df)

    def prepare_trigger_dataframe(self, df: pd.DataFrame) -> pd.DataFrame:
        return self.engine.prepare_trigger_dataframe(df)

    def analyze_market_context(self, now_utc: Any, trend_df: pd.DataFrame, setup_df: pd.DataFrame, trigger_df: pd.DataFrame, spread_points: float) -> dict[str, Any]:
        return self.engine.analyze_market_context(now_utc, trend_df, setup_df, trigger_df, spread_points)

    def generate_setup_candidates(self, market_context: dict[str, Any], trend_df: pd.DataFrame, setup_df: pd.DataFrame, trigger_df: pd.DataFrame, symbol_spec: dict[str, Any]) -> list[StrategyCandidate]:
        raw = self.engine.generate_setup_candidates(market_context, trend_df, setup_df, trigger_df, symbol_spec)
        symbol = str(symbol_spec.get("name") or symbol_spec.get("symbol") or "XAUUSD")
        return [StrategyCandidate.from_mapping(item, strategy_name=self.strategy_name, symbol=symbol) for item in raw]

    def evaluate_entry(self, candidate: dict[str, Any], trigger_df: pd.DataFrame, symbol_spec: dict[str, Any], now_utc: Any, live_profile: bool) -> dict[str, Any]:
        return self.engine.evaluate_entry(candidate, trigger_df, symbol_spec, now_utc, live_profile)

    def __getattr__(self, item: str) -> Any:
        return getattr(self.engine, item)


def _strategy_name_for_family(family: str) -> str:
    return {
        "TREND_PULLBACK_RECLAIM": "XAU_BOT_TREND_PU",
        "LIQUIDITY_SWEEP_REVERSAL": "XAU_BOT_LIQUIDIT",
        "COMPRESSION_RELEASE": "XAU_BOT_COMPRESS",
        "BREAKOUT_RETEST_CONTINUATION": "XAU_BOT_BREAKOUT",
        "LEBPRIM_SCALP": "XAU_LEBPRIM",
    }.get(family, family)

