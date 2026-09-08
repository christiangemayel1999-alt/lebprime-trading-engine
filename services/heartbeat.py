"""Heartbeat helpers for loop-level bot health reporting."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from utils import to_utc, utc_now


class HeartbeatService:
    """Write heartbeat rows and evaluate staleness."""

    def __init__(self, database_service: Any) -> None:
        self.database_service = database_service

    def record(self, heartbeat: dict[str, Any]) -> None:
        """Persist a heartbeat snapshot."""
        self.database_service.insert_heartbeat(heartbeat)

    @staticmethod
    def is_stale(last_timestamp: str | datetime | None, stale_after_seconds: int) -> bool:
        """Determine whether a heartbeat is stale."""
        heartbeat_time = to_utc(last_timestamp)
        if heartbeat_time is None:
            return True
        return (utc_now() - heartbeat_time).total_seconds() > stale_after_seconds
