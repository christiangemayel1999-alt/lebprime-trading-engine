"""Windows startup bootstrap for the trading bot stack.

This helper ensures MetaTrader 5 is running before the bot starts and launches
the dashboard in the background when requested.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from mt5_connector import MT5Connector
from utils import ensure_directory, load_runtime_config, mt5_runtime_config_presence, resolve_python_executable, utc_now
from services.worker_control import ensure_worker_running


def parse_args() -> argparse.Namespace:
    """Parse bootstrap command line arguments."""
    parser = argparse.ArgumentParser(description="Bootstrap MT5 and dashboard before bot startup")
    parser.add_argument("--no-mt5", action="store_true", help="Skip MT5 launch checks")
    parser.add_argument("--no-dashboard", action="store_true", help="Skip dashboard launch")
    parser.add_argument("--wait-seconds", type=int, default=45, help="How long to wait for MT5/dashboard readiness")
    return parser.parse_args()


def _launcher_log_path() -> Path:
    """Return the shared launcher log path."""
    return PROJECT_ROOT / "logs" / "launcher.log"


def _log(message: str) -> None:
    """Write a launcher message to console and log file."""
    ensure_directory(_launcher_log_path().parent)
    line = f"[{utc_now().strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    try:
        with _launcher_log_path().open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except Exception:
        pass
    print(line)


class _BootstrapLogger:
    """Adapter that lets MT5Connector emit messages into the bootstrap log."""

    def __init__(self, log_func: Any) -> None:
        self._log_func = log_func

    def info(self, message: str) -> None:
        self._log_func(f"INFO: {message}")

    def warning(self, message: str) -> None:
        self._log_func(f"WARNING: {message}")

    def error(self, message: str) -> None:
        self._log_func(f"ERROR: {message}")

    def structured(self, category: str, payload: Any, level: str = "INFO") -> None:
        try:
            rendered = json.dumps(payload, sort_keys=True, default=str)
        except Exception:
            rendered = str(payload)
        self._log_func(f"{level}: {category} {rendered}")


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    """Return whether a TCP port already accepts connections."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _dashboard_command(host: str, port: int, python_executable: str) -> list[str]:
    """Return the command used to launch the dashboard."""
    return [python_executable, str(PROJECT_ROOT / "scripts" / "run_dashboard.py"), "--host", host, "--port", str(port)]


def _start_worker(wait_seconds: int = 5) -> bool:
    """Ensure the DB-backed backtest/OOS worker is available."""
    python_executable, python_reason = resolve_python_executable(PROJECT_ROOT)
    _log(f"Backtest worker interpreter resolved to {python_executable} ({python_reason})")
    result = ensure_worker_running(
        PROJECT_ROOT,
        python_executable=python_executable,
        wait_seconds=min(max(float(wait_seconds), 0.0), 5.0),
        log=_log,
    )
    if result.get("worker_available") or result.get("started"):
        return True
    warning = result.get("warning") or result.get("message") or "worker unavailable"
    _log(f"WARNING: Backtest worker is not available: {warning}")
    return False


def _start_dashboard(config: dict[str, Any], wait_seconds: int) -> bool:
    """Launch the dashboard if it is not already listening."""
    dashboard_cfg = config.get("dashboard", {})
    host = str(dashboard_cfg.get("host", "127.0.0.1"))
    port = int(dashboard_cfg.get("port") or dashboard_cfg.get("default_port", 8501))
    probe_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    if _port_open(probe_host, port):
        _log(f"Dashboard already listening on {host}:{port}")
        return True

    python_executable, python_reason = resolve_python_executable(PROJECT_ROOT)
    _log(f"Dashboard interpreter resolved to {python_executable} ({python_reason})")
    stdout_path = PROJECT_ROOT / "logs" / "dashboard_stdout.log"
    stderr_path = PROJECT_ROOT / "logs" / "dashboard_stderr.log"
    ensure_directory(stdout_path.parent)
    try:
        stdout_handle = stdout_path.open("a", encoding="utf-8")
        stderr_handle = stderr_path.open("a", encoding="utf-8")
        try:
            subprocess.Popen(
                _dashboard_command(host, port, python_executable),
                cwd=str(PROJECT_ROOT),
                stdout=stdout_handle,
                stderr=stderr_handle,
            )
        finally:
            stdout_handle.close()
            stderr_handle.close()
        _log(f"Dashboard launch requested on {host}:{port}")
    except Exception as exc:
        _log(f"WARNING: Dashboard launch failed: {exc}")
        return False

    deadline = time.time() + max(10, wait_seconds)
    while time.time() < deadline:
        if _port_open(probe_host, port):
            _log(f"Dashboard is ready on {host}:{port}")
            return True
        time.sleep(1.0)

    _log(f"WARNING: Dashboard did not become ready within {wait_seconds}s")
    return False


def _mt5_candidate_paths(config: dict[str, Any]) -> list[Path]:
    """Return candidate MT5 executable paths."""
    mt5_cfg = config.get("mt5", {})
    candidates: list[Path] = []

    terminal_path = str(mt5_cfg.get("terminal_path") or os.environ.get("MT5_PATH") or "").strip()
    if terminal_path:
        path = Path(terminal_path)
        if path.is_dir():
            candidates.extend([path / "terminal64.exe", path / "terminal.exe"])
        else:
            candidates.append(path)

    program_files = os.environ.get("ProgramFiles")
    program_files_x86 = os.environ.get("ProgramFiles(x86)")
    local_appdata = os.environ.get("LOCALAPPDATA")
    search_roots = [value for value in [program_files, program_files_x86, local_appdata] if value]
    for root in search_roots:
        root_path = Path(root)
        candidates.extend(
            [
                root_path / "MetaTrader 5" / "terminal64.exe",
                root_path / "MetaTrader 5" / "terminal.exe",
                root_path / "Programs" / "MetaTrader 5" / "terminal64.exe",
                root_path / "Programs" / "MetaTrader 5" / "terminal.exe",
            ]
        )

    # De-duplicate while preserving order.
    seen: set[str] = set()
    unique_candidates: list[Path] = []
    for candidate in candidates:
        key = str(candidate).lower()
        if key not in seen:
            seen.add(key)
            unique_candidates.append(candidate)
    return unique_candidates


def _process_running_by_name(image_name: str) -> bool:
    """Return whether a process image appears in tasklist output."""
    try:
        process_name = Path(image_name).stem
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"(Get-Process -Name '{process_name}' -ErrorAction SilentlyContinue | Measure-Object).Count",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        output = (result.stdout or "").strip()
        if output.isdigit():
            return int(output) > 0
    except Exception:
        pass

    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image_name}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        output = (result.stdout or "").strip().lower()
        if not output or "no tasks are running" in output:
            return False
        return image_name.lower() in output
    except Exception:
        return False


def _mt5_running() -> bool:
    """Check whether a likely MT5 terminal process is already running."""
    return any(_process_running_by_name(image) for image in ["terminal64.exe", "terminal.exe"])


def _probe_mt5_ready(config: dict[str, Any], wait_seconds: int) -> tuple[bool, str]:
    """Probe MT5 bridge readiness through the Python API."""
    connector = MT5Connector(config, _BootstrapLogger(_log))
    startup_bars = max(50, min(120, int(config.get("timeframes", {}).get("bars_to_fetch", 320))))
    startup_ok = connector.initialize()
    if not startup_ok:
        return False, "initialize_failure"
    try:
        readiness_ok, readiness_reason = connector.validate_startup(bars=startup_bars, require_live_tick=False)
    except Exception as exc:
        return False, f"startup_probe_exception: {exc}"
    if not readiness_ok:
        return False, readiness_reason
    try:
        connection_ok, connection_reason, _snapshot = connector.verify_connection_state()
    except Exception as exc:
        return False, f"connection_probe_exception: {exc}"
    if not connection_ok:
        return False, connection_reason
    return True, "ready"


def _start_mt5(config: dict[str, Any], wait_seconds: int) -> bool:
    """Ensure MT5 is open before the bot starts."""
    if _mt5_running():
        _log("MT5 process detected; probing Python bridge readiness")
    else:
        candidates = _mt5_candidate_paths(config)
        executable = next((candidate for candidate in candidates if candidate.exists()), None)
        if executable is None:
            _log("ERROR: No MT5 executable was found. Set mt5.terminal_path or MT5_PATH.")
            return False

        try:
            subprocess.Popen([str(executable)], cwd=str(executable.parent))
            _log(f"MT5 launch requested: {executable}")
        except Exception as exc:
            _log(f"ERROR: Failed to launch MT5: {exc}")
            return False

    deadline = time.time() + max(15, wait_seconds)
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        ready, reason = _probe_mt5_ready(config, wait_seconds)
        if ready:
            _log("MT5 bridge is ready")
            return True
        _log(f"WARNING: MT5 readiness probe failed on attempt {attempt}: {reason}")
        time.sleep(1.0)

    _log(f"ERROR: MT5 did not become ready within {wait_seconds}s")
    return False


def main() -> int:
    """Run the bootstrap sequence."""
    args = parse_args()
    config = load_runtime_config(PROJECT_ROOT, require_mt5_credentials=False)
    _log(f"MT5 runtime config presence: {mt5_runtime_config_presence(config)}")
    python_executable, python_reason = resolve_python_executable(PROJECT_ROOT)
    _log(f"Bootstrap Python interpreter resolved to {python_executable} ({python_reason})")

    mt5_ok = True
    dashboard_ok = True

    if not args.no_mt5:
        mt5_ok = _start_mt5(config, args.wait_seconds)

    if not args.no_dashboard:
        _start_worker(args.wait_seconds)
        dashboard_ok = _start_dashboard(config, args.wait_seconds)

    if not mt5_ok:
        return 1

    # Dashboard startup is best-effort; the bot can still run if the server
    # is already up or the local browser environment is unavailable.
    return 0 if dashboard_ok or args.no_dashboard else 0


if __name__ == "__main__":
    raise SystemExit(main())
