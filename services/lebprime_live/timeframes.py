"""Closed-candle timeframe definitions and deterministic M45 aggregation."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd
from trading_bot.core.timeframes import normalize_timeframe_label, resolve_timeframe


TIMEFRAMES: tuple[str, ...] = ("M1", "M3", "M5", "M15", "M30", "M45")
MT5_TIMEFRAME_LABELS: dict[str, str] = {label: label for label in TIMEFRAMES}


def normalize_lebprime_timeframe(label: str) -> str:
    canonical = normalize_timeframe_label(label)
    if canonical not in TIMEFRAMES:
        raise ValueError(f"Unsupported LebPrime timeframe: {label}")
    return canonical


def _utc_timestamp(value: Any) -> pd.Timestamp:
    timestamp = pd.to_datetime(value, utc=True)
    if pd.isna(timestamp):
        raise ValueError(f"Invalid candle timestamp: {value}")
    return timestamp


def aggregate_m45(frame: pd.DataFrame, now_utc: datetime | None = None) -> pd.DataFrame:
    """Aggregate complete M15 candles into fixed UTC-aligned M45 candles.

    Boundaries are anchored to UTC midnight at 00:00, 00:45, 01:30, and so on.
    A period is emitted only when all three expected M15 opens are present and the
    period end is no later than ``now_utc``. No future or partial candle is used.
    """
    columns = ["time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume"]
    if frame is None or frame.empty:
        return pd.DataFrame(columns=columns)
    source = frame.copy()
    time_column = "time" if "time" in source.columns else "timestamp"
    source["time"] = source[time_column].map(_utc_timestamp)
    source = source[source["time"].dt.minute.isin((0, 15, 30, 45)) & source["time"].dt.second.eq(0)]
    source = source.sort_values("time").drop_duplicates("time", keep="last")
    for column in ("tick_volume", "spread", "real_volume"):
        if column not in source.columns:
            source[column] = 0.0

    minute_of_day = source["time"].dt.hour * 60 + source["time"].dt.minute
    source["period_start"] = source["time"].dt.normalize() + pd.to_timedelta((minute_of_day // 45) * 45, unit="m")
    cutoff = _utc_timestamp(now_utc or datetime.now(timezone.utc))
    rows: list[dict[str, Any]] = []
    for period_start, group in source.groupby("period_start", sort=True):
        expected = {period_start + pd.Timedelta(minutes=offset) for offset in (0, 15, 30)}
        actual = set(group["time"].tolist())
        if not expected.issubset(actual) or period_start + pd.Timedelta(minutes=45) > cutoff:
            continue
        complete = group[group["time"].isin(expected)].sort_values("time")
        rows.append(
            {
                "time": period_start,
                "open": float(complete.iloc[0]["open"]),
                "high": float(complete["high"].max()),
                "low": float(complete["low"].min()),
                "close": float(complete.iloc[-1]["close"]),
                "tick_volume": float(complete["tick_volume"].sum()),
                "spread": float(complete["spread"].max()),
                "real_volume": float(complete["real_volume"].sum()),
            }
        )
    return pd.DataFrame(rows, columns=columns)
