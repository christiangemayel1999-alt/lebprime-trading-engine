"""Shared job-type vocabulary for queued execution."""

from __future__ import annotations

from enum import Enum


class JobType(str, Enum):
    BACKTEST = "BACKTEST"
    OOS_MATRIX = "OOS_MATRIX"


def normalize_job_type(value: str | JobType | None, default: str = JobType.BACKTEST.value) -> str:
    """Return a valid uppercase job type with a safe fallback."""
    if isinstance(value, JobType):
        return value.value
    text = str(value or "").strip().upper()
    return text if text in {item.value for item in JobType} else default
