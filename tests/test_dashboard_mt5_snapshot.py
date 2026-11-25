"""VPS dashboard (no MT5 in process): positions and trade history come from the read-only snapshot."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import services.manual_trade_service as mts

MAGIC = 3400


class _DB:
    def _query_dataframe(self, *_):
        return [{"mt5_order_ticket": 11, "zone_id": "Z1", "context_key": "c"}]


def _service(tmp_path, monkeypatch, snapshot):
    path = tmp_path / "snapshot.json"
    if snapshot is not None:
        path.write_text(json.dumps(snapshot))
    monkeypatch.setattr(mts, "MT5_ACCOUNT_SNAPSHOT", path)
    monkeypatch.setattr(mts, "MT5_IMPORT_ERROR", ModuleNotFoundError("MetaTrader5"))
    service = mts.ManualTradeService.__new__(mts.ManualTradeService)
    service.config_manager = SimpleNamespace(load_runtime=lambda: {"mt5": {"magic_number": MAGIC, "symbol": "XAUUSD"}})
    service.audit_service = SimpleNamespace(log_action=lambda *a, **k: None)
    service.database = _DB()
    return service


def _snap(age=5):
    now = time.time()
    taken = datetime.fromtimestamp(now - age, timezone.utc).isoformat(timespec="seconds")
    pos = {"ticket": 7, "identifier": 7, "symbol": "XAUUSD", "type": 0, "volume": 0.01, "price_open": 2600.0, "sl": 2590.0, "tp": 2620.0,
           "profit": 1.5, "time": int(now), "comment": "LP ENTRY1", "magic": MAGIC}
    other = {**pos, "ticket": 8, "magic": 1}
    t0 = int(now) - 3600
    deals = [
        {"ticket": 1, "order": 11, "position_id": 7, "time": t0, "time_msc": t0 * 1000, "type": 0, "entry": 0, "magic": MAGIC, "symbol": "XAUUSD",
         "volume": 0.01, "price": 2600.0, "profit": 0.0, "commission": -0.07, "swap": 0.0, "fee": 0.0, "comment": "LP ENTRY1"},
        {"ticket": 2, "order": 12, "position_id": 7, "time": t0 + 600, "time_msc": (t0 + 600) * 1000, "type": 1, "entry": 1, "magic": MAGIC, "symbol": "XAUUSD",
         "volume": 0.01, "price": 2610.0, "profit": 10.0, "commission": -0.07, "swap": 0.0, "fee": 0.0, "comment": "tp"},
        {"ticket": 3, "order": 13, "position_id": 9, "time": t0, "time_msc": t0 * 1000, "type": 0, "entry": 0, "magic": 1, "symbol": "XAUUSD",
         "volume": 1, "price": 1, "profit": 0, "commission": 0, "swap": 0, "fee": 0, "comment": "manual"},
    ]
    return {"taken_at": taken, "server_time_offset_seconds": 0, "positions": [pos, other], "deals": deals, "errors": {}}


def test_positions_from_snapshot_only_bot_magic(tmp_path, monkeypatch):
    out = _service(tmp_path, monkeypatch, _snap()).list_positions("dashboard", "dashboard")
    assert out["ok"] and out["count"] == 1 and out["positions"][0]["ticket"] == 7 and out["stale"] is False


def test_trade_history_from_snapshot(tmp_path, monkeypatch):
    out = _service(tmp_path, monkeypatch, _snap()).trade_history(30)
    assert out["ok"] and len(out["trades"]) == 1
    assert out["trades"][0]["symbol"] == "XAUUSD"


def test_stale_and_missing_snapshot(tmp_path, monkeypatch):
    assert _service(tmp_path, monkeypatch, _snap(age=900)).list_positions("d", "d")["stale"] is True
    (tmp_path / "empty").mkdir()
    missing = _service(tmp_path / "empty", monkeypatch, None)
    assert missing.list_positions("d", "d")["ok"] is False
    with pytest.raises(RuntimeError, match="no positions/deals snapshot"):
        missing.trade_history(30)
