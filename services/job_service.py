"""DB-backed job orchestration for worker jobs (Indicator Lab campaigns)."""

from __future__ import annotations

import logging
from copy import deepcopy
from pathlib import Path
from typing import Any

from services.config_manager import ConfigManager
from services.database import DatabaseService
from trading_bot.core.job_state import JobState, normalize_job_state
from trading_bot.core.job_types import normalize_job_type
from utils import to_utc, utc_now


class JobService:
    """Create, inspect, reconcile, and control queued execution jobs."""

    def __init__(
        self,
        base_dir: str | Path,
        config_manager: ConfigManager,
        database: DatabaseService,
        oos_service: Any = None,
        ensure_worker_on_backtest_create: bool = False,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.config_manager = config_manager
        self.database = database
        self._runtime_logger = logging.getLogger("mtf_sniper_bot")
        self.reconcile_stale_jobs()

    @staticmethod
    def _parse_json(value: Any, default: Any) -> Any:
        if value in (None, ""):
            return deepcopy(default)
        if isinstance(value, (dict, list)):
            return deepcopy(value)
        try:
            import json

            parsed = json.loads(value)
            return parsed if parsed is not None else deepcopy(default)
        except Exception:
            return deepcopy(default)

    def _log(self, event_type: str, payload: dict[str, Any]) -> None:
        self._runtime_logger.info("%s %s", event_type, payload)

    def _job_payload(self, row: dict[str, Any]) -> dict[str, Any]:
        payload = {
            **row,
            "job_type": normalize_job_type(row.get("job_type")),
            "state": normalize_job_state(row.get("state")),
            "payload_json": self._parse_json(row.get("payload_json"), {}),
            "result_json": self._parse_json(row.get("result_json"), {}),
            "metadata_json": self._parse_json(row.get("metadata_json"), {}),
            "cancel_requested": bool(row.get("cancel_requested")),
        }
        payload["related_run"] = None
        return payload

    def get_job(self, job_id: int) -> dict[str, Any]:
        """Return one parsed job payload."""
        self.reconcile_stale_jobs()
        row = self.database.get_job(job_id)
        if not row:
            raise ValueError(f"Job {job_id} not found")
        return self._job_payload(row)

    def list_jobs(self, *, limit: int = 50, job_type: str | None = None, state: str | None = None) -> list[dict[str, Any]]:
        """Return recent jobs with linked run context."""
        self.reconcile_stale_jobs()
        return [self._job_payload(row) for row in self.database.get_jobs(limit=limit, job_type=job_type, state=state)]

    def is_cancel_requested(self, job_id: int) -> bool:
        row = self.database.get_job(job_id) or {}
        return bool(row.get("cancel_requested"))

    def cancel_job(self, job_id: int, *, actor: str = "dashboard") -> dict[str, Any]:
        """Cancel a queued job or request cancellation for a running one."""
        row = self.database.get_job(job_id)
        if not row:
            raise ValueError(f"Job {job_id} not found")
        state = normalize_job_state(row.get("state"))
        reason = f"cancel_requested_by_{actor}"
        if state == JobState.QUEUED.value:
            self.database.finalize_job(job_id, state=JobState.INTERRUPTED.value, interrupted_reason=reason)
            self._log("job_interrupted", {"job_id": job_id, "job_type": row.get("job_type"), "reason": reason})
        elif state == JobState.RUNNING.value:
            self.database.request_job_cancel(job_id, reason=reason)
            self._log("job_cancel_requested", {"job_id": job_id, "job_type": row.get("job_type"), "reason": reason})
        return self.get_job(job_id)

    def resume_job(self, job_id: int, *, actor: str = "dashboard") -> dict[str, Any]:
        """Jobs are not resumable; Indicator Lab campaigns resume through their own pause/resume."""
        row = self.database.get_job(job_id)
        if not row:
            raise ValueError(f"Job {job_id} not found")
        raise ValueError(f"Unsupported job type: {normalize_job_type(row.get('job_type'))}")

    def reconcile_stale_jobs(self, stale_seconds: int = 300) -> None:
        """Mark running jobs as interrupted when their heartbeat is stale."""
        now = utc_now()
        for row in self.database.get_active_jobs():
            if normalize_job_state(row.get("state")) != JobState.RUNNING.value:
                continue
            heartbeat_at = to_utc(row.get("last_heartbeat_at"))
            if heartbeat_at is None:
                continue
            if (now - heartbeat_at).total_seconds() <= float(stale_seconds):
                continue
            reason = "job_heartbeat_stale"
            self.database.finalize_job(int(row["id"]), state=JobState.INTERRUPTED.value, interrupted_reason=reason)
            self._log("job_interrupted", {"job_id": row["id"], "job_type": row.get("job_type"), "reason": reason})
