"""Stable dashboard launcher for Windows batch wrappers."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import uvicorn

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from utils import load_runtime_config, resolve_python_executable
from services.worker_control import ensure_worker_running


def parse_args() -> argparse.Namespace:
    """Parse CLI args for dashboard startup."""
    parser = argparse.ArgumentParser(description="Run the trading bot dashboard")
    parser.add_argument("--host", default=None, help="Override dashboard host")
    parser.add_argument("--port", type=int, default=None, help="Override dashboard port")
    parser.add_argument("--reload", action="store_true", help="Enable uvicorn reload")
    parser.add_argument("--no-worker", action="store_true", help="Do not auto-start the backtest/OOS worker")
    return parser.parse_args()


def main() -> None:
    """Run uvicorn for the integrated dashboard app."""
    root = PROJECT_ROOT
    config = load_runtime_config(root, require_mt5_credentials=False)
    dashboard_cfg = config.get("dashboard", {})
    args = parse_args()
    host = args.host or str(dashboard_cfg.get("host", "0.0.0.0"))
    port = int(args.port or dashboard_cfg.get("port") or dashboard_cfg.get("default_port", 8501))
    if not args.no_worker:
        worker_python, worker_reason = resolve_python_executable(root)
        print(f"INFO: Backtest worker interpreter resolved to {worker_python} ({worker_reason})", file=sys.stderr)
        result = ensure_worker_running(root, python_executable=worker_python, wait_seconds=2.0)
        if not result.get("worker_available") and result.get("warning"):
            print(f"WARNING: {result['warning']}", file=sys.stderr)
    uvicorn.run("dashboard.app:app", host=host, port=port, reload=bool(args.reload))


if __name__ == "__main__":
    main()
