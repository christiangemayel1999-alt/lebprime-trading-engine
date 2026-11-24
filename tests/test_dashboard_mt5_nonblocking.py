"""The dashboard must stay responsive when MT5 is unreachable (VPS dashboard has no gateway)."""

from __future__ import annotations

import inspect
import time

import pytest

import services.manual_trade_service as mts
from dashboard import routes


class _SlowFailingConnector:
    calls = 0

    def connection_health_check(self, reconnect_if_needed: bool = True):
        type(self).calls += 1
        time.sleep(0.2)
        return {"ok": False, "reason_code": "mt5_unavailable"}


def _service():
    service = mts.ManualTradeService.__new__(mts.ManualTradeService)
    import threading

    service._mt5_lock = threading.Lock()
    service._mt5_failed_at = None
    service._mt5_failure_reason = ""
    return service


def test_no_mt5_in_process_fails_fast(monkeypatch):
    monkeypatch.setattr(mts, "MT5_IMPORT_ERROR", ModuleNotFoundError("MetaTrader5"))
    service = _service()
    _SlowFailingConnector.calls = 0
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="not available in this process"):
        service._ensure_connection(_SlowFailingConnector())
    assert time.monotonic() - started < 0.1
    assert _SlowFailingConnector.calls == 0


def test_failed_connection_is_cached(monkeypatch):
    monkeypatch.setattr(mts, "MT5_IMPORT_ERROR", None)
    service = _service()
    _SlowFailingConnector.calls = 0
    with pytest.raises(RuntimeError):
        service._ensure_connection(_SlowFailingConnector())
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="cached"):
        service._ensure_connection(_SlowFailingConnector())
    assert time.monotonic() - started < 0.1
    assert _SlowFailingConnector.calls == 1


def test_mt5_routes_are_not_async():
    source = inspect.getsource(routes)
    for name in ("manual_positions", "lebprime_trades", "control_close_all", "indicator_lab_create_dataset"):
        assert f"    def {name}(" in source
        assert f"async def {name}(" not in source
