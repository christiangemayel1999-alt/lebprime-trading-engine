from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from main import TradingBot
from strategy_factory import build_strategy_engine
from strategy import StrategyEngine
from trading_bot.core.execution_mode import V1_BASELINE, V2_FULL
from trading_bot.strategy.engine import UnifiedStrategyEngine
from utils import load_runtime_config, save_json_atomic


class _FakeConnector:
    def __init__(self, startup_ok: bool = True, startup_reason: str = "Startup market-data validation passed (tick deferred)") -> None:
        self.startup_ok = startup_ok
        self.startup_reason = startup_reason
        self.validate_calls: list[dict[str, object]] = []

    def initialize(self) -> bool:
        return True

    def validate_startup(self, bars: int = 120, require_live_tick: bool = True):
        self.validate_calls.append({"bars": bars, "require_live_tick": require_live_tick})
        return self.startup_ok, self.startup_reason

    def health_report(self):
        return {"connection_ok": True, "symbol_ok": True, "tick_ok": False, "connection": {}, "symbol_snapshot": {}}

    def get_account_info(self):
        return SimpleNamespace(login=1, server="demo", trade_allowed=True, balance=10000.0, equity=10000.0, margin_free=10000.0)

    def execution_path_summary(self):
        return {"path": "test"}


class _FakeLogger:
    def structured(self, *_args, **_kwargs):
        return None

    def info(self, *_args, **_kwargs):
        return None

    def warning(self, *_args, **_kwargs):
        return None


def _minimal_strategy_config(execution_mode: str) -> dict[str, object]:
    return {
        "bot": {"trading_mode": "DRY_RUN", "execution_mode": execution_mode},
        "mt5": {"symbol": "XAUUSD"},
        "symbols": {},
        "dashboard": {},
        "telegram": {},
        "storage": {"database_path": "storage/bot.db", "state_path": "storage/state.json"},
        "sessions": {"buckets": [], "live_allowed_buckets": [], "allow_asia_session": True},
        "strategy": {"setup_families": {}, "setup_controls": {}, "entry_modes": {}},
        "regime": {},
        "execution": {"max_spread_points": 25.0},
        "risk": {"risk_percent": 0.25},
        "backtest": {},
        "indicators": {
            "ema_fast": 20,
            "ema_slow": 50,
            "ema_trend": 200,
            "rsi_period": 14,
            "atr_period": 14,
            "adx_period": 14,
            "volume_period": 20,
        },
    }


def _build_stub_bot(mode: str, startup_reason: str):
    bot = TradingBot.__new__(TradingBot)
    bot.state = {}
    bot.base_dir = PROJECT_ROOT
    bot.connector = _FakeConnector(startup_ok=True, startup_reason=startup_reason)
    bot.logger = _FakeLogger()
    bot.strategy = SimpleNamespace(classify_session=lambda _now: {"session_name": "ASIA"})
    bot.notifier = SimpleNamespace(send_info=lambda *_a, **_k: True)
    bot.telegram_command_service = SimpleNamespace(start=lambda: None)
    bot.config = {
        "mt5": {"reconnect_retries": 3, "symbol": "XAUUSD", "magic_number": 1},
        "timeframes": {"bars_to_fetch": 320},
        "bot": {"trading_mode": mode, "force_reduced_risk_mode": False, "auto_recover_live_positions": False},
        "sessions": {"buckets": [], "live_allowed_buckets": [], "allow_asia_session": True},
        "regime": {"allow_range_regime": True},
        "strategy": {"require_trend_alignment": False, "live_thresholds": {}, "setup_families": {}, "setup_controls": {}},
    }
    bot._record_event = lambda *_a, **_k: None
    bot._sync_risk_state = lambda *_a, **_k: None
    bot._safe_notify_call = lambda callback: callback()
    bot._reconcile_startup_position_state = lambda: None
    bot._resolve_unresolved_trades = lambda source=None: {"source": source}
    bot._reconcile_mt5_positions_with_database = lambda: {"ok": True}
    bot.save_state = lambda: True
    return bot


def test_strategy_factory_uses_legacy_engine_for_v1_baseline(tmp_path: Path) -> None:
    config = _minimal_strategy_config(V1_BASELINE)
    engine = build_strategy_engine(config, tmp_path)
    assert isinstance(engine, StrategyEngine)
    assert not isinstance(engine, UnifiedStrategyEngine)
    assert config["bot"]["execution_mode_summary"]["engine_type"] == "legacy_strategy_engine"


def test_strategy_factory_uses_unified_engine_for_v2_full(tmp_path: Path) -> None:
    config = _minimal_strategy_config(V2_FULL)
    engine = build_strategy_engine(config, tmp_path)
    assert isinstance(engine, UnifiedStrategyEngine)
    assert config["bot"]["execution_mode_summary"]["engine_type"] == "unified_strategy_engine"


def test_requires_live_tick_validation_is_disabled_for_backtest():
    bot = _build_stub_bot("BACKTEST", "Startup market-data validation passed (tick deferred)")
    assert bot.requires_live_tick_validation() is False


def test_live_startup_with_stale_tick_does_not_raise():
    bot = _build_stub_bot("LIVE", "Startup market-data validation passed (tick deferred)")
    TradingBot.startup(bot)
    assert bot.connector.validate_calls
    assert bot.connector.validate_calls[-1]["require_live_tick"] is False


def test_dry_run_startup_with_stale_tick_does_not_raise():
    bot = _build_stub_bot("DRY_RUN", "Startup market-data validation passed (tick deferred)")
    TradingBot.startup(bot)
    assert bot.connector.validate_calls
    assert bot.connector.validate_calls[-1]["require_live_tick"] is False


def test_backtest_startup_with_stale_tick_does_not_raise():
    bot = _build_stub_bot("BACKTEST", "Startup market-data validation passed (tick deferred)")
    TradingBot.startup(bot)
    assert bot.connector.validate_calls
    assert bot.connector.validate_calls[-1]["require_live_tick"] is False


def test_strategy_patch_does_not_overwrite_persisted_live_mode(tmp_path: Path) -> None:
    config = {
        "bot": {"trading_mode": "LIVE", "allow_live_execution": True, "dry_run": False},
        "mt5": {"symbol": "XAUUSD"},
        "dashboard": {},
        "telegram": {},
        "storage": {"database_path": "storage/bot.db", "state_path": "storage/state.json"},
        "sessions": {"buckets": []},
        "strategy": {"setup_families": {}, "setup_controls": {}, "entry_modes": {}},
        "execution": {"max_spread_points": 25.0, "manual_trade_comment_tag_dashboard": "D", "manual_trade_comment_tag_telegram": "T"},
        "risk": {"risk_percent": 0.35, "max_lot": 2.0, "min_lot": 0.01, "min_margin_level": 300, "max_trades_per_day": 4, "max_trades_per_symbol": 1, "max_daily_drawdown_pct": 1.5},
    }
    save_json_atomic(tmp_path / "config.json", config)

    loaded = load_runtime_config(tmp_path, require_mt5_credentials=False, apply_mode_env_override=False)
    assert loaded["bot"]["trading_mode"] == "LIVE"
    assert loaded["bot"]["allow_live_execution"] is True
    assert loaded["bot"]["dry_run"] is False

    strategy_config = json.loads(json.dumps(loaded))
    build_strategy_engine(strategy_config, tmp_path)
    assert strategy_config["bot"]["trading_mode"] == "LIVE"
    assert strategy_config["bot"]["allow_live_execution"] is True
    assert strategy_config["bot"]["dry_run"] is False


def test_stale_control_state_does_not_overwrite_live_boot_mode(tmp_path: Path) -> None:
    bot = _build_stub_bot("LIVE", "Startup market-data validation passed (tick deferred)")
    bot.control_state = SimpleNamespace(
        summarize=lambda: {
            "process_alive": True,
            "bot_running": True,
            "runtime": {"mode": "DRY_RUN"},
        },
        update=lambda *_args, **_kwargs: None,
    )
    TradingBot._sync_runtime_mode_from_control_state(bot, log_change=False)
    assert bot.config["bot"]["trading_mode"] == "LIVE"
    assert bot.config["bot"]["allow_live_execution"] is True
    assert bot.config["bot"]["dry_run"] is False
    assert bot.trading_mode == "LIVE"


def test_missing_live_flags_still_normalize_to_live(tmp_path: Path) -> None:
    config = {
        "bot": {"trading_mode": "LIVE"},
        "mt5": {"symbol": "XAUUSD"},
        "dashboard": {},
        "telegram": {},
        "storage": {"database_path": "storage/bot.db", "state_path": "storage/state.json"},
        "sessions": {"buckets": []},
        "strategy": {"setup_families": {}, "setup_controls": {}, "entry_modes": {}},
        "execution": {"max_spread_points": 25.0, "manual_trade_comment_tag_dashboard": "D", "manual_trade_comment_tag_telegram": "T"},
        "risk": {"risk_percent": 0.35, "max_lot": 2.0, "min_lot": 0.01, "min_margin_level": 300, "max_trades_per_day": 4, "max_trades_per_symbol": 1, "max_daily_drawdown_pct": 1.5},
    }
    save_json_atomic(tmp_path / "config.json", config)
    loaded = load_runtime_config(tmp_path, require_mt5_credentials=False, apply_mode_env_override=False)
    assert loaded["bot"]["trading_mode"] == "LIVE"
    assert loaded["bot"]["allow_live_execution"] is True
    assert loaded["bot"]["dry_run"] is False


def test_startup_banner_mode_matches_effective_execution_mode(tmp_path: Path) -> None:
    bot = _build_stub_bot("LIVE", "Startup market-data validation passed (tick deferred)")
    events: list[tuple[str, str, dict[str, object] | None]] = []

    def record_event(level: str, event_type: str, message: str, **kwargs):
        events.append((event_type, message, kwargs or None))

    bot._record_event = record_event
    TradingBot.startup(bot)
    startup_ready = [item for item in events if item[0] == "startup_ready"]
    assert startup_ready, "startup_ready event was not emitted"
    assert "mode=LIVE" in startup_ready[-1][1]


def test_startup_banner_mode_matches_dry_run_execution_mode(tmp_path: Path) -> None:
    bot = _build_stub_bot("DRY_RUN", "Startup market-data validation passed (tick deferred)")
    events: list[tuple[str, str, dict[str, object] | None]] = []

    def record_event(level: str, event_type: str, message: str, **kwargs):
        events.append((event_type, message, kwargs or None))

    bot._record_event = record_event
    TradingBot.startup(bot)
    startup_ready = [item for item in events if item[0] == "startup_ready"]
    assert startup_ready, "startup_ready event was not emitted"
    assert "mode=DRY_RUN" in startup_ready[-1][1]
