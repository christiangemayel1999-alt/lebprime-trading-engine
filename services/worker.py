"""DB-backed worker loop for Indicator Lab campaign jobs."""

from __future__ import annotations

import logging
import os
import socket
import time
import json
from pathlib import Path
from typing import Any

from services.config_manager import ConfigManager
from services.database import DatabaseService
from services.indicator_lab import IndicatorLabService
from services.job_service import JobService
from services.worker_control import write_worker_status
from trading_bot.core.job_state import JobState
from trading_bot.core.job_types import JobType, normalize_job_type
from utils import utc_now


class WorkerService:
    """Poll queued jobs from SQLite and execute them outside the dashboard process."""

    def __init__(
        self,
        base_dir: str | Path,
        *,
        poll_interval_seconds: float = 2.0,
        heartbeat_stale_seconds: int = 300,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.poll_interval_seconds = float(poll_interval_seconds)
        self.heartbeat_stale_seconds = int(heartbeat_stale_seconds)
        self.config_manager = ConfigManager(self.base_dir)
        runtime_cfg = self.config_manager.dashboard_view()
        self.database = DatabaseService(self.base_dir / runtime_cfg["storage"]["database_path"])
        self.indicator_lab_service = IndicatorLabService(self.base_dir, self.database, runtime_cfg)
        self.job_service = JobService(
            self.base_dir,
            self.config_manager,
            self.database,
        )
        self.worker_id = f"{socket.gethostname()}:{os.getpid()}"
        self.started_at = utc_now().isoformat()
        self.current_job_id: int | None = None
        self._runtime_logger = logging.getLogger("mtf_sniper_bot")
        self._shutdown_requested = False

    def _log(self, event_type: str, payload: dict[str, Any]) -> None:
        self._runtime_logger.info("%s %s", event_type, payload)

    def _write_status(self, status: str = "running", *, error: str | None = None) -> None:
        try:
            write_worker_status(
                self.base_dir,
                worker_id=self.worker_id,
                pid=os.getpid(),
                started_at=self.started_at,
                status=status,
                current_job_id=self.current_job_id,
                error=error,
            )
        except Exception as exc:
            self._log("worker_status_write_failed", {"worker_id": self.worker_id, "error": str(exc)})

    @staticmethod
    def _json_value(value: Any, default: Any) -> Any:
        if value in (None, ""):
            return default
        if isinstance(value, (dict, list)):
            return value
        try:
            parsed = json.loads(value)
            return parsed if parsed is not None else default
        except Exception:
            return default

    def request_shutdown(self) -> None:
        self._shutdown_requested = True

    def mark_stopped(self) -> None:
        """Persist a clean stopped worker status."""
        self.current_job_id = None
        self._write_status("stopped")

    def _should_interrupt(self, job_id: int) -> bool:
        row = self.database.get_job(job_id) or {}
        if bool(row.get("cancel_requested")):
            return True
        return self._shutdown_requested

    def _on_indicator_progress(self, job_id: int, campaign_id: int, current: int, total: int) -> None:
        self.current_job_id = int(job_id)
        self._write_status("running")
        self.database.heartbeat_job(
            job_id,
            worker_id=self.worker_id,
            progress_current=current,
            progress_total=total,
            result={"indicator_campaign_id": campaign_id},
        )
        self._log("job_progress", {"job_id": job_id, "job_type": JobType.INDICATOR_CAMPAIGN.value, "indicator_campaign_id": campaign_id, "progress_current": current, "progress_total": total})

    def _execute_indicator_campaign_job(self, job: dict[str, Any]) -> dict[str, Any]:
        payload = job.get("payload_json") if isinstance(job.get("payload_json"), dict) else {}
        campaign_id = int(payload.get("campaign_id") or job.get("related_run_id") or 0)
        if campaign_id <= 0:
            raise ValueError(f"Job {job['id']} has no campaign_id")
        self.database.update_job(int(job["id"]), {"related_run_type": "indicator_campaign", "related_run_id": campaign_id})
        status = self.indicator_lab_service.execute_campaign(
            campaign_id,
            worker_id=self.worker_id,
            progress_callback=lambda run_id, current, total: self._on_indicator_progress(int(job["id"]), int(run_id), int(current), int(total)),
            should_interrupt=lambda: self._should_interrupt(int(job["id"])),
        )
        state = str(status.get("state") or JobState.COMPLETED.value).upper()
        result = {
            "indicator_campaign_id": campaign_id,
            "run_state": state,
            "progress": {
                "current": int(status.get("progress_current", 0) or 0),
                "total": int(status.get("progress_total", 0) or 0),
            },
            "best_test_id": status.get("best_test_id"),
        }
        final_state = JobState.PARTIAL.value if state == "PARTIAL" else (JobState.INTERRUPTED.value if state in {"PAUSED", "CANCELED"} else JobState.COMPLETED.value)
        self.database.finalize_job(
            int(job["id"]),
            state=final_state,
            result=result,
            progress_current=result["progress"]["current"],
            progress_total=result["progress"]["total"],
        )
        return result

    def execute_job(self, job: dict[str, Any]) -> dict[str, Any]:
        """Execute one claimed job and persist the final job state."""
        job_id = int(job["id"])
        job_type = normalize_job_type(job.get("job_type"))
        self.current_job_id = job_id
        self._write_status("running")
        self._log("job_started", {"job_id": job_id, "job_type": job_type, "worker_id": self.worker_id})
        self.database.heartbeat_job(job_id, worker_id=self.worker_id, progress_current=int(job.get("progress_current", 0) or 0), progress_total=int(job.get("progress_total", 0) or 0))
        try:
            if job_type == JobType.INDICATOR_CAMPAIGN.value:
                result = self._execute_indicator_campaign_job(job)
            else:
                raise ValueError(f"Unsupported job type: {job_type}")
            self._log("job_completed", {"job_id": job_id, "job_type": job_type, "result": result})
            return result
        except InterruptedError as exc:
            self.database.finalize_job(job_id, state=JobState.INTERRUPTED.value, interrupted_reason=str(exc))
            self._log("job_interrupted", {"job_id": job_id, "job_type": job_type, "reason": str(exc)})
            raise
        except KeyboardInterrupt as exc:
            self.database.finalize_job(job_id, state=JobState.INTERRUPTED.value, interrupted_reason=str(exc) or "keyboard_interrupt")
            self._log("job_interrupted", {"job_id": job_id, "job_type": job_type, "reason": str(exc)})
            raise
        except Exception as exc:
            self.database.finalize_job(job_id, state=JobState.FAILED.value, failure_reason=str(exc))
            self._log("job_failed", {"job_id": job_id, "job_type": job_type, "error": str(exc)})
            self._write_status("error", error=str(exc))
            raise
        finally:
            self.current_job_id = None
            if not self._shutdown_requested:
                self._write_status("running")

    def run_once(self) -> dict[str, Any] | None:
        """Claim and execute at most one job."""
        self._write_status("running")
        self.job_service.reconcile_stale_jobs(stale_seconds=self.heartbeat_stale_seconds)
        job = self.database.claim_next_job(self.worker_id)
        if not job:
            return None
        job_payload = self.job_service.get_job(int(job["id"]))
        self.execute_job(job_payload)
        return job_payload

    def run_forever(self) -> None:
        """Continuously poll and execute queued jobs."""
        self._write_status("starting")
        self._log("worker_started", {"worker_id": self.worker_id, "base_dir": str(self.base_dir)})
        while not self._shutdown_requested:
            try:
                self._write_status("running")
                claimed = self.run_once()
                if claimed is None:
                    time.sleep(self.poll_interval_seconds)
            except KeyboardInterrupt:
                self.request_shutdown()
                break
            except Exception as exc:
                self._write_status("error", error=str(exc))
                self._log("worker_loop_error", {"worker_id": self.worker_id, "error": str(exc)})
                time.sleep(self.poll_interval_seconds)
        self.mark_stopped()
        self._log("worker_stopped", {"worker_id": self.worker_id, "stopped_at": utc_now().isoformat()})
