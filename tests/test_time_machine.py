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


# ------------------------------------------------------------------ trailing exit, Telegram, watcher
def test_trail_stop_moves_only_from_the_next_candle_and_only_forward():
    t = tm.Trail(start_r=1.0, mode="Distance R", distance_r=1.0)
    best = np.array([100.5, 102.5, 102.0, 104.0])
    close = np.array([100.4, 102.2, 101.0, 103.5])
    stops = t.stops(1, fill=100.0, sl=99.0, best=best, close=close, atr=1.0)
    # candle 0: stop = SL; candle 1 uses candle 0 (mfe 0.5R, no move); candle 2 uses candle 1 (best 102.5 -> 101.5); candle 3 keeps 101.5
    assert list(stops) == [99.0, 99.0, 101.5, 101.5]


def test_exit_trail_locks_profit_on_m1():
    closes = [100.6, 100.0, 101.0, 102.0, 103.0] + [103.0] * 10 + [100.5] * 5
    m = _m1(closes, spread=0.01, wick=0.0)
    r = tm.simulate(m, 1, entry=100.62, sl=99.6, tp=200.0, start=0, expire=3, commission_price=0.0)
    ctx = tm.Context(m, 1)
    out = tm.exit_trail(m, r["fill_idx"], r["fill"], 1, 99.6, 1, ctx, tm.Trail(start_r=1.0, mode="Distance R", distance_r=1.0), 0.0)
    assert out["reason"] == "TRAIL" and out["net_r"] > 0.5


def test_live_parameter_defaults_match_the_live_service():
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import time_machine_job
    from services.lebprime_live.service import V340_CHART_DEFAULTS

    assert time_machine_job.V340_CHART_DEFAULTS == V340_CHART_DEFAULTS


def _summary():
    head = {"key": "sl_only", "trades": 40, "trusted": True, "win_pct": 45.0, "avg_r": -0.1, "label": "x"}
    trail_head = {**head, "win_pct": 38.0, "avg_r": 0.05}
    return {"zone": {"symbol": "XAUUSD", "timeframe": "M15", "side": "BUY", "entry1": 3600.0, "sl": 3590.0, "live_zone_id": "XAU-15M-B-1",
                     "decision_time": "2026-11-26T09:15:00+00:00"},
            "features": {"h1_trend": "with", "session": "London", "sl_atr": 1.2}, "live_exit": "trail", "p_fill": 70.0,
            "exits": {"tp": {"analogs": {"headline": "sl_only", "tiers": [head]}, "monte_carlo": {"p_tp_first": 48.0, "breakeven_win_pct": 49.9, "expected_r": -0.04}},
                      "trail": {"analogs": {"headline": "sl_only", "tiers": [trail_head]}, "monte_carlo": {"win_pct": 41.0, "expected_r": 0.02}}}}


def test_telegram_message_leads_with_the_bots_exit():
    from dashboard.time_machine import telegram_message

    title, lines = telegram_message(_summary())
    assert title == "🔮 Zone odds · XAUUSD M15 BUY" and len(lines) == 4
    assert lines[1].startswith("Bot exit (trail): 40 similar past zones 38% win +0.05R/trade")
    assert lines[2].startswith("Alt exit (TP 1R)") and "fill chance 70%" in lines[3] and "Positive odds" in lines[3]


class _Notifier:
    def __init__(self):
        self.sent = []

    def _send(self, title, lines):
        self.sent.append((title, lines))
        return True


def test_watcher_queues_new_live_zones_once_and_sends_finished_odds(tmp_path):
    from datetime import datetime, timezone

    from dashboard.time_machine_watcher import TimeMachineWatcher

    client, root, db = _client(tmp_path)
    con = sqlite3.connect(db)
    con.execute("INSERT INTO lebprime_live_zones VALUES ('NAS-5M-B-2', 'NAS100', 'M5', 'BUY', 100, 110, 110, 95, 'EXECUTION_SKIPPED', 'PENDING', "
                "'2026-11-24T20:10:00+00:00', '{\"limit_time\": \"2026-11-24T20:05:00+00:00\"}')")          # same prices as NAS-5M-B-1: a twin
    con.execute("INSERT INTO lebprime_live_zones VALUES ('XAU-1M-S-9', 'XAUUSD', 'M1', 'SELL', 1, 2, 1, 3, 'HISTORICAL_REPLAY', 'PENDING', "
                "'2026-11-24T20:10:00+00:00', '{}')")                                                  # replay: never queued
    con.commit()
    con.close()
    notifier = _Notifier()
    cfg = {"telegram": {"enabled": True}, "time_machine": {}}
    w = TimeMachineWatcher(tmp_path, lambda: cfg, db, tmp_path / "dispatch", notifier_factory=lambda _: notifier)
    w.cursor["since"] = "2000-01-01"
    now = datetime(2026, 11, 24, 20, 30, tzinfo=timezone.utc)
    w.poll_once(now)
    runs = client.get("/api/time-machine").json()["runs"]
    assert len(runs) == 1 and runs[0]["request"]["source"] == "auto" and runs[0]["request"]["symbol"] == "USTEC"
    w.poll_once(now)
    assert len(client.get("/api/time-machine").json()["runs"]) == 1          # queued once, twin skipped
    assert client.post("/api/time-machine", json=ZONE).status_code == 200   # automatic runs never block a manual one
    run_id = runs[0]["id"]
    (root / run_id / "summary.json").write_text(json.dumps(_summary()))
    (root / run_id / "status.json").write_text(json.dumps({"state": "COMPLETED"}))
    (tmp_path / "dispatch" / f"{run_id}.done").write_text("success")
    w.poll_once(now)
    w.poll_once(now)
    assert len(notifier.sent) == 1 and notifier.sent[0][0].startswith("🔮 Zone odds")


def test_watcher_ignores_old_zones(tmp_path):
    from datetime import datetime, timezone

    from dashboard.time_machine_watcher import TimeMachineWatcher

    client, _, db = _client(tmp_path)
    w = TimeMachineWatcher(tmp_path, lambda: {"time_machine": {}}, db, tmp_path / "dispatch")
    w.poll_once(datetime(2026, 11, 26, 9, 0, tzinfo=timezone.utc))              # zone created 24 Nov: too old
    assert client.get("/api/time-machine").json()["runs"] == []


# ------------------------------------------------------------------ decisions per zone
def test_watcher_records_the_decision_for_each_zone_and_the_zones_view_scores_it(tmp_path):
    from datetime import datetime, timezone

    from dashboard.time_machine_watcher import TimeMachineWatcher
    from services import time_machine_decisions

    client, root, db = _client(tmp_path)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE lebprime_broker_entries (zone_id TEXT, historical INTEGER, broker_filled_at TEXT)")
    con.execute("INSERT INTO lebprime_broker_entries VALUES ('NAS-5M-B-1', 0, '2026-11-24T20:20:00+00:00')")
    con.execute("UPDATE lebprime_live_zones SET theoretical_state='SL_HIT', metadata_json='{\"limit_time\": \"2026-11-24T19:55:00+00:00\", \"result_r\": -1.0}'")
    con.commit()
    con.close()
    w = TimeMachineWatcher(tmp_path, lambda: {"time_machine": {}}, db, tmp_path / "dispatch")
    w.cursor["since"] = "2000-01-01"
    run_id = client.post("/api/time-machine", json=ZONE).json()["run"]["id"]
    summary = {**_summary(), "request": {"limit_time": "2026-11-24T19:55:00+00:00"}}
    (root / run_id / "summary.json").write_text(json.dumps(summary))
    (root / run_id / "status.json").write_text(json.dumps({"state": "COMPLETED"}))
    (tmp_path / "dispatch" / f"{run_id}.done").write_text("success")
    w.poll_once(datetime(2026, 11, 24, 23, 0, tzinfo=timezone.utc))
    d = time_machine_decisions.load(tmp_path)["NAS-5M-B-1"]
    assert d["verdict"] == "positive" and d["exit"] == "trail" and d["trades"] == 40 and d["run_id"] == run_id
    assert time_machine_decisions.line(d).startswith("🔮 TM ✅ Positive odds · 40 zones 38% win +0.05R · fill 70%")
    assert client.get("/api/time-machine/decisions").json()["decisions"]["NAS-5M-B-1"]["verdict"] == "positive"
    view = client.get("/api/time-machine/zones?days=60").json()
    assert view["zones"][0]["traded"] and view["zones"][0]["result_r"] == -1.0 and view["zones"][0]["time_machine"]["verdict"] == "positive"
    assert view["scorecard"] == [{"verdict": "positive", "zones": 1, "win_pct": 0.0, "net_r": -1.0, "avg_r": -1.0, "traded": 1, "traded_net_r": -1.0}]


def test_trade_telegram_lines_carry_the_time_machine_decision():
    from services.telegram_notifier import TelegramNotifier

    n = TelegramNotifier(None, None, dry_print=True)
    n.send_trade_open({"symbol": "XAUUSD", "side": "BUY", "zone_id": "Z1", "time_machine": "🔮 TM ❌ Negative odds"})
    assert "🔮 TM ❌ Negative odds" in n.last_message
    n.send_trade_close({"symbol": "XAUUSD", "side": "BUY", "zone_id": "Z1", "pnl": 5, "time_machine": "🔮 TM ✅ Positive odds"})
    assert "🔮 TM ✅ Positive odds" in n.last_message
    n.send_trade_open({"symbol": "XAUUSD", "side": "BUY", "zone_id": "Z1"})
    assert "TM" not in n.last_message
