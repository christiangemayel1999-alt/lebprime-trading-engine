"""Shared dashboard/bot control state persisted to disk."""

from __future__ import annotations

import os
import subprocess
import ctypes
import time
from pathlib import Path
from typing import Any
from contextlib import contextmanager

from utils import ensure_directory, load_json, normalize_trading_mode, save_json_atomic, to_utc, utc_now

try:
    import fcntl  # type: ignore
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None  # type: ignore

try:
    import msvcrt  # type: ignore
except ImportError:  # pragma: no cover - non-Windows fallback
    msvcrt = None  # type: ignore


def default_control_state() -> dict[str, Any]:
    """Return the default dashboard control state."""
    return {
        "desired_state": "STOPPED",
        "bot_running": False,
        "bot_pid": None,
        "bot_started_at": None,
        "bot_stopped_at": None,
        "execution_paused": False,
        "kill_switch": False,
        "kill_switch_reason": None,
        "auto_execution_enabled": True,
        "signal_generation_enabled": True,
        "request_shutdown": False,
        "request_reload_config": False,
        "pending_restart": False,
        "restart_required_reasons": [],
        "last_config_apply": None,
        "last_command": None,
        "last_command_at": None,
        "last_command_by": None,
        "last_error": None,
        "runtime": {
            "mode": "DRY_RUN",
            "heartbeat_at": None,
            "heartbeat_status": None,
            "last_execution_result": None,
            "last_signal": None,
            "session": None,
            "regime": None,
            "spread_points": None,
            "mt5_connected": False,
            "trade_allowed": False,
            "account_login": None,
            "resolved_runtime": {},
        },
    }


class ControlStateService:
    """Load and persist shared control state for the bot and dashboard."""

    def __init__(self, base_dir: str | Path, relative_path: str = "storage/control_state.json") -> None:
        self.base_dir = Path(base_dir)
        self.path = self.base_dir / relative_path
        ensure_directory(self.path.parent)
        self._lock_path = self.path.parent / ".control_state.lock"
        ensure_directory(self._lock_path.parent)

    @contextmanager
    def _acquire_lock(self, timeout_seconds: float = 5.0):
        """Acquire an exclusive file-level lock for atomic state operations.
        
        This ensures that concurrent reads/writes from bot and dashboard processes
        don't create race conditions where state is half-read or half-written.
        """
        lock_file = None
        lock_acquired = False
        start_time = time.time()
        
        try:
            # Open the lock file in binary append mode so we can use a real OS lock.
            lock_file = open(self._lock_path, "a+b")
            if lock_file.tell() == 0:
                lock_file.write(b"0")
                lock_file.flush()

            # Try to acquire exclusive lock with timeout.
            while not lock_acquired and (time.time() - start_time) < timeout_seconds:
                try:
                    lock_file.seek(0)
                    if fcntl is not None and hasattr(fcntl, "flock"):  # Unix-like systems
                        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        lock_acquired = True
                    elif msvcrt is not None and os.name == "nt":  # Windows
                        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                        lock_acquired = True
                    else:
                        lock_acquired = True
                except (IOError, OSError):
                    time.sleep(0.01)

            if not lock_acquired:
                raise TimeoutError(f"Could not acquire control_state lock after {timeout_seconds}s")

            yield

        finally:
            if lock_file:
                try:
                    if lock_acquired:
                        lock_file.seek(0)
                        if fcntl is not None and hasattr(fcntl, "flock"):
                            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                        elif msvcrt is not None and os.name == "nt":
                            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                    lock_file.close()
                except Exception:
                    pass

    def load(self) -> dict[str, Any]:
        """Load control state from disk atomically.
        
        Acquires file-level lock to ensure we don't read while another process
        is writing, preventing partial reads.
        """
        with self._acquire_lock():
            return self._merge_defaults(load_json(self.path, default_control_state()))

    def save(self, state: dict[str, Any]) -> dict[str, Any]:
        """Persist control state atomically with file-level lock.
        
        Ensures exclusive access during write to prevent dashboard and bot
        from partially overwriting each other's changes.
        """
        with self._acquire_lock():
            merged = self._merge_defaults(state)
            save_json_atomic(self.path, merged)
            return merged

    def update(self, updates: dict[str, Any]) -> dict[str, Any]:
        """Apply top-level updates to control state atomically.
        
        Read-modify-write is atomic thanks to the file lock.
        """
        with self._acquire_lock():
            state = self._merge_defaults(load_json(self.path, default_control_state()))
            self._deep_merge(state, updates)
            merged = self._merge_defaults(state)
            save_json_atomic(self.path, merged)
            return merged

    def mark_command(self, command: str, actor: str, **extra: Any) -> dict[str, Any]:
        """Record the latest dashboard command atomically."""
        with self._acquire_lock():
            state = self._merge_defaults(load_json(self.path, default_control_state()))
            state["last_command"] = command
            state["last_command_at"] = utc_now().isoformat()
            state["last_command_by"] = actor
            self._deep_merge(state, extra)
            merged = self._merge_defaults(state)
            save_json_atomic(self.path, merged)
            return merged

    def mark_process_started(self, pid: int, mode: str) -> dict[str, Any]:
        """Mark the bot process as started atomically."""
        with self._acquire_lock():
            state = self._merge_defaults(load_json(self.path, default_control_state()))
            normalized_mode = normalize_trading_mode(mode)
            state["desired_state"] = "RUNNING"
            state["bot_running"] = True
            state["bot_pid"] = int(pid)
            state["bot_started_at"] = utc_now().isoformat()
            state["bot_stopped_at"] = None
            state["request_shutdown"] = False
            state["runtime"]["mode"] = normalized_mode
            state["runtime"]["heartbeat_status"] = "STARTING"
            state["runtime"]["heartbeat_at"] = utc_now().isoformat()
            merged = self._merge_defaults(state)
            save_json_atomic(self.path, merged)
            return merged

    def mark_process_stopped(self, reason: str | None = None) -> dict[str, Any]:
        """Mark the bot process as stopped atomically."""
        with self._acquire_lock():
            state = self._merge_defaults(load_json(self.path, default_control_state()))
            state["desired_state"] = "STOPPED"
            state["bot_running"] = False
            state["bot_pid"] = None
            state["bot_stopped_at"] = utc_now().isoformat()
            state["request_shutdown"] = False
            state["runtime"]["heartbeat_status"] = "STOPPED"
            state["runtime"]["heartbeat_at"] = utc_now().isoformat()
            if reason:
                state["last_error"] = reason
            merged = self._merge_defaults(state)
            save_json_atomic(self.path, merged)
            return merged

    def record_runtime_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Persist runtime status from the bot loop atomically."""
        with self._acquire_lock():
            state = self._merge_defaults(load_json(self.path, default_control_state()))
            runtime = state.setdefault("runtime", {})
            top_level_fields = {
                "bot_pid",
                "bot_running",
                "desired_state",
                "execution_paused",
                "kill_switch",
                "kill_switch_reason",
                "auto_execution_enabled",
                "signal_generation_enabled",
                "request_shutdown",
                "request_reload_config",
                "pending_restart",
                "restart_required_reasons",
                "bot_started_at",
                "bot_stopped_at",
                "last_error",
            }
            for key, value in snapshot.items():
                if key in top_level_fields:
                    state[key] = value
                else:
                    runtime[key] = value
            runtime["heartbeat_at"] = snapshot.get("heartbeat_at") or utc_now().isoformat()
            pid = snapshot.get("bot_pid")
            if pid is not None:
                state["bot_pid"] = int(pid)
            if "bot_running" in snapshot:
                state["bot_running"] = bool(snapshot.get("bot_running"))
            elif "heartbeat_status" in snapshot:
                state["bot_running"] = str(snapshot.get("heartbeat_status") or "").upper() != "STOPPED"
            if "desired_state" in snapshot:
                state["desired_state"] = str(snapshot.get("desired_state") or state.get("desired_state") or "RUNNING")
            elif "heartbeat_status" in snapshot:
                state["desired_state"] = "STOPPED" if str(snapshot.get("heartbeat_status") or "").upper() == "STOPPED" else "RUNNING"
            merged = self._merge_defaults(state)
            save_json_atomic(self.path, merged)
            return merged

    def is_process_alive(self, pid: int | None) -> bool:
        """Return whether a stored bot pid still appears alive."""
        if not pid:
            return False
        try:
            kernel32 = ctypes.windll.kernel32
            process = kernel32.OpenProcess(0x1000, False, int(pid))
            if process:
                try:
                    exit_code = ctypes.c_ulong()
                    if kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code)):
                        return int(exit_code.value) == 259
                finally:
                    kernel32.CloseHandle(process)
                return False
        except Exception:
            pass
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            output = (result.stdout or "").strip().lower()
            if not output or "no tasks are running" in output:
                return False
            return str(int(pid)) in output
        except Exception:
            try:
                os.kill(int(pid), 0)
            except Exception:
                return False
            return True

    def summarize(self, stale_after_seconds: int = 90) -> dict[str, Any]:
        """Return a compact status summary."""
        state = self.load()
        runtime = state.get("runtime", {})
        heartbeat_at = to_utc(runtime.get("heartbeat_at"))
        stale = True
        if heartbeat_at is not None:
            stale = (utc_now() - heartbeat_at).total_seconds() > stale_after_seconds
        pid = state.get("bot_pid")
        process_alive = self.is_process_alive(pid)
        if not process_alive and (state.get("bot_running") or pid):
            state = self.mark_process_stopped("stale_pid_detected")
            runtime = state.get("runtime", {})
            pid = state.get("bot_pid")
            process_alive = False
        summary = {**state, "process_alive": process_alive, "heartbeat_stale": stale}
        if not process_alive:
            summary["bot_running"] = False
            summary["desired_state"] = "STOPPED"
        return summary

    def _merge_defaults(self, state: dict[str, Any]) -> dict[str, Any]:
        """Ensure the control state has all required keys."""
        merged = default_control_state()
        merged.update(state or {})
        runtime = merged.get("runtime")
        merged["runtime"] = {**default_control_state()["runtime"], **(runtime if isinstance(runtime, dict) else {})}
        return merged

    def _deep_merge(self, target: dict[str, Any], updates: dict[str, Any]) -> None:
        """Recursively merge nested dictionaries into a target state payload."""
        for key, value in (updates or {}).items():
            if isinstance(value, dict) and isinstance(target.get(key), dict):
                self._deep_merge(target[key], value)
            else:
                target[key] = value
