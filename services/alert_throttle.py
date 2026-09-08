"""In-memory alert throttling helpers for noisy operational events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from utils import utc_now


@dataclass
class _AlertEntry:
    """Track the latest emitted payload for a throttled alert key."""

    emitted_at: datetime
    fingerprint: str
    severity: str


class AlertThrottle:
    """Throttle repeated alerts unless fingerprint/severity changes or cooldown expires."""

    def __init__(self) -> None:
        self._entries: dict[str, _AlertEntry] = {}

    def should_emit(
        self,
        key: str,
        *,
        fingerprint: str,
        severity: str,
        cooldown_seconds: int,
        now: datetime | None = None,
    ) -> bool:
        """Return whether an alert should be emitted for the provided key."""
        normalized_key = str(key or "").strip()
        if not normalized_key:
            return True
        current_time = now or utc_now()
        normalized_fingerprint = str(fingerprint or "").strip() or "default"
        normalized_severity = str(severity or "INFO").upper()
        cooldown = max(0, int(cooldown_seconds))
        existing = self._entries.get(normalized_key)
        if existing is None:
            self._entries[normalized_key] = _AlertEntry(current_time, normalized_fingerprint, normalized_severity)
            return True
        if existing.fingerprint != normalized_fingerprint or existing.severity != normalized_severity:
            self._entries[normalized_key] = _AlertEntry(current_time, normalized_fingerprint, normalized_severity)
            return True
        elapsed = (current_time - existing.emitted_at).total_seconds()
        if elapsed >= cooldown:
            self._entries[normalized_key] = _AlertEntry(current_time, normalized_fingerprint, normalized_severity)
            return True
        return False

    def clear(self, key: str) -> None:
        """Clear a key so the next failure is emitted immediately."""
        normalized_key = str(key or "").strip()
        if not normalized_key:
            return
        self._entries.pop(normalized_key, None)

    def snapshot(self) -> dict[str, Any]:
        """Return a serializable snapshot for diagnostics/debugging."""
        payload: dict[str, Any] = {}
        for key, value in self._entries.items():
            payload[key] = {
                "emitted_at": value.emitted_at.isoformat(),
                "fingerprint": value.fingerprint,
                "severity": value.severity,
            }
        return payload
