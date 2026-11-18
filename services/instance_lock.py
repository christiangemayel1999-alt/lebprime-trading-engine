"""PID and process-identity aware single-instance lock."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Callable

from utils import utc_now


def pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["powershell", "-NoProfile", "-Command", f"Get-Process -Id {int(pid)} -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            return str(pid) in (result.stdout or "").split()
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def process_belongs_to_bot(pid: int, base_dir: Path) -> bool:
    """Verify that a live PID command line is this workspace's main.py."""
    try:
        if os.name == "nt":
            quoted = str(int(pid))
            result = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    f"(Get-CimInstance Win32_Process -Filter \"ProcessId={quoted}\").CommandLine",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            command_line = (result.stdout or "").strip().lower()
        else:
            command_line = Path(f"/proc/{int(pid)}/cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace").lower()
    except Exception:
        return False
    root = str(base_dir.resolve()).lower()
    return "main.py" in command_line and (root in command_line or Path(root).name.lower() in command_line)


class InstanceLock:
    def __init__(
        self,
        path: Path,
        base_dir: Path,
        *,
        pid: int | None = None,
        is_alive: Callable[[int], bool] = pid_is_alive,
        belongs_to_bot: Callable[[int, Path], bool] = process_belongs_to_bot,
    ) -> None:
        self.path = Path(path)
        self.base_dir = Path(base_dir)
        self.pid = int(pid if pid is not None else os.getpid())
        self.is_alive = is_alive
        self.belongs_to_bot = belongs_to_bot
        self.acquired = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"pid": self.pid, "started_at": utc_now().isoformat(), "base_dir": str(self.base_dir.resolve())}
        while True:
            try:
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(payload, handle)
                self.acquired = True
                return
            except FileExistsError:
                try:
                    existing = json.loads(self.path.read_text(encoding="utf-8"))
                    existing_pid = int(existing.get("pid") or 0)
                except Exception:
                    existing_pid = 0
                if existing_pid and self.is_alive(existing_pid) and self.belongs_to_bot(existing_pid, self.base_dir):
                    raise RuntimeError(f"Another bot instance is already running with PID {existing_pid}")
                self.path.unlink(missing_ok=True)

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            existing = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            existing = {}
        if int(existing.get("pid") or 0) == self.pid:
            self.path.unlink(missing_ok=True)
        self.acquired = False
