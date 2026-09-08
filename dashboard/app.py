"""FastAPI dashboard application for bot controls and operations visibility."""

from __future__ import annotations

import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dashboard.auth import DashboardAuth  # noqa: E402
from dashboard.routes import create_router  # noqa: E402
from dashboard.services import DashboardDataService  # noqa: E402
from services.audit_service import AuditService  # noqa: E402
from services.bot_control_service import BotControlService  # noqa: E402
from services.config_manager import ConfigManager  # noqa: E402
from services.control_plane_service import ControlPlaneService  # noqa: E402
from services.control_state import ControlStateService  # noqa: E402
from services.database import DatabaseService  # noqa: E402
from services.job_service import JobService  # noqa: E402
from services.manual_trade_service import ManualTradeService  # noqa: E402
from services.operator_service import OperatorService  # noqa: E402
from services.oos_service import OOSService  # noqa: E402
from services.telegram_command_service import TelegramCommandService  # noqa: E402
from utils import load_runtime_config  # noqa: E402


def create_app() -> FastAPI:
    """Build and configure the dashboard FastAPI app."""
    config = load_runtime_config(PROJECT_ROOT, require_mt5_credentials=False)
    database = DatabaseService(PROJECT_ROOT / config["storage"]["database_path"])
    config_manager = ConfigManager(PROJECT_ROOT)
    control_state = ControlStateService(PROJECT_ROOT)
    audit_service = AuditService(database, PROJECT_ROOT)
    bot_control = BotControlService(PROJECT_ROOT, config_manager, control_state, audit_service)
    control_plane_service = ControlPlaneService(PROJECT_ROOT, config_manager, control_state, audit_service, bot_control)
    dashboard_data = DashboardDataService(PROJECT_ROOT, database)
    manual_trade_service = ManualTradeService(PROJECT_ROOT, config_manager, control_state, audit_service)
    operator_service = OperatorService(PROJECT_ROOT, config_manager, audit_service, bot_control, control_plane_service)
    oos_service = OOSService(PROJECT_ROOT, config_manager, database)
    job_service = JobService(PROJECT_ROOT, config_manager, database, oos_service, ensure_worker_on_backtest_create=True)
    telegram_command_service = TelegramCommandService(
        PROJECT_ROOT,
        config_manager,
        control_state,
        dashboard_data,
        audit_service,
        operator_service,
        manual_trade_service,
    )
    dashboard_auth = DashboardAuth(
        username=config.get("dashboard", {}).get("username") or None,
        password=config.get("dashboard", {}).get("password") or None,
    )

    app = FastAPI(title="Trading Bot Control Dashboard", version="1.0.0")
    app.state.telegram_command_service = telegram_command_service
    templates = Jinja2Templates(directory=str(PROJECT_ROOT / "dashboard" / "templates"))
    app.mount("/static", StaticFiles(directory=str(PROJECT_ROOT / "dashboard" / "static")), name="static")
    app.include_router(
        create_router(
            {
                "base_dir": PROJECT_ROOT,
                "config_manager": config_manager,
                "control_state": control_state,
                "control_plane": control_plane_service,
                "audit_service": audit_service,
                "bot_control": bot_control,
                "operator_service": operator_service,
                "manual_trade_service": manual_trade_service,
                "dashboard_data": dashboard_data,
                "database": database,
                "dashboard_auth": dashboard_auth,
                "oos_service": oos_service,
                "job_service": job_service,
            },
            templates,
        )
    )

    @app.on_event("startup")
    async def startup_remote_control() -> None:
        app.state.telegram_command_service.start()

    @app.on_event("shutdown")
    async def shutdown_remote_control() -> None:
        app.state.telegram_command_service.stop()

    return app


app = create_app()
