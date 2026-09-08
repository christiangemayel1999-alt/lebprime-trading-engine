"""Live MT5 feed implementation matching the MarketDataFeed interface."""

from __future__ import annotations

from typing import Any

import pandas as pd

from mt5_connector import MT5Connector
from services.market_data import MarketDataFeed


class LiveMT5Feed(MarketDataFeed):
    """Small adapter over MT5Connector for live bar polling."""

    def __init__(self, connector: MT5Connector, timeframe: str, bars_to_fetch: int = 500) -> None:
        self.connector = connector
        self.timeframe = timeframe
        self.bars_to_fetch = max(50, int(bars_to_fetch))
        self._last_time: str | None = None
        self._current: dict[str, Any] | None = None

    def _load(self) -> pd.DataFrame:
        return self.connector.fetch_rates(self.timeframe, self.bars_to_fetch, min_rows=50)

    def get_next_bar(self) -> dict[str, Any] | None:
        frame = self._load()
        if frame.empty:
            return None
        latest = frame.iloc[-1].to_dict()
        ts = str(latest.get("time"))
        if self._last_time == ts:
            return None
        self._last_time = ts
        self._current = latest
        return dict(latest)

    def get_recent_bars(self, n: int) -> pd.DataFrame:
        frame = self._load()
        return frame.tail(max(1, int(n))).reset_index(drop=True)

    def get_current_bar(self) -> dict[str, Any] | None:
        return dict(self._current) if self._current else None

    def is_finished(self) -> bool:
        return False
