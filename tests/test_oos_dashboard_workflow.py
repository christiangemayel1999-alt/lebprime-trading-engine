from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates

from dashboard.routes import create_router
from services.database import DatabaseService
from services.oos_service import OOSService
from strategy import StrategyEngine
from trading_bot.backtest.oos_evaluation import OOSEvaluationFramework, ScenarioResult, ScenarioSpec


def _config() -> dict:
    config = json.loads(Path("config.json").read_text(encoding="utf-8"))
    config.setdefault("storage", {})
    config["storage"]["database_path"] = "bot.db"
    return config


class _ConfigManager:
    def __init__(self, config: dict) -> None:
        self._config = config

    def dashboard_view(self) -> dict:
        return self._config


def _wait_for_terminal(service: OOSService, run_id: int, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        payload = service.get_status(run_id)
        state = payload.get("state")
        if state in {"COMPLETED", "FAILED", "INTERRUPTED", "PARTIAL"}:
            return payload
        time.sleep(0.05)
    raise AssertionError(f"OOS run {run_id} did not finish within {timeout} seconds")


def _scenario_result(spec: ScenarioSpec, run_id: int = 0) -> ScenarioResult:
    bundle = {
        "run": {
            "id": run_id,
            "symbol": "XAUUSD",
            "timeframe": "M1",
            "start_date": "2026-04-15T00:00:00+00:00",
            "end_date": "2026-04-16T23:59:59+00:00",
        },
        "signals": [],
        "trades": [],
        "equity_curve": [],
        "artifacts": {"bundle_json": f"bundle-{spec.key}.json", "run_dir": f"run-{spec.key}"},
    }
    return ScenarioResult(
        key=spec.key,
        label=spec.label,
        source=spec.source,
        bundle=bundle,
        description=spec.description,
        warnings=[],
        metadata={},
    )


def test_strategy_engine_empty_slices_return_safely() -> None:
    engine = StrategyEngine(_config())
    empty = pd.DataFrame()
    market = engine.analyze_market_context(None, empty, empty, empty, 20.0)
    candidates = engine.generate_setup_candidates(market, empty, empty, empty, {"digits": 2, "point": 0.01})
    assert market["regime"]["regime_name"] == "NO_TRADE"
    assert candidates == []


def test_oos_service_marks_partial_and_resume_skips_completed(tmp_path: Path, monkeypatch) -> None:
    config = _config()
    config["storage"]["database_path"] = "bot.db"
    db = DatabaseService(tmp_path / "bot.db")
    service = OOSService(tmp_path, _ConfigManager(config), db)
    specs = [
        ScenarioSpec(key="scenario_a", label="Scenario A", description="first", source="run"),
        ScenarioSpec(key="scenario_b", label="Scenario B", description="second", source="run"),
    ]
    calls: list[str] = []

    monkeypatch.setattr(OOSEvaluationFramework, "default_scenarios", staticmethod(lambda **_kwargs: specs))

    def fake_build_report(self, request, scenario_results):
        return {"generated_at": "2026-04-19T00:00:00+00:00", "request": request, "scenario_manifest": [], "tables": {}, "quality_verdicts": []}

    def fake_write_report(self, output_dir, report):
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        (out / "report.json").write_text(json.dumps(report), encoding="utf-8")
        (out / "evaluation_summary.md").write_text("summary", encoding="utf-8")

    monkeypatch.setattr(OOSEvaluationFramework, "build_report", fake_build_report)
    monkeypatch.setattr(OOSEvaluationFramework, "write_report", fake_write_report)

    def first_materialize(self, *, spec, request, baseline_database_path=None):
        calls.append(spec.key)
        if spec.key == "scenario_a":
            return _scenario_result(spec)
        raise RuntimeError("scenario_b_failed")

    monkeypatch.setattr(OOSEvaluationFramework, "materialize_scenario", first_materialize)
    status = service.start_run(
        request={
            "symbol": "XAUUSD",
            "timeframe": "M1",
            "start_date": "2026-04-15",
            "end_date": "2026-04-16",
            "enabled_strategies": ["XAU_BOT_BREAKOUT"],
            "session_filter": {"enabled": False, "allowed_sessions": []},
            "initial_balance": 10000.0,
            "risk_percent": 0.5,
            "spread_model": {"type": "fixed_points", "points": 20.0},
            "slippage_model": {"type": "fixed_points", "points": 2.0},
            "execution_model": "next_bar_open",
            "notes": None,
        }
    )
    run_id = int(status["run"]["id"])
    service.execute_run(run_id, rerun_partial=False)
    partial = _wait_for_terminal(service, run_id)
    assert partial["state"] == "PARTIAL"
    assert calls == ["scenario_a", "scenario_b"]
    scenario_statuses = {item["scenario_key"]: item["status"] for item in partial["run"]["scenarios"]}
    assert scenario_statuses["scenario_a"] == "COMPLETED"
    assert scenario_statuses["scenario_b"] == "FAILED"

    def fake_load_completed_result(self, *, framework, spec, scenario_row, oos_row):
        if spec.key == "scenario_a" and scenario_row.get("status") == "COMPLETED":
            return _scenario_result(spec)
        return None

    resumed_calls: list[str] = []

    def resumed_materialize(self, *, spec, request, baseline_database_path=None):
        resumed_calls.append(spec.key)
        return _scenario_result(spec)

    monkeypatch.setattr(OOSService, "_load_completed_result", fake_load_completed_result)
    monkeypatch.setattr(OOSEvaluationFramework, "materialize_scenario", resumed_materialize)
    resumed = service.resume_run(run_id, rerun_partial=True)
    assert resumed["state"] in {"QUEUED", "RUNNING", "PARTIAL", "COMPLETED"}
    service.execute_run(run_id, rerun_partial=True)
    completed = _wait_for_terminal(service, run_id)
    assert completed["state"] == "COMPLETED"
    assert resumed_calls == ["scenario_b"]


def test_oos_service_rebuild_report_from_existing_completed_runs(tmp_path: Path, monkeypatch) -> None:
    config = _config()
    config["storage"]["database_path"] = "bot.db"
    db = DatabaseService(tmp_path / "bot.db")
    service = OOSService(tmp_path, _ConfigManager(config), db)
    specs = [ScenarioSpec(key="scenario_a", label="Scenario A", description="first", source="run")]
    monkeypatch.setattr(OOSEvaluationFramework, "default_scenarios", staticmethod(lambda **_kwargs: specs))
    monkeypatch.setattr(OOSEvaluationFramework, "build_report", lambda self, request, scenario_results: {"generated_at": "2026-04-19T00:00:00+00:00", "request": request, "scenario_manifest": [], "tables": {}, "quality_verdicts": []})
    monkeypatch.setattr(OOSEvaluationFramework, "write_report", lambda self, output_dir, report: (Path(output_dir).mkdir(parents=True, exist_ok=True), (Path(output_dir) / "report.json").write_text(json.dumps(report), encoding="utf-8")))
    monkeypatch.setattr(OOSService, "_load_completed_result", lambda self, *, framework, spec, scenario_row, oos_row: _scenario_result(spec))

    run_id = db.create_oos_run(
        {
            "created_at": "2026-04-19T00:00:00+00:00",
            "state": "PARTIAL",
            "symbol": "XAUUSD",
            "timeframe": "M1",
            "start_date": "2026-04-15",
            "end_date": "2026-04-16",
            "request_json": {"symbol": "XAUUSD", "timeframe": "M1", "start_date": "2026-04-15", "end_date": "2026-04-16"},
            "output_dir": str(tmp_path / "reports" / "rebuilt"),
        }
    )
    db.upsert_oos_scenario(
        {
            "oos_run_id": run_id,
            "scenario_index": 1,
            "scenario_key": "scenario_a",
            "scenario_label": "Scenario A",
            "source": "run",
            "status": "COMPLETED",
            "created_at": "2026-04-19T00:00:00+00:00",
            "updated_at": "2026-04-19T00:00:00+00:00",
            "run_id": 1,
        }
    )
    result = service.rebuild_report(run_id)
    assert result["ok"] is True
    assert Path(result["report_path"]).exists()


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


def test_oos_dashboard_api_endpoints(tmp_path: Path) -> None:
    class StubOOSService:
        def __init__(self) -> None:
            self.last_run = None
            self.last_resume = None
            self.last_rebuild = None

        def get_status(self, oos_run_id=None):
            return {"ok": True, "run": {"id": oos_run_id or 7, "state": "RUNNING", "progress": {"current": 1, "total": 6}, "scenarios": []}, "history": [], "state": "RUNNING"}

        def history(self, limit=20):
            return {"ok": True, "runs": [{"id": 7, "state": "RUNNING", "progress": {"current": 1, "total": 6}}]}

        def start_run(self, **kwargs):
            self.last_run = kwargs
            return self.get_status(7)

        def resume_run(self, oos_run_id, rerun_partial=False):
            self.last_resume = {"oos_run_id": oos_run_id, "rerun_partial": rerun_partial}
            return self.get_status(oos_run_id)

        def rebuild_report(self, oos_run_id):
            self.last_rebuild = oos_run_id
            return {"ok": True, "report_path": str(tmp_path / "report.json")}

    oos_service = StubOOSService()
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
        "oos_service": oos_service,
    }
    app = FastAPI()
    templates = Jinja2Templates(directory=str((Path(__file__).resolve().parents[1] / "dashboard" / "templates")))
    app.include_router(create_router(services, templates))

    status_code, payload = _request_json(app, "POST", "/api/oos/run", {"actor": "dashboard", "symbol": "XAUUSD", "timeframe": "M1", "start_date": "2026-04-15", "end_date": "2026-04-16"})
    assert status_code == 200
    assert payload["run"]["id"] == 7
    assert oos_service.last_run is not None

    status_code, payload = _request_json(app, "GET", "/api/oos/status?oos_run_id=7")
    assert status_code == 200
    assert payload["state"] == "RUNNING"

    status_code, payload = _request_json(app, "POST", "/api/oos/resume", {"actor": "dashboard", "oos_run_id": 7, "rerun_partial": True})
    assert status_code == 200
    assert oos_service.last_resume == {"oos_run_id": 7, "rerun_partial": True}

    status_code, payload = _request_json(app, "POST", "/api/oos/rebuild-report", {"actor": "dashboard", "oos_run_id": 7})
    assert status_code == 200
    assert "report_path" in payload

    status_code, payload = _request_json(app, "GET", "/api/oos/history")
    assert status_code == 200
    assert payload["runs"][0]["id"] == 7
