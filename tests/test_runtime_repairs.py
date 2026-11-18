from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from mt5_connector import MT5Connector
from services.config_manager import ConfigManager
from services.instance_lock import InstanceLock
from services.lebprime_live.timeframes import aggregate_m45
from trading_bot.core.timeframes import normalize_timeframe_label, resolve_timeframe


class _Logger:
    def structured(self, *_args, **_kwargs):
        return None


def test_all_lebprime_timeframes_resolve_authoritatively():
    fake_mt5 = SimpleNamespace(TIMEFRAME_M1=1, TIMEFRAME_M3=3, TIMEFRAME_M5=5, TIMEFRAME_M15=15, TIMEFRAME_M30=30)
    expected = {"M1": 1, "M3": 3, "M5": 5, "M15": 15, "M30": 30}
    for label, value in expected.items():
        resolution = resolve_timeframe(label, fake_mt5)
        assert resolution.label == label
        assert resolution.mt5_value == value
        assert not resolution.synthetic
    m45 = resolve_timeframe("M45", fake_mt5)
    assert m45.synthetic and m45.synthetic_source == "M15" and m45.mt5_value is None
    assert normalize_timeframe_label("15min") == "M15"


def test_m45_rejects_partial_and_misaligned_sources_and_is_deterministic():
    rows = [
        {"time": "2026-09-21T00:00:00Z", "open": 10, "high": 12, "low": 9, "close": 11, "tick_volume": 1, "real_volume": 10},
        {"time": "2026-09-21T00:15:00Z", "open": 11, "high": 14, "low": 10, "close": 13, "tick_volume": 2, "real_volume": 20},
        {"time": "2026-09-21T00:30:00Z", "open": 13, "high": 15, "low": 12, "close": 14, "tick_volume": 3, "real_volume": 30},
        {"time": "2026-09-21T00:31:00Z", "open": 99, "high": 999, "low": 0, "close": 99, "tick_volume": 100, "real_volume": 100},
        {"time": "2026-09-21T00:45:00Z", "open": 14, "high": 16, "low": 13, "close": 15, "tick_volume": 4, "real_volume": 40},
        {"time": "2026-09-21T01:00:00Z", "open": 15, "high": 17, "low": 14, "close": 16, "tick_volume": 5, "real_volume": 50},
    ]
    first = aggregate_m45(pd.DataFrame(rows), pd.Timestamp("2026-09-21T01:30:00Z").to_pydatetime())
    second = aggregate_m45(pd.DataFrame(reversed(rows)), pd.Timestamp("2026-09-21T01:30:00Z").to_pydatetime())
    pd.testing.assert_frame_equal(first, second)
    assert len(first) == 1
    assert first.iloc[0]["open"] == 10
    assert first.iloc[0]["high"] == 15
    assert first.iloc[0]["low"] == 9
    assert first.iloc[0]["close"] == 14
    assert first.iloc[0]["tick_volume"] == 6
    assert first.iloc[0]["real_volume"] == 60


def _connector_for_filling() -> MT5Connector:
    connector = MT5Connector.__new__(MT5Connector)
    connector.symbol = "XAUUSD"
    connector.logger = _Logger()
    connector._proven_filling_modes = {}
    connector.symbol_info = SimpleNamespace(filling_mode=2, trade_exemode=2)
    return connector


def test_filling_mode_fallback_accepts_second_precheck_without_early_send():
    connector = _connector_for_filling()
    checked: list[int] = []
    sent: list[dict] = []

    def check(request):
        checked.append(request["type_filling"])
        if len(checked) == 1:
            return {"ok": False, "reason_code": "invalid_fill_mode", "payload": {"retcode": 10030}}
        return {"ok": True, "reason_code": "order_check_ok", "payload": {"retcode": 0}}

    connector.order_check_stage = check
    request = {"type_filling": 2}
    result = connector.precheck_order(request)
    assert result["ok"]
    assert checked == [2, 1]
    assert request["type_filling"] == 1
    assert connector._proven_filling_modes["XAUUSD"] == 1
    assert sent == []


def test_filling_mode_total_failure_never_calls_order_send():
    connector = _connector_for_filling()
    sent: list[dict] = []
    connector.order_check_stage = lambda request: {"ok": False, "reason_code": "invalid_fill_mode", "payload": {"retcode": 10030}}
    connector.order_send_stage = lambda request, dry_run=False: sent.append(request)
    result = connector.precheck_order({"type_filling": 2})
    assert not result["ok"]
    assert result["chosen_filling_mode"] is None
    assert sent == []


def test_instance_lock_live_stale_malformed_and_clean_shutdown(tmp_path: Path):
    path = tmp_path / "storage" / "bot_instance.lock"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"pid": 41}), encoding="utf-8")
    live = InstanceLock(path, tmp_path, pid=99, is_alive=lambda pid: True, belongs_to_bot=lambda pid, root: True)
    with pytest.raises(RuntimeError, match="PID 41"):
        live.acquire()

    stale = InstanceLock(path, tmp_path, pid=99, is_alive=lambda pid: False, belongs_to_bot=lambda pid, root: False)
    stale.acquire()
    assert json.loads(path.read_text(encoding="utf-8"))["pid"] == 99
    stale.release()
    assert not path.exists()

    path.write_text("not-json", encoding="utf-8")
    malformed = InstanceLock(path, tmp_path, pid=100, is_alive=lambda pid: False, belongs_to_bot=lambda pid, root: False)
    malformed.acquire()
    assert json.loads(path.read_text(encoding="utf-8"))["pid"] == 100
    malformed.release()


def test_config_json_is_authoritative_mode_and_save_mirrors_shadow(tmp_path: Path):
    source = json.loads((Path(__file__).resolve().parents[1] / "config.json").read_text(encoding="utf-8"))
    source["bot"]["trading_mode"] = "DRY_RUN"
    (tmp_path / "config.json").write_text(json.dumps(source), encoding="utf-8")
    (tmp_path / "storage").mkdir()
    (tmp_path / "storage" / "config.json").write_text(json.dumps({"bot": {"trading_mode": "LIVE"}}), encoding="utf-8")
    manager = ConfigManager(tmp_path)
    assert manager.resolve_runtime_mode({"runtime": {"mode": "LIVE"}}) == ("DRY_RUN", "config_file")
    updated = manager.apply_updates({"bot": {"trading_mode": "LIVE"}})
    manager.save(updated, actor="test", reason="mode")
    assert manager.load_raw()["bot"]["trading_mode"] == "LIVE"
    assert manager.load_shadow()["bot"]["trading_mode"] == "LIVE"
