from __future__ import annotations

from enum import Enum


class JobState(str, Enum):
    """Shared job-state vocabulary for dashboard, backtests, and OOS runs."""

    IDLE = "IDLE"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"
    PARTIAL = "PARTIAL"


ACTIVE_JOB_STATES = {JobState.QUEUED.value, JobState.RUNNING.value}
TERMINAL_JOB_STATES = {
    JobState.COMPLETED.value,
    JobState.FAILED.value,
    JobState.INTERRUPTED.value,
    JobState.PARTIAL.value,
}


def normalize_job_state(value: str | JobState | None, default: str = JobState.IDLE.value) -> str:
    """Return a valid uppercase job state with a safe fallback."""

    if isinstance(value, JobState):
        return value.value
    text = str(value or "").strip().upper()
    return text if text in {item.value for item in JobState} else default


def is_active_job_state(value: str | JobState | None) -> bool:
    return normalize_job_state(value) in ACTIVE_JOB_STATES


def is_terminal_job_state(value: str | JobState | None) -> bool:
    return normalize_job_state(value) in TERMINAL_JOB_STATES
