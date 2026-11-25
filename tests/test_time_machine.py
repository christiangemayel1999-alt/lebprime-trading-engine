"""Time Machine: execution rules, no look-ahead, Monte Carlo sanity, and the dashboard routes (queue only)."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dashboard.strategy_tests import create_strategy_test_router
from dashboard.time_machine import create_time_machine_router
from services.indicator_lab import time_machine as tm


def _m1(closes, spread=0.1, start="2024-01-02T00:00:00+00:00", wick=0.05):
    c = np.asarray(closes, float)
    o = np.r_[c[0], c[:-1]]
    n = len(c)
    t = tm.to_min(start)[0] + np.arange(n)
    return tm.M1(t=t, o=o, h=np.maximum(o, c) + wick, l=np.minimum(o, c) - wick, c=c, sp=np.full(n, spread), tick_volume=np.ones(n))


def test_buy_fills_on_the_ask_and_hits_tp():
    m = _m1([105, 103, 101, 99.8, 100.5, 102, 104, 106, 108, 110, 112])
    r = tm.simulate(m, 1, entry=100.0, sl=98.0, tp=102.2, start=0, expire=10, commission_price=0.0)
    assert r["reason"] == "TP" and r["fill_idx"] == 3          # low 99.75 + spread 0.1 <= 100 only at minute 3
    assert abs(r["gross_r"] - 1.1) < 1e-9


def test_stop_wins_a_same_minute_tie_and_unfilled_is_none():
    m = _m1([101, 100.5, 99.9, 99.9])
    m.h[3], m.l[3] = 110.0, 90.0                                   # one candle through both TP and SL
    r = tm.simulate(m, 1, entry=100.0, sl=98.0, tp=102.0, start=0, expire=3, commission_price=0.0)
    assert r["reason"] == "SL" and r["net_r"] <= -1.0
    assert tm.simulate(m, -1, entry=150.0, sl=152.0, tp=148.0, start=0, expire=3, commission_price=0.0) is None


def test_commission_is_charged_in_r():
    m = _m1([105, 99.0, 104, 106])
    r = tm.simulate(m, 1, entry=100.0, sl=98.0, tp=102.0, start=0, expire=3, commission_price=0.07)
    assert abs(r["commission_r"] - 0.07 / r["risk"]) < 1e-12 and r["net_r"] < r["gross_r"]


def test_features_only_use_closed_candles():
    rng = np.random.default_rng(1)
    m = _m1(2000 + np.cumsum(rng.normal(0, 0.5, 6000)))
    decision = int(m.t[4000])
    args = (np.array([decision]), np.array([1]), np.array([m.c[3999]]), np.array([m.c[3999] - 5]), np.array([np.nan]), np.array([np.nan]))
    before = tm.Context(m, 15).at(*args).iloc[0]
    m2 = tm.M1(m.t, m.o.copy(), m.h.copy(), m.l.copy(), m.c.copy(), m.sp, m.tick_volume)
    m2.h[4000:] += 500
    m2.c[4000:] += 300                                             # the future changes completely
    after = tm.Context(m2, 15).at(*args).iloc[0]
    assert before.atr == after.atr and before.h1_trend == after.h1_trend and before.sl_atr == after.sl_atr


def _lib(n=60):
    rows = []
    for k in range(n):
        rows.append({"side": "BUY", "filled": True, "decision_min": 1000 + k, "exit_min": 2000 + k * 100, "exit_time": f"{2020 + k % 3}-01-01T00:00",
                     "sl_atr": 1.0, "width_atr": 0.5, "h1_trend": "with", "session": "London", "net_r": 1.0 if k % 2 else -1.0,
                     "reason": "TP" if k % 2 else "SL", "commission_r": 0.01, "spread_r": 0.02, "limit_time": "x", "entry1": 1.0, "sl": 0.9})
    return pd.DataFrame(rows)


def test_analogs_never_count_trades_that_exit_after_the_decision():
    lib = _lib()
    target = {"side": "BUY", "decision_min": 2000 + 40 * 100, "sl_atr": 1.0, "width_atr": 0.5, "h1_trend": "with", "session": "London"}
    out = tm.analogs(lib, target)
    assert out["past_trades"] == 40                               # exits 2000..5900 < 6000
    closest = out["tiers"][0]
    assert closest["trades"] == 40 and closest["win_pct"] == 50.0 and set(closest["by_year"]) == {"2020", "2021", "2022"}
    assert out["headline"] == "closest"


def test_monte_carlo_symmetric_walk_is_about_half():
    rng = np.random.default_rng(3)
    m = _m1(1000 + np.cumsum(rng.normal(0, 0.3, 30_000)), spread=1e-9, wick=0.0)
    mc = tm.monte_carlo(m, 1, entry=1000.0, sl=995.0, tp=1005.0, commission=0.0, paths=4000)
    assert 45 <= mc["p_tp_first"] <= 55 and abs(mc["expected_r"]) < 0.12


# ------------------------------------------------------------------ routes
def _client(tmp_path: Path):
    dispatch = tmp_path / "dispatch"
    dispatch.mkdir()
    store = tmp_path / "M1"
    store.mkdir()
    db = tmp_path / "bot.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE lebprime_live_zones (zone_id TEXT, logical_symbol TEXT, timeframe TEXT, side TEXT, zone_bottom REAL, zone_top REAL, "
                "entry1 REAL, stop_loss REAL, execution_status TEXT, theoretical_state TEXT, created_at TEXT, metadata_json TEXT)")
    con.execute("INSERT INTO lebprime_live_zones VALUES ('NAS-5M-B-1', 'NAS100', 'M5', 'BUY', 100, 110, 110, 95, 'EXECUTION_SKIPPED', 'PENDING', "
                "'2026-11-24T20:00:00+00:00', '{\"limit_time\": \"2026-11-24T19:55:00+00:00\"}')")
    con.commit()
    con.close()
    app = FastAPI()
    app.include_router(create_strategy_test_router(tmp_path, lambda: None, None, dispatch, store))
    app.include_router(create_time_machine_router(tmp_path, lambda: None, None, db, dispatch, store))
    return TestClient(app), tmp_path / "reports" / "dashboard_tests", db


ZONE = {"symbol": "NAS100", "timeframe": "M5", "side": "BUY", "entry1": 110, "sl": 95, "limit_time": "2026-11-24T19:55:00+00:00", "live_zone_id": "NAS-5M-B-1"}


def test_queue_writes_a_time_machine_request_and_lists_apart(tmp_path):
    client, root, db = _client(tmp_path)
    before = db.read_bytes()
    zones = client.get("/api/time-machine/live-zones").json()["zones"]
    assert zones[0]["symbol"] == "USTEC" and zones[0]["open"] and zones[0]["limit_time"].startswith("2026-11-24T19:55")
    assert db.read_bytes() == before                               # read-only
    run = client.post("/api/time-machine", json=ZONE).json()["run"]
    request = json.loads((root / run["id"] / "request.json").read_text())
    assert request["kind"] == "time_machine" and request["symbol"] == "USTEC" and request["source"] == "live"
    assert [r["id"] for r in client.get("/api/time-machine").json()["runs"]] == [run["id"]]
    assert client.get("/api/strategy-tests").json()["tests"] == []
    assert client.get(f"/api/time-machine/{run['id']}").json()["run"]["state"] == "QUEUED"


def test_queue_rejects_bad_zones(tmp_path):
    client, _, _ = _client(tmp_path)
    assert client.post("/api/time-machine", json={**ZONE, "sl": 120}).status_code == 400          # SL above a buy
    assert client.post("/api/time-machine", json={**ZONE, "symbol": "BTCUSD"}).status_code == 400
    assert client.post("/api/time-machine", json={**ZONE, "timeframe": "M7"}).status_code == 400
    assert client.post("/api/time-machine", json={**ZONE, "limit_time": "2031-01-01T00:00:00"}).status_code == 400
    assert client.post("/api/time-machine", json={**ZONE, "paths": 10}).status_code == 400
