"""UI refresh (PR 1): escaping, read-only round trip, research guard, Telegram formatting, trade watcher."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from fastapi.testclient import TestClient

import services.telegram_notifier as tn
from dashboard.auth import DashboardAuth
from dashboard.research import create_research_router
from dashboard.routes import create_router
from services.telegram_notifier import TelegramNotifier, bounded_html, price
from services.telegram_trade_watcher import TelegramTradeWatcher

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "dashboard" / "templates"
JS = (ROOT / "dashboard" / "static" / "dashboard.js").read_text(encoding="utf-8")
SHARED_JS = (ROOT / "dashboard" / "static" / "shared.js").read_text(encoding="utf-8")


# --- dashboard front end -------------------------------------------------------------------

def test_esc_escapes_quotes_and_dashboard_uses_shared_esc() -> None:
    escape_map = SHARED_JS[SHARED_JS.index("const esc"):SHARED_JS.index("function time")]
    for raw, entity in (("&", "&amp;"), ("<", "&lt;"), (">", "&gt;"), ('"', "&quot;"), ("'", "&#39;")):
        assert entity in escape_map, raw
    assert "/[&<>\"']/g" in escape_map
    assert "const esc = window.LebPrimeUI.esc;" in JS


def test_dangerous_runtime_actions_ask_first() -> None:
    start = JS.index("async function handleRuntimeActionClick")
    handler = JS[start:JS.index("\n    }\n", start)]
    for action in ("start", "stop", "resume", "kill"):
        block = handler[handler.index(f'action === "{action}"'):]
        block = block[:block.index("runControl(")]
        assert "window.confirm(" in block, f"{action} must confirm before calling the API"


def test_log_filters_do_not_mark_settings_dirty() -> None:
    listener = JS[JS.index('document.addEventListener("input"'):]
    listener = listener[:listener.index("});") + 3]
    assert '#section-risk, #section-config, #section-alerts' in listener
    assert '"system"' not in listener


def test_research_page_routes_stay_get_only() -> None:
    app = FastAPI()
    app.include_router(create_research_router(ROOT, DashboardAuth().dependency, Jinja2Templates(directory=str(TEMPLATES))))
    methods = {m for route in app.routes for m in getattr(route, "methods", set())}
    assert methods <= {"GET", "HEAD"}


# --- read-only toggle ----------------------------------------------------------------------

def _router_app(tmp_path: Path) -> tuple[FastAPI, dict]:
    config = {"storage": {"database_path": "bot.db"}, "dashboard": {"readonly_mode": False}, "bot": {}}

    def set_readonly(actor: str, enabled: bool) -> dict:
        config["dashboard"]["readonly_mode"] = bool(enabled)
        return {"ok": True, "readonly_mode": bool(enabled)}

    services = {
        "config_manager": SimpleNamespace(dashboard_view=lambda: config),
        "control_state": SimpleNamespace(summarize=lambda stale_after_seconds=120: {}),
        "control_plane": SimpleNamespace(update_config=lambda **_kw: {"ok": True}),
        "bot_control": SimpleNamespace(),
        "operator_service": SimpleNamespace(set_readonly=set_readonly),
        "manual_trade_service": SimpleNamespace(),
        "dashboard_data": SimpleNamespace(),
        "database": SimpleNamespace(),
        "audit_service": SimpleNamespace(log_action=lambda *_a, **_kw: None),
        "dashboard_auth": SimpleNamespace(dependency=lambda: None, public_status=lambda: {"enabled": False}),
        "base_dir": tmp_path,
    }
    app = FastAPI()
    app.include_router(create_router(services, Jinja2Templates(directory=str(TEMPLATES))))
    return app, config


def test_readonly_toggle_round_trip_has_a_way_back(tmp_path: Path) -> None:
    app, config = _router_app(tmp_path)
    client = TestClient(app)
    patch = {"actor": "dashboard", "updates": {"dashboard": {"refresh_seconds": 5}}, "reason": "t"}
    assert client.post("/api/control/readonly", json={"actor": "dashboard", "enabled": True}).status_code == 200
    assert config["dashboard"]["readonly_mode"] is True
    assert client.post("/api/config", json=patch).status_code == 403
    # The dedicated toggle still works while read-only is on.
    assert client.post("/api/control/readonly", json={"actor": "dashboard", "enabled": False}).status_code == 200
    assert config["dashboard"]["readonly_mode"] is False
    assert client.post("/api/config", json=patch).status_code == 200
    assert '"dashboard-readonly-toggle"' in JS and '"/api/control/readonly"' in JS


# --- research guard ------------------------------------------------------------------------

def test_research_detail_on_non_trades_csv_is_400(tmp_path: Path) -> None:
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "summary.csv").write_text("name,value\nx,1\n", encoding="utf-8")
    app = FastAPI()
    app.include_router(create_research_router(tmp_path, DashboardAuth().dependency, Jinja2Templates(directory=str(TEMPLATES))))
    response = TestClient(app).get("/api/research/trades/detail", params={"path": "summary.csv", "group": "x"})
    assert response.status_code == 400


# --- Telegram formatting -------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _fresh_throttle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(TelegramNotifier, "_throttle", tn.AlertThrottle())
    monkeypatch.setattr(TelegramNotifier, "_repeats", {})
    monkeypatch.setattr(TelegramNotifier, "_retry_at", {})


def _notifier() -> TelegramNotifier:
    return TelegramNotifier("t", "1", enabled=True, display_timezone="UTC", dry_print=True)


def test_notifier_handles_none_values_without_crashing(capsys: pytest.CaptureFixture[str]) -> None:
    n = _notifier()
    none_trade = {"symbol": "XAUUSD", "side": "BUY", "entry": None, "sl": None, "tp": None, "pnl": None, "exit_price": None, "volume": None}
    assert n.send_trade_open(none_trade)
    assert n.send_trade_close(none_trade)
    assert "P&amp;L unavailable" in n.last_message
    assert n.send_signal(none_trade)
    assert n.send_daily_summary({"source": "mt5_deals", "trades": None, "pnl": None})
    assert n.send_heartbeat({"status": None, "spread": None})
    assert "DEMO" in capsys.readouterr().out


def test_trade_close_win_loss_breakeven_labels() -> None:
    n = _notifier()
    n.send_trade_close({"symbol": "XAUUSD", "side": "BUY", "pnl": 37.2, "net_r": 2.0, "exit_price": 2668.9})
    assert n.last_message.startswith("✅ WIN +$37.20 (+2.0R) · XAUUSD BUY")
    n.send_trade_close({"symbol": "US30", "side": "SELL", "pnl": -10})
    assert n.last_message.startswith("❌ LOSS -$10.00")
    n.send_trade_close({"symbol": "US30", "side": "SELL", "pnl": 0})
    assert n.last_message.startswith("➖ BREAKEVEN")


def test_messages_are_bounded_and_escaped() -> None:
    n = _notifier()
    n.send_info("<b>" + "x" * 10_000)
    assert len(n.last_message) <= 4096
    assert "<b>" not in n.last_message and "&lt;b&gt;" in n.last_message
    assert len(bounded_html("&" * 5000)) <= 4096 * 5
    assert not bounded_html("&" * 5000)[:-1].endswith("&amp")  # never cuts an entity in half


def test_per_symbol_decimals() -> None:
    assert price(2650.4, {"symbol": "XAUUSD"}) == "2,650.40"
    assert price(44000, {"symbol": "US30"}) == "44,000.0"
    assert price(1.08456, {"symbol": "EURUSD"}) == "1.08456"
    assert price(1.5, {"symbol": "XAUUSD", "digits": 3}) == "1.500"
    assert price(None, {"symbol": "XAUUSD"}) == "—"


def test_throttle_suppresses_repeats_and_counts_them() -> None:
    n = _notifier()
    assert n.send_error("MT5 disconnected") is True
    assert n.send_error("MT5 disconnected") is False
    assert n.send_error("MT5 disconnected") is False
    assert n.send_error("Different problem") is True
    assert sum(TelegramNotifier._repeats.values()) == 2


# --- trade watcher (read-only, SQLite only) ------------------------------------------------

def _db(path: Path, rows: list[tuple]) -> None:
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS lebprime_broker_entries (idempotency_key TEXT PRIMARY KEY, zone_id TEXT, entry_number INTEGER,"
            " broker_symbol TEXT, side TEXT, entry_price REAL, normalized_price REAL, volume REAL, sl REAL, tp REAL,"
            " mt5_order_ticket TEXT, mt5_position_ticket TEXT, broker_status TEXT, historical INTEGER, comment TEXT,"
            " failure_reason TEXT, updated_at TEXT)"
        )
        db.executemany("INSERT OR REPLACE INTO lebprime_broker_entries VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)


def _row(key: str, status: str, updated: str) -> tuple:
    return (key, "XAU_H4_0412", 1, "XAUUSD", "BUY", 2650.4, None, 0.02, 2641.1, 2668.9, "111", "222", status, 0, None, None, updated)


def test_watcher_baselines_then_alerts_each_transition_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "storage").mkdir()
    db = tmp_path / "storage" / "bot.db"
    config = {"storage": {"database_path": "storage/bot.db"}, "telegram": {"enabled": True, "bot_token": "t", "chat_id": "1"}}
    sent: list[str] = []
    monkeypatch.setattr(TelegramNotifier, "_send", lambda self, title, lines: sent.append(title) or True)

    _db(db, [_row("old", "CLOSED", "2026-11-01")])
    watcher = TelegramTradeWatcher(tmp_path, lambda: config)
    watcher.poll_once()
    assert sent == []  # first run only records what already exists

    _db(db, [_row("new", "PENDING", "2026-11-24T06:00")])
    watcher.poll_once()
    _db(db, [_row("new", "FILLED", "2026-11-24T06:15")])
    watcher.poll_once()
    watcher.poll_once()
    assert [t.split(" ")[1] for t in sent] == ["LIMIT", "BUY"]

    # A restart reads the saved cursor and does not resend.
    restarted = TelegramTradeWatcher(tmp_path, lambda: config)
    restarted.poll_once()
    assert len(sent) == 2
    _db(db, [_row("new", "CLOSED", "2026-11-24T09:20")])
    restarted.poll_once()
    assert len(sent) == 3 and sent[-1].startswith("Closed · P&L unavailable")
    assert json.loads((tmp_path / "storage" / "telegram_watcher_cursor.json").read_text())["entries"]["new"] == "CLOSED"


def test_watcher_never_touches_mt5_or_order_code() -> None:
    import ast

    source = (ROOT / "services" / "telegram_trade_watcher.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)} | {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names
    }
    assert imported <= {"__future__", "sqlite3", "threading", "pathlib", "typing", "services.telegram_notifier", "utils"}
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)} | {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    for forbidden in ("mt5", "MT5Connector", "trade_history", "order_send", "positions_get", "history_deals_get"):
        assert forbidden not in names, forbidden
    assert "?mode=ro" in source


def test_watcher_does_nothing_when_telegram_disabled(tmp_path: Path) -> None:
    watcher = TelegramTradeWatcher(tmp_path, lambda: {"telegram": {"enabled": False}, "storage": {"database_path": "x.db"}})
    watcher.start()
    assert watcher.thread is None
    watcher.poll_once()
    assert not (tmp_path / "storage" / "telegram_watcher_cursor.json").exists()
