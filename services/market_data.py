"""Shared feed interfaces used by live and historical execution modes."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

import pandas as pd


class MarketDataFeed(ABC):
    """Contract for bar-based market data replay or streaming."""

    @abstractmethod
    def get_next_bar(self) -> dict[str, Any] | None:
        """Advance feed and return the next bar."""

    @abstractmethod
    def get_recent_bars(self, n: int) -> pd.DataFrame:
        """Return the most recent `n` bars up to current pointer."""

    @abstractmethod
    def get_current_bar(self) -> dict[str, Any] | None:
        """Return current bar at pointer, if any."""

    @abstractmethod
    def is_finished(self) -> bool:
        """Return whether replay reached end-of-data."""
