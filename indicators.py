"""Indicator calculations and pattern helpers for the MT5 scalping bot."""

from __future__ import annotations

from typing import Literal

import numpy as np
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import ADXIndicator, EMAIndicator
from ta.volatility import AverageTrueRange


Direction = Literal["LONG", "SHORT"]


def add_trend_indicators(
    df: pd.DataFrame,
    ema_fast_period: int,
    ema_slow_period: int,
    atr_period: int,
) -> pd.DataFrame:
    """Append higher-timeframe trend indicators to a dataframe."""
    frame = df.copy()
    frame["ema_50"] = EMAIndicator(close=frame["close"], window=ema_fast_period).ema_indicator()
    frame["ema_200"] = EMAIndicator(close=frame["close"], window=ema_slow_period).ema_indicator()
    frame["atr"] = AverageTrueRange(
        high=frame["high"],
        low=frame["low"],
        close=frame["close"],
        window=atr_period,
    ).average_true_range()
    frame["ema_50_slope_3"] = frame["ema_50"] - frame["ema_50"].shift(3)
    frame["ema_50_slope_6"] = frame["ema_50"] - frame["ema_50"].shift(6)
    return frame


def add_setup_indicators(
    df: pd.DataFrame,
    ema_fast: int,
    ema_slow: int,
    rsi_period: int,
    atr_period: int,
    adx_period: int,
    volume_period: int,
) -> pd.DataFrame:
    """Append setup timeframe indicators to a dataframe."""
    frame = df.copy()
    frame["ema_20"] = EMAIndicator(close=frame["close"], window=ema_fast).ema_indicator()
    frame["ema_50"] = EMAIndicator(close=frame["close"], window=ema_slow).ema_indicator()
    frame["rsi"] = RSIIndicator(close=frame["close"], window=rsi_period).rsi()
    frame["atr"] = AverageTrueRange(
        high=frame["high"],
        low=frame["low"],
        close=frame["close"],
        window=atr_period,
    ).average_true_range()
    frame["adx"] = ADXIndicator(
        high=frame["high"],
        low=frame["low"],
        close=frame["close"],
        window=adx_period,
    ).adx()
    frame["volume_sma_20"] = frame["tick_volume"].rolling(window=volume_period, min_periods=1).mean()
    frame["volume_ratio"] = frame["tick_volume"] / frame["volume_sma_20"].replace(0, np.nan)
    frame["recent_high_6"] = frame["high"].rolling(window=6, min_periods=2).max()
    frame["recent_low_6"] = frame["low"].rolling(window=6, min_periods=2).min()
    return frame


def add_trigger_indicators(
    df: pd.DataFrame,
    volume_period: int,
) -> pd.DataFrame:
    """Append trigger timeframe indicators to a dataframe."""
    frame = df.copy()
    frame["volume_sma_20"] = frame["tick_volume"].rolling(window=volume_period, min_periods=1).mean()
    frame["volume_ratio"] = frame["tick_volume"] / frame["volume_sma_20"].replace(0, np.nan)
    frame["highest_high_3"] = frame["high"].rolling(window=3, min_periods=3).max()
    frame["lowest_low_3"] = frame["low"].rolling(window=3, min_periods=3).min()
    frame["highest_high_5"] = frame["high"].rolling(window=5, min_periods=5).max()
    frame["lowest_low_5"] = frame["low"].rolling(window=5, min_periods=5).min()
    return frame


def candle_body(row: pd.Series) -> float:
    """Return candle body size."""
    return abs(float(row["close"]) - float(row["open"]))


def candle_range(row: pd.Series) -> float:
    """Return total candle range."""
    return max(float(row["high"]) - float(row["low"]), 1e-9)


def lower_wick(row: pd.Series) -> float:
    """Return lower wick size."""
    return min(float(row["open"]), float(row["close"])) - float(row["low"])


def upper_wick(row: pd.Series) -> float:
    """Return upper wick size."""
    return float(row["high"]) - max(float(row["open"]), float(row["close"]))


def close_position_in_range(row: pd.Series) -> float:
    """Return candle close location within range as a 0-1 ratio."""
    return (float(row["close"]) - float(row["low"])) / candle_range(row)


def is_rejection_candle(
    row: pd.Series,
    direction: Direction,
    min_body_price: float,
    wick_ratio_threshold: float,
) -> bool:
    """Validate bullish or bearish rejection-candle structure."""
    total_range = candle_range(row)
    body = candle_body(row)
    if body < min_body_price:
        return False
    if direction == "LONG":
        wick_ratio = lower_wick(row) / total_range
        return wick_ratio >= wick_ratio_threshold and float(row["close"]) > float(row["open"])
    wick_ratio = upper_wick(row) / total_range
    return wick_ratio >= wick_ratio_threshold and float(row["close"]) < float(row["open"])


def indicators_are_valid(df: pd.DataFrame, columns: list[str]) -> bool:
    """Validate that the most recent trading rows have usable indicator values."""
    if df.empty or len(df) < 2:
        return False
    subset = df[columns].tail(5)
    if subset.empty:
        return False
    return not subset.isna().any().any() and np.isfinite(subset.to_numpy()).all()
