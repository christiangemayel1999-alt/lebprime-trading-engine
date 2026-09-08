"""Historical MT5 feed that replays bars without look-ahead bias."""

from __future__ import annotations

from typing import Any

import pandas as pd

from services.market_data import MarketDataFeed


class HistoricalMT5Feed(MarketDataFeed):
    """In-memory bar replay feed for deterministic backtests."""

    def __init__(self, bars: pd.DataFrame) -> None:
        frame = bars.copy()
        if "time" not in frame.columns:
            raise ValueError("Historical feed requires a 'time' column")
        self.bars = frame.sort_values("time").reset_index(drop=True)
        self.pointer = -1

    def get_next_bar(self) -> dict[str, Any] | None:
        if self.is_finished():
            return None
        self.pointer += 1
        return self.get_current_bar()

    def get_recent_bars(self, n: int) -> pd.DataFrame:
        if self.pointer < 0:
            return self.bars.iloc[0:0].copy()
        start = max(0, self.pointer - max(1, int(n)) + 1)
        return self.bars.iloc[start : self.pointer + 1].copy().reset_index(drop=True)

    def get_current_bar(self) -> dict[str, Any] | None:
        if self.pointer < 0 or self.pointer >= len(self.bars):
            return None
        return dict(self.bars.iloc[self.pointer].to_dict())

    def peek_next_bar(self) -> dict[str, Any] | None:
        next_index = self.pointer + 1
        if next_index < 0 or next_index >= len(self.bars):
            return None
        return dict(self.bars.iloc[next_index].to_dict())

    def is_finished(self) -> bool:
        return self.pointer >= len(self.bars) - 1

    def bars_until(self, ts: Any) -> pd.DataFrame:
        """Return all bars with timestamp <= ts."""
        return self.bars[self.bars["time"] <= ts].copy()
