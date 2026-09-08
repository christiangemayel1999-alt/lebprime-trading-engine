"""Audit logging for dashboard actions and config changes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from services.telegram_notifier import TelegramNotifier
from utils import load_runtime_config
from utils import utc_now


class AuditService:
    """Record dashboard and control actions in SQLite."""

    def __init__(self, database_service: Any, base_dir: str | Path) -> None:
        self.database_service = database_service
        self.base_dir = Path(base_dir)

    def log_action(
        self,
        actor: str,
        action: str,
        target: str,
        details: dict[str, Any] | None = None,
        level: str = "INFO",
    ) -> None:
        """Insert an audit row."""
        self.database_service.insert_audit_event(
            {
                "timestamp": utc_now().isoformat(),
                "actor": actor,
                "action": action,
                "target": target,
                "level": level,
                "details_json": json.dumps(details or {}, sort_keys=True),
            }
        )
        self._notify_dashboard_action(actor, action, target, details or {}, level)

    def _notify_dashboard_action(
        self,
        actor: str,
        action: str,
        target: str,
        details: dict[str, Any],
        level: str,
    ) -> None:
        """Forward dashboard actions to Telegram when enabled."""
        try:
            config = load_runtime_config(self.base_dir, require_mt5_credentials=False)
            telegram_cfg = config.get("telegram", {})
            if not bool(telegram_cfg.get("enabled", False)):
                return
            if not bool(telegram_cfg.get("dashboard_change_alerts", True)):
                return
            notifier = TelegramNotifier(
                token=telegram_cfg.get("bot_token"),
                chat_id=telegram_cfg.get("chat_id"),
                timeout_seconds=int(telegram_cfg.get("timeout_seconds", 5)),
                enabled=bool(telegram_cfg.get("enabled", False)),
            )
            notifier.send_dashboard_action(actor, action, target, details, severity=level)
        except Exception:
            return
