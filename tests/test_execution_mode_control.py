from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates

from dashboard.routes import create_router
from services.config_manager import ConfigManager
from services.job_service import JobService
from services.database import DatabaseService
from services.oos_service import OOSService
from trading_bot.backtest.oos_evaluation import OOSEvaluationFramework
from trading_bot.core.execution_mode import V1_BASELINE, V2_FULL, apply_execution_mode_to_config


def _write_config(base: Path) -> None:
    config = json.loads(Path("config.json").read_text(encoding="utf-8"))
    config["storage"]["database_path"] = "storage/test_mode.db"
    (base / "storage").mkdir(parents=True, exist_ok=True)
    (base / "config.json").write_text(json.dumps(config), encoding="utf-8")


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


def test_execution_mode_resolver_enforces_v1_and_v2_contracts() -> None:
    base = json.loads(Path("config.json").read_text(encoding="utf-8"))

    v1_config, v1 = apply_execution_mode_to_config(base, V1_BASELINE)
    assert v1.selected_mode == V1_BASELINE
    assert v1.engine_type == "legacy_strategy_engine"
    assert v1.adaptive_execution is False
    assert v1.family_specific_management is False
    assert v1.lebprim_enabled is False
    assert v1.enabled_strategy_names == ["XAU_BOT_BREAKOUT", "XAU_BOT_COMPRESS"]
    assert v1_config["execution"]["enable_contextual_entry_mode"] is False
    assert v1_config["strategy"]["lebprim_mode"] == "off"
    assert v1_config["strategy"]["setup_controls"]["lebprim_scalp"]["enabled"] is False

    v2_config, v2 = apply_execution_mode_to_config(base, V2_FULL)
    assert v2.selected_mode == V2_FULL
    assert v2.engine_type == "unified_strategy_engine"
    assert v2.adaptive_execution is True
    assert v2.family_specific_management is True
    assert v2.lebprim_enabled is True
    assert v2.enabled_strategy_names == ["XAU_BOT_BREAKOUT", "XAU_BOT_COMPRESS", "XAU_LEBPRIM"]
    assert v2_config["execution"]["enable_contextual_entry_mode"] is True
    assert v2_config["strategy"]["lebprim_mode"] == "restricted"
    assert v2_config["strategy"]["setup_controls"]["lebprim_scalp"]["enabled"] is True
    assert v2_config["strategy"]["setup_controls"]["lebprim_scalp"]["live_allowed"] is True


def test_dashboard_mode_api_persists_selected_execution_mode(tmp_path: Path) -> None:
    base = tmp_path / "mode_api_env"
    base.mkdir(parents=True, exist_ok=True)
    _write_config(base)
    config_manager = ConfigManager(base)

    class _BotControl:
        control_state = SimpleNamespace(update=lambda *_args, **_kwargs: None)

        @staticmethod
        def request_config_reload(actor: str) -> dict:
            return {"ok": True, "actor": actor}

    class _Audit:
        @staticmethod
        def log_action(*_args, **_kwargs) -> None:
            return None

    from services.operator_service import OperatorService

    operator_service = OperatorService(base, config_manager, _Audit(), _BotControl(), control_plane_service=None)
    services = {
        "config_manager": config_manager,
        "control_state": SimpleNamespace(summarize=lambda stale_after_seconds=120: {"process_alive": False, "bot_running": False, "runtime": {"mode": "DRY_RUN"}}),
        "control_plane": SimpleNamespace(
            get_section=lambda section: {"mode": config_manager.dashboard_view()["mode"], "options": [{"value": "V1_BASELINE"}, {"value": "V2_FULL"}]} if section == "mode" else {},
            get_config=lambda: {},
            update_config=lambda **_kwargs: {},
            clear_pending_restart=lambda *_args, **_kwargs: {},
        ),
        "bot_control": _BotControl(),
        "operator_service": operator_service,
        "manual_trade_service": SimpleNamespace(),
        "dashboard_data": SimpleNamespace(status_snapshot=lambda *_args, **_kwargs: {"metrics": {}, "backtest_runs": []}, list_backtest_runs=lambda limit=20: [], get_backtest_run_bundle=lambda run_id: {}),
        "database": DatabaseService(base / "storage" / "test_mode.db"),
        "audit_service": _Audit(),
        "dashboard_auth": SimpleNamespace(dependency=lambda: None, public_status=lambda: {"enabled": False, "username": ""}),
        "base_dir": base,
        "oos_service": SimpleNamespace(get_status=lambda oos_run_id=None: {"ok": True, "run": None, "history": [], "state": "IDLE"}, history=lambda limit=20: {"ok": True, "runs": []}),
        "job_service": SimpleNamespace(list_jobs=lambda limit=10: []),
    }
    app = FastAPI()
    templates = Jinja2Templates(directory=str((Path(__file__).resolve().parents[1] / "dashboard" / "templates")))
    app.include_router(create_router(services, templates))

    status_code, payload = _request_json(app, "POST", "/api/mode", {"actor": "dashboard", "execution_mode": "V1_BASELINE"})
    assert status_code == 200
    assert payload["mode"]["selected_mode"] == "V1_BASELINE"
    assert config_manager.dashboard_view()["mode"]["selected_mode"] == "V1_BASELINE"

    status_code, payload = _request_json(app, "GET", "/api/mode")
    assert status_code == 200
    assert payload["mode"]["selected_mode"] == "V1_BASELINE"


def test_job_service_snapshots_execution_mode_for_backtest_and_override(tmp_path: Path) -> None:
    base = tmp_path / "job_mode_env"
    base.mkdir(parents=True, exist_ok=True)
    _write_config(base)
    config_manager = ConfigManager(base)
    config_manager.save(config_manager.apply_updates({"bot": {"execution_mode": "V1_BASELINE"}}), actor="test", reason="seed_mode")
    db = DatabaseService(base / "storage" / "test_mode.db")
    oos_service = OOSService(base, config_manager, db)
    job_service = JobService(base, config_manager, db, oos_service)

    default_job = job_service.create_backtest_job(
        {
            "symbol": "XAUUSD",
            "timeframe": "M1",
            "start_date": "2026-04-15",
            "end_date": "2026-04-16",
            "enabled_strategies": [],
            "session_filter": {"enabled": False, "allowed_sessions": []},
            "initial_balance": 10000.0,
            "risk_percent": 0.5,
            "spread_model": {"type": "fixed_points", "points": 20.0},
            "slippage_model": {"type": "fixed_points", "points": 2.0},
            "execution_model": "next_bar_open",
            "notes": None,
        }
    )
    assert default_job["payload_json"]["request"]["engine_mode"] == "V1_BASELINE"

    override_job = job_service.create_backtest_job(
        {
            "symbol": "XAUUSD",
            "timeframe": "M1",
            "start_date": "2026-04-15",
            "end_date": "2026-04-16",
            "enabled_strategies": [],
            "session_filter": {"enabled": False, "allowed_sessions": []},
            "initial_balance": 10000.0,
            "risk_percent": 0.5,
            "spread_model": {"type": "fixed_points", "points": 20.0},
            "slippage_model": {"type": "fixed_points", "points": 2.0},
            "execution_model": "next_bar_open",
            "engine_mode": "V2_FULL",
            "notes": None,
        }
    )
    assert override_job["payload_json"]["request"]["engine_mode"] == "V2_FULL"


def test_oos_default_scenarios_have_explicit_modes() -> None:
    scenarios = OOSEvaluationFramework.default_scenarios()
    lookup = {scenario.key: scenario.execution_mode for scenario in scenarios}
    assert lookup["bot_v1_baseline"] == "V1_BASELINE"
    assert lookup["bot_v2_full"] == "V2_FULL"
    assert lookup["bot_v2_lebprim_disabled"] == "V2_NO_LEBPRIM"
    assert lookup["bot_v2_adaptive_only"] == "V2_ADAPTIVE_ONLY"
    assert lookup["bot_v2_family_management_only"] == "V2_FAMILY_MANAGEMENT_ONLY"
