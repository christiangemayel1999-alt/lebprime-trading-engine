"""Without MT5 in the process (VPS dashboard), "create dataset from MT5" uses the M1 store."""

from __future__ import annotations

import pandas as pd

import mt5_connector
import services.indicator_lab.dataset_service as ds
from services.database import DatabaseService


def _store(tmp_path):
    store = tmp_path / "M1"
    store.mkdir()
    times = pd.date_range("2026-11-20T00:00:00Z", periods=24, freq="1min")
    frame = pd.DataFrame({"timestamp_utc": times.strftime("%Y-%m-%dT%H:%M:%S+00:00"), "open": range(24), "high": [x + 2 for x in range(24)],
                          "low": [x - 1 for x in range(24)], "close": [x + 1 for x in range(24)], "tick_volume": 1.0, "spread": 3.0, "real_volume": 0.0})
    frame.to_csv(store / "XAUUSD.csv", index=False)
    return store


def test_store_dataset_m5_drops_unfinished_candle(tmp_path, monkeypatch):
    monkeypatch.setattr(mt5_connector, "MT5_IMPORT_ERROR", ModuleNotFoundError("MetaTrader5"))
    monkeypatch.setattr(ds, "MT5_STORE_DIR", _store(tmp_path))
    service = ds.IndicatorDatasetService(tmp_path, DatabaseService(tmp_path / "bot.db"), {})
    item = service.create_mt5_dataset(name="t", symbol="XAUUSD", timeframe="M5", start_date="2026-11-20", end_date="2026-11-20")
    assert item["status"] == "READY"
    frame = service.load_frame(item["id"])
    # 24 M1 candles = four full M5 candles (00:00..00:15); the 00:20 candle has only 4 minutes and is dropped.
    assert list(frame.timestamp_utc) == [f"2026-11-20T00:{m:02d}:00+00:00" for m in (0, 5, 10, 15)]
    first = frame.iloc[0]
    assert (first.open, first.high, first.low, first.close) == (0, 6, -1, 5)
    assert "/" in item["file_path"] and "\\\\" not in item["file_path"]


def test_windows_paths_load(tmp_path, monkeypatch):
    monkeypatch.setattr(mt5_connector, "MT5_IMPORT_ERROR", ModuleNotFoundError("MetaTrader5"))
    monkeypatch.setattr(ds, "MT5_STORE_DIR", _store(tmp_path))
    service = ds.IndicatorDatasetService(tmp_path, DatabaseService(tmp_path / "bot.db"), {})
    item = service.create_mt5_dataset(name="w", symbol="XAUUSD", timeframe="M1", start_date="2026-11-20", end_date="2026-11-20")
    service._update_dataset(item["id"], {"file_path": item["file_path"].replace("/", "\\\\")})
    assert len(service.load_frame(item["id"])) == 24
