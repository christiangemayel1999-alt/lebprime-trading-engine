"""Unified DB-backed job orchestration for backtests and OOS matrices."""

from __future__ import annotations

import logging
from copy import deepcopy
from pathlib import Path
from typing import Any

from services.config_manager import ConfigManager
from services.database import DatabaseService
from services.oos_service import OOSService
from services.worker_control import ensure_worker_running
from trading_bot.core.job_state import JobState, is_active_job_state, normalize_job_state
from trading_bot.core.job_types import JobType, normalize_job_type
from utils import to_utc, utc_now


class JobService:
    """Create, inspect, reconcile, and control queued execution jobs."""

    def __init__(
        self,
        base_dir: str | Path,
        config_manager: ConfigManager,
        database: DatabaseService,
        oos_service: OOSService,
        ensure_worker_on_backtest_create: bool = False,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.config_manager = config_manager
        self.database = database
        self.oos_service = oos_service
        self.ensure_worker_on_backtest_create = bool(ensure_worker_on_backtest_create)
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
        related_run_type = str(payload.get("related_run_type") or "")
        related_run_id = payload.get("related_run_id")
        if related_run_type == "backtest" and related_run_id not in (None, ""):
            payload["related_run"] = self.database.get_backtest_run(int(related_run_id))
        elif related_run_type == "oos" and related_run_id not in (None, ""):
            payload["related_run"] = self.oos_service.get_status(int(related_run_id)).get("run")
        else:
            payload["related_run"] = None
        return payload

    def create_backtest_job(self, request: dict[str, Any], *, actor: str = "dashboard") -> dict[str, Any]:
        """Queue a backtest execution job."""
        config = self.config_manager.dashboard_view()
        worker_probe = (
            ensure_worker_running(self.base_dir, wait_seconds=1.0)
            if self.ensure_worker_on_backtest_create
            else {"worker_available": None, "launch_attempted": False, "started": False, "status": {}}
        )
        selected_mode = str(request.get("engine_mode") or (config.get("mode", {}) or {}).get("selected_mode") or config.get("bot", {}).get("execution_mode") or "")
        request_payload = deepcopy(request)
        request_payload["engine_mode"] = selected_mode
        warning = (
            "Backtest job queued, but no worker is running yet."
            if self.ensure_worker_on_backtest_create and not worker_probe.get("worker_available")
            else None
        )
        created_at = utc_now().isoformat()
        job_id = self.database.create_job(
            {
                "created_at": created_at,
                "updated_at": created_at,
                "job_type": JobType.BACKTEST.value,
                "state": JobState.QUEUED.value,
                "payload_json": {"request": request_payload, "actor": actor, "execution_mode": config.get("mode", {})},
                "metadata_json": {
                    "actor": actor,
                    "execution_mode": config.get("mode", {}),
                    "worker_available": bool(worker_probe.get("worker_available")),
                    "worker_status": worker_probe.get("status", {}),
                    "worker_launch_attempted": bool(worker_probe.get("launch_attempted")),
                    "worker_launch_started": bool(worker_probe.get("started")),
                    "warning": warning,
                },
            }
        )
        self._log(
            "job_created",
            {
                "job_id": job_id,
                "job_type": JobType.BACKTEST.value,
                "actor": actor,
                "request": request_payload,
                "execution_mode": config.get("mode", {}),
                "worker_available": bool(worker_probe.get("worker_available")),
                "warning": warning,
            },
        )
        payload = self.get_job(job_id)
        if warning:
            payload["warning"] = warning
        return payload

    def create_oos_job(
        self,
        *,
        request: dict[str, Any],
        actor: str = "dashboard",
        baseline_run_id: int | None = None,
        baseline_bundle_path: str | None = None,
        baseline_database_path: str | None = None,
        output_dir: str | None = None,
        title: str | None = None,
        notes: str | None = None,
        rerun_partial: bool = False,
        existing_oos_run_id: int | None = None,
    ) -> dict[str, Any]:
        """Create or resume an OOS run manifest and enqueue a worker job for it."""
        config = self.config_manager.dashboard_view()
        request_payload = deepcopy(request)
        request_payload["engine_mode"] = str(request.get("engine_mode") or (config.get("mode", {}) or {}).get("selected_mode") or config.get("bot", {}).get("execution_mode") or "")
        if existing_oos_run_id is None:
            run_status = self.oos_service.start_run(
                request=request_payload,
                actor=actor,
                baseline_run_id=baseline_run_id,
                baseline_bundle_path=baseline_bundle_path,
                baseline_database_path=baseline_database_path,
                output_dir=output_dir,
                title=title,
                notes=notes,
            )
        else:
            run_status = self.oos_service.resume_run(existing_oos_run_id, rerun_partial=rerun_partial)
        oos_run = run_status.get("run") or {}
        created_at = utc_now().isoformat()
        job_id = self.database.create_job(
            {
                "created_at": created_at,
                "updated_at": created_at,
                "job_type": JobType.OOS_MATRIX.value,
                "state": JobState.QUEUED.value,
                "payload_json": {
                    "oos_run_id": int(oos_run.get("id") or existing_oos_run_id or 0),
                    "rerun_partial": bool(rerun_partial),
                    "actor": actor,
                    "request": request_payload,
                    "execution_mode": config.get("mode", {}),
                },
                "related_run_type": "oos",
                "related_run_id": int(oos_run.get("id") or existing_oos_run_id or 0),
                "progress_current": int((oos_run.get("progress") or {}).get("current", 0) or 0),
                "progress_total": int((oos_run.get("progress") or {}).get("total", 0) or 0),
                "metadata_json": {"actor": actor, "execution_mode": config.get("mode", {})},
            }
        )
        self._log(
            "job_created",
            {
                "job_id": job_id,
                "job_type": JobType.OOS_MATRIX.value,
                "actor": actor,
                "oos_run_id": oos_run.get("id") or existing_oos_run_id,
                "rerun_partial": rerun_partial,
                "execution_mode": config.get("mode", {}),
            },
        )
        return self.get_job(job_id)

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
            related_run_type = str(row.get("related_run_type") or "")
            related_run_id = row.get("related_run_id")
            if related_run_type == "oos" and related_run_id not in (None, ""):
                self.database.update_oos_run(int(related_run_id), {"state": JobState.INTERRUPTED.value, "interrupted_reason": reason})
            self._log("job_interrupted", {"job_id": job_id, "job_type": row.get("job_type"), "reason": reason})
        elif state == JobState.RUNNING.value:
            self.database.request_job_cancel(job_id, reason=reason)
            self._log("job_cancel_requested", {"job_id": job_id, "job_type": row.get("job_type"), "reason": reason})
        return self.get_job(job_id)

    def resume_job(self, job_id: int, *, actor: str = "dashboard") -> dict[str, Any]:
        """Queue a follow-up job to resume or retry a previous execution."""
        row = self.database.get_job(job_id)
        if not row:
            raise ValueError(f"Job {job_id} not found")
        job_type = normalize_job_type(row.get("job_type"))
        if is_active_job_state(row.get("state")):
            related_run_type = str(row.get("related_run_type") or "")
            related_run_id = row.get("related_run_id")
            related_state = ""
            if related_run_type == "oos" and related_run_id not in (None, ""):
                related = self.database.get_oos_run(int(related_run_id)) or {}
                related_state = normalize_job_state(related.get("state"))
            elif related_run_type == "backtest" and related_run_id not in (None, ""):
                related = self.database.get_backtest_run(int(related_run_id)) or {}
                related_state = normalize_job_state(related.get("state"))
            if related_state and not is_active_job_state(related_state):
                self.database.finalize_job(
                    int(job_id),
                    state=related_state,
                    failure_reason=f"related_{related_run_type}_already_{related_state.lower()}",
                )
                row = self.database.get_job(job_id) or row
            else:
                raise RuntimeError(f"Job {job_id} is still active")
        payload = self._parse_json(row.get("payload_json"), {})
        if job_type == JobType.BACKTEST.value:
            request = payload.get("request")
            if not isinstance(request, dict) or not request:
                raise ValueError(f"Job {job_id} has no backtest payload to resume")
            resumed = self.database.create_job(
                {
                    "job_type": JobType.BACKTEST.value,
                    "state": JobState.QUEUED.value,
                    "payload_json": {"request": deepcopy(request), "actor": actor},
                    "parent_job_id": int(job_id),
                    "metadata_json": {"actor": actor, "resumed_from_job_id": job_id},
                }
            )
            self._log("job_created", {"job_id": resumed, "job_type": JobType.BACKTEST.value, "resumed_from_job_id": job_id})
            return self.get_job(resumed)
        if job_type == JobType.OOS_MATRIX.value:
            oos_run_id = int(payload.get("oos_run_id") or row.get("related_run_id") or 0)
            if oos_run_id <= 0:
                raise ValueError(f"Job {job_id} has no OOS run to resume")
            return self.create_oos_job(
                request={},
                actor=actor,
                rerun_partial=True,
                existing_oos_run_id=oos_run_id,
            )
        raise ValueError(f"Unsupported job type: {job_type}")

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
            related_run_type = str(row.get("related_run_type") or "")
            related_run_id = row.get("related_run_id")
            if related_run_type == "backtest" and related_run_id not in (None, ""):
                self.database.update_backtest_run_state(int(related_run_id), JobState.INTERRUPTED.value, interrupted_reason=reason)
            elif related_run_type == "oos" and related_run_id not in (None, ""):
                scenario_rows = self.database.get_oos_scenarios(int(related_run_id))
                completed = sum(1 for scenario in scenario_rows if normalize_job_state(scenario.get("status")) == JobState.COMPLETED.value)
                oos_state = JobState.PARTIAL.value if completed else JobState.INTERRUPTED.value
                self.database.update_oos_run(int(related_run_id), {"state": oos_state, "interrupted_reason": reason})
                for scenario in scenario_rows:
                    if normalize_job_state(scenario.get("status")) == JobState.RUNNING.value:
                        self.database.update_oos_scenario(
                            int(scenario["id"]),
                            {"status": JobState.INTERRUPTED.value, "error": reason, "finished_at": utc_now().isoformat()},
                        )
            self._log("job_interrupted", {"job_id": row["id"], "job_type": row.get("job_type"), "reason": reason})
