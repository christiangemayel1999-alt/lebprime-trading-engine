"""Compatibility wrapper for the journal repository import path."""

from __future__ import annotations

from typing import Any


class JournalRepository:
    """Minimal adapter around the trade journal service used by the bot."""

    def __init__(self, logger: Any, trade_journal: Any) -> None:
        self.logger = logger
        self.trade_journal = trade_journal

