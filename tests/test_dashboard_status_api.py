from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates

from dashboard.routes import create_router
from dashboard.services import DashboardDataService
from services.database import DatabaseService
from services.trade_journal import TradeJournalService


class _StubLogger:
    def log_signal_event(self, _row):
        return None

    def log_trade_event(self, _row):
        return None


def _journal(tmp_path: Path) -> tuple[DatabaseService, TradeJournalService]:
    db = DatabaseService(tmp_path / "bot.db")
    journal = TradeJournalService(tmp_path, db, _StubLogger(), {})
    return db, journal


def _trade_open_payload(ticket: str, ts: str = "2026-04-16T08:00:00+00:00") -> dict[str, object]:
    return {
        "timestamp": ts,
        "mode": "LIVE",
        "symbol": "XAUUSD",
        "side": "LONG",
        "ticket": ticket,
        "position_id": ticket,
        "trade_id": ticket,
        "entry_price": 100.0,
        "entry": 100.0,
        "volume": 0.5,
        "requested_volume": 0.5,
        "final_volume": 0.5,
        "setup_fingerprint": f"fp-{ticket}",
        "setup_family": "LEBPRIM_SCALP",
        "setup_type": "LEBPRIM_MOMENTUM",
        "regime_at_entry": "TREND_CONTINUATION",
        "session_at_entry": "LONDON",
        "entry_mode": "lebprim_limit",
        "comment": f"fp-{ticket}",
        "metadata": {"source": "test"},
    }


def _trade_close_payload(ticket: str, ts: str = "2026-04-16T08:10:00+00:00") -> dict[str, object]:
    return {
        "timestamp": ts,
        "mode": "LIVE",
        "symbol": "XAUUSD",
        "side": "LONG",
        "ticket": ticket,
        "position_id": ticket,
        "trade_id": ticket,
        "entry_price": 100.0,
        "entry": 100.0,
        "exit_price": 101.2,
        "volume": 0.5,
        "final_volume": 0.5,
        "pnl": 1.2,
        "realized_r": 0.6,
        "hold_minutes": 10.0,
        "setup_fingerprint": f"fp-{ticket}",
        "setup_family": "LEBPRIM_SCALP",
        "regime_at_entry": "TREND_CONTINUATION",
        "session_at_entry": "LONDON",
        "close_reason": "take_profit",
        "status": "FINALIZED",
        "event_type": "TRADE_FINALIZED",
        "closed_at": ts,
        "metadata": {"source": "test"},
    }


def _get_json(app: FastAPI, path: str) -> tuple[int, dict[str, object]]:
    async def _request() -> tuple[int, dict[str, object]]:
        messages: list[dict[str, object]] = []

        async def receive() -> dict[str, object]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, object]) -> None:
            messages.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "headers": [],
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
            "root_path": "",
        }
        await app(scope, receive, send)
        status = next(int(message["status"]) for message in messages if message["type"] == "http.response.start")
        body = b"".join(bytes(message.get("body", b"")) for message in messages if message["type"] == "http.response.body")
        payload = json.loads(body.decode("utf-8") or "{}")
        return status, payload

    return asyncio.run(_request())


def test_status_api_returns_dashboard_payload_shape(tmp_path: Path) -> None:
    db, journal = _journal(tmp_path)
    journal.record_trade_open(_trade_open_payload("API100"))
    journal.record_trade_close(_trade_close_payload("API100"))
    journal.record_trade_open(_trade_open_payload("API101", ts="2026-04-16T08:20:00+00:00"))
    journal.record_trade_close(
        {
            **_trade_close_payload("API101", ts="2026-04-16T08:25:00+00:00"),
            "status": "PENDING_CLOSE_RESOLUTION",
            "event_type": "TRADE_CLOSED",
            "pnl": None,
            "realized_r": None,
            "exit_price": None,
            "close_reason": "history_unavailable",
        }
    )
    db.insert_heartbeat(
        {
            "timestamp": "2026-04-16T08:30:00+00:00",
            "status": "RUNNING",
            "symbol": "XAUUSD",
            "spread": 18.5,
            "trend": "UP",
            "setup": "LEBPRIM_SCALP",
            "balance": 10000.0,
            "equity": 10012.0,
            "open_positions": 1,
            "regime": "TREND_CONTINUATION",
            "session": "LONDON",
            "mode": "LIVE",
        }
    )

    dashboard_data = DashboardDataService(tmp_path, db)
    config = {
        "storage": {"database_path": "bot.db"},
        "dashboard": {"stale_heartbeat_seconds": 120, "readonly_mode": False},
        "bot": {"trading_mode": "LIVE"},
    }
    control_summary = {
        "process_alive": True,
        "bot_running": True,
        "execution_paused": False,
        "kill_switch": False,
        "runtime": {
            "mode": "LIVE",
            "heartbeat_at": "2026-04-16T08:30:00+00:00",
            "last_execution_result": "executed",
            "account_login": "123456",
            "trade_allowed": True,
            "session": "LONDON",
            "regime": "TREND_CONTINUATION",
        },
    }
    services = {
        "config_manager": SimpleNamespace(
            dashboard_view=lambda: config,
            source_of_truth_snapshot=lambda **_kwargs: {
                "effective": {"mode": "LIVE", "database_path": "bot.db", "execution_enabled": True},
                "sources": {"mode": "process_runtime", "database_path": "root_config", "execution_enabled": "persisted_control_state"},
                "raw": {},
                "drift": {"detected": False},
                "precedence": ["process_runtime", "persisted_control_state", "root_config", "storage_config"],
            },
        ),
        "control_state": SimpleNamespace(summarize=lambda stale_after_seconds=120: control_summary),
        "control_plane": SimpleNamespace(get_section=lambda *_args, **_kwargs: {}, get_config=lambda: {}, update_config=lambda **_kwargs: {}, clear_pending_restart=lambda *_args, **_kwargs: {}),
        "bot_control": SimpleNamespace(request_config_reload=lambda *_args, **_kwargs: {"ok": True}),
        "operator_service": SimpleNamespace(),
        "manual_trade_service": SimpleNamespace(),
        "dashboard_data": dashboard_data,
        "database": db,
        "audit_service": SimpleNamespace(log_action=lambda *_args, **_kwargs: None),
        "dashboard_auth": SimpleNamespace(dependency=lambda: None, public_status=lambda: {"enabled": False, "username": ""}),
        "base_dir": tmp_path,
    }

    app = FastAPI()
    templates = Jinja2Templates(directory=str((Path(__file__).resolve().parents[1] / "dashboard" / "templates")))
    app.include_router(create_router(services, templates))

    status_code, payload = _get_json(app, "/api/status")
    assert status_code == 200

    assert payload["ok"] is True
    assert payload["config_mode"] == "LIVE"
    assert isinstance(payload["execution_mode"], dict)
    assert payload["readonly_mode"] is False
    assert isinstance(payload["control"], dict)
    assert isinstance(payload["runtime"], dict)
    assert isinstance(payload["auth"], dict)
    assert isinstance(payload["source_of_truth"], dict)

    runtime = payload["runtime"]
    metrics = runtime["metrics"]
    assert isinstance(metrics["open_positions"], int)
    assert isinstance(metrics["today_pnl"], float)
    assert isinstance(metrics["spread"], float)
    assert isinstance(metrics["enabled_strategy_count"], int)
    assert isinstance(metrics["enabled_family_count"], int)
    assert isinstance(runtime["recent_trades"], list)
    assert isinstance(runtime["recent_signals"], list)
    assert isinstance(runtime["recent_events"], list)
    assert isinstance(runtime["daily_pnl_curve"], list)
    assert isinstance(runtime["rejection_summary"], list)
    assert isinstance(runtime["blocked_setups"], list)
    assert isinstance(runtime["valid_not_executed"], list)
    assert isinstance(runtime["family_block_matrix"], list)
    assert isinstance(runtime["strategy_block_matrix"], list)
    assert isinstance(runtime["resolved_runtime"], dict)
    assert isinstance(runtime["unresolved_trades"], list)
    assert isinstance(runtime["recent_closed_trades"], list)
    assert isinstance(runtime["backtest_runs"], list)
    assert isinstance(runtime["live_integrity"], dict)
    assert isinstance(runtime["live_integrity"]["unresolved_trades_count"], int)
    assert isinstance(runtime["last_signal"], dict)
    assert isinstance(runtime["last_error"], dict)

    unresolved = runtime["unresolved_trades"][0]
    closed = runtime["recent_closed_trades"][0]
    performance = runtime["daily_performance"]
    assert isinstance(unresolved["resolution_attempts"], int)
    assert isinstance(unresolved["entry_price"], float)
    assert isinstance(closed["pnl"], float)
    assert isinstance(closed["realized_r"], float)
    assert isinstance(performance["summary"]["live_trades"], int)
    assert isinstance(performance["summary"]["total_pnl"], float)
    assert isinstance(performance["performance_rows"], list)
    if performance["performance_rows"]:
        perf_row = performance["performance_rows"][0]
        assert isinstance(perf_row["trade_count"], int)
        assert isinstance(perf_row["total_r"], float)
    assert isinstance(runtime["resolved_runtime"].get("enabled_strategies", []), list)
    assert isinstance(runtime["resolved_runtime"].get("enabled_families", []), list)


def test_status_api_handles_empty_database_without_crashing(tmp_path: Path) -> None:
    db = DatabaseService(tmp_path / "bot.db")
    dashboard_data = DashboardDataService(tmp_path, db)
    config = {
        "storage": {"database_path": "bot.db"},
        "dashboard": {"stale_heartbeat_seconds": 120, "readonly_mode": False},
        "bot": {"trading_mode": "DRY_RUN"},
    }
    services = {
        "config_manager": SimpleNamespace(dashboard_view=lambda: config),
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
    }

    app = FastAPI()
    templates = Jinja2Templates(directory=str((Path(__file__).resolve().parents[1] / "dashboard" / "templates")))
    app.include_router(create_router(services, templates))

    status_code, payload = _get_json(app, "/api/status")
    assert status_code == 200
    assert payload["ok"] is True
    assert isinstance(payload["execution_mode"], dict)
    assert payload["runtime"]["metrics"]["today_trades"] == 0
    assert payload["runtime"]["metrics"]["open_positions"] == 0
    assert payload["runtime"]["metrics"]["balance"] == 0.0
    assert isinstance(payload["runtime"]["resolved_runtime"], dict)
