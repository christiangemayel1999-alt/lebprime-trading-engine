"""Tune the original LebPrime + fixes on months that contain both strong uptrends and downtrends, then validate.
Tune: Jun-Oct 2026. For every symbol it holds at least one clearly bullish and one clearly bearish month (regime_calendar_2026.csv).
A setting passes the tune only if it is net positive in the bullish months AND in the bearish months, after costs.
Check: 1-22 Nov 2026 (unseen), then 2020-2025 continuous (unseen years). Fixed broker SL (Price touch) everywhere.
Stage outputs: /var/lib/lebprime/reports/trend_months/{tune,check}.csv"""
import sys, os, json, itertools
sys.path.insert(0, os.getcwd()); sys.path.insert(0, "scripts")
from concurrent.futures import ProcessPoolExecutor
import numpy as np, pandas as pd

OUT = os.environ.get("OUT", "/var/lib/lebprime/reports/trend_months"); os.makedirs(OUT, exist_ok=True)
SYMS = ["XAUUSD", "XAGUSD", "US30", "USTEC", "WTI", "EURUSD"]; BASE = {"M5": 130, "M15": 124, "M30": 58}
CAL = {("XAUUSD", "2026-06"): "Bearish", ("XAUUSD", "2026-07"): "Mixed", ("XAUUSD", "2026-08"): "Bullish", ("XAUUSD", "2026-09"): "Bearish", ("XAUUSD", "2026-10"): "Bearish",
       ("XAGUSD", "2026-06"): "Bearish", ("XAGUSD", "2026-07"): "Bearish", ("XAGUSD", "2026-08"): "Bullish", ("XAGUSD", "2026-09"): "Bearish", ("XAGUSD", "2026-10"): "Bearish",
       ("US30", "2026-06"): "Bullish", ("US30", "2026-07"): "Mixed", ("US30", "2026-08"): "Bullish", ("US30", "2026-09"): "Bearish", ("US30", "2026-10"): "Bullish",
       ("USTEC", "2026-06"): "Mixed", ("USTEC", "2026-07"): "Bearish", ("USTEC", "2026-08"): "Bullish", ("USTEC", "2026-09"): "Bullish", ("USTEC", "2026-10"): "Bearish",
       ("WTI", "2026-06"): "Bearish", ("WTI", "2026-07"): "Bullish", ("WTI", "2026-08"): "Mixed", ("WTI", "2026-09"): "Bullish", ("WTI", "2026-10"): "Bullish",
       ("EURUSD", "2026-06"): "Bearish", ("EURUSD", "2026-07"): "Mixed", ("EURUSD", "2026-08"): "Bullish", ("EURUSD", "2026-09"): "Bearish", ("EURUSD", "2026-10"): "Bearish"}
FIX = {"gold_buffer": 1.0, "us30_buffer": 30.0, "nas_buffer": 15.0, "stop_rule": "Price touch", "tp1_execution": "Broker orders"}
GRID = {"momentum_count": [2, 4], "filters": ["off", "on"], "tp1_r": [0.75, 1.0], "trend_filter": ["None", "With H4 trend"], "lock": ["off", "on"]}


def settings(combo):
    m, filt, tp1, trend, lock = combo
    o = {**FIX, "momentum_count": m, "tp1_r": tp1, "trend_filter": trend, "limit_expiry_bars": 2000}
    if filt == "on":
        o.update({"entry2_mode": "Off", "max_spread_risk": 0.1, "block_beirut_window": "15:00-17:00"})
    if lock == "on":
        o.update({"lock_at_r": 1.0, "lock_r": 0.5})
    return o


def run(job):
    sym, tf, ds, ov, window = job
    from phase4_search import _frame, POINT, COMMISSION_PER_LOT, VALUE_PER_PRICE_PER_LOT
    from services.indicator_lab.lebprime_engine import LebPrimeEngine
    from services.indicator_lab.parameter_registry import default_parameters
    meta, fr = _frame(ds, window)
    start = window.split("..")[0]
    res = LebPrimeEngine({**default_parameters("LEBPRIME"), **ov}, symbol=sym, timeframe=tf).run(fr)
    rows = []
    for z in res["zones"]:
        if z.terminal_state not in {"SL_HIT", "TP1_FINAL", "TP2_HIT", "BE_EXIT", "EARLY_EXIT"} or str(z.limit_created_time) < start:
            continue
        risk = abs(float(z.entry_price) - float(z.sl_price)); filled = sum(1 for l in (z.metadata.get("live_legs") or {}).values() if l.get("filled")) or 1
        w = 1.0 if (filled == 1 and ov.get("entry2_mode") == "Off") else 0.5
        cost = (float(z.metadata.get("entry1_spread") or 0) * POINT.get(sym, 0) / risk) * w * filled if risk else 0
        v = VALUE_PER_PRICE_PER_LOT.get(sym, 0); cost += (COMMISSION_PER_LOT.get(sym, 0) / (risk * v)) * w * filled if risk and v else 0
        rows.append({"side": "BUY" if z.side == "DEMAND" else "SELL", "exit_time": str(z.terminal_time), "h4": z.metadata.get("limit_h4_trend"), "net_r": float(z.result_r or 0) - cost})
    return rows


def summary(sym, t):
    if not len(t):
        return {"trades": 0, "net_r": 0.0, "bull_r": 0.0, "bear_r": 0.0}
    t = t.copy(); t["month"] = t.exit_time.str[:7]; t["reg"] = [CAL.get((sym, m), "Other") for m in t.month]
    w = t[t.net_r > 0]; l = t[t.net_r <= 0]
    return {"trades": len(t), "win": round(len(w) / len(t) * 100, 1), "avg_win": round(w.net_r.mean(), 2) if len(w) else 0, "avg_loss": round(l.net_r.mean(), 2) if len(l) else 0,
            "net_r": round(t.net_r.sum(), 1), "bull_r": round(t[t.reg == "Bullish"].net_r.sum(), 1), "bear_r": round(t[t.reg == "Bearish"].net_r.sum(), 1),
            "buys_r": round(t[t.side == "BUY"].net_r.sum(), 1), "sells_r": round(t[t.side == "SELL"].net_r.sum(), 1)}


if __name__ == "__main__":
    W = int(os.environ.get("WORKERS", "3"))
    combos = list(itertools.product(*GRID.values()))
    tune_p = f"{OUT}/tune.csv"
    if not os.path.exists(tune_p):
        jobs = [(s, tf, BASE[tf] + i, settings(c), "2026-06-01..2026-11-01") for i, s in enumerate(SYMS) for tf in BASE for c in combos]
        keys = [(s, tf, c) for i, s in enumerate(SYMS) for tf in BASE for c in combos]
        out = []
        with ProcessPoolExecutor(W) as p:
            for (s, tf, c), rows in zip(keys, p.map(run, jobs, chunksize=2)):
                out.append({"symbol": s, "tf": tf, **dict(zip(GRID, c)), **summary(s, pd.DataFrame(rows))})
        pd.DataFrame(out).to_csv(tune_p, index=False)
        print("TUNE DONE", flush=True)
    tune = pd.read_csv(tune_p)
    ok = tune[(tune.bull_r > 0) & (tune.bear_r > 0) & (tune.trades >= 20)].copy()
    ok["worst"] = ok[["bull_r", "bear_r"]].min(axis=1)
    pick = ok.sort_values("worst", ascending=False).groupby(["symbol", "tf"]).head(2)
    print("passed tune:", len(ok), "of", len(tune)); print(pick.to_string(index=False), flush=True)
    jobs, keys = [], []
    for r in pick.itertuples():
        c = (int(r.momentum_count), r.filters, float(r.tp1_r), r.trend_filter, r.lock); ds = BASE[r.tf] + SYMS.index(r.symbol)
        for lab, win in (("Nov 2026", "2026-11-01..2026-11-23"), ("2020-2025", "2020-01-01..2026-01-01")):
            jobs.append((r.symbol, r.tf, ds, settings(c), win)); keys.append((r.symbol, r.tf, c, lab))
    out = []
    with ProcessPoolExecutor(W) as p:
        for (s, tf, c, lab), rows in zip(keys, p.map(run, jobs)):
            t = pd.DataFrame(rows); sm = summary(s, t)
            yr = t.groupby(t.exit_time.str[:4]).net_r.sum() if len(t) else pd.Series(dtype=float)
            out.append({"symbol": s, "tf": tf, **dict(zip(GRID, c)), "period": lab, **sm, "losing_years": int((yr <= 0).sum()) if lab == "2020-2025" else None,
                        **({y: round(yr.get(y, np.nan), 1) for y in map(str, range(2020, 2026))} if lab == "2020-2025" else {})})
    pd.DataFrame(out).to_csv(f"{OUT}/check.csv", index=False)
    print(pd.DataFrame(out).to_string(index=False)); print("ALL DONE")
