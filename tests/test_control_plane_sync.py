from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from main import TradingBot
from services.bot_control_service import BotControlService
from services.config_manager import ConfigManager
from services.control_plane_service import ControlPlaneService
from services.control_state import ControlStateService
from services.operator_service import OperatorService
from services.telegram_notifier import TelegramNotifier
from services.telegram_command_service import TelegramCommandService


class _AuditRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, dict[str, object], str]] = []

    def log_action(self, actor: str, action: str, target: str, details: dict[str, object], level: str = "INFO") -> None:
        self.calls.append((actor, action, target, details, level))


def _write_config(base_dir: Path, trading_mode: str) -> None:
    config = {
        "bot": {
            "trading_mode": trading_mode,
            "rollout_phase": 1 if trading_mode in {"DRY_RUN", "DEMO"} else 2,
            "force_reduced_risk_mode": trading_mode != "LIVE",
        },
        "execution": {
            "max_spread_points": 25.0,
            "manual_trading_enabled": True,
            "manual_trade_comment_tag_dashboard": "MANUAL_DASH",
            "manual_trade_comment_tag_telegram": "MANUAL_TG",
        },
        "risk": {
            "risk_percent": 0.35,
            "max_lot": 2.0,
            "min_lot": 0.01,
            "min_margin_level": 300,
            "max_trades_per_day": 4,
            "max_trades_per_symbol": 1,
            "max_daily_drawdown_pct": 1.5,
        },
        "mt5": {"symbol": "XAUUSD", "magic_number": 1, "deviation": 10},
        "dashboard": {"readonly_mode": False},
        "telegram": {
            "enabled": False,
            "remote_control_enabled": True,
            "bot_token": "",
            "chat_id": "",
            "admin_chat_ids": [],
            "admin_user_ids": [],
            "confirmation_required_commands": ["LIVE"],
            "confirmation_ttl_seconds": 120,
            "poll_interval_seconds": 3,
            "polling_timeout_seconds": 20,
        },
        "storage": {"database_path": "storage/bot.db", "state_path": "storage/state.json"},
        "sessions": {"buckets": [], "live_allowed_buckets": []},
        "strategy": {"setup_families": {}, "setup_controls": {}, "entry_modes": {}},
    }
    (base_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")


def _make_config_manager(base_dir: Path, trading_mode: str = "DRY_RUN") -> ConfigManager:
    _write_config(base_dir, trading_mode)
    return ConfigManager(base_dir)


def test_runtime_loader_ignores_env_mode_override_when_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TRADING_MODE", "DRY_RUN")
    manager = _make_config_manager(tmp_path, "LIVE")
    runtime = manager.load_runtime()
    assert runtime["bot"]["trading_mode"] == "LIVE"
    assert runtime["bot"]["allow_live_execution"] is True
    assert runtime["bot"]["dry_run"] is False


def test_switch_dry_run_to_live_updates_config_and_control_state(tmp_path: Path) -> None:
    manager = _make_config_manager(tmp_path, "DRY_RUN")
    control_state = ControlStateService(tmp_path)
    audit = _AuditRecorder()
    bot_control = BotControlService(tmp_path, manager, control_state, audit)
    operator = OperatorService(tmp_path, manager, audit, bot_control)

    result = operator.switch_mode("dashboard", "LIVE", confirmation_text="LIVE")

    assert result["ok"] is True
    runtime = control_state.load()["runtime"]
    assert runtime["mode"] == "LIVE"
    assert manager.load_runtime()["bot"]["trading_mode"] == "LIVE"
    assert manager.load_runtime()["bot"]["allow_live_execution"] is True
    assert manager.load_runtime()["bot"]["dry_run"] is False


def test_switch_live_to_dry_run_updates_config_and_control_state(tmp_path: Path) -> None:
    manager = _make_config_manager(tmp_path, "LIVE")
    control_state = ControlStateService(tmp_path)
    audit = _AuditRecorder()
    bot_control = BotControlService(tmp_path, manager, control_state, audit)
    operator = OperatorService(tmp_path, manager, audit, bot_control)

    result = operator.switch_mode("dashboard", "DRY_RUN")

    assert result["ok"] is True
    runtime = control_state.load()["runtime"]
    assert runtime["mode"] == "DRY_RUN"
    assert manager.load_runtime()["bot"]["trading_mode"] == "DRY_RUN"
    assert manager.load_runtime()["bot"]["allow_live_execution"] is False
    assert manager.load_runtime()["bot"]["dry_run"] is True


def test_bot_start_writes_control_state_and_pid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _make_config_manager(tmp_path, "LIVE")
    control_state = ControlStateService(tmp_path)
    audit = _AuditRecorder()
    bot_control = BotControlService(tmp_path, manager, control_state, audit)

    class _FakeProcess:
        pid = 4321

    monkeypatch.setattr("services.bot_control_service.subprocess.Popen", lambda *args, **kwargs: _FakeProcess())

    result = bot_control.start_bot("dashboard")

    assert result["ok"] is True
    state = control_state.load()
    assert state["bot_running"] is True
    assert state["bot_pid"] == 4321
    assert state["desired_state"] == "RUNNING"
    assert state["runtime"]["mode"] == "LIVE"


def test_bot_start_uses_persisted_config_mode_not_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _make_config_manager(tmp_path, "LIVE")
    control_state = ControlStateService(tmp_path)
    audit = _AuditRecorder()
    bot_control = BotControlService(tmp_path, manager, control_state, audit)

    captured: dict[str, object] = {}

    class _FakeProcess:
        pid = 4322

    def fake_load_runtime_config(base_dir, require_mt5_credentials=True, *, apply_mode_env_override=True):
        captured["apply_mode_env_override"] = apply_mode_env_override
        return manager.load_runtime()

    monkeypatch.setattr("services.bot_control_service.load_runtime_config", fake_load_runtime_config)
    monkeypatch.setattr("services.bot_control_service.subprocess.Popen", lambda *args, **kwargs: _FakeProcess())

    result = bot_control.start_bot("dashboard")

    assert result["ok"] is True
    assert captured["apply_mode_env_override"] is False
    assert control_state.load()["runtime"]["mode"] == "LIVE"


def test_dead_pid_is_auto_healed_from_control_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    control_state = ControlStateService(tmp_path)
    control_state.save(
        {
            "bot_running": True,
            "bot_pid": 999999,
            "desired_state": "RUNNING",
            "runtime": {"mode": "LIVE", "heartbeat_at": "2026-04-13T00:00:00+00:00", "heartbeat_status": "RUNNING"},
        }
    )
    monkeypatch.setattr(control_state, "is_process_alive", lambda pid: False)

    summary = control_state.summarize()

    assert summary["process_alive"] is False
    assert summary["bot_running"] is False
    assert summary["desired_state"] == "STOPPED"
    assert summary["bot_pid"] is None


def test_runtime_snapshot_can_mark_bot_stopped(tmp_path: Path) -> None:
    control_state = ControlStateService(tmp_path)
    control_state.save(
        {
            "bot_running": True,
            "bot_pid": 1234,
            "desired_state": "RUNNING",
            "runtime": {"mode": "LIVE", "heartbeat_at": "2026-04-13T00:00:00+00:00", "heartbeat_status": "RUNNING"},
        }
    )

    control_state.record_runtime_snapshot(
        {
            "bot_pid": 1234,
            "heartbeat_at": "2026-04-13T01:00:00+00:00",
            "heartbeat_status": "STOPPED",
            "bot_running": False,
            "desired_state": "STOPPED",
            "mode": "LIVE",
        }
    )

    state = control_state.load()
    assert state["bot_running"] is False
    assert state["desired_state"] == "STOPPED"
    assert state["runtime"]["heartbeat_status"] == "STOPPED"


def test_telegram_poll_error_uses_bounded_backoff(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _make_config_manager(tmp_path, "DRY_RUN")
    control_state = ControlStateService(tmp_path)
    dashboard_data = SimpleNamespace(status_snapshot=lambda *_args, **_kwargs: {"metrics": {}})
    audit = _AuditRecorder()
    telegram = TelegramCommandService(tmp_path, manager, control_state, dashboard_data, audit, SimpleNamespace(), SimpleNamespace())

    waits: list[float] = []
    telegram._stop_event = SimpleNamespace(wait=lambda seconds: waits.append(seconds) or True)

    class _Response:
        status_code = 401
        text = "unauthorized"

    telegram._handle_poll_error(RuntimeError("bad token"), _Response(), manager.load_runtime())

    assert waits and waits[0] >= 5.0
    assert any(call[1] == "telegram_remote_poll_unauthorized" for call in audit.calls)


def test_telegram_poll_dns_failure_is_classified_as_unreachable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _make_config_manager(tmp_path, "DRY_RUN")
    control_state = ControlStateService(tmp_path)
    dashboard_data = SimpleNamespace(status_snapshot=lambda *_args, **_kwargs: {"metrics": {}})
    audit = _AuditRecorder()
    telegram = TelegramCommandService(tmp_path, manager, control_state, dashboard_data, audit, SimpleNamespace(), SimpleNamespace())

    waits: list[float] = []
    telegram._stop_event = SimpleNamespace(wait=lambda seconds: waits.append(seconds) or True)

    class _Exc(RuntimeError):
        pass

    telegram._handle_poll_error(_Exc("HTTPSConnectionPool(host='api.telegram.org', port=443): Max retries exceeded with url: /botX/getUpdates (Caused by NameResolutionError(\"Failed to resolve 'api.telegram.org' ([Errno 11001] getaddrinfo failed)\"))"), None, manager.load_runtime())

    assert waits and waits[0] >= 60.0
    assert any(call[1] == "telegram_remote_poll_unreachable" for call in audit.calls)


def test_pause_resume_updates_are_seen_by_the_bot(tmp_path: Path) -> None:
    control_state = ControlStateService(tmp_path)
    control_state.update({"execution_paused": True, "auto_execution_enabled": False, "signal_generation_enabled": False, "runtime": {"mode": "LIVE"}})

    bot = TradingBot.__new__(TradingBot)
    bot.base_dir = tmp_path
    bot.config = {"bot": {"trading_mode": "LIVE"}, "mt5": {"symbol": "XAUUSD"}}
    bot.control_state = control_state
    bot.state = {"locks": {}}
    bot.logger = SimpleNamespace(structured=lambda *args, **kwargs: None)
    bot.trading_mode = "LIVE"
    bot.execution_paused = False
    bot.auto_execution_enabled = True
    bot.signal_generation_enabled = True
    bot.demo_mode = False
    bot.dry_run = False
    bot.test_mode = False
    bot.backtest_mode = False

    TradingBot._apply_runtime_updates(bot)
    assert bot.execution_paused is True
    assert bot.auto_execution_enabled is False
    assert bot.signal_generation_enabled is False

    control_state.update({"execution_paused": False, "auto_execution_enabled": True, "signal_generation_enabled": True})
    TradingBot._apply_runtime_updates(bot)
    assert bot.execution_paused is False
    assert bot.auto_execution_enabled is True
    assert bot.signal_generation_enabled is True


def test_control_plane_hot_update_persists_and_requests_reload(tmp_path: Path) -> None:
    manager = _make_config_manager(tmp_path, "DRY_RUN")
    control_state = ControlStateService(tmp_path)
    audit = _AuditRecorder()
    bot_control = BotControlService(tmp_path, manager, control_state, audit)
    control_plane = ControlPlaneService(tmp_path, manager, control_state, audit, bot_control)

    result = control_plane.update_config(
        actor="dashboard",
        updates={"risk": {"risk_percent": 0.42}},
        reason="risk_update",
        target="risk",
    )

    assert result["ok"] is True
    assert result["apply_plan"]["applied_immediately"] is True
    assert result["apply_plan"]["pending_restart"] is False
    assert manager.load_runtime()["risk"]["risk_percent"] == 0.42
    state = control_state.load()
    assert state["request_reload_config"] is True
    assert state["pending_restart"] is False
    assert state["last_config_apply"]["target"] == "risk"


def test_control_plane_restart_required_update_is_marked_pending(tmp_path: Path) -> None:
    manager = _make_config_manager(tmp_path, "DRY_RUN")
    control_state = ControlStateService(tmp_path)
    audit = _AuditRecorder()
    bot_control = BotControlService(tmp_path, manager, control_state, audit)
    control_plane = ControlPlaneService(tmp_path, manager, control_state, audit, bot_control)

    result = control_plane.update_config(
        actor="dashboard",
        updates={"dashboard": {"default_port": 8600}},
        reason="dashboard_settings_update",
        target="dashboard",
    )

    assert result["ok"] is True
    assert result["apply_plan"]["pending_restart"] is True
    state = control_state.load()
    assert state["pending_restart"] is True
    assert "dashboard.default_port" in state["restart_required_reasons"]


def test_operator_routes_config_changes_through_control_plane_when_available(tmp_path: Path) -> None:
    manager = _make_config_manager(tmp_path, "DRY_RUN")
    control_state = ControlStateService(tmp_path)
    audit = _AuditRecorder()
    bot_control = BotControlService(tmp_path, manager, control_state, audit)
    control_plane = ControlPlaneService(tmp_path, manager, control_state, audit, bot_control)
    operator = OperatorService(tmp_path, manager, audit, bot_control, control_plane)

    result = operator.set_readonly("dashboard", True)

    assert result["ok"] is True
    assert result["apply_plan"]["applied_immediately"] is True
    assert manager.load_runtime()["dashboard"]["readonly_mode"] is True
    assert control_state.load()["request_reload_config"] is True


def test_telegram_status_renders_meaningful_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _make_config_manager(tmp_path, "LIVE")
    control_state = ControlStateService(tmp_path)
    control_state.save(
        {
            "bot_running": True,
            "bot_pid": 1234,
            "desired_state": "RUNNING",
            "execution_paused": False,
            "kill_switch": True,
            "auto_execution_enabled": True,
            "runtime": {
                "mode": "LIVE",
                "heartbeat_at": "2026-04-13T01:23:45+00:00",
                "heartbeat_status": "RUNNING",
                "mt5_connected": True,
                "trade_allowed": True,
                "session": "LONDON",
                "regime": "TREND",
            },
        }
    )
    dashboard_data = SimpleNamespace(
        status_snapshot=lambda *_args, **_kwargs: {"metrics": {"open_positions": 2, "today_pnl": 12.34}}
    )
    telegram = TelegramCommandService(
        tmp_path,
        manager,
        control_state,
        dashboard_data,
        _AuditRecorder(),
        SimpleNamespace(),
        SimpleNamespace(),
    )
    monkeypatch.setattr(control_state, "is_process_alive", lambda pid: True)

    text = telegram._status_text(manager.load_runtime())

    assert "Mode: LIVE" in text
    assert "Bot Running: True" in text
    assert "Execution Paused: False" in text
    assert "Kill Switch: True" in text
    assert "Heartbeat: 2026-04-13T01:23:45+00:00" in text


def test_stale_telegram_poller_lock_recovery(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    manager = _make_config_manager(tmp_path, "DRY_RUN")
    control_state = ControlStateService(tmp_path)
    dashboard_data = SimpleNamespace(status_snapshot=lambda *_args, **_kwargs: {"metrics": {}})
    audit = _AuditRecorder()
    telegram = TelegramCommandService(tmp_path, manager, control_state, dashboard_data, audit, SimpleNamespace(), SimpleNamespace())
    lock_path = tmp_path / "storage" / "telegram_remote_control.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(json.dumps({"pid": 999999, "started_at": "2026-04-13T00:00:00+00:00"}), encoding="utf-8")
    monkeypatch.setattr(telegram, "_pid_is_alive", lambda pid: False)

    assert telegram._claim_polling_lock() is True
    assert telegram._owns_polling_lock is True
    assert lock_path.exists()
    telegram._release_polling_lock()


def test_operator_switch_and_config_save_share_the_same_runtime_mode(tmp_path: Path) -> None:
    manager = _make_config_manager(tmp_path, "DRY_RUN")
    control_state = ControlStateService(tmp_path)
    audit = _AuditRecorder()
    bot_control = BotControlService(tmp_path, manager, control_state, audit)
    operator = OperatorService(tmp_path, manager, audit, bot_control)

    operator.switch_mode("dashboard", "LIVE", confirmation_text="LIVE")
    assert control_state.load()["runtime"]["mode"] == "LIVE"

    manager.save(manager.apply_updates({"bot": {"trading_mode": "DRY_RUN"}}), actor="dashboard", reason="manual_save")
    control_state.update({"runtime": {"mode": "DRY_RUN"}})
    assert manager.load_runtime()["bot"]["trading_mode"] == "DRY_RUN"
    assert control_state.load()["runtime"]["mode"] == "DRY_RUN"


def test_telegram_notifier_uses_form_payload_and_trade_close_format(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class _Response:
        ok = True
        status_code = 200
        text = "ok"

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured["kwargs"] = kwargs
        return _Response()

    monkeypatch.setattr("services.telegram_notifier.requests.post", fake_post)
    notifier = TelegramNotifier("token", "123", enabled=True)
    assert notifier.send_info("hello world") is True
    assert "data" in captured["kwargs"]
    assert "json" not in captured["kwargs"]
    assert notifier.send_trade_close({"mode": "LIVE", "symbol": "XAUUSD", "side": "BUY", "exit_price": 1.0, "pnl": 2.5, "status": "closed", "ticket": 7}) is True
