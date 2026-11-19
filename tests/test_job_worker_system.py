from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates

from dashboard.routes import create_router
from dashboard.services import DashboardDataService
from services.config_manager import ConfigManager
from services.database import DatabaseService
from services.job_service import JobService
from services.worker_control import ensure_worker_running, read_worker_status, write_worker_status
from services.worker import WorkerService
from trading_bot.core.job_state import JobState
from utils import resolve_python_executable


def _worker_base(tmp_path: Path) -> Path:
    base = tmp_path / "worker_env"
    base.mkdir(parents=True, exist_ok=True)
    config = json.loads(Path("config.json").read_text(encoding="utf-8"))
    config["storage"]["database_path"] = "storage/test_jobs.db"
    (base / "storage").mkdir(parents=True, exist_ok=True)
    (base / "config.json").write_text(json.dumps(config), encoding="utf-8")
    return base


def _request_json(app: FastAPI, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    async def _request() -> tuple[int, dict]:
        messages: list[dict] = []
        payload_bytes = json.dumps(body or {}).encode("utf-8")

        async def receive() -> dict:
            return {"type": "http.request", "body": payload_bytes if method != "GET" else b"", "more_body": False}

        async def send(message: dict) -> None:
            messages.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path.split("?")[0],
            "raw_path": path.split("?")[0].encode("ascii"),
            "query_string": path.split("?", 1)[1].encode("ascii") if "?" in path else b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "root_path": "",
        }
        await app(scope, receive, send)
        status = next(int(message["status"]) for message in messages if message["type"] == "http.response.start")
        body_bytes = b"".join(bytes(message.get("body", b"")) for message in messages if message["type"] == "http.response.body")
        payload = json.loads(body_bytes.decode("utf-8") or "{}")
        return status, payload

    return asyncio.run(_request())


def test_running_worker_keeps_fresh_heartbeat(tmp_path: Path) -> None:
    base = _worker_base(tmp_path)
    worker = WorkerService(base, poll_interval_seconds=0.01, heartbeat_stale_seconds=60)
    assert worker.run_once() is None
    status = read_worker_status(base, stale_seconds=60)
    assert status["alive"] is True
    assert status["state"] == "running"
    assert status["pid"] == __import__("os").getpid()


def test_python_resolver_prefers_proven_current_interpreter_over_launcher(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("utils.shutil.which", lambda name: "C:/Windows/py.exe" if name == "py" else None)
    executable, reason = resolve_python_executable(tmp_path)
    assert Path(executable).resolve() == Path(__import__("sys").executable).resolve()
    assert reason == "current interpreter"


def test_worker_marks_failed_job_on_exception(tmp_path: Path, monkeypatch) -> None:
    base = _worker_base(tmp_path)
    worker = WorkerService(base, poll_interval_seconds=0.01, heartbeat_stale_seconds=60)
    job_id = worker.database.create_job({"job_type": "INDICATOR_CAMPAIGN", "state": "QUEUED", "payload_json": {"campaign_id": 1}})

    def boom(*_args, **_kwargs):
        raise RuntimeError("campaign_boom")

    monkeypatch.setattr(worker.indicator_lab_service, "execute_campaign", boom)
    try:
        worker.run_once()
    except RuntimeError:
        pass
    final_job = worker.job_service.get_job(int(job_id))
    assert final_job["state"] == "FAILED"
    assert "campaign_boom" in str(final_job["failure_reason"])


def test_job_service_reconciles_stale_running_job(tmp_path: Path) -> None:
    base = _worker_base(tmp_path)
    db = DatabaseService(base / "storage" / "test_jobs.db")
    config_manager = ConfigManager(base)
    job_service = JobService(base, config_manager, db)
    job_id = db.create_job(
        {
            "job_type": "INDICATOR_CAMPAIGN",
            "state": "RUNNING",
            "last_heartbeat_at": "2020-01-01T00:00:00+00:00",
            "payload_json": {"request": {"symbol": "XAUUSD"}},
        }
    )
    job_service.reconcile_stale_jobs(stale_seconds=1)
    job = job_service.get_job(job_id)
    assert job["state"] == "INTERRUPTED"
    assert job["interrupted_reason"] == "job_heartbeat_stale"


def test_job_api_endpoints_return_job_payloads(tmp_path: Path) -> None:
    class StubJobService:
        def list_jobs(self, **kwargs):
            return [{"id": 7, "job_type": "BACKTEST", "state": "RUNNING", "progress_current": 5, "progress_total": 10}]

        def get_job(self, job_id):
            return {"id": job_id, "job_type": "BACKTEST", "state": "RUNNING"}

        def resume_job(self, job_id, actor="dashboard"):
            return {"id": 9, "job_type": "BACKTEST", "state": "QUEUED", "parent_job_id": job_id}

        def cancel_job(self, job_id, actor="dashboard"):
            return {"id": job_id, "job_type": "BACKTEST", "state": "INTERRUPTED"}

    services = {
        "config_manager": SimpleNamespace(dashboard_view=lambda: {"storage": {"database_path": "bot.db"}, "dashboard": {"stale_heartbeat_seconds": 120, "readonly_mode": False}, "bot": {"trading_mode": "DRY_RUN"}}),
        "control_state": SimpleNamespace(summarize=lambda stale_after_seconds=120: {"process_alive": False, "bot_running": False, "runtime": {"mode": "DRY_RUN"}}),
        "control_plane": SimpleNamespace(get_section=lambda *_args, **_kwargs: {}, get_config=lambda: {}, update_config=lambda **_kwargs: {}, clear_pending_restart=lambda *_args, **_kwargs: {}),
        "bot_control": SimpleNamespace(request_config_reload=lambda *_args, **_kwargs: {"ok": True}),
        "operator_service": SimpleNamespace(),
        "manual_trade_service": SimpleNamespace(),
        "dashboard_data": SimpleNamespace(status_snapshot=lambda *_args, **_kwargs: {"metrics": {}, "backtest_runs": []}, list_backtest_runs=lambda limit=20: [], get_backtest_run_bundle=lambda run_id: {}),
        "database": DatabaseService(tmp_path / "bot.db"),
        "audit_service": SimpleNamespace(log_action=lambda *_args, **_kwargs: None),
        "dashboard_auth": SimpleNamespace(dependency=lambda: None, public_status=lambda: {"enabled": False, "username": ""}),
        "base_dir": tmp_path,
        "oos_service": SimpleNamespace(get_status=lambda oos_run_id=None: {"ok": True, "run": None, "history": [], "state": "IDLE"}, history=lambda limit=20: {"ok": True, "runs": []}, rebuild_report=lambda oos_run_id: {"ok": True}),
        "job_service": StubJobService(),
    }
    app = FastAPI()
    templates = Jinja2Templates(directory=str((Path(__file__).resolve().parents[1] / "dashboard" / "templates")))
    app.include_router(create_router(services, templates))

    for removed in ("/api/jobs/backtest", "/api/jobs/oos"):
        status_code, _ = _request_json(app, "POST", removed, {"actor": "dashboard"})
        assert status_code in {404, 405}

    status_code, payload = _request_json(app, "GET", "/api/jobs/7")
    assert status_code == 200
    assert payload["job"]["id"] == 7

    status_code, payload = _request_json(app, "GET", "/api/jobs")
    assert status_code == 200
    assert payload["jobs"][0]["id"] == 7

    status_code, payload = _request_json(app, "POST", "/api/jobs/7/resume", {"actor": "dashboard", "job_id": 7})
    assert status_code == 200
    assert payload["job"]["id"] == 9

    status_code, payload = _request_json(app, "POST", "/api/jobs/7/cancel", {"actor": "dashboard", "job_id": 7})
    assert status_code == 200
    assert payload["job"]["state"] == "INTERRUPTED"


def test_worker_status_endpoint_unavailable_without_heartbeat(tmp_path: Path) -> None:
    db = DatabaseService(tmp_path / "bot.db")
    dashboard_data = DashboardDataService(tmp_path, db)
    services = {
        "config_manager": SimpleNamespace(dashboard_view=lambda: {"storage": {"database_path": "bot.db"}, "dashboard": {"stale_heartbeat_seconds": 120, "readonly_mode": False}, "bot": {"trading_mode": "DRY_RUN"}}),
        "control_state": SimpleNamespace(summarize=lambda stale_after_seconds=120: {"process_alive": False, "bot_running": False, "runtime": {"mode": "DRY_RUN"}}),
        "control_plane": SimpleNamespace(get_section=lambda *_args, **_kwargs: {}, get_config=lambda: {}, update_config=lambda **_kwargs: {}, clear_pending_restart=lambda *_args, **_kwargs: {}),
        "bot_control": SimpleNamespace(request_config_reload=lambda *_args, **_kwargs: {"ok": True}),
        "operator_service": SimpleNamespace(),
        "manual_trade_service": SimpleNamespace(),
        "dashboard_data": dashboard_data,
        "database": db,
        "audit_service": SimpleNamespace(log_action=lambda *_args, **_kwargs: None),
        "dashboard_auth": SimpleNamespace(dependency=lambda: None, public_status=lambda: {"enabled": False, "username": ""}),
        "base_dir": tmp_path,
        "oos_service": SimpleNamespace(get_status=lambda oos_run_id=None: {"ok": True, "run": None, "history": [], "state": "IDLE"}),
        "job_service": SimpleNamespace(list_jobs=lambda **_kwargs: []),
    }
    app = FastAPI()
    templates = Jinja2Templates(directory=str((Path(__file__).resolve().parents[1] / "dashboard" / "templates")))
    app.include_router(create_router(services, templates))

    status_code, payload = _request_json(app, "GET", "/api/worker/status")
    assert status_code == 200
    assert payload["worker"]["state"] == "unavailable"
    assert payload["worker"]["available"] is False


def test_worker_status_endpoint_running_with_fresh_heartbeat(tmp_path: Path) -> None:
    write_worker_status(tmp_path, worker_id="worker-test", pid=123, started_at="2026-04-21T00:00:00+00:00", status="running", current_job_id=99)
    db = DatabaseService(tmp_path / "bot.db")
    dashboard_data = DashboardDataService(tmp_path, db)
    services = {
        "config_manager": SimpleNamespace(dashboard_view=lambda: {"storage": {"database_path": "bot.db"}, "dashboard": {"stale_heartbeat_seconds": 120, "readonly_mode": False}, "bot": {"trading_mode": "DRY_RUN"}}),
        "control_state": SimpleNamespace(summarize=lambda stale_after_seconds=120: {"process_alive": False, "bot_running": False, "runtime": {"mode": "DRY_RUN"}}),
        "control_plane": SimpleNamespace(get_section=lambda *_args, **_kwargs: {}, get_config=lambda: {}, update_config=lambda **_kwargs: {}, clear_pending_restart=lambda *_args, **_kwargs: {}),
        "bot_control": SimpleNamespace(request_config_reload=lambda *_args, **_kwargs: {"ok": True}),
        "operator_service": SimpleNamespace(),
        "manual_trade_service": SimpleNamespace(),
        "dashboard_data": dashboard_data,
        "database": db,
        "audit_service": SimpleNamespace(log_action=lambda *_args, **_kwargs: None),
        "dashboard_auth": SimpleNamespace(dependency=lambda: None, public_status=lambda: {"enabled": False, "username": ""}),
        "base_dir": tmp_path,
        "oos_service": SimpleNamespace(get_status=lambda oos_run_id=None: {"ok": True, "run": None, "history": [], "state": "IDLE"}),
        "job_service": SimpleNamespace(list_jobs=lambda **_kwargs: []),
    }
    app = FastAPI()
    templates = Jinja2Templates(directory=str((Path(__file__).resolve().parents[1] / "dashboard" / "templates")))
    app.include_router(create_router(services, templates))

    status_code, payload = _request_json(app, "GET", "/api/worker/status")
    assert status_code == 200
    assert payload["worker"]["state"] == "running"
    assert payload["worker"]["available"] is True
    assert payload["worker"]["current_job_id"] == 99


def test_ensure_worker_running_does_not_launch_duplicate_when_heartbeat_is_active(tmp_path: Path, monkeypatch) -> None:
    write_worker_status(tmp_path, worker_id="active-worker", pid=321, started_at="2026-04-21T00:00:00+00:00", status="running")
    calls = []
    monkeypatch.setattr("services.worker_control.subprocess.Popen", lambda *args, **kwargs: calls.append((args, kwargs)))

    result = ensure_worker_running(tmp_path, wait_seconds=0)

    assert result["worker_available"] is True
    assert result["launch_attempted"] is False
    assert calls == []


def test_bootstrap_launcher_starts_worker_when_dashboard_requested(monkeypatch) -> None:
    import scripts.bootstrap_launcher as bootstrap_launcher

    calls = []
    monkeypatch.setattr(bootstrap_launcher, "parse_args", lambda: SimpleNamespace(no_mt5=True, no_dashboard=False, wait_seconds=1))
    monkeypatch.setattr(bootstrap_launcher, "load_runtime_config", lambda *_args, **_kwargs: {"dashboard": {"host": "127.0.0.1", "port": 8501}})
    monkeypatch.setattr(bootstrap_launcher, "_start_worker", lambda wait_seconds: calls.append(wait_seconds) or True)
    monkeypatch.setattr(bootstrap_launcher, "_start_dashboard", lambda config, wait_seconds: True)

    assert bootstrap_launcher.main() == 0
    assert calls == [1]


def test_bootstrap_launcher_waits_for_mt5_bridge_readiness(monkeypatch) -> None:
    import scripts.bootstrap_launcher as bootstrap_launcher

    probe_calls = []
    probe_results = iter([(False, "ipc_unavailable"), (True, "ready")])
    monkeypatch.setattr(bootstrap_launcher, "_mt5_running", lambda: True)
    monkeypatch.setattr(
        bootstrap_launcher,
        "_probe_mt5_ready",
        lambda config, wait_seconds: probe_calls.append((config, wait_seconds)) or next(probe_results),
    )

    assert bootstrap_launcher._start_mt5({"mt5": {}, "timeframes": {"bars_to_fetch": 120}}, wait_seconds=2) is True
    assert len(probe_calls) == 2


def test_run_dashboard_ensures_worker_before_uvicorn(monkeypatch) -> None:
    import scripts.run_dashboard as run_dashboard

    calls = []
    monkeypatch.setattr(run_dashboard.sys, "argv", ["run_dashboard.py", "--host", "127.0.0.1", "--port", "8999"])
    monkeypatch.setattr(run_dashboard, "load_runtime_config", lambda *_args, **_kwargs: {"dashboard": {"host": "127.0.0.1", "port": 8999}})
    monkeypatch.setattr(run_dashboard, "ensure_worker_running", lambda *args, **kwargs: calls.append((args, kwargs)) or {"worker_available": True})
    monkeypatch.setattr(run_dashboard.uvicorn, "run", lambda *args, **kwargs: None)

    run_dashboard.main()

    assert calls


def test_run_dashboard_no_worker_flag_skips_worker_start(monkeypatch) -> None:
    import scripts.run_dashboard as run_dashboard

    calls = []
    monkeypatch.setattr(run_dashboard.sys, "argv", ["run_dashboard.py", "--no-worker"])
    monkeypatch.setattr(run_dashboard, "load_runtime_config", lambda *_args, **_kwargs: {"dashboard": {"host": "127.0.0.1", "port": 8999}})
    monkeypatch.setattr(run_dashboard, "ensure_worker_running", lambda *args, **kwargs: calls.append((args, kwargs)) or {"worker_available": True})
    monkeypatch.setattr(run_dashboard.uvicorn, "run", lambda *args, **kwargs: None)

    run_dashboard.main()

    assert calls == []
