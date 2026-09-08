"""Worker heartbeat, health, and launch helpers."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable

from utils import ensure_directory, resolve_python_executable, save_json_atomic, to_utc, utc_now


WORKER_STATUS_RELATIVE_PATH = Path("storage") / "worker_status.json"
DEFAULT_WORKER_STALE_SECONDS = 30


def worker_status_path(base_dir: str | Path) -> Path:
    """Return the persisted worker status path."""
    return Path(base_dir) / WORKER_STATUS_RELATIVE_PATH


def write_worker_status(
    base_dir: str | Path,
    *,
    worker_id: str,
    pid: int,
    started_at: str,
    status: str,
    current_job_id: int | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """Persist a compact worker heartbeat/status payload."""
    payload: dict[str, Any] = {
        "worker_id": worker_id,
        "pid": int(pid),
        "started_at": started_at,
        "last_seen_at": utc_now().isoformat(),
        "status": str(status),
        "current_job_id": current_job_id,
    }
    if error:
        payload["error"] = str(error)
    path = worker_status_path(base_dir)
    ensure_directory(path.parent)
    save_json_atomic(path, payload)
    return payload


def read_worker_status(base_dir: str | Path, *, stale_seconds: int = DEFAULT_WORKER_STALE_SECONDS) -> dict[str, Any]:
    """Read worker heartbeat state and classify freshness for dashboard/API use."""
    path = worker_status_path(base_dir)
    if not path.exists():
        return {
            "ok": True,
            "available": False,
            "alive": False,
            "stale": False,
            "state": "unavailable",
            "status": "unavailable",
            "path": str(path),
            "message": "Worker heartbeat file not found",
        }
    try:
        raw = json.loads(path.read_text(encoding="utf-8") or "{}")
    except Exception as exc:
        return {
            "ok": False,
            "available": False,
            "alive": False,
            "stale": True,
            "state": "unavailable",
            "status": "unavailable",
            "path": str(path),
            "message": f"Worker heartbeat unreadable: {exc}",
        }

    now = utc_now()
    last_seen = to_utc(raw.get("last_seen_at"))
    age_seconds = (now - last_seen).total_seconds() if last_seen is not None else None
    stale = last_seen is None or age_seconds > float(stale_seconds)
    raw_status = str(raw.get("status") or "unknown").lower()
    terminal = raw_status in {"stopped", "error"}
    alive = (not stale) and (not terminal)
    state = "running" if alive else ("stale" if stale and not terminal else "unavailable")
    return {
        "ok": True,
        "available": bool(alive),
        "alive": bool(alive),
        "stale": bool(stale),
        "state": state,
        "status": raw_status,
        "path": str(path),
        "age_seconds": age_seconds,
        "worker_id": raw.get("worker_id"),
        "pid": raw.get("pid"),
        "started_at": raw.get("started_at"),
        "last_seen_at": raw.get("last_seen_at"),
        "current_job_id": raw.get("current_job_id"),
        "message": "Worker heartbeat is fresh" if alive else ("Worker heartbeat is stale" if stale else f"Worker status is {raw_status}"),
    }


def _popen_creation_flags() -> int:
    if not sys.platform.startswith("win"):
        return 0
    return int(getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0))


def ensure_worker_running(
    base_dir: str | Path,
    *,
    python_executable: str | None = None,
    stale_seconds: int = DEFAULT_WORKER_STALE_SECONDS,
    wait_seconds: float = 0.0,
    log: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Best-effort detached worker startup with heartbeat-based duplicate avoidance."""
    base = Path(base_dir)
    status = read_worker_status(base, stale_seconds=stale_seconds)
    if status.get("alive"):
        message = f"Backtest worker already active | worker_id={status.get('worker_id')} | pid={status.get('pid')}"
        if log:
            log(message)
        return {"worker_available": True, "launch_attempted": False, "started": False, "status": status, "message": message}

    if str(os.environ.get("DISABLE_BACKTEST_WORKER", "")).strip() == "1":
        message = "Backtest worker startup skipped because DISABLE_BACKTEST_WORKER=1"
        if log:
            log(message)
        return {
            "worker_available": False,
            "launch_attempted": False,
            "started": False,
            "status": status,
            "warning": message,
            "message": message,
        }

    stdout_path = base / "logs" / "worker_stdout.log"
    stderr_path = base / "logs" / "worker_stderr.log"
    ensure_directory(stdout_path.parent)
    resolved_python = python_executable
    if not resolved_python:
        resolved_python, reason = resolve_python_executable(base)
        if log:
            log(f"Backtest worker interpreter resolved to {resolved_python} ({reason})")
    command = [resolved_python or sys.executable, str(base / "scripts" / "run_worker.py")]
    try:
        stdout_handle = stdout_path.open("a", encoding="utf-8")
        stderr_handle = stderr_path.open("a", encoding="utf-8")
        try:
            subprocess.Popen(
                command,
                cwd=str(base),
                stdout=stdout_handle,
                stderr=stderr_handle,
                stdin=subprocess.DEVNULL,
                creationflags=_popen_creation_flags(),
            )
        finally:
            stdout_handle.close()
            stderr_handle.close()
    except Exception as exc:
        message = f"Backtest worker launch failed: {exc}"
        if log:
            log(f"WARNING: {message}")
        return {
            "worker_available": False,
            "launch_attempted": True,
            "started": False,
            "status": status,
            "warning": message,
            "message": message,
        }

    if log:
        log(f"Backtest worker launch requested | command={' '.join(command)}")
    deadline = time.time() + max(0.0, float(wait_seconds))
    latest = status
    while time.time() < deadline:
        latest = read_worker_status(base, stale_seconds=stale_seconds)
        if latest.get("alive"):
            break
        time.sleep(0.25)
    available = True
    message = "Backtest worker launch requested"
    return {
        "worker_available": available,
        "launch_attempted": True,
        "started": True,
        "status": latest,
        "message": message,
    }
