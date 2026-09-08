from __future__ import annotations

from pathlib import Path

from trading_bot.config.loader import UnifiedConfigLoader
from utils import load_runtime_config, resolve_python_executable, save_json_atomic


def test_resolve_python_executable_prefers_repo_local_venv(tmp_path: Path, monkeypatch) -> None:
    project_root = tmp_path
    dotvenv_python = project_root / ".venv" / "Scripts" / "python.exe"
    explicit_python = project_root / "custom" / "python.exe"
    dotvenv_python.parent.mkdir(parents=True, exist_ok=True)
    explicit_python.parent.mkdir(parents=True, exist_ok=True)
    dotvenv_python.write_text("", encoding="utf-8")
    explicit_python.write_text("", encoding="utf-8")

    monkeypatch.setenv("PYTHON_PATH", str(explicit_python))
    chosen, reason = resolve_python_executable(project_root)

    assert Path(chosen) == dotvenv_python
    assert ".venv" in reason


def test_runtime_config_loader_is_consistent_across_entrypoint_contexts(tmp_path: Path, monkeypatch) -> None:
    save_json_atomic(
        tmp_path / "config.json",
        {
            "bot": {"trading_mode": "DRY_RUN"},
            "mt5": {"symbol": "XAUUSD", "terminal_path": "C:/Program Files/MetaTrader 5/terminal64.exe"},
            "dashboard": {"host": "127.0.0.1", "port": 8501},
            "storage": {"database_path": "storage/bot.db", "state_path": "storage/state.json"},
            "sessions": {"buckets": []},
            "strategy": {"setup_families": {}, "setup_controls": {}, "entry_modes": {}},
            "telegram": {},
            "execution": {"max_spread_points": 25.0},
            "risk": {"risk_percent": 0.5},
        },
    )
    (tmp_path / ".env").write_text("MT5_LOGIN=123456\nMT5_PASSWORD=secret\nMT5_SERVER=Demo-Server\n", encoding="utf-8")
    monkeypatch.delenv("MT5_PATH", raising=False)
    monkeypatch.delenv("MT5_LOGIN", raising=False)
    monkeypatch.delenv("MT5_PASSWORD", raising=False)
    monkeypatch.delenv("MT5_SERVER", raising=False)
    monkeypatch.chdir(tmp_path)

    loaded = load_runtime_config(tmp_path, require_mt5_credentials=False, apply_mode_env_override=False)
    normalized = UnifiedConfigLoader(tmp_path).load(require_mt5_credentials=False, apply_mode_env_override=False)

    assert loaded["mt5"]["symbol"] == "XAUUSD"
    assert loaded["mt5"]["terminal_path"] == "C:/Program Files/MetaTrader 5/terminal64.exe"
    assert loaded["mt5"]["login"] == 123456
    assert normalized["mt5"]["symbol"] == "XAUUSD"
    assert normalized["dashboard"]["port"] == 8501
