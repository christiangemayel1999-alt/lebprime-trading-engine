"""Trading-session labelling for heartbeats, risk counters and diagnostics."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytz


def _minutes(hhmm: str) -> int:
    hours, minutes = str(hhmm).split(":")
    return int(hours) * 60 + int(minutes)


def classify_session(config: dict[str, Any], now_utc: datetime) -> dict[str, Any]:
    """Return the configured session bucket that ``now_utc`` falls in (``UNCLASSIFIED`` if none)."""
    sessions_cfg = config.get("sessions", {}) if isinstance(config.get("sessions"), dict) else {}
    timezone_name = str(sessions_cfg.get("timezone", "UTC"))
    local_now = now_utc.astimezone(pytz.timezone(timezone_name))
    local_minutes = local_now.hour * 60 + local_now.minute
    selected: dict[str, Any] | None = None
    for bucket in sessions_cfg.get("buckets", []) or []:
        try:
            start, end = _minutes(bucket["start"]), _minutes(bucket["end"])
        except (KeyError, ValueError):
            continue
        inside = start <= local_minutes <= end if start <= end else (local_minutes >= start or local_minutes <= end)
        if inside:
            selected = bucket
            break
    selected = selected or {"name": "UNCLASSIFIED", "start": "", "end": ""}
    return {
        "session_name": str(selected.get("name", "UNCLASSIFIED")),
        "timezone": timezone_name,
        "local_time": local_now.isoformat(),
        "bucket": dict(selected),
    }
