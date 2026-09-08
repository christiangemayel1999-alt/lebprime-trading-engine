"""Process and runtime controls for the trading bot dashboard."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from services.audit_service import AuditService
from services.config_manager import ConfigManager
from services.control_state import ControlStateService
from utils import load_runtime_config


class BotControlService:
    """Coordinate config updates, control state, and bot process lifecycle."""

    def __init__(
        self,
        base_dir: str | Path,
        config_manager: ConfigManager,
        control_state: ControlStateService,
        audit_service: AuditService,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.config_manager = config_manager
        self.control_state = control_state
        self.audit_service = audit_service

    def start_bot(self, actor: str) -> dict[str, object]:
        """Launch the bot in a detached process if it is not already running."""
        summary = self.control_state.summarize()
        if summary["process_alive"]:
            return {"ok": True, "message": "Bot is already running", "pid": summary.get("bot_pid")}

        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

        process = subprocess.Popen(  # noqa: S603
            [sys.executable, "main.py"],
            cwd=str(self.base_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )
        # Use the persisted config mode so a dashboard/Telegram switch survives restart.
        config = load_runtime_config(self.base_dir, require_mt5_credentials=False, apply_mode_env_override=False)
        state = self.control_state.mark_process_started(process.pid, str(config["bot"]["trading_mode"]))
        self.audit_service.log_action(actor, "start_bot", "bot", {"pid": process.pid})
        return {"ok": True, "message": "Bot start requested", "pid": process.pid, "control_state": state}

    def stop_bot(self, actor: str, force: bool = False) -> dict[str, object]:
        """Request bot shutdown, optionally forcing the process to terminate."""
        summary = self.control_state.summarize()
        if not summary["process_alive"]:
            state = self.control_state.mark_process_stopped("stop_requested_but_pid_dead")
            self.audit_service.log_action(actor, "stop_bot", "bot", {"force": force, "pid": summary.get("bot_pid"), "already_stopped": True})
            return {"ok": True, "message": "Bot is already stopped", "control_state": state}

        state = self.control_state.mark_command(
            "stop_bot",
            actor,
            desired_state="STOPPED",
            request_shutdown=True,
        )
        pid = state.get("bot_pid")
        if force:
            self._force_stop_process(pid)
            state = self.control_state.mark_process_stopped("force_stop_requested")
            self.audit_service.log_action(actor, "stop_bot", "bot", {"force": True, "pid": pid, "forced": True})
            return {"ok": True, "message": "Bot force stop requested", "control_state": state}

        if self._wait_for_graceful_shutdown(pid, timeout_seconds=15):
            state = self.control_state.mark_process_stopped("graceful_stop_complete")
            self.audit_service.log_action(actor, "stop_bot", "bot", {"force": False, "pid": pid, "graceful": True})
            return {"ok": True, "message": "Bot stop requested and confirmed", "control_state": state}

        self._force_stop_process(pid)
        state = self.control_state.mark_process_stopped("graceful_stop_timed_out")
        self.audit_service.log_action(actor, "stop_bot", "bot", {"force": False, "pid": pid, "graceful": False, "forced_after_timeout": True})
        return {"ok": True, "message": "Bot stop requested; forced stop after timeout", "control_state": state}

    def pause_execution(self, actor: str) -> dict[str, object]:
        """Pause live order execution while leaving the bot running."""
        state = self.control_state.mark_command("pause_execution", actor, execution_paused=True)
        self.audit_service.log_action(actor, "pause_execution", "execution", {})
        return {"ok": True, "message": "Execution paused", "control_state": state}

    def resume_execution(self, actor: str) -> dict[str, object]:
        """Resume live order execution."""
        state = self.control_state.mark_command("resume_execution", actor, execution_paused=False)
        self.audit_service.log_action(actor, "resume_execution", "execution", {})
        return {"ok": True, "message": "Execution resumed", "control_state": state}

    def set_kill_switch(self, actor: str, enabled: bool, reason: str | None = None) -> dict[str, object]:
        """Toggle the kill switch for new trades."""
        state = self.control_state.mark_command(
            "kill_switch_on" if enabled else "kill_switch_off",
            actor,
            kill_switch=bool(enabled),
            kill_switch_reason=reason if enabled else None,
        )
        self.audit_service.log_action(actor, "kill_switch", "execution", {"enabled": enabled, "reason": reason})
        return {"ok": True, "message": "Kill switch updated", "control_state": state}

    def set_auto_execution(self, actor: str, enabled: bool) -> dict[str, object]:
        """Toggle order execution while keeping signal generation active."""
        state = self.control_state.mark_command("auto_execution", actor, auto_execution_enabled=bool(enabled))
        self.audit_service.log_action(actor, "auto_execution", "execution", {"enabled": enabled})
        return {"ok": True, "message": "Auto execution updated", "control_state": state}

    def set_signal_generation(self, actor: str, enabled: bool) -> dict[str, object]:
        """Toggle new signal/entry generation while preserving position management."""
        state = self.control_state.mark_command("signal_generation", actor, signal_generation_enabled=bool(enabled))
        self.audit_service.log_action(actor, "signal_generation", "runtime", {"enabled": enabled})
        return {"ok": True, "message": "New entry generation updated", "control_state": state}

    def request_config_reload(self, actor: str) -> dict[str, object]:
        """Ask the running bot to reload config on the next cycle."""
        state = self.control_state.mark_command("reload_config", actor, request_reload_config=True)
        self.audit_service.log_action(actor, "reload_config", "config", {})
        return {"ok": True, "message": "Config reload requested", "control_state": state}

    def _wait_for_graceful_shutdown(self, pid: int | None, timeout_seconds: int = 15) -> bool:
        """Wait briefly for the running process to exit on its own."""
        if not pid or not self.control_state.is_process_alive(pid):
            return True
        deadline = time.monotonic() + max(1, int(timeout_seconds))
        while time.monotonic() < deadline:
            if not self.control_state.is_process_alive(pid):
                return True
            time.sleep(0.5)
        return not self.control_state.is_process_alive(pid)

    def _force_stop_process(self, pid: int | None) -> None:
        """Force terminate a running bot process tree on Windows."""
        if not pid:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(int(pid)), "/T", "/F"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
                return
            os.kill(int(pid), signal.SIGTERM)
        except Exception:
            try:
                os.kill(int(pid), signal.SIGKILL)
            except Exception:
                pass
