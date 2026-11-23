"""One-day test (default 23 Nov 2026, Beirut day) of the leak-free best setup per symbol, on fresh MT5 candles.
Christian's settings: $1,000 account, 2% risk per trade, fixed broker SL, 1 entry. Lots rounded down to the broker step.
Zone setups get several weeks of history first so their zones, ATR and H4 trend are settled."""
import sys, os, json, math, time
sys.path.insert(0, os.getcwd()); sys.path.insert(0, "scripts"); sys.path.insert(0, "reports/phase4")
import pandas as pd
import MetaTrader5 as mt5
import phase4_search
from services.indicator_lab.lebprime_engine import LebPrimeEngine
from services.indicator_lab.parameter_registry import default_parameters
from money_sim import all_jobs
from final_review import pick_zone
import orb_v2

DAY = sys.argv[1] if len(sys.argv) > 1 else "2026-11-23"
BEIRUT = "Asia/Beirut"
TF = {"M3": mt5.TIMEFRAME_M3, "M5": mt5.TIMEFRAME_M5, "M30": mt5.TIMEFRAME_M30}
VALUE = {"XAUUSD": 100.0, "XAGUSD": 5000.0, "US30": 1.0, "USTEC": 1.0, "WTI": 1000.0}
STEP = {"XAUUSD": 0.01, "XAGUSD": 0.01, "US30": 0.1, "USTEC": 0.1, "WTI": 0.01}


def mt5_frame(sym, tf, bars):
    r = pd.DataFrame(mt5.copy_rates_from_pos(sym, TF[tf], 0, bars))
    offset = round((mt5.symbol_info_tick(sym).time - time.time()) / 3600)   # server clock minus UTC, in hours
    t = pd.to_datetime(r.time, unit="s") - pd.Timedelta(hours=offset)
    f = pd.DataFrame({"timestamp_utc": t.dt.strftime("%Y-%m-%dT%H:%M:%S+00:00"), "open": r.open, "high": r.high, "low": r.low,
                      "close": r.close, "tick_volume": r.tick_volume.astype(float), "spread": r.spread.astype(float), "real_volume": r.real_volume.astype(float)})
    return f, offset


def zone_trades(name, sym, tf, ov, f):
    ov = {**ov, "stop_rule": "Price touch", "entry2_mode": "Off"}
    res = LebPrimeEngine({**default_parameters("LEBPRIME"), **ov}, symbol=sym, timeframe=tf).run(f)
    out = []
    for z in res["zones"]:
        if z.entry1_activated_time is None:
            continue
        risk = abs(float(z.entry_price) - float(z.sl_price))
        closed = z.terminal_state in {"SL_HIT", "TP1_FINAL", "TP2_HIT", "BE_EXIT", "EARLY_EXIT"}
        cost = 0.0
        if risk:
            cost = float(z.metadata.get("entry1_spread") or 0) * phase4_search.POINT.get(sym, 0) / risk
            v = phase4_search.VALUE_PER_PRICE_PER_LOT.get(sym, 0)
            cost += phase4_search.COMMISSION_PER_LOT.get(sym, 0) / (risk * v) if v else 0
        out.append({"setup": name, "symbol": sym, "side": "BUY" if z.side == "DEMAND" else "SELL", "entry_time": pd.Timestamp(z.entry1_activated_time),
                    "exit_time": pd.Timestamp(z.terminal_time) if closed else None, "entry": float(z.entry_price), "sl": float(z.sl_price),
                    "tp1": float(z.tp1_price or 0), "tp2": float(z.tp2_price or 0), "state": z.terminal_state or "OPEN", "risk_price": risk,
                    "net_r": (float(z.result_r or 0) - cost) if closed else None})
    return out


if __name__ == "__main__":
    mt5.initialize()
    jobs = {j[1]: j for j in all_jobs() if j[0] == "zone"}
    wti = pick_zone()[("WTI", "M30")][0]
    setups = [("Gold M5 G1B", "XAUUSD", "M5", jobs["Gold M5 G1B"][5], 20000), ("Silver M30 uptrend buys", "XAGUSD", "M30", jobs["Silver M30 uptrend buys"][5], 6000),
              ("USTEC M30 sweep sells", "USTEC", "M30", jobs["USTEC M30 sweep sells"][5], 6000), ("WTI M30 buys (BOS, H4)", "WTI", "M30", wti, 6000)]
    rows = []; cover = {}
    for name, sym, tf, ov, bars in setups:
        f, off = mt5_frame(sym, tf, bars); cover[sym] = f.timestamp_utc.iloc[-1]
        rows += zone_trades(name, sym, tf, ov, f)
    f, off = mt5_frame("US30", "M3", 30000); cover["US30"] = f.timestamp_utc.iloc[-1]
    f["t"] = pd.to_datetime(f.timestamp_utc, utc=True)
    orb_v2.frame = lambda *a, **k: f
    for r in orb_v2.orb("US30", "M3", 15, 1.5, "H4", 4, "trail"):
        rows.append({"setup": "US30 M3 NY breakout", "symbol": "US30", "side": r["side"], "entry_time": pd.Timestamp(r["entry_time"]), "exit_time": pd.Timestamp(r["exit_time"]),
                     "entry": None, "sl": None, "tp1": None, "tp2": None, "state": r["exit_reason"], "risk_price": r["risk_price"], "net_r": r["net_r"]})
    mt5.shutdown()
    t = pd.DataFrame(rows)
    t["entry_time"] = pd.to_datetime(t.entry_time, utc=True); t["exit_time"] = pd.to_datetime(t.exit_time, utc=True)
    loc = t.entry_time.dt.tz_convert(BEIRUT).dt.strftime("%Y-%m-%d"); locx = t.exit_time.dt.tz_convert(BEIRUT).dt.strftime("%Y-%m-%d")
    d = t[(loc == DAY) | (locx == DAY) | ((t.state == "OPEN") & (loc <= DAY))].copy()
    lots = (20.0 / (d.risk_price * d.symbol.map(VALUE)) / d.symbol.map(STEP)).apply(lambda x: math.floor(x + 1e-9)) * d.symbol.map(STEP)
    d["lots"] = lots.round(2); d["usd"] = (d.net_r * d.risk_price * d.symbol.map(VALUE) * d.lots).round(2)
    d["entry_beirut"] = d.entry_time.dt.tz_convert(BEIRUT).dt.strftime("%d %b %H:%M"); d["exit_beirut"] = d.exit_time.dt.tz_convert(BEIRUT).dt.strftime("%d %b %H:%M")
    d.to_csv(f"LebPrime AI Research/02 - Backtest Results/day_test_{DAY}.csv", index=False)
    print("Data covers up to (UTC):", cover)
    print(d[["setup", "side", "entry_beirut", "exit_beirut", "entry", "sl", "tp1", "tp2", "state", "net_r", "lots", "usd"]].to_string(index=False))
    print("ALL DONE")
