"""Persistent state management for the MT5 trading bot."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from utils import default_state, ensure_directory, load_json, save_json_atomic


class StateManager:
    """Load and save bot state from the storage directory."""

    def __init__(
        self,
        base_dir: str | Path,
        relative_path: str = "storage/state.json",
        logger: Any | None = None,
        retry_count: int = 3,
        retry_delay_seconds: float = 0.35,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.path = self.base_dir / relative_path
        self.legacy_path = self.base_dir / "state.json"
        self.logger = logger
        self.retry_count = max(1, int(retry_count))
        self.retry_delay_seconds = max(0.0, float(retry_delay_seconds))
        ensure_directory(self.path.parent)

    def load_state(self) -> dict[str, Any]:
        """Load state from storage, migrating the legacy root file if needed."""
        if self.path.exists():
            return load_json(self.path, default_state())
        if self.legacy_path.exists():
            state = load_json(self.legacy_path, default_state())
            self.save_state(state)
            return state
        return default_state()

    def save_state(self, state: dict[str, Any]) -> dict[str, Any]:
        """Persist state atomically, tolerating transient Windows file locks."""
        last_error: OSError | None = None

        for attempt in range(1, self.retry_count + 1):
            try:
                save_json_atomic(self.path, state, retries=1, retry_delay=self.retry_delay_seconds)
                if attempt > 1 and self.logger is not None:
                    self.logger.structured(
                        "state_save_recovered",
                        {
                            "path": str(self.path),
                            "attempt": attempt,
                            "retry_count": self.retry_count,
                        },
                        level="WARNING",
                    )
                return {
                    "ok": True,
                    "path": str(self.path),
                    "attempts": attempt,
                }
            except OSError as exc:
                last_error = exc
                if self.logger is not None:
                    self.logger.structured(
                        "state_save_retry",
                        {
                            "path": str(self.path),
                            "attempt": attempt,
                            "retry_count": self.retry_count,
                            "error_type": type(exc).__name__,
                            "error": str(exc),
                            "one_drive_path": "onedrive" in str(self.base_dir).lower(),
                        },
                        level="WARNING",
                    )
                if attempt < self.retry_count:
                    time.sleep(self.retry_delay_seconds * attempt)

        return {
            "ok": False,
            "path": str(self.path),
            "attempts": self.retry_count,
            "error": str(last_error) if last_error is not None else "unknown_state_save_error",
            "error_type": type(last_error).__name__ if last_error is not None else "OSError",
        }
