"""Strategy-test trade chart: the job saves candles + zones, the dashboard serves windows of them."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
from fastapi import FastAPI
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from dashboard.strategy_tests import create_strategy_test_router  # noqa: E402
from strategy_test_chart import write_chart  # noqa: E402

TEST_ID = "20261126-180000-abcdef"


def _frame(n: int = 500) -> pd.DataFrame:
    t = pd.date_range("2024-12-31 20:00", periods=n, freq="5min", tz="UTC")
    close = 2000 + np.arange(n) * 0.1
    return pd.DataFrame({"timestamp_utc": t.strftime("%Y-%m-%dT%H:%M:%S+00:00"), "open": close, "high": close + 1,
                         "low": close - 1, "close": close})


def _zone(zid: str, created: str, end: str | None, entry: str | None, state: str | None) -> SimpleNamespace:
    return SimpleNamespace(engine_zone_id=zid, side="DEMAND", created_time=created, terminal_time=end, terminal_state=state,
                           terminal_reason="SL touched" if state else None, zone_top=2010.0, zone_bottom=2008.0,
                           limit_created_time=created, entry1_activated_time=entry, entry2_activated_time=None,
                           entry_price=2010.0, entry1=2010.0, entry2=2008.0, sl_price=2007.0, tp_price=2016.0,
                           tp1_price=2013.0, tp2_price=2016.0, tp1_hit_time=None, exit_price=2007.0 if state else None,
                           result_r=-1.0 if state else None, mfe_r=0.2, mae_r=1.0, metadata={"entry1_fill_price": 2010.0})


def _client(tmp_path: Path) -> TestClient:
    folder = tmp_path / "reports" / "dashboard_tests" / TEST_ID
    folder.mkdir(parents=True)
    (folder / "request.json").write_text(json.dumps({"id": TEST_ID, "symbols": ["XAUUSD"], "timeframe": "M5"}))
    zones = [
        _zone("warmup", "2024-12-31T21:00:00+00:00", None, None, None),             # before start: left out
        _zone("traded", "2025-01-01T01:00:00+00:00", "2025-01-01T03:00:00+00:00", "2025-01-01T02:00:00+00:00", "SL_HIT"),
        _zone("open", "2025-01-02T00:00:00+00:00", None, None, None),                # never traded, still open at the end
    ]
    events = [SimpleNamespace(event_type="STOP_MOVED", zone_engine_id="traded", timestamp="2025-01-01T02:30:00+00:00", price=2009.0)]
    rows = [{"zone_id": "traded", "net_r": -1.05, "cost_r": 0.05}]
    out = write_chart(folder, "XAUUSD", "M5", _frame(), {"zones": zones, "events": events}, "2025-01-01", rows, trail=True)
    assert out == {"bars": 452, "zones": 2, "trades": 1}
    app = FastAPI()
    app.include_router(create_strategy_test_router(tmp_path, lambda: None, None, tmp_path / "dispatch", tmp_path / "M1"))
    return TestClient(app)


def test_overview_lists_trades_and_detail_reports_chart(tmp_path):
    client = _client(tmp_path)
    assert client.get(f"/api/strategy-tests/{TEST_ID}").json()["test"]["chart_symbols"] == ["XAUUSD"]
    meta = client.get(f"/api/strategy-tests/{TEST_ID}/chart?symbol=xauusd").json()
    assert meta["bars"] == 452 and meta["bar_seconds"] == 300 and meta["exit_mode"] == "Trail"
    [trade] = meta["trades"]
    assert trade["id"] == "traded" and trade["net_r"] == -1.05 and trade["stops"] == [[1735698600, 2009.0]]
    assert client.get(f"/api/strategy-tests/{TEST_ID}/chart?symbol=US30").status_code == 404


def test_bars_window_by_time_and_index(tmp_path):
    client = _client(tmp_path)
    t0 = int(pd.Timestamp("2025-01-01T01:00:00Z").timestamp())
    out = client.get(f"/api/strategy-tests/{TEST_ID}/chart/bars", params={"symbol": "XAUUSD", "t0": t0, "t1": t0 + 7200}).json()
    assert out["from_index"] == 0 and out["to_index"] == 451 and len(out["candles"]) == 452
    assert out["candles"][0] == [int(pd.Timestamp("2025-01-01T00:00:00Z").timestamp()), 2004.8, 2005.8, 2003.8, 2004.8]
    assert {z["id"] for z in out["zones"]} == {"traded", "open"}
    early = client.get(f"/api/strategy-tests/{TEST_ID}/chart/bars", params={"symbol": "XAUUSD", "i0": 0, "i1": 9}).json()
    assert len(early["candles"]) == 10 and [z["id"] for z in early["zones"]] == []
    late = client.get(f"/api/strategy-tests/{TEST_ID}/chart/bars", params={"symbol": "XAUUSD", "i0": 440, "i1": 9999}).json()
    assert late["to_index"] == 451 and {z["id"] for z in late["zones"]} == {"open"}
