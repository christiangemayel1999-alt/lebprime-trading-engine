from __future__ import annotations

from types import SimpleNamespace

import mt5_connector
from mt5_connector import MT5Connector


class _FakeLogger:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str]] = []

    def info(self, message: str) -> None:
        self.messages.append(("INFO", message))

    def warning(self, message: str) -> None:
        self.messages.append(("WARNING", message))

    def error(self, message: str) -> None:
        self.messages.append(("ERROR", message))

    def structured(self, *_args, **_kwargs) -> None:
        return None


def test_initialize_reuses_authenticated_terminal_when_credentials_missing(monkeypatch) -> None:
    connector = MT5Connector({"mt5": {"symbol": "XAUUSD", "timeout": 1}, "logging": {}}, _FakeLogger())

    class _TerminalInfo:
        connected = True
        trade_allowed = True
        tradeapi_disabled = False
        path = "C:/Program Files/MetaTrader 5"

    class _AccountInfo:
        login = 123456
        server = "MetaQuotes-Demo"
        trade_mode = 0
        balance = 10000.0
        equity = 10000.0
        margin = 0.0
        margin_free = 10000.0
        margin_level = 0.0

    monkeypatch.setattr(mt5_connector.mt5, "initialize", lambda **_kwargs: True)
    monkeypatch.setattr(mt5_connector.mt5, "terminal_info", lambda: _TerminalInfo())
    monkeypatch.setattr(mt5_connector.mt5, "account_info", lambda: _AccountInfo())
    monkeypatch.setattr(mt5_connector.mt5, "last_error", lambda: (1, "Success"))
    monkeypatch.setattr(mt5_connector.mt5, "login", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("login should be skipped")))
    monkeypatch.setattr(mt5_connector.mt5, "shutdown", lambda: None)
    monkeypatch.setattr(connector, "ensure_symbol", lambda log_details=True: True)
    monkeypatch.setattr(
        connector,
        "get_tick_with_retry",
        lambda: (SimpleNamespace(time=1713850000, bid=4698.44, ask=4698.61, last=4698.5, volume=1), None),
    )

    connector.mt5_config.update({"login": None, "password": None, "server": None})

    assert connector.initialize() is True
    assert connector.connected is True


def test_initialize_waits_for_transient_ipc_recovery_without_credentials(monkeypatch) -> None:
    connector = MT5Connector({"mt5": {"symbol": "XAUUSD", "timeout": 1}, "logging": {}}, _FakeLogger())

    class _TerminalInfoDisconnected:
        connected = False
        trade_allowed = False
        tradeapi_disabled = False
        path = None

    class _TerminalInfoConnected:
        connected = True
        trade_allowed = True
        tradeapi_disabled = False
        path = "C:/Program Files/MetaTrader 5"

    class _AccountInfo:
        login = 123456
        server = "MetaQuotes-Demo"
        trade_mode = 0
        balance = 10000.0
        equity = 10000.0
        margin = 0.0
        margin_free = 10000.0
        margin_level = 0.0

    terminal_states = [_TerminalInfoDisconnected(), _TerminalInfoConnected(), _TerminalInfoConnected()]
    account_states = [None, _AccountInfo(), _AccountInfo()]
    terminal_calls = {"count": 0}
    account_calls = {"count": 0}

    def _terminal_info():
        idx = min(terminal_calls["count"], len(terminal_states) - 1)
        terminal_calls["count"] += 1
        return terminal_states[idx]

    def _account_info():
        idx = min(account_calls["count"], len(account_states) - 1)
        account_calls["count"] += 1
        return account_states[idx]

    monkeypatch.setattr(mt5_connector.mt5, "initialize", lambda **_kwargs: True)
    monkeypatch.setattr(mt5_connector.mt5, "terminal_info", _terminal_info)
    monkeypatch.setattr(mt5_connector.mt5, "account_info", _account_info)
    monkeypatch.setattr(mt5_connector.mt5, "last_error", lambda: (1, "Success"))
    monkeypatch.setattr(mt5_connector.mt5, "login", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("login should be skipped")))
    monkeypatch.setattr(mt5_connector.mt5, "shutdown", lambda: (_ for _ in ()).throw(AssertionError("shutdown should be skipped")))
    monkeypatch.setattr(connector, "ensure_symbol", lambda log_details=True: True)
    monkeypatch.setattr(
        connector,
        "get_tick_with_retry",
        lambda: (SimpleNamespace(time=1713850000, bid=4698.44, ask=4698.61, last=4698.5, volume=1), None),
    )

    connector.mt5_config.update({"login": None, "password": None, "server": None})

    assert connector.initialize() is True
    assert connector.connected is True
